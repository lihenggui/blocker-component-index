"""Fixture helpers. Every test blocks the actual HTTP transport."""

import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from indexlib.http import Response
from indexlib.storage import Store

FIXTURES = Path(__file__).parent / "fixtures"


def response(value=b"", status=200, headers=None):
    if isinstance(value, (dict, list)):
        value = json.dumps(value).encode()
    elif isinstance(value, str):
        value = value.encode()
    return Response(status, headers or {}, value)


def aar(manifest=None, method=zipfile.ZIP_DEFLATED, comment=b""):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=method) as zipped:
        zipped.writestr("AndroidManifest.xml", manifest if manifest is not None else FIXTURES.joinpath("manifest.xml").read_bytes())
        zipped.writestr("classes.jar", b"fixture-only payload")
        zipped.comment = comment
    return stream.getvalue()


def metadata_fixture(coordinate="org.example:sdk", versions=("1", "2", "3")):
    group, artifact = coordinate.split(":")
    return (f"<metadata><groupId>{group}</groupId><artifactId>{artifact}</artifactId><versioning><versions>"
            + "".join(f"<version>{version}</version>" for version in versions)
            + "</versions></versioning></metadata>").encode()


class FixtureClient:
    token = None

    def __init__(self, routes=None, handler=None, token=None, clock=time.monotonic, deadline=None):
        self.routes = routes or {}
        self.handler = handler
        self.token = token
        self.calls = []
        self.clock = clock
        self.deadline = deadline

    def check(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            from indexlib.http import DeadlineReached
            raise DeadlineReached()

    def get(self, url, **kwargs):
        self.check()
        self.calls.append((url, kwargs))
        if self.handler:
            return self.handler(url, **kwargs)
        if url not in self.routes:
            raise AssertionError(f"Missing HTTP fixture: {url}")
        result = self.routes[url]
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.guard = patch("indexlib.http.urlopen", side_effect=AssertionError("Tests must never use the network"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "config/seeds.yaml").write_text("repositories: []\ncoordinates: []\n")
        (self.root / "config/sdk-overrides.yaml").write_text("groups: {}\n")
        self.store = Store(self.root)

    def rules(self):
        path = self.root / ".rules/rules/en/general.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((FIXTURES / "general.json").read_bytes())
        return self.root / ".rules"
