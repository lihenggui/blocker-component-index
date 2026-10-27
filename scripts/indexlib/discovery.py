"""Persisted GitHub size shards, repository provenance, and coordinate seeds."""

import logging
import time
from datetime import datetime
from urllib.parse import quote, urlencode, urlsplit
from .parsing import BUILTINS, FILES, coordinates, gradle_repositories, normalize_repository
from .http import within_share
from .storage import checkpointed, read_json, timestamp, valid_coordinate

LOG = logging.getLogger(__name__)
SIZE_BUCKETS = ((0, 1023), (1024, 4095), (4096, 16383), (16384, 65535))


def split_shard(shard, total_count):
    if total_count <= 1000 or shard["low"] >= shard["high"]:
        return []
    middle = (shard["low"] + shard["high"]) // 2
    if not shard["host"] and min(middle - shard["low"] + 1, shard["high"] - middle) < 256:
        return []
    return [{**shard, "high": middle, "page": 1}, {**shard, "low": middle + 1, "page": 1}]


def add_repository(store, url, source):
    url = normalize_repository(url)
    if not url:
        return None
    repository = next((item for item in store.repositories if item["url"] == url), None)
    if repository is None:
        repository = {"url": url, "capabilities": [], "discoveredBy": [], "occurrences": 0, "lastChecked": timestamp()}
        store.repositories.append(repository)
        completed = store.state.get("completed", [])
        completed[:] = [stage for stage in completed if stage == "discover"]
        pending = store.state.get("probe", {}).get("queue")
        if pending is not None and url not in pending:
            pending.append(url)
    repository["occurrences"] += 1
    repository["discoveredBy"] = sorted(set(repository["discoveredBy"]) | {source})
    return repository


def _shards(host=""):
    if host:
        return [{"file": name, "low": 0, "high": 393215, "page": 1, "host": host} for name in FILES]
    return [{"file": name, "low": low, "high": high, "page": 1, "host": host}
            for name in FILES for low, high in SIZE_BUCKETS]


def bootstrap(store):
    before = {item["url"] for item in store.repositories}
    seeds = store.config("seeds.yaml")
    for source, urls in (("builtin", BUILTINS), ("seeds", seeds.get("repositories", []))):
        for url in urls:
            normalized = normalize_repository(url)
            existing = next((item for item in store.repositories if item["url"] == normalized), None)
            if existing is None or source not in existing["discoveredBy"]:
                add_repository(store, url, source)
    configured = {coordinate for coordinate in seeds.get("coordinates", []) if valid_coordinate(coordinate)}
    coordinates_by_repository = store.state.setdefault("coordinates", {})
    legacy = set(coordinates_by_repository.get("*", [])) - configured
    if legacy:
        # Recover old global dependency seeds from their existing evidence/handoff.
        for path in (store.root / "data/artifacts").glob("*/*.json"):
            evidence = read_json(path, None)
            for version in evidence["versions"].values():
                for dependency in version["dependencies"]:
                    store.seed(":".join(dependency.split(":")[:2]), evidence["repository"])
        for job in store.state.get("extractions", []):
            for dependency in job["dependencies"]:
                store.seed(":".join(dependency.split(":")[:2]), job["repository"])
    if configured:
        coordinates_by_repository["*"] = sorted(set(coordinates_by_repository.get("*", [])) & configured)
    else:
        coordinates_by_repository.pop("*", None)
    for coordinate in configured:
        store.seed(coordinate)
    for url in store.state.pop("pom_repositories", []):
        add_repository(store, url, "pom")
    added = {item["url"] for item in store.repositories} - before
    pending = store.state.get("probe", {}).get("queue")
    if pending is not None:
        pending.extend(sorted(added - set(pending)))
    store.save()
    return added


def _query(shard):
    # Broad sweeps keep to Android builds: settings files declare google(), build files reference com.android.
    android = "google" if shard["file"].startswith("settings.") else '"com.android"'
    prefix = f'"{shard["host"]}" {android}' if shard["host"] else f"maven url {android}"
    if shard["host"] and shard["low"] == 0 and shard["high"] == 393215:
        return f'{prefix} filename:{shard["file"]}'
    return f'{prefix} filename:{shard["file"]} size:{shard["low"]}..{shard["high"]}'


