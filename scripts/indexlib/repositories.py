"""Capability probing and checkpointed enumeration for generic Maven servers."""

import json
import time
from datetime import datetime
import logging
import string
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree.ElementTree import ParseError as ET_ERROR
from defusedxml.common import DefusedXmlException
from .http import within_share
from .parsing import BUILTINS, metadata, xml
from .versions import ordered
from .storage import checkpointed, timestamp, valid_coordinate

LOG = logging.getLogger(__name__)
CENTRAL = BUILTINS[1]
# Directory listings can expose huge non-Android trees; bound the crawl per repository and cycle.
LISTING_LIMIT = 500
# Within one cycle a URL is enumerated once; new repositories and seeds still run on every invocation.
CYCLE_SECONDS = 7 * 86400


class Links(HTMLParser):
    def __init__(self, raw):
        super().__init__()
        self.links = []
        self.feed(raw.decode("utf-8", errors="replace"))

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def children(url, raw):
    base = url.rstrip("/") + "/"
    result = []
    for link in Links(raw).links:
        target = urljoin(base, link)
        parts = urlsplit(target)
        if (urlsplit(base).netloc == parts.netloc and target.startswith(base) and target != base
                and not parts.query and not parts.fragment
                and (parts.path.endswith("/") or parts.path.endswith("/maven-metadata.xml"))):
            result.append(target)
    return sorted(set(result))


def endpoints(url):
    parts = urlsplit(url)
    origin = parts.scheme + "://" + parts.netloc
    path = parts.path.strip("/").split("/")
    name = path[-1] if path and path[-1] else "releases"
    art_repo = path[path.index("artifactory") + 1] if "artifactory" in path and path.index("artifactory") + 1 < len(path) else name
    return {
        "nexus3": origin + "/service/rest/v1/search?" + urlencode({"repository": name}),
        "nexus2": origin + "/service/local/lucene/search?" + urlencode({"repositoryId": name}),
        "artifactory": [origin + prefix + quote(art_repo, safe="") + "?list&deep=1"
                        for prefix in ("/artifactory/api/storage/", "/api/storage/")],
    }


def listing_roots(repository, client):
    """Artifactory virtual/remote repositories mix proxied caches into listings; list the host's local Maven repositories instead."""
    parts = urlsplit(repository)
    origin = parts.scheme + "://" + parts.netloc
    name = parts.path.strip("/").split("/")[-1]
    response = client.get(origin + "/artifactory/api/repositories")
    try:
        repositories = response.json() if response.status == 200 else None
    except ValueError:
        repositories = None
    if not isinstance(repositories, list):
        return [repository + "/"]
    current = next((item for item in repositories if isinstance(item, dict) and item.get("key") == name), None)
    if not current or current.get("type") == "LOCAL":
        return [repository + "/"]
    # The canonical /artifactory/<key>/ path; some front ends route bare /<key>/ back to the virtual repository.
    local = sorted(origin + "/artifactory/" + item["key"] + "/" for item in repositories if isinstance(item, dict) and item.get("key")
                   and item.get("type") == "LOCAL" and item.get("packageType", "Maven").lower() == "maven")
    return local or [repository + "/"]


def metadata_url(repository, coordinate):
    group, artifact = coordinate.split(":")
    return repository + "/" + group.replace(".", "/") + "/" + artifact + "/maven-metadata.xml"


