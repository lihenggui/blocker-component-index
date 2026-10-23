"""Newest-first AAR selection and a durable fetch-to-extract handoff."""

import logging
import zipfile
from urllib.parse import quote, urljoin
from xml.etree.ElementTree import ParseError
from defusedxml.common import DefusedXmlException
from .archives import read_manifest
from .http import within_share
from .evidence import components_at, merge, parse_manifest
from .parsing import metadata, module_info, pom
from .repositories import metadata_url
from .storage import checkpointed, timestamp, write_json
from .versions import ordered

LOG = logging.getLogger(__name__)


def _version(store, client, artifact, version, max_aar_mb):
    coordinate, repository = artifact["coordinate"], artifact["repository"]
    group, name = coordinate.split(":")
    base = repository + "/" + group.replace(".", "/") + "/" + name + "/" + quote(version, safe="") + "/"
    stem = name + "-" + quote(version, safe="")
    response = client.get(base + stem + ".pom")
    if response.status not in (200, 404):
        raise OSError(f"POM HTTP {response.status}: {coordinate}:{version}")
    packaging, dependencies, repositories = pom(response.body) if response.status == 200 else ("jar", [], [])
    for url in repositories:
        pending = store.state.setdefault("pom_repositories", [])
        if url not in pending:
            pending.append(url)
    for dependency in dependencies:
        store.seed(":".join(dependency.split(":")[:2]), repository)
    store.save()
    files = []
    if response.status == 404 or b"published-with-gradle-metadata" in response.body:
        module = client.get(base + stem + ".module")
        if module.status == 200:
            files, module_dependencies = module_info(module.body)
            if files:
                dependencies = module_dependencies
        elif module.status != 404:
            raise OSError(f"Gradle module HTTP {module.status}: {coordinate}:{version}")
    for dependency in dependencies:
        store.seed(":".join(dependency.split(":")[:2]), repository)
    store.save()
    if packaging != "aar" and not files:
        return None
    url = urljoin(base, files[0]) if files else base + stem + ".aar"
    manifest, sha256 = read_manifest(client, url, int(max_aar_mb * 1024 * 1024))
    # The newest manifest determines whether historical versions are worth fetching.
    components = parse_manifest(manifest)
    # Extract inline: evidence is written as soon as the manifest is read, so nothing waits in a queue or cache.
    evidence = merge(store.evidence(coordinate), coordinate, repository, version, timestamp(), sha256, dependencies, components)
    write_json(store.evidence_path(coordinate), evidence)
    return components


def _history(store, client, artifact, max_versions):
    """Search-enumerated artifacts only know their latest version; read the rest from maven-metadata.xml."""
    response = client.get(metadata_url(artifact["repository"], artifact["coordinate"]))
    if response.status != 200:
        return None
    _, versions = metadata(response.body)
    if not versions:
        return None
    artifact["versions"] = versions[-max_versions:]
    artifact.pop("latestOnly", None)
    return list(reversed(artifact["versions"]))


def _already_fetched(store, coordinate, version):
    evidence = store.evidence(coordinate)
    if evidence and version in evidence["versions"]:
        return components_at(evidence, version)
    for job in list(store.state.get("extractions", [])):
        if job["coordinate"] == coordinate and job["version"] == version:
            path = store.root / job["manifest"]
            if path.exists():
                return parse_manifest(path.read_bytes())
            store.state["extractions"].remove(job)
            store.save()
    return None


# Leave time in every run for the report (and draining manifests queued by older crawler versions).
EXTRACT_RESERVE_SECONDS = 10 * 60


@checkpointed
def fetch(store, client, *, max_versions=20, max_aar_mb=50, **_):
    if client.deadline is None:
        return _fetch(store, client, max_versions, max_aar_mb)
    share = max(0.0, client.deadline - client.clock() - EXTRACT_RESERVE_SECONDS)
    return within_share(client, share, lambda: _fetch(store, client, max_versions, max_aar_mb), "Fetch")


def _fetch(store, client, max_versions, max_aar_mb):
    progress = store.state.setdefault("fetch", {})
    if "queue" not in progress:
        progress["queue"] = sorted(store.state.get("artifacts", {}))
        progress["failures"] = {}
        store.save()
    failures = progress.setdefault("failures", {})
    pending = progress["queue"]
    for _ in range(len(pending)):
        client.check()
        coordinate = pending[0]
        artifact = store.state["artifacts"][coordinate]
        versions = list(reversed(ordered(artifact["versions"])))
        try:
            if versions:
                latest = _already_fetched(store, coordinate, versions[0])
                if latest is None:
                    latest = _version(store, client, artifact, versions[0], max_aar_mb)
                if latest and artifact.get("latestOnly"):
                    versions = _history(store, client, artifact, max_versions) or versions
                if latest:
                    for version in versions[1:max_versions]:
                        client.check()
                        if _already_fetched(store, coordinate, version) is None:
                            _version(store, client, artifact, version, max_aar_mb)
            pending.pop(0)
            failures.pop(coordinate, None)
        except (OSError, ValueError, ParseError, KeyError, zipfile.BadZipFile, DefusedXmlException) as exc:
            failures[coordinate] = failures.get(coordinate, 0) + 1
            pending.pop(0)
            if failures[coordinate] >= 3:
                LOG.warning("Fetch dropped %s after 3 consecutive failures; retry next cycle: %s", coordinate, exc)
            else:
                LOG.warning("Fetch will retry %s (failure %s/3): %s", coordinate, failures[coordinate], exc)
                pending.append(coordinate)
            store.save(force=True)
        store.save()
    if not pending:
        progress.pop("queue", None)
    store.save()
    return not pending


@checkpointed
def extract(store, client, **_):
    """Drain manifests queued by crawler versions that fetched and extracted in separate stages."""
    pending = store.state.setdefault("extractions", [])
    for _ in range(len(pending)):
        client.check()
        job = pending[0]
        path = store.root / job["manifest"]
        if not path.exists():
            # The version is not in evidence, so the next fetch cycle downloads it again.
            LOG.warning("Manifest cache missing; dropping queued %s:%s for refetch", job["coordinate"], job["version"])
            pending.pop(0)
            store.save()
            continue
        evidence = merge(store.evidence(job["coordinate"]), job["coordinate"], job["repository"], job["version"],
                         job["fetchedAt"], job["sha256"], job["dependencies"], parse_manifest(path.read_bytes()))
        write_json(store.evidence_path(job["coordinate"]), evidence)
        pending.pop(0)
        store.save()
    return not pending