def _tree_paths(paths):
    return sorted(set(paths), key=lambda path: (
        0 if "/" not in path or path == "gradle/libs.versions.toml" else 1,
        path.count("/"), 0 if path.rsplit("/", 1)[-1].startswith("settings.") else 1, path,
    ))[:50]


def _tree_priority(store, tree):
    # Repositories with the fewest harvested coordinates (typically closed vendor repos) are harvested first.
    seeds = store.state.get("coordinates", {})
    return min((len(seeds.get(url, [])) for url in tree.get("repositories", [])), default=0)


def _tree_seeds(store, client, progress, *, limit=None):
    processed = 0
    trees = progress.setdefault("trees", {})
    for name, tree in sorted(trees.items(), key=lambda item: (_tree_priority(store, item[1]), item[0])):
        if tree.get("done"):
            continue
        if limit is not None and processed >= limit:
            break
        processed += 1
        client.check()
        if "branch" not in tree:
            response = client.get(f"https://api.github.com/repos/{name}")
            if response.status in (404, 451):
                tree["done"] = True
                store.save()
                continue
            if response.status != 200:
                raise OSError(f"GitHub repository HTTP {response.status}: {name}")
            tree["branch"] = response.json()["default_branch"]
            store.save()
        if "paths" not in tree:
            response = client.get(f'https://api.github.com/repos/{name}/git/trees/{quote(tree["branch"], safe="")}?recursive=1', limit=64 * 1024 * 1024)
            if response.status in (404, 409, 422):
                # Empty, missing or oversized repositories have no harvestable tree.
                tree["done"] = True
                store.save()
                continue
            if response.status != 200:
                raise OSError(f"GitHub tree HTTP {response.status}: {name}")
            value = response.json()
            if value.get("truncated"):
                LOG.warning("GitHub returned a truncated tree for %s", name)
            tree["paths"] = _tree_paths(item["path"] for item in value.get("tree", []) if item.get("type") == "blob" and (
                item["path"].rsplit("/", 1)[-1] in FILES
                or item["path"] == "gradle/libs.versions.toml"))
            tree["cursor"] = 0
            store.save()
        while tree["cursor"] < len(tree["paths"]):
            path = tree["paths"][tree["cursor"]]
            response = client.get(f'https://raw.githubusercontent.com/{name}/{quote(tree["branch"], safe="")}/{quote(path, safe="/")}')
            if response.status not in (200, 404):
                raise OSError(f"GitHub raw HTTP {response.status}: {name}/{path}")
            if response.status == 200:
                for coordinate in coordinates(response.body.decode("utf-8", errors="replace"), catalog=path.endswith(".toml")):
                    harvested = tree.setdefault("coordinates", [])
                    if coordinate not in harvested:
                        harvested.append(coordinate)
                    for repository in tree["repositories"]:
                        store.seed(coordinate, repository)
            tree["cursor"] += 1
            store.save()
        tree["done"] = True
        store.save()


def _prepare(store, progress):
    queue = progress.setdefault("queue", [])
    queue[:] = [{**shard, "high": min(shard["high"], 65535)} if not shard["host"] else shard
                for shard in queue if shard["host"] or shard["low"] <= 65535]
    completed_at = progress.get("broadCompletedAt")
    due = not completed_at or time.time() - datetime.fromisoformat(completed_at).timestamp() >= 28 * 86400
    starting = due and not any(not shard["host"] for shard in queue)
    if starting:
        queue.extend(_shards())
    hosts = progress.setdefault("hosts", [])
    builtin_hosts = {urlsplit(url).hostname for url in BUILTINS}
    for repository in store.repositories:
        host = urlsplit(repository["url"]).hostname
        if host not in builtin_hosts and host not in hosts:
            hosts.append(host)
            if not any(shard["host"] == host for shard in queue):
                queue.extend(_shards(host))
    queue.sort(key=lambda shard: not bool(shard["host"]))
    trees = progress.setdefault("trees", {})
    if starting:
        # Re-harvest finished trees when a broad cycle starts, not every run: a full refresh takes longer than the
        # harvesting budget, so refreshing each run would starve trees that were never harvested.
        pending = {name: tree for name, tree in trees.items() if not tree.get("done")}
        refreshed = {name: {"repositories": tree["repositories"], "coordinates": []}
                     for name, tree in trees.items() if tree.get("done")}
        progress["trees"] = {**pending, **refreshed}
    for tree in progress["trees"].values():
        if "paths" in tree and (len(tree["paths"]) > 50 or "coordinates" not in tree):
            tree["paths"] = _tree_paths(tree["paths"])
            tree["cursor"] = 0
        tree.setdefault("coordinates", [])
    store.save()