def capabilities(url, client):
    if url != CENTRAL:
        response = client.get(url + "/junit/junit/maven-metadata.xml")
        if response.status == 200:
            try:
                coordinate, versions = metadata(response.body)
                if coordinate == "junit:junit" and versions:
                    return ["mirror"]
            except (ValueError, ET_ERROR, DefusedXmlException):
                pass
    response = client.get(url + "/master-index.xml")
    if response.status == 200:
        try:
            if xml(response.body).tag == "metadata":
                return ["google-index", "metadata"]
        except (ValueError, ET_ERROR, DefusedXmlException):
            pass
    if url == CENTRAL:
        return ["central-search", "metadata"]
    urls = endpoints(url)
    response = client.get(urls["nexus3"])
    if response.status == 200:
        try:
            if isinstance(response.json().get("items"), list):
                return ["nexus3", "metadata"]
        except (ValueError, AttributeError):
            pass
    response = client.get(urls["nexus2"])
    if response.status == 200:
        try:
            if xml(response.body).tag in ("searchNGResponse", "search-results"):
                return ["nexus2", "metadata"]
        except (ValueError, ET_ERROR, DefusedXmlException):
            pass
    for endpoint in urls["artifactory"]:
        response = client.get(endpoint)
        if response.status == 200:
            try:
                if isinstance(response.json().get("files"), list):
                    return ["artifactory", "metadata"]
            except (ValueError, AttributeError):
                pass
    response = client.get(url + "/")
    if response.status == 200 and children(url, response.body):
        return ["listing", "metadata"]
    return ["metadata"]


@checkpointed
def probe(store, client, **_):
    progress = store.state.setdefault("probe", {})
    pending = progress.setdefault("queue", [item["url"] for item in store.repositories])
    failures = progress.setdefault("failures", {})
    for _ in range(len(pending)):
        client.check()
        url = pending[0]
        repository = next(item for item in store.repositories if item["url"] == url)
        try:
            repository["capabilities"] = capabilities(url, client)
            repository["lastChecked"] = timestamp()
            pending.pop(0)
            failures.pop(url, None)
        except OSError as exc:
            failures[url] = failures.get(url, 0) + 1
            pending.pop(0)
            if failures[url] >= 3:
                # Unreachable hosts keep empty capabilities and are probed again next cycle.
                LOG.warning("Probe dropped %s after 3 consecutive failures; retry next cycle: %s", url, exc)
            else:
                LOG.warning("Probe will retry %s (failure %s/3): %s", url, failures[url], exc)
                pending.append(url)
            store.save(force=True)
        store.save()
    if not pending:
        progress.clear()
        store.save()
        return True
    return False


def _central_url(prefix="", exact=False, start=0):
    query = "p:aar"
    if prefix:
        escaped = prefix.replace("-", "\\-").replace("+", "\\+")
        query += " AND g:" + escaped + ("" if exact else "*")
    return "https://search.maven.org/solrsearch/select?" + urlencode({"q": query, "rows": 200, "start": start, "wt": "json"})


def central_shards(task):
    prefix = task.get("prefix", "")
    alphabet = string.ascii_letters + string.digits + ("_.-+" if prefix else "_")
    children = [{**task, "prefix": prefix + char, "start": 0} for char in alphabet]
    if prefix:
        children.append({**task, "exact": True, "start": 0})
    return children


