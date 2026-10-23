from unittest.mock import patch
from crawl import STAGES, run
from indexlib.evidence import components_at
from indexlib.http import DeadlineReached
from indexlib.parsing import BUILTINS
from indexlib.repositories import metadata_url
from indexlib.storage import Store
from test_support import FixtureClient, FixtureTest, aar, metadata_fixture, response


class PipelineTests(FixtureTest):
    def test_given_fixture_repositories_when_all_stages_run_then_evidence_and_coverage_are_generated_offline(self):
        google, central = BUILTINS

        def handler(url, **kwargs):
            if url.endswith("/junit/junit/maven-metadata.xml"):
                return response(status=404)
            if url == google + "/master-index.xml":
                return response("<metadata><org.example/></metadata>")
            if url == central + "/master-index.xml":
                return response(status=404)
            if url == google + "/org/example/group-index.xml":
                return response('<org.example><sdk versions="1,2,3"/></org.example>')
            if url == metadata_url(google, "org.example:sdk"):
                return response(metadata_fixture())
            if url.startswith("https://search.maven.org/solrsearch/select?"):
                return response({"response": {"numFound": 0, "docs": []}})
            if url.endswith(".pom"):
                return response("<project><packaging>aar</packaging><dependencies/></project>")
            if url.endswith(".aar"):
                return response(aar(b"<manifest><application/></manifest>" if "/2/" in url else None))
            raise AssertionError(url)
        client = FixtureClient(handler=handler)
        self.assertEqual(run(self.store, client, "all", rules_dir=self.rules()), 0)
        evidence = self.store.evidence("org.example:sdk")
        self.assertEqual(list(evidence["versions"]), ["1", "2", "3"])
        self.assertEqual(len(components_at(evidence, "1")), 5)
        self.assertEqual(len(components_at(evidence, "2")), 0)
        self.assertTrue((self.root / "reports/coverage.md").exists())
        self.assertEqual(Store(self.root).state["completed"], [])

    def test_given_time_budget_exhausted_mid_pipeline_when_resumed_then_completed_stages_are_skipped(self):
        calls = []

        def stage(name, stop=False):
            def execute(*args, **kwargs):
                calls.append(name)
                if stop:
                    raise DeadlineReached()
                return True
            return execute
        with patch.dict(STAGES, {name: stage(name, name == "fetch") for name in STAGES}):
            self.assertEqual(run(self.store, FixtureClient(), "all"), 0)
        self.assertEqual(Store(self.root).state["completed"], ["discover", "probe", "enumerate"])
        calls.clear()
        with patch.dict(STAGES, {name: stage(name) for name in STAGES}):
            self.assertEqual(run(Store(self.root), FixtureClient(), "all"), 0)
        self.assertEqual(calls, ["fetch", "extract", "group", "report"])

    def test_given_partial_upstream_stage_when_pipeline_runs_then_later_stages_remain_eligible_for_next_run(self):
        stages = {name: (lambda *args, **kwargs: True) for name in STAGES}
        stages["enumerate"] = lambda *args, **kwargs: False
        with patch.dict(STAGES, stages):
            self.assertEqual(run(self.store, FixtureClient(), "all"), 0)
        self.assertEqual(Store(self.root).state["completed"], ["discover", "probe"])
