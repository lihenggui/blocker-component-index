from indexlib.fetching import extract, fetch
from indexlib.http import DeadlineReached
from indexlib.storage import Store
from test_support import FIXTURES, FixtureClient, FixtureTest, aar, metadata_fixture, response

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

    def test_given_latest_has_components_when_fetched_then_versions_are_newest_first_and_evidence_preserves_a_gap(self):
        client = FixtureClient(self.routes())
        self.assertTrue(fetch(self.store, client))
        self.assertEqual([url.rsplit("/", 1)[-1] for url, _ in client.calls if url.endswith(".pom")], ["sdk-3.pom", "sdk-2.pom", "sdk-1.pom"])
        self.assertNotIn("extractions", self.store.state)
        evidence = self.store.evidence(COORDINATE)
        self.assertEqual(list(evidence["versions"]), ["1", "2", "3"])
        self.assertEqual(evidence["components"][0]["versionRanges"], [["1", "1"], ["3", "3"]])
        self.assertFalse(list(self.root.rglob("*.aar")))
        self.assertFalse((self.root / ".cache").exists())

    def test_given_latest_has_no_components_when_fetched_then_older_versions_are_not_downloaded(self):
        client = FixtureClient(self.routes(empty_latest=True))
        self.assertTrue(fetch(self.store, client))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.store.evidence(COORDINATE)["components"], [])
        self.assertEqual(list(self.store.evidence(COORDINATE)["versions"]), ["3"])

    def test_given_latest_only_artifact_with_components_when_fetched_then_history_comes_from_metadata(self):
        self.store.artifact(REPOSITORY, COORDINATE, ["3"], latest_only=True)
        routes = self.routes()
        routes[f"{REPOSITORY}/org/example/sdk/maven-metadata.xml"] = response(metadata_fixture())
        self.assertTrue(fetch(self.store, FixtureClient(routes)))
        self.assertEqual(list(self.store.evidence(COORDINATE)["versions"]), ["1", "2", "3"])
        self.assertNotIn("latestOnly", self.store.state["artifacts"][COORDINATE])

    def test_given_latest_only_artifact_without_components_when_fetched_then_metadata_is_not_requested(self):
        self.store.artifact(REPOSITORY, COORDINATE, ["3"], latest_only=True)
        client = FixtureClient(self.routes(empty_latest=True))
        self.assertTrue(fetch(self.store, client))
        self.assertEqual(len(client.calls), 2)

    def test_given_version_cap_when_fetched_then_only_the_latest_requested_versions_are_downloaded(self):
        self.assertTrue(fetch(self.store, FixtureClient(self.routes()), max_versions=2))
        self.assertEqual(list(self.store.evidence(COORDINATE)["versions"]), ["2", "3"])

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
        self.assertEqual(list(saved.evidence(COORDINATE)["versions"]), ["3"])
        client = FixtureClient(self.routes())
        fetch(saved, client)
        self.assertTrue(all("/3/" not in url for url, _ in client.calls))
        self.assertEqual(list(saved.evidence(COORDINATE)["versions"]), ["1", "2", "3"])

    def test_given_pom_repositories_when_fetched_then_feedback_is_queued_for_next_discovery(self):
        fetch(self.store, FixtureClient(self.routes()), max_versions=1)
        self.assertEqual(Store(self.root).state["pom_repositories"], ["https://pom.example.org/maven"])
        self.assertEqual(Store(self.root).state["coordinates"][REPOSITORY], ["org.dep:compile", "org.dep:managed", "org.dep:runtime"])
        self.assertNotIn("*", self.store.state["coordinates"])

    def test_given_jar_packaging_when_fetched_then_no_aar_or_evidence_is_created(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        client = FixtureClient({REPOSITORY + "/org/example/sdk/1/sdk-1.pom": response("<project><packaging>jar</packaging></project>")})
        self.assertTrue(fetch(self.store, client))
        self.assertIsNone(self.store.evidence(COORDINATE))
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
        self.assertEqual(self.store.evidence(COORDINATE)["versions"]["1"]["dependencies"], ["org.dep:lib:5"])
        self.assertIn("org.dep:lib", self.store.state["coordinates"][REPOSITORY])
        self.assertNotIn("*", self.store.state["coordinates"])

    def test_given_missing_pom_when_module_declares_aar_then_the_module_variant_can_still_be_indexed(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        base = REPOSITORY + "/org/example/sdk/1/sdk-1"
        client = FixtureClient({base + ".pom": response(status=404),
                                base + ".module": response({"variants": [{"files": [{"name": "sdk-1.aar"}]}]}),
                                base + ".aar": response(aar())})
        self.assertTrue(fetch(self.store, client))
        self.assertEqual(self.store.evidence(COORDINATE)["versions"]["1"]["dependencies"], [])

    def test_given_manifest_queued_by_an_older_crawler_when_extracted_then_it_becomes_evidence(self):
        manifest = self.root / ".cache/manifests/legacy.xml"
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes((FIXTURES / "manifest.xml").read_bytes())
        self.store.state["extractions"] = [{"coordinate": COORDINATE, "repository": REPOSITORY, "version": "3", "fetchedAt": "2026-10-23T00:00:00Z",
                                            "sha256": "0" * 64, "dependencies": [], "manifest": ".cache/manifests/legacy.xml"}]
        self.assertTrue(extract(self.store, FixtureClient()))
        self.assertEqual(list(self.store.evidence(COORDINATE)["versions"]), ["3"])
        self.assertEqual(self.store.state["extractions"], [])

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

    def test_given_permanently_failing_coordinate_when_three_attempts_fail_then_it_is_dropped_until_the_next_cycle(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        self.store.save(force=True)
        base = REPOSITORY + "/org/example/sdk/1/sdk-1"
        for attempt in range(1, 4):
            store = Store(self.root)
            client = FixtureClient({base + ".pom": response(status=503)})
            with self.assertLogs("indexlib.fetching", level="WARNING") as logs:
                finished = fetch(store, client)
            self.assertEqual(finished, attempt == 3)
            saved = Store(self.root).state["fetch"]
            self.assertEqual(saved["failures"][COORDINATE], attempt)
            self.assertEqual(len(client.calls), 1)
        self.assertIn("retry next cycle", " ".join(logs.output))
        self.assertNotIn("queue", saved)
        store = Store(self.root)
        client = FixtureClient({base + ".pom": response("<project><packaging>aar</packaging></project>"), base + ".aar": response(aar())})
        self.assertTrue(fetch(store, client))
        self.assertNotIn(COORDINATE, Store(self.root).state["fetch"]["failures"])
        self.assertEqual(len(client.calls), 2)

    def test_given_failure_then_success_when_a_later_cycle_fails_then_consecutive_failure_count_restarts_at_one(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        base = REPOSITORY + "/org/example/sdk/1/sdk-1"
        self.assertFalse(fetch(self.store, FixtureClient({base + ".pom": response(status=503)})))
        self.assertTrue(fetch(self.store, FixtureClient({base + ".pom": response("<project><packaging>aar</packaging></project>"), base + ".aar": response(aar())})))
        extract(self.store, FixtureClient())
        self.store.state["artifacts"][COORDINATE]["versions"].append("2")
        self.assertFalse(fetch(self.store, FixtureClient({REPOSITORY + "/org/example/sdk/2/sdk-2.pom": response(status=503)})))
        self.assertEqual(Store(self.root).state["fetch"]["failures"][COORDINATE], 1)

    def test_given_deadline_during_fetch_when_progress_saved_then_budget_interruption_is_not_counted_as_failure(self):
        self.store.state["artifacts"][COORDINATE]["versions"] = ["1"]
        client = FixtureClient({REPOSITORY + "/org/example/sdk/1/sdk-1.pom": DeadlineReached()})
        with self.assertRaises(DeadlineReached):
            fetch(self.store, client)
        saved = Store(self.root).state["fetch"]
        self.assertEqual(saved["queue"], [COORDINATE])
        self.assertEqual(saved["failures"], {})

    def test_given_run_deadline_when_fetching_then_time_is_reserved_for_the_report(self):
        from test_http import Clock
        clock = Clock()
        clock.now = 0.0
        client = FixtureClient(self.routes(), clock=clock, deadline=20 * 60)

        def slow(url, **kwargs):
            clock.now += 6 * 60
            client.check()
            return self.routes()[url]
        client.handler = slow
        self.assertFalse(fetch(self.store, client))
        self.assertEqual(client.deadline, 20 * 60)
        self.assertLessEqual(clock.now, 20 * 60 - 10 * 60 + 6 * 60)

    def test_given_queued_manifest_missing_from_cache_when_extracted_then_job_is_dropped_and_fetch_reloads_it(self):
        self.store.state["extractions"] = [{"coordinate": COORDINATE, "repository": REPOSITORY, "version": "3", "fetchedAt": "2026-10-23T00:00:00Z",
                                            "sha256": "0" * 64, "dependencies": [], "manifest": ".cache/manifests/gone.xml"}]
        self.assertTrue(extract(self.store, FixtureClient()))
        self.assertTrue(fetch(self.store, FixtureClient(self.routes()), max_versions=1))
        self.assertEqual(list(self.store.evidence(COORDINATE)["versions"]), ["3"])
