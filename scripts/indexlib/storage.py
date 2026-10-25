"""Atomic JSON checkpoints; only data/ holds committed crawl state."""

import json
from bisect import bisect_left
import re
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import yaml


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def valid_coordinate(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*:[A-Za-z0-9_][A-Za-z0-9_.+\-]*", value))


def checkpointed(stage):
    @wraps(stage)
    def run(store, *args, **kwargs):
        try:
            return stage(store, *args, **kwargs)
        finally:
            # Stage completion, interruption, and errors must bypass throttling.
            store.save(force=True)
    return run


class Store:
    def __init__(self, root, *, clock=time.monotonic):
        self.root = Path(root)
        self.clock = clock
        self.last_save = None
        self.state = read_json(self.root / "data/state/progress.json", {})
        self.repositories = read_json(self.root / "data/repositories.json", [])

    def config(self, name):
        path = self.root / "config" / name
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}

    def save(self, *, force=False):
        now = self.clock()
        if not force and self.last_save is not None and now - self.last_save < 30:
            return
        write_json(self.root / "data/repositories.json", sorted(self.repositories, key=lambda item: item["url"]))
        write_json(self.root / "data/state/progress.json", self.state)
        self.last_save = self.clock()

    def evidence_path(self, coordinate):
        if not valid_coordinate(coordinate):
            raise ValueError(f"Invalid coordinate: {coordinate}")
        group, artifact = coordinate.split(":")
        return self.root / "data/artifacts" / group / (artifact + ".json")

    def evidence(self, coordinate):
        return read_json(self.evidence_path(coordinate), None)

    def seed(self, coordinate, repository="*"):
        if valid_coordinate(coordinate):
            coordinates = self.state.setdefault("coordinates", {}).setdefault(repository, [])
            if coordinate not in coordinates:
                coordinates.append(coordinate)
                coordinates.sort()
                completed = self.state.get("completed", [])
                completed[:] = [stage for stage in completed if stage in ("discover", "probe")]

    def seed_dependency(self, dependency, parent, repository):
        """Seed a dependency where its parent lives and, while unresolved, wherever builds declared the parent.

        Vendor SDKs are often pulled in by an adapter hosted elsewhere (an ad-mediation adapter on Google Maven
        depending on the vendor's SDK); the vendor repository is only known from builds that declared the adapter.
        """
        self.seed(dependency, repository)
        if not valid_coordinate(dependency) or dependency in self.state.get("artifacts", {}) or self.evidence(dependency):
            return
        mirrors = {item["url"] for item in self.repositories if item.get("capabilities", [])[:1] == ["mirror"]}
        for url, coordinates in list(self.state.get("coordinates", {}).items()):
            if url in ("*", repository) or url in mirrors:
                continue
            index = bisect_left(coordinates, parent)
            if index < len(coordinates) and coordinates[index] == parent:
                self.seed(dependency, url)

    def artifact(self, repository, coordinate, versions, *, latest_only=False):
        if not valid_coordinate(coordinate) or not versions:
            return
        artifacts = self.state.setdefault("artifacts", {})
        existing = artifacts.get(coordinate)
        evidence = self.evidence(coordinate)
        owner = existing["repository"] if existing else evidence["repository"] if evidence else repository
        if owner != repository:
            return
        artifacts[coordinate] = {"repository": repository, "coordinate": coordinate, "versions": versions}
        if latest_only:
            # Crawl state only: fetch reads full history from maven-metadata.xml when the latest version has components.
            artifacts[coordinate]["latestOnly"] = True