def _run_task(task, client, store, max_versions=20):
    repository, kind = task["repository"], task["kind"]
    followups = []

    def coordinate(group, artifact):
        value = group + ":" + artifact
        if valid_coordinate(value):
            followups.append({"repository": repository, "kind": "metadata", "coordinate": value,
                              "url": metadata_url(repository, value)})

    response = client.get(task["url"], limit=64 * 1024 * 1024)
    if response.status == 404 and kind == "artifactory" and task["url"] == endpoints(repository)["artifactory"][0]:
        return [{**task, "url": endpoints(repository)["artifactory"][1]}]
    # Some hosts (e.g. JitPack) answer unknown coordinates with 401/403 rather than 404.
    if response.status == 404 or (kind == "metadata" and response.status in (401, 403)):
        return []
    if response.status != 200:
        # Central sometimes rejects paging beyond its window. Split instead of losing hits.
        if kind == "central-search" and task.get("start", 0) and response.status == 400 and not task.get("exact"):
            return [{**child, "url": _central_url(child["prefix"], child.get("exact", False))} for child in central_shards(task)]
        raise OSError(f"HTTP {response.status}: {task['url']}")
    if kind == "metadata":
        if response.body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            # Some hosts answer unknown coordinates with an HTML page instead of 404.
            return []
        discovered, versions = metadata(response.body)
        value = task.get("coordinate") or discovered
        if task.get("coordinate") and discovered and value != discovered:
            raise ValueError(f"Metadata coordinate mismatch: expected {value}, got {discovered}")
        if value and versions:
            store.artifact(repository, value, versions[-max_versions:])
    elif kind == "google-index":
        for group in xml(response.body):
            followups.append({"repository": repository, "kind": "google-group", "group": group.tag,
                              "url": repository + "/" + group.tag.replace(".", "/") + "/group-index.xml"})
    elif kind == "google-group":
        # group-index.xml already lists versions, so no per-artifact metadata request is needed.
        for artifact in xml(response.body):
            versions = [v.strip() for v in (artifact.get("versions") or "").split(",") if v.strip()]
            if versions:
                store.artifact(repository, task["group"] + ":" + artifact.tag, ordered(versions)[-max_versions:])
            else:
                coordinate(task["group"], artifact.tag)
    elif kind == "central-search":
        value = response.json()["response"]
        total, docs = value["numFound"], value["docs"]
        start = task.get("start", 0)
        if not task.get("exact") and (total > 10000 or (not docs and start < total)):
            return [{**child, "url": _central_url(child["prefix"], child.get("exact", False))} for child in central_shards(task)]
        # Search hits carry the latest version; fetch reads full metadata only if that version declares components.
        for item in docs:
            if item.get("latestVersion"):
                store.artifact(repository, item["g"] + ":" + item["a"], [item["latestVersion"]], latest_only=True)
            else:
                coordinate(item["g"], item["a"])
        if docs and start + len(docs) < total:
            start += len(docs)
            followups.append({**task, "start": start, "url": _central_url(task.get("prefix", ""), task.get("exact", False), start)})
    elif kind == "nexus3":
        value = response.json()
        for item in value["items"]:
            if item.get("group") and item.get("name"):
                coordinate(item["group"], item["name"])
        token = value.get("continuationToken")
        if token:
            followups.append({**task, "url": endpoints(repository)["nexus3"] + "&" + urlencode({"continuationToken": token})})
    elif kind == "nexus2":
        root = xml(response.body)
        items = root.findall(".//artifact")
        for item in items:
            group, artifact = item.findtext("groupId"), item.findtext("artifactId")
            if group and artifact:
                coordinate(group, artifact)
        start = task.get("start", 0) + len(items)
        total = int(root.findtext("totalCount") or len(items))
        if items and start < total:
            followups.append({**task, "start": start, "url": endpoints(repository)["nexus2"] + "&" + urlencode({"from": start, "count": 100})})
    elif kind == "artifactory":
        for item in response.json()["files"]:
            path = item.get("uri", "").strip("/").split("/")
            if len(path) >= 4 and path[-1].endswith(".aar"):
                coordinate(".".join(path[:-3]), path[-3])
    elif kind == "listing":
        links = children(task["url"], response.body)
        metadata_links = [url for url in links if url.endswith("/maven-metadata.xml")]
        # An artifact directory lists its versions as subdirectories; its metadata already names them all.
        followups.extend({"repository": repository, "kind": "metadata" if url.endswith("maven-metadata.xml") else "listing", "url": url}
                         for url in (metadata_links or links))
    return followups


@checkpointed
def enumerate_artifacts(store, client, *, max_minutes=300, max_versions=20, **_):
    return within_share(client, max_minutes * 60 / 3, lambda: _enumerate(store, client, max_versions), "Enumeration")


