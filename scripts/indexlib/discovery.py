"""Persisted GitHub size shards, repository provenance, and coordinate seeds."""

import logging
from urllib.parse import quote, urlencode, urlsplit
from .parsing import BUILTINS, FILES, coordinates, gradle_repositories, normalize_repository
from .storage import timestamp

LOG = logging.getLogger(__name__)
SIZE_BUCKETS = ((0, 1023), (1024, 4095), (4096, 16383), (16384, 65535), (65536, 393215))


def split_shard(shard, total_count):
    if total_count <= 1000 or shard["low"] >= shard["high"]:
        return []
    middle = (shard["low"] + shard["high"]) // 2
    return [{**shard, "high": middle, "page": 1}, {**shard, "low": middle + 1, "page": 1}]


def add_repository(store, url, source):
    url = normalize_repository(url)
    if not url:
        return None
    repository = next((item for item in store.repositories if item["url"] == url), None)
    if repository is None:
        repository = {"url": url, "capabilities": [], "discoveredBy": [], "occurrences": 0, "lastChecked": timestamp()}
        store.repositories.append(repository)
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
    for coordinate in seeds.get("coordinates", []):
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
    prefix = f'"{shard["host"]}"' if shard["host"] else "maven url"
    if shard["host"] and shard["low"] == 0 and shard["high"] == 393215:
        return f'{prefix} filename:{shard["file"]}'
    return f'{prefix} filename:{shard["file"]} size:{shard["low"]}..{shard["high"]}'


def _tree_seeds(store, client, progress):
    for name, tree in list(progress.setdefault("trees", {}).items()):
        if tree.get("done"):
            continue
        client.check()
        if "branch" not in tree:
            response = client.get(f"https://api.github.com/repos/{name}")
            if response.status == 404:
                tree["done"] = True
                store.save()
                continue
            if response.status != 200:
                raise OSError(f"GitHub repository HTTP {response.status}: {name}")
            tree["branch"] = response.json()["default_branch"]
            store.save()
        if "paths" not in tree:
            response = client.get(f'https://api.github.com/repos/{name}/git/trees/{quote(tree["branch"], safe="")}?recursive=1', limit=64 * 1024 * 1024)
            if response.status != 200:
                raise OSError(f"GitHub tree HTTP {response.status}: {name}")
            value = response.json()
            if value.get("truncated"):
                LOG.warning("GitHub returned a truncated tree for %s", name)
            tree["paths"] = sorted(item["path"] for item in value.get("tree", []) if item.get("type") == "blob" and (
                item["path"].rsplit("/", 1)[-1] in ("build.gradle", "build.gradle.kts")
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
                    for repository in tree["repositories"]:
                        store.seed(coordinate, repository)
            tree["cursor"] += 1
            store.save()
        tree["done"] = True
        store.save()


def discover(store, client, **_):
    bootstrap(store)
    progress = store.state.setdefault("discover", {})
    if "queue" not in progress:
        progress["queue"] = _shards()
        store.save()
    if not client.token:
        LOG.warning("Skipping GitHub discovery: CODE_SEARCH_TOKEN and GITHUB_TOKEN are unset")
        return True
    while progress["queue"]:
        client.check()
        shard = progress["queue"][0]
        params = urlencode({"q": _query(shard), "per_page": 100, "page": shard["page"]})
        response = client.get("https://api.github.com/search/code?" + params,
                              headers={"Accept": "application/vnd.github.text-match+json"}, search=True)
        if response.status != 200:
            raise OSError(f"GitHub search HTTP {response.status}")
        value = response.json()
        if value.get("incomplete_results"):
            raise OSError("GitHub search returned incomplete_results; leaving shard for retry")
        children = split_shard(shard, value.get("total_count", 0))
        if children:
            progress["queue"][0:1] = children
            store.save()
            continue
        if value.get("total_count", 0) > 1000:
            raise ValueError(f"Unsplittable GitHub size shard exceeds 1000 hits; retained for retry: {_query(shard)}")
        for item in value.get("items", []):
            hit = item.get("html_url") or item.get("url") or str(item)
            seen = progress.setdefault("hits", [])
            if hit in seen:
                continue
            seen.append(hit)
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
                        # A completed tree can seed another repository without storing source files.
                        tree.pop("done", None)
                        tree["cursor"] = 0
        if shard["page"] * 100 < min(value.get("total_count", 0), 1000):
            shard["page"] += 1
        else:
            progress["queue"].pop(0)
        store.save()
        # Do not let a large search queue starve coordinate harvesting.
        _tree_seeds(store, client, progress)
    _tree_seeds(store, client, progress)
    del progress["queue"]
    store.save()
    return True
