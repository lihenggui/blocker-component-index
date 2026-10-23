from indexlib.fetching import extract, fetch
from indexlib.http import DeadlineReached
from indexlib.storage import Store
from test_support import FIXTURES, FixtureClient, FixtureTest, aar, response

REPOSITORY = "https://sdk.example.org/repo"
COORDINATE = "org.example:sdk"


class FetchTests(FixtureTest):
    def setUp(self):
        super().setUp()
        self.store.artifact(REPOSITORY, COORDINATE, ["1", "2", "3"])

    def routes(self, empty_latest=False):
        routes = {}
        for version in ["1", "2", "3"]:
            base = f"{REPOSITORY}/org/example/sdk/{version}/sdk-{version}"
            routes[base + ".pom"] = response((FIXTURES / "artifact.pom").read_bytes())
            manifest = b"<manifest><application/></manifest>" if version == "2" or (empty_latest and version == "3") else None
            routes[base + ".aar"] = response(aar(manifest))
        return routes

    def test_given_latest_has_components_when_fetched_then_versions_are_newest_first_and_extraction_preserves_a_gap(self):
        client = FixtureClient(self.routes())
        self.assertTrue(fetch(self.store, client))
        self.assertEqual([job["version"] for job in self.store.state["extractions"]], ["3", "2", "1"])
        self.assertIsNone(self.store.evidence(COORDINATE))
        self.assertTrue(extract(Store(self.root), client))
        evidence = self.store.evidence(COORDINATE)
        self.assertEqual(list(evidence["versions"]), ["1", "2", "3"])
        self.assertEqual(evidence["components"][0]["versionRanges"], [["1", "1"], ["3", "3"]])
        self.assertEqual(len(list((self.root / ".cache/downloads").glob("*.aar"))), 3)

    def test_given_latest_has_no_components_when_fetched_then_older_versions_are_not_downloaded(self):
        client = FixtureClient(self.routes(empty_latest=True))
        self.assertTrue(fetch(self.store, client))
        self.assertEqual([job["version"] for job in self.store.state["extractions"]], ["3"])
        self.assertEqual(len(client.calls), 2)
        extract(self.store, client)
        self.assertEqual(self.store.evidence(COORDINATE)["components"], [])
        self.assertEqual(list(self.store.evidence(COORDINATE)["versions"]), ["3"])

    def test_given_version_cap_when_fetched_then_only_the_latest_requested_versions_are_downloaded(self):
        self.assertTrue(fetch(self.store, FixtureClient(self.routes()), max_versions=2))
        self.assertEqual([job["version"] for job in self.store.state["extractions"]], ["3", "2"])

    def test_given_already_checked_versions_when_fetch_repeats_then_no_network_requests_are_needed(self):
        client = FixtureClient(self.routes())
        fetch(self.store, client)
        extract(self.store, client)
        client = FixtureClient()
        self.assertTrue(fetch(Store(self.root), client))
        self.assertEqual(client.calls, [])

    def test_given_interruption_after_latest_download_when_resumed_then_latest_is_not_downloaded_twice(self):
        routes = self.routes()
        base = REPOSITORY + "/org/example/sdk/2/sdk-2.pom"
        routes[base] = DeadlineReached()
        with self.assertRaises(DeadlineReached):
            fetch(self.store, FixtureClient(routes))
        saved = Store(self.root)
        self.assertEqual([job["version"] for job in saved.state["extractions"]], ["3"])
        client = FixtureClient(self.routes())
        fetch(saved, client)
        self.assertTrue(all("/3/" not in url for url, _ in client.calls))
        self.assertEqual(len(saved.state["extractions"]), 3)

    def test_given_pom_repositories_when_fetched_then_feedback_is_queued_for_next_discovery(self):
        fetch(self.store, FixtureClient(self.routes()), max_versions=1)
        self.assertEqual(Store(self.root).state["pom_repositories"], ["https://pom.example.org/maven"])
        self.assertEqual(Store(self.root).state["coordinates"]["*"], ["org.dep:compile", "org.dep:managed", "org.dep:runtime"])

    def test_given_jar_packaging_when_fetched_then_no_aar_or_evidence_is_created(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        client = FixtureClient({REPOSITORY + "/org/example/sdk/1/sdk-1.pom": response("<project><packaging>jar</packaging></project>")})
        self.assertTrue(fetch(self.store, client))
        self.assertNotIn("extractions", self.store.state)
        self.assertEqual(len(client.calls), 1)

    def test_given_gradle_metadata_reference_when_aar_variant_exists_then_module_dependencies_and_file_are_used(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        base = REPOSITORY + "/org/example/sdk/1/sdk-1"
        module = {"variants": [{"attributes": {"org.gradle.usage": "java-runtime"},
                                "files": [{"name": "custom.aar", "url": "custom.aar"}],
                                "dependencies": [{"group": "org.dep", "module": "lib", "version": {"requires": "5"}}]}]}
        client = FixtureClient({base + ".pom": response("<!-- do_not_remove: published-with-gradle-metadata --><project/>"),
                                base + ".module": response(module),
                                REPOSITORY + "/org/example/sdk/1/custom.aar": response(aar())})
        self.assertTrue(fetch(self.store, client))
        self.assertEqual(self.store.state["extractions"][0]["dependencies"], ["org.dep:lib:5"])
        self.assertIn("org.dep:lib", self.store.state["coordinates"]["*"])

    def test_given_missing_pom_when_module_declares_aar_then_the_module_variant_can_still_be_indexed(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        base = REPOSITORY + "/org/example/sdk/1/sdk-1"
        client = FixtureClient({base + ".pom": response(status=404),
                                base + ".module": response({"variants": [{"files": [{"name": "sdk-1.aar"}]}]}),
                                base + ".aar": response(aar())})
        self.assertTrue(fetch(self.store, client))
        self.assertEqual(self.store.state["extractions"][0]["dependencies"], [])

    def test_given_manifest_cache_evicted_when_extract_resumes_then_fetch_is_reenabled_and_recovers_without_losing_evidence(self):
        client = FixtureClient(self.routes())
        fetch(self.store, client, max_versions=1)
        self.store.state["completed"] = ["discover", "probe", "enumerate", "fetch"]
        job = self.store.state["extractions"][0]
        (self.root / job["manifest"]).unlink()
        self.store.save()
        saved = Store(self.root)
        self.assertFalse(extract(saved, FixtureClient()))
        self.assertNotIn("fetch", saved.state["completed"])
        self.assertTrue(fetch(saved, FixtureClient(self.routes()), max_versions=1))
        self.assertTrue(extract(saved, FixtureClient()))
        self.assertEqual(list(saved.evidence(COORDINATE)["versions"]), ["3"])

    def test_given_new_weekly_version_when_fetched_then_only_new_version_is_requested(self):
        client = FixtureClient(self.routes())
        fetch(self.store, client)
        extract(self.store, client)
        self.store.state["artifacts"][COORDINATE]["versions"].append("4")
        base = REPOSITORY + "/org/example/sdk/4/sdk-4"
        client = FixtureClient({base + ".pom": response((FIXTURES / "artifact.pom").read_bytes()), base + ".aar": response(aar())})
        self.assertTrue(fetch(self.store, client))
        self.assertEqual(len(client.calls), 2)
        extract(self.store, client)
        self.assertEqual(list(self.store.evidence(COORDINATE)["versions"]), ["1", "2", "3", "4"])
