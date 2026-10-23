from datetime import datetime, timezone
from indexlib.http import DeadlineReached, DownloadTooLarge, HttpClient, USER_AGENT, retry_delay
from test_support import FixtureTest, response


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class HttpTests(FixtureTest):
    def client(self, results, **kwargs):
        self.clock = Clock()
        self.calls = []

        def transport(url, headers, limit, timeout):
            self.calls.append((url, headers, limit, timeout, self.clock()))
            return results.pop(0)
        return HttpClient(clock=self.clock, wall=self.clock, sleep=self.clock.sleep, transport=transport, **kwargs)

    def test_given_same_and_different_hosts_when_requested_then_only_same_host_is_paced_and_user_agent_is_set(self):
        client = self.client([response()] * 3)
        client.get("https://one.example/a")
        client.get("https://two.example/b")
        client.get("https://one.example/c")
        self.assertEqual(self.clock.sleeps, [0.5])
        self.assertEqual(self.calls[0][1]["User-Agent"], USER_AGENT)

    def test_given_retryable_responses_when_requested_then_retry_after_and_exponential_backoff_are_honored(self):
        client = self.client([response(status=429, headers={"Retry-After": "7"}), response(status=503), response()])
        client.get("https://repo.example/a")
        self.assertEqual(self.clock.sleeps, [7, 2])

    def test_given_retry_after_http_date_when_delay_computed_then_wall_time_is_used(self):
        epoch = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()
        self.assertEqual(retry_delay({"Retry-After": "Thu, 22 Oct 2026 00:00:15 GMT"}, 4, epoch), 15)
        self.assertEqual(retry_delay({"Retry-After": "invalid"}, 3, epoch), 8)

    def test_given_search_requests_when_limit_reached_then_no_more_than_ten_occur_in_a_minute(self):
        client = self.client([response()] * 21)
        for _ in range(21):
            client.get("https://api.github.com/search/code", search=True)
        times = [call[-1] for call in self.calls]
        for time in times:
            self.assertLessEqual(sum(time <= later < time + 60 for later in times), 10)

    def test_given_remaining_zero_when_next_github_request_occurs_then_reset_header_controls_sleep(self):
        client = self.client([response(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "90"}), response()])
        client.get("https://api.github.com/search/code", search=True)
        client.get("https://api.github.com/search/code", search=True)
        self.assertGreaterEqual(self.calls[1][-1], 90)

    def test_given_secondary_rate_limit_when_forbidden_then_retry_after_is_respected(self):
        client = self.client([response("secondary rate limit", 403, {"Retry-After": "61"}), response()])
        client.get("https://api.github.com/search/code", search=True)
        self.assertEqual(self.clock.sleeps, [61])

    def test_given_deadline_before_retry_when_rate_limited_then_stop_without_sleeping_past_budget(self):
        client = self.client([response(status=429, headers={"Retry-After": "60"})], deadline=10)
        with self.assertRaises(DeadlineReached):
            client.get("https://repo.example/a")
        self.assertEqual(self.clock.sleeps, [])

    def test_given_github_token_when_repository_host_requested_then_authorization_is_not_leaked(self):
        client = self.client([response(), response()], token="fixture-token")
        client.get("https://api.github.com/repos/o/r")
        client.get("https://raw.githubusercontent.com/o/r/main/build.gradle")
        self.assertEqual(self.calls[0][1]["Authorization"], "Bearer fixture-token")
        self.assertNotIn("Authorization", self.calls[1][1])

    def test_given_oversized_body_when_downloaded_then_size_limit_is_enforced(self):
        client = self.client([response(b"12345")])
        with self.assertRaises(DownloadTooLarge):
            client.get("https://repo.example/a", limit=4)

    def test_given_persistent_server_error_when_requested_then_retry_count_is_bounded(self):
        client = self.client([response(status=500)] * 5)
        with self.assertRaises(OSError):
            client.get("https://repo.example/a")
        self.assertEqual(len(self.calls), 5)

    def test_given_github_429_without_retry_after_when_searched_then_backoff_waits_at_least_a_minute_per_attempt(self):
        client = self.client([response(status=429), response(status=429), response()])
        client.get("https://api.github.com/search/code?q=x", search=True)
        retry_waits = [seconds for seconds in self.clock.sleeps if seconds >= 1]
        self.assertEqual(retry_waits[:2], [60.0, 120.0])

    def test_given_cdn_host_when_requested_twice_then_shorter_interval_is_used(self):
        client = self.client([response()] * 4)
        client.get("https://repo1.maven.org/maven2/a")
        client.get("https://repo1.maven.org/maven2/b")
        client.get("https://vendor.example/a")
        client.get("https://vendor.example/b")
        self.assertEqual(self.clock.sleeps, [0.1, 0.5])
