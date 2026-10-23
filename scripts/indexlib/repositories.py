"""Capability probing and checkpointed enumeration for generic Maven servers."""

import json
import logging
import string
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree.ElementTree import ParseError as ET_ERROR
from defusedxml.common import DefusedXmlException
from .parsing import BUILTINS, metadata, xml
from .storage import timestamp, valid_coordinate

LOG = logging.getLogger(__name__)
CENTRAL = BUILTINS[1]


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


def probe(store, client, **_):
    progress = store.state.setdefault("probe", {})
    pending = progress.setdefault("queue", [item["url"] for item in store.repositories])
    for _ in range(len(pending)):
        client.check()
        url = pending[0]
        repository = next(item for item in store.repositories if item["url"] == url)
        try:
            repository["capabilities"] = capabilities(url, client)
            repository["lastChecked"] = timestamp()
            pending.pop(0)
        except OSError as exc:
            LOG.warning("Probe will retry %s: %s", url, exc)
            pending.append(pending.pop(0))
        store.save()
    if not pending:
        del progress["queue"]
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


def _run_task(task, client, store):
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
    if response.status == 404:
        return []
    if response.status != 200:
        # Central sometimes rejects paging beyond its window. Split instead of losing hits.
        if kind == "central-search" and task.get("start", 0) and response.status == 400 and not task.get("exact"):
            return [{**child, "url": _central_url(child["prefix"], child.get("exact", False))} for child in central_shards(task)]
        raise OSError(f"HTTP {response.status}: {task['url']}")
    if kind == "metadata":
        discovered, versions = metadata(response.body)
        value = task.get("coordinate") or discovered
        if task.get("coordinate") and discovered and value != discovered:
            raise ValueError(f"Metadata coordinate mismatch: expected {value}, got {discovered}")
        if value and versions:
            store.artifact(repository, value, versions)
    elif kind == "google-index":
        for group in xml(response.body):
            followups.append({"repository": repository, "kind": "google-group", "group": group.tag,
                              "url": repository + "/" + group.tag.replace(".", "/") + "/group-index.xml"})
    elif kind == "google-group":
        for artifact in xml(response.body):
            coordinate(task["group"], artifact.tag)
    elif kind == "central-search":
        value = response.json()["response"]
        total, docs = value["numFound"], value["docs"]
        start = task.get("start", 0)
        if not task.get("exact") and (total > 10000 or (not docs and start < total)):
            return [{**child, "url": _central_url(child["prefix"], child.get("exact", False))} for child in central_shards(task)]
        for item in docs:
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
        followups.extend({"repository": repository, "kind": "metadata" if url.endswith("maven-metadata.xml") else "listing", "url": url}
                         for url in children(task["url"], response.body))
    return followups


def enumerate_artifacts(store, client, **_):
    progress = store.state.setdefault("enumerate", {})
    initial = []
    seeds = store.state.get("coordinates", {})
    for item in store.repositories:
        repository, caps = item["url"], item["capabilities"]
        if not caps or "mirror" in caps:
            continue
        best = caps[0]
        urls = endpoints(repository)
        if best == "metadata":
            known = set(seeds.get("*", [])) | set(seeds.get(repository, []))
            known.update(coord for coord, artifact in store.state.get("artifacts", {}).items() if artifact["repository"] == repository)
            for value in sorted(known):
                resolved = store.state.get("artifacts", {}).get(value)
                if not resolved or resolved["repository"] == repository:
                    initial.append({"repository": repository, "kind": "metadata", "coordinate": value, "url": metadata_url(repository, value)})
        else:
            url = (repository + "/master-index.xml" if best == "google-index" else _central_url() if best == "central-search"
                   else urls[best][0] if best == "artifactory" else repository + "/" if best == "listing" else urls[best])
            initial.append({"repository": repository, "kind": best, "url": url})
    progress.setdefault("queue", [])
    progress.setdefault("seen", [])
    known = set(progress["seen"]) | {json.dumps(task, sort_keys=True) for task in progress["queue"] + progress.get("deferred", [])}
    progress["queue"].extend(task for task in initial if json.dumps(task, sort_keys=True) not in known)
    store.save()
    queue = progress["queue"]
    queue.extend(progress.pop("deferred", []))
    failed = progress.setdefault("deferred", [])
    while queue:
        client.check()
        task = queue[0]
        try:
            followups = _run_task(task, client, store)
            key = json.dumps(task, sort_keys=True)
            progress["seen"].append(key)
            queue.pop(0)
            seen = set(progress["seen"])
            queued = {json.dumps(value, sort_keys=True) for value in queue + failed}
            for value in followups:
                key = json.dumps(value, sort_keys=True)
                if key not in seen and key not in queued:
                    queue.append(value)
                    queued.add(key)
        except (OSError, ValueError, ET_ERROR, DefusedXmlException) as exc:
            LOG.warning("Enumeration will retry %s: %s", task["url"], exc)
            failed.append(queue.pop(0))
        store.save()
    progress["queue"] = list(failed)
    progress.pop("deferred", None)
    if not failed:
        progress.clear()
    store.save()
    return not failed