def _enumerate(store, client, max_versions):
    progress = store.state.setdefault("enumerate", {})
    started = progress.get("cycleStartedAt")
    if not started or time.time() - datetime.fromisoformat(started).timestamp() >= CYCLE_SECONDS:
        progress.clear()
        progress["cycleStartedAt"] = timestamp()
    initial = []
    seeds = store.state.get("coordinates", {})
    for item in store.repositories:
        repository, caps = item["url"], item["capabilities"]
        if not caps:
            continue
        best = caps[0]
        urls = endpoints(repository)
        if best in ("metadata", "mirror"):
            known = set(seeds.get(repository, []))
            if best == "metadata":
                known.update(seeds.get("*", []))
                known.update(coord for coord, artifact in store.state.get("artifacts", {}).items() if artifact["repository"] == repository)
            for value in sorted(known):
                resolved = store.state.get("artifacts", {}).get(value) or store.evidence(value)
                if not resolved or resolved["repository"] == repository:
                    initial.append({"repository": repository, "kind": "metadata", "coordinate": value, "url": metadata_url(repository, value)})
        elif best == "listing":
            try:
                roots = listing_roots(repository, client)
            except OSError as exc:
                LOG.warning("Artifactory repository lookup failed for %s: %s", repository, exc)
                roots = [repository + "/"]
            initial.extend({"repository": repository, "kind": "listing", "url": url} for url in roots)
        else:
            url = (repository + "/master-index.xml" if best == "google-index" else _central_url() if best == "central-search"
                   else urls[best][0] if best == "artifactory" else urls[best])
            initial.append({"repository": repository, "kind": best, "url": url})
    progress.setdefault("queue", [])
    progress.setdefault("seen", [])
    caps_by_repository = {item["url"]: item["capabilities"] for item in store.repositories}

    def eligible(task):
        caps = caps_by_repository.get(task["repository"], [])
        if not caps or caps[0] not in ("metadata", "mirror"):
            return True
        if task["kind"] != "metadata" or "coordinate" not in task:
            return False
        coordinate = task["coordinate"]
        resolved = store.state.get("artifacts", {}).get(coordinate) or store.evidence(coordinate)
        if resolved and resolved["repository"] != task["repository"]:
            return False
        attributed = coordinate in seeds.get(task["repository"], [])
        return attributed or (caps[0] == "metadata" and (
            coordinate in seeds.get("*", []) or (resolved and resolved["repository"] == task["repository"])))

    progress["queue"] = [task for task in progress["queue"] if eligible(task)]
    if "deferred" in progress:
        progress["deferred"] = [task for task in progress["deferred"] if eligible(task)]
    known = set(progress["seen"]) | {task["url"] for task in progress["queue"] + progress.get("deferred", [])}
    progress["queue"].extend(task for task in initial if task["url"] not in known)
    store.save()
    queue = progress["queue"]
    queue.extend(progress.pop("deferred", []))
    failed = progress.setdefault("deferred", [])
    queue.sort(key=lambda value: value["kind"] == "listing")
    seen = set(progress["seen"])
    queued = {value["url"] for value in queue}
    listed = progress.setdefault("listed", {})
    while queue:
        client.check()
        task = queue[0]
        if task["kind"] == "listing" and listed.get(task["repository"], 0) >= LISTING_LIMIT:
            if listed[task["repository"]] == LISTING_LIMIT:
                LOG.warning("Listing of %s capped at %s pages this cycle", task["repository"], LISTING_LIMIT)
                listed[task["repository"]] += 1
            queue.pop(0)
            queued.discard(task["url"])
            continue
        try:
            followups = _run_task(task, client, store, max_versions)
            if task["kind"] == "listing":
                listed[task["repository"]] = listed.get(task["repository"], 0) + 1
            key = task["url"]
            progress["seen"].append(key)
            queue.pop(0)
            seen.add(key)
            queued.discard(key)
            for value in followups:
                key = value["url"]
                if key not in seen and key not in queued:
                    # Cheap targeted tasks go first so slow directory crawls cannot starve them.
                    if value["kind"] == "listing":
                        queue.append(value)
                    else:
                        queue.insert(0, value)
                    queued.add(key)
        except (OSError, ValueError, ET_ERROR, DefusedXmlException) as exc:
            LOG.warning("Enumeration will retry %s: %s", task["url"], exc)
            failed.append(queue.pop(0))
            store.save(force=True)
        store.save()
    progress["queue"] = list(failed)
    progress.pop("deferred", None)
    store.save()
    return not failed
