"""The crawler's only HTTP transport, including pacing and deadline handling."""

import email.utils
import logging
import json
import time
from collections import deque
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

USER_AGENT = "blocker-component-index (+https://github.com/lihenggui/blocker-component-index)"


class DeadlineReached(Exception):
    """Work remains on disk and can be resumed by the next invocation."""


class DownloadTooLarge(ValueError):
    pass


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def __post_init__(self):
        self.headers = {k.lower(): v for k, v in self.headers.items()}

    def json(self):
        return json.loads(self.body)


def retry_delay(headers, attempt, now):
    """Retry-After may be either seconds or an HTTP date."""
    headers = {k.lower(): v for k, v in headers.items()}
    value = headers.get("retry-after")
    if value is not None:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2 ** attempt)


class HttpClient:
    def __init__(self, *, deadline=None, interval=0.5, token=None,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep, transport=None):
        self.deadline = deadline
        self.interval = interval
        self.token = token
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.transport = transport or self._request
        self.last_host = {}
        self.search_times = deque()
        self.budgets = {}

    def check(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            raise DeadlineReached()

    def pause(self, seconds):
        self.check()
        if self.deadline is not None and self.clock() + seconds >= self.deadline:
            raise DeadlineReached()
        if seconds > 0:
            self.sleep(seconds)
        self.check()

    def _pace(self, host, search):
        now = self.clock()
        self.pause(max(0.0, self.last_host.get(host, -float("inf")) + self.interval - now))
        resource = "search" if search else "core"
        if host == "api.github.com":
            remaining, reset = self.budgets.get(resource, (1, 0))
            if remaining == 0:
                self.pause(max(0.0, reset - self.wall()) + 1)
        if search:
            now = self.clock()
            while self.search_times and now - self.search_times[0] >= 60:
                self.search_times.popleft()
            if len(self.search_times) >= 10:
                self.pause(max(0.0, self.search_times[0] + 60 - now))
            self.search_times.append(self.clock())
        self.last_host[host] = self.clock()

    def get(self, url, *, headers=None, search=False, limit=16 * 1024 * 1024):
        host = urlsplit(url).hostname
        request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        # Never send a GitHub token to repository hosts or raw.githubusercontent.com.
        if self.token and host == "api.github.com":
            request_headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(5):
            self.check()
            self._pace(host, search)
            timeout = 30.0
            if self.deadline is not None:
                timeout = min(timeout, max(0.001, self.deadline - self.clock()))
            response = self.transport(url, request_headers, limit, timeout)
            if len(response.body) > limit:
                raise DownloadTooLarge(url)
            if host == "api.github.com":
                resource = "search" if search else "core"
                try:
                    self.budgets[resource] = (
                        int(response.headers["x-ratelimit-remaining"]),
                        float(response.headers["x-ratelimit-reset"]),
                    )
                except (KeyError, ValueError):
                    pass
            secondary = response.status == 403 and (
                "retry-after" in response.headers
                or b"rate limit" in response.body.lower()
                or response.headers.get("x-ratelimit-remaining") == "0"
            )
            if response.status == 429 or 500 <= response.status < 600 or secondary:
                if attempt == 4:
                    raise OSError(f"HTTP {response.status}: {url}")
                delay = retry_delay(response.headers, attempt, self.wall())
                if host == "api.github.com" and (response.status == 429 or secondary) and "retry-after" not in response.headers:
                    # GitHub's secondary limits clear after about a minute; short exponential waits just burn retries.
                    delay = max(delay, 60.0 * (attempt + 1))
                if response.headers.get("x-ratelimit-remaining") == "0":
                    delay = max(delay, float(response.headers.get("x-ratelimit-reset", 0)) - self.wall() + 1)
                self.pause(delay)
                continue
            return response
        raise AssertionError("unreachable")

    def _request(self, url, headers, limit, timeout):
        try:
            stream = urlopen(Request(url, headers=headers), timeout=timeout)
        except HTTPError as exc:
            stream = exc
        with stream:
            length = stream.headers.get("Content-Length")
            if length and int(length) > limit:
                raise DownloadTooLarge(url)
            body = bytearray()
            while True:
                self.check()
                chunk = stream.read(min(65536, limit + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limit:
                    raise DownloadTooLarge(url)
            return Response(stream.code, dict(stream.headers), bytes(body))


def within_share(client, seconds, stage, label):
    """Run stage() with at most `seconds` of the client's budget; False if that share ran out first."""
    original = client.deadline
    share = client.clock() + seconds
    client.deadline = min(original, share) if original is not None else share
    try:
        return stage()
    except DeadlineReached:
        if original is not None and original <= share:
            raise
        logging.getLogger(__name__).info("%s budget reached; continuing other stages with it unfinished", label)
        return False
    finally:
        client.deadline = original