def _discover(store, client):
    bootstrap(store)
    progress = store.state.setdefault("discover", {})
    if not client.token:
        LOG.warning("Skipping GitHub discovery: CODE_SEARCH_TOKEN and GITHUB_TOKEN are unset")
        return True
    _prepare(store, progress)
    # Harvesting uses the core API and raw files, not search quota: give it half of discovery's time up front
    # so search rate limits cannot starve the coordinate seeds that closed vendor repositories depend on.
    if client.deadline is not None:
        within_share(client, max(0.0, client.deadline - client.clock()) / 2,
                     lambda: _tree_seeds(store, client, progress), "Coordinate harvesting")
    else:
        _tree_seeds(store, client, progress, limit=1)
    # Disjoint size shards rarely repeat a file; dedupe in memory instead of committing every hit URL.
    progress.pop("hits", None)
    seen = set()
    while progress["queue"]:
        client.check()
        shard = progress["queue"][0]
        params = urlencode({"q": _query(shard), "per_page": 100, "page": shard["page"]})
        try:
            response = client.get("https://api.github.com/search/code?" + params,
                                  headers={"Accept": "application/vnd.github.text-match+json"}, search=True)
        except OSError as exc:
            if "HTTP 429" not in str(exc) and "HTTP 403" not in str(exc):
                raise
            # A persistent search limit is routine, not a failure: resume the shard on the next run.
            LOG.warning("GitHub search still rate limited; discovery resumes next run: %s", exc)
            store.save()
            return False
        if response.status != 200:
            raise OSError(f"GitHub search HTTP {response.status}")
        value = response.json()
        children = split_shard(shard, value.get("total_count", 0))
        if children:
            progress["queue"][0:1] = children
            store.save()
            continue
        if value.get("incomplete_results") and value.get("total_count", 0) <= 1000:
            raise OSError("GitHub search returned incomplete_results; leaving shard for retry")
        if value.get("total_count", 0) > 1000:
            if shard["page"] == 1:
                LOG.warning("GitHub shard at size floor; taking first 1000 of %s hits: %s", value["total_count"], _query(shard))
        for item in value.get("items", []):
            hit = item.get("html_url") or item.get("url") or str(item)
            if hit in seen:
                continue
            seen.add(hit)
            fragments = "\n".join(match.get("fragment", "") for match in item.get("text_matches", []))
            for url in gradle_repositories(fragments):
                add_repository(store, url, "github")
                host = urlsplit(url).hostname
                hosts = progress.setdefault("hosts", [])
                if host not in hosts:
                    hosts.append(host)
                    progress["queue"].extend(_shards(host))
                name = item.get("repository", {}).get("full_name")
                if not name:
                    continue
                sources = progress.setdefault("sources", {}).setdefault(url, [])
                if name not in sources and len(sources) < 30:
                    sources.append(name)
                if name in sources:
                    tree = progress.setdefault("trees", {}).setdefault(name, {"repositories": []})
                    if url not in tree["repositories"]:
                        tree["repositories"].append(url)
                        for coordinate in tree.get("coordinates", []):
                            store.seed(coordinate, url)
        if shard["page"] * 100 < min(value.get("total_count", 0), 1000):
            shard["page"] += 1
        else:
            progress["queue"].pop(0)
            if not shard["host"] and not any(not pending["host"] for pending in progress["queue"]):
                progress["broadCompletedAt"] = timestamp()
        progress["queue"].sort(key=lambda pending: not bool(pending["host"]))
        store.save()
        # Do not let a large search queue starve coordinate harvesting.
        _tree_seeds(store, client, progress, limit=1)
    _tree_seeds(store, client, progress)
    del progress["queue"]
    store.save()
    return True


@checkpointed
def discover(store, client, *, max_minutes=300, **_):
    # Discovery saturates quickly and mostly waits on search limits; fetch needs the time more.
    return within_share(client, max_minutes * 60 / 6, lambda: _discover(store, client), "Discovery")
