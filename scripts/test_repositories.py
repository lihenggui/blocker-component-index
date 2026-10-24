from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from indexlib.discovery import add_repository
from indexlib.http import DeadlineReached
from indexlib.parsing import BUILTINS
from indexlib.repositories import _central_url, capabilities, endpoints, enumerate_artifacts, metadata_url, probe
from indexlib.storage import Store
from test_support import FixtureClient, FixtureTest, metadata_fixture, response


class RepositoryTests(FixtureTest):
    def repository(self, url="https://sdk.example.org/repo", caps=None):
        item = add_repository(self.store, url, "seeds")
        item["capabilities"] = caps or ["metadata"]
        return url

    def test_given_junit_mirror_when_probed_then_it_is_marked_mirror_and_no_other_endpoint_is_requested(self):
        url = "https://mirror.example.org/maven"
        client = FixtureClient({url + "/junit/junit/maven-metadata.xml": response(metadata_fixture("junit:junit", ["4.13.2"]))})
        self.assertEqual(capabilities(url, client), ["mirror"])
        self.assertEqual(len(client.calls), 1)

    def test_given_central_repository_when_probed_then_it_is_never_classified_as_a_mirror(self):
        url = BUILTINS[1]
        client = FixtureClient({url + "/master-index.xml": response(status=404)})
        self.assertEqual(capabilities(url, client), ["central-search", "metadata"])
        self.assertEqual(len(client.calls), 1)

    def test_given_each_enumerable_capability_when_probed_then_best_supported_capability_and_metadata_are_recorded(self):
        url = "https://sdk.example.org/repo"
        urls = endpoints(url)
        for cap in ["google-index", "nexus3", "nexus2", "artifactory", "listing", "metadata"]:
            def handler(target, **kwargs):
                fixtures = {url + "/master-index.xml": ("google-index", response("<metadata><org.example/></metadata>")),
                            urls["nexus3"]: ("nexus3", response({"items": []})),
                            urls["nexus2"]: ("nexus2", response("<searchNGResponse><data/></searchNGResponse>")),
                            urls["artifactory"][1]: ("artifactory", response({"files": []})),
                            url + "/": ("listing", response('<a href="org/">org</a>'))}
                return fixtures[target][1] if target in fixtures and fixtures[target][0] == cap else response(status=404)
            self.assertEqual(capabilities(url, FixtureClient(handler=handler)), [cap, "metadata"] if cap != "metadata" else ["metadata"])

    def test_given_probe_failure_when_resumed_then_successful_repositories_are_not_reprobed(self):
        first = self.repository()
        second = self.repository("https://second.example.org/repo")
        with patch("indexlib.repositories.capabilities", side_effect=[["metadata"], OSError("fixture offline")]):
            self.assertFalse(probe(self.store, FixtureClient()))
        saved = Store(self.root)
        self.assertEqual(saved.state["probe"]["queue"], [second])
        self.assertIsNotNone(next(item for item in saved.repositories if item["url"] == first)["lastChecked"])
        with patch("indexlib.repositories.capabilities", return_value=["metadata"]) as call:
            self.assertTrue(probe(saved, FixtureClient()))
        self.assertEqual(call.call_count, 1)

    def test_given_metadata_seeds_resolved_elsewhere_and_mirror_when_enumerated_then_only_unresolved_seeds_are_tried(self):
        url = self.repository()
        self.repository("https://mirror.example.org/repo", ["mirror"])
        self.store.seed("org.example:sdk")
        self.store.seed("org.other:resolved")
        self.store.artifact("https://other.example.org", "org.other:resolved", ["1"])
        client = FixtureClient({metadata_url(url, "org.example:sdk"): response(metadata_fixture())})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(Store(self.root).state["artifacts"]["org.example:sdk"]["versions"], ["1", "2", "3"])

    def test_given_google_index_when_enumerated_then_groups_artifacts_and_metadata_are_followed(self):
        url = self.repository(caps=["google-index", "metadata"])
        client = FixtureClient({url + "/master-index.xml": response("<metadata><org.example/></metadata>"),
                                url + "/org/example/group-index.xml": response('<org.example><sdk versions="1,2,3"/></org.example>')})
        self.assertTrue(enumerate_artifacts(self.store, client, max_versions=2))
        self.assertEqual(Store(self.root).state["artifacts"]["org.example:sdk"]["versions"], ["2", "3"])
        self.assertEqual(len(client.calls), 2)

    def test_given_nexus_continuation_when_enumerated_then_next_page_and_metadata_are_checkpointed(self):
        url = self.repository(caps=["nexus3", "metadata"])
        first = endpoints(url)["nexus3"]
        client = FixtureClient({first: response({"items": [{"group": "org.example", "name": "sdk"}], "continuationToken": "page2"}),
                                first + "&continuationToken=page2": response({"items": [], "continuationToken": None}),
                                metadata_url(url, "org.example:sdk"): response(metadata_fixture())})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(client.calls), 3)

    def test_given_nexus2_search_when_enumerated_then_group_and_artifact_are_resolved(self):
        url = self.repository(caps=["nexus2", "metadata"])
        client = FixtureClient({endpoints(url)["nexus2"]: response("<searchNGResponse><totalCount>1</totalCount><data><artifact><groupId>org.example</groupId><artifactId>sdk</artifactId></artifact></data></searchNGResponse>"),
                                metadata_url(url, "org.example:sdk"): response(metadata_fixture())})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertIn("org.example:sdk", self.store.state["artifacts"])

    def test_given_artifactory_second_endpoint_when_enumerated_then_fallback_and_aar_paths_are_used(self):
        url = self.repository(caps=["artifactory", "metadata"])
        paths = endpoints(url)["artifactory"]
        client = FixtureClient({paths[0]: response(status=404), paths[1]: response({"files": [{"uri": "/org/example/sdk/1/sdk-1.aar"}]}),
                                metadata_url(url, "org.example:sdk"): response(metadata_fixture())})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertIn("org.example:sdk", self.store.state["artifacts"])

    def test_given_central_search_hit_with_latest_version_when_enumerated_then_no_metadata_request_is_made(self):
        url = self.repository(BUILTINS[1], ["central-search", "metadata"])
        client = FixtureClient({_central_url(): response({"response": {"numFound": 1, "docs": [{"g": "org.example", "a": "sdk", "latestVersion": "3"}]}})})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(self.store.state["artifacts"]["org.example:sdk"],
                         {"repository": url, "coordinate": "org.example:sdk", "versions": ["3"], "latestOnly": True})
        self.assertEqual(len(client.calls), 1)

    def test_given_listing_beyond_page_limit_when_enumerated_then_crawl_stops_at_the_limit(self):
        url = self.repository(caps=["listing", "metadata"])

        def handler(target, **kwargs):
            # Every directory links two deeper directories, an unbounded tree.
            return response('<a href="a/">a</a><a href="b/">b</a>')
        client = FixtureClient(handler=handler)
        with patch("indexlib.repositories.LISTING_LIMIT", 5):
            self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(client.calls), 5)

    def test_given_central_search_paging_when_enumerated_then_all_pages_are_resolved(self):
        url = self.repository(BUILTINS[1], ["central-search", "metadata"])
        client = FixtureClient({_central_url(): response({"response": {"numFound": 2, "docs": [{"g": "org.example", "a": "sdk"}]}}),
                                _central_url(start=1): response({"response": {"numFound": 2, "docs": [{"g": "org.other", "a": "sdk"}]}}),
                                metadata_url(url, "org.example:sdk"): response(metadata_fixture()),
                                metadata_url(url, "org.other:sdk"): response(metadata_fixture("org.other:sdk"))})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(self.store.state["artifacts"]), 2)

    def test_given_central_search_window_cap_when_enumerated_then_group_prefix_shards_are_used(self):
        self.repository(BUILTINS[1], ["central-search", "metadata"])
        client = FixtureClient(handler=lambda url, **kwargs: response({"response": {"numFound": 10001 if url == _central_url() else 0, "docs": []}}))
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertTrue(any("g%3Aa%2A" in url for url, _ in client.calls))

    def test_given_html_listing_when_enumerated_then_only_child_directories_and_metadata_are_followed(self):
        url = self.repository(caps=["listing", "metadata"])
        client = FixtureClient({url + "/": response('<a href="../">parent</a><a href="org/">org</a><a href="https://other.example/x/">external</a>'),
                                url + "/org/": response('<a href="maven-metadata.xml">metadata</a>'),
                                url + "/org/maven-metadata.xml": response(metadata_fixture())})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(client.calls), 3)

    def test_given_failed_enumeration_task_and_deadline_when_resumed_then_failed_and_unattempted_tasks_both_survive(self):
        url = self.repository()
        self.store.seed("org.example:a")
        self.store.seed("org.example:b")
        calls = 0

        def handler(target, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("fixture failure")
            raise DeadlineReached()
        # Without an overall deadline, a deadline inside the stage is its own budget share running out.
        self.assertFalse(enumerate_artifacts(self.store, FixtureClient(handler=handler)))
        saved = Store(self.root)
        self.assertEqual(len(saved.state["enumerate"]["queue"]), 1)
        self.assertEqual(len(saved.state["enumerate"]["deferred"]), 1)
        client = FixtureClient({metadata_url(url, f"org.example:{name}"): response(metadata_fixture(f"org.example:{name}")) for name in ["a", "b"]})
        self.assertTrue(enumerate_artifacts(saved, client))
        self.assertEqual(len(saved.state["artifacts"]), 2)

    def test_given_new_repository_during_enumeration_resume_when_run_then_new_and_pending_repositories_are_processed(self):
        first = self.repository()
        self.store.seed("org.example:sdk")
        self.assertFalse(enumerate_artifacts(self.store, FixtureClient({metadata_url(first, "org.example:sdk"): DeadlineReached()})))
        saved = Store(self.root)
        second = "https://new.example.org/repo"
        add_repository(saved, second, "pom")["capabilities"] = ["metadata"]
        saved.seed("org.other:sdk", second)
        client = FixtureClient(handler=lambda url, **kwargs: response(metadata_fixture("org.other:sdk"))
                               if "/org/other/" in url else response(metadata_fixture()))
        self.assertTrue(enumerate_artifacts(saved, client))
        self.assertIn("org.other:sdk", saved.state["artifacts"])
        self.assertEqual(saved.state["artifacts"]["org.other:sdk"]["repository"], second)

    def test_given_seed_metadata_for_a_different_coordinate_when_enumerated_then_it_is_rejected_and_left_for_retry(self):
        url = self.repository()
        self.store.seed("org.example:sdk")
        client = FixtureClient({metadata_url(url, "org.example:sdk"): response(metadata_fixture("org.other:wrong"))})
        self.assertFalse(enumerate_artifacts(self.store, client))
        self.assertNotIn("org.example:sdk", self.store.state.get("artifacts", {}))
        self.assertEqual(len(self.store.state["enumerate"]["queue"]), 1)

    def test_given_group_prefix_contains_solr_operators_when_sharded_then_operators_are_escaped_as_literal_characters(self):
        url = _central_url("org.example-sdk+")
        self.assertEqual(parse_qs(urlsplit(url).query)["q"], ["p:aar AND g:org.example\\-sdk\\+*"])

    def test_given_repository_scoped_dependency_seed_when_metadata_repositories_enumerated_then_other_repositories_are_not_queried(self):
        first = self.repository()
        self.repository("https://other.example.org/repo")
        self.store.seed("org.example:sdk", first)
        client = FixtureClient({metadata_url(first, "org.example:sdk"): response(metadata_fixture())})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual([url for url, _ in client.calls], [metadata_url(first, "org.example:sdk")])

    def test_given_mixed_mirror_with_attributed_seeds_when_enumerated_then_only_unresolved_attributed_coordinates_are_tried(self):
        url = self.repository(caps=["mirror"])
        self.store.seed("org.example:vendor", url)
        self.store.seed("org.other:resolved", url)
        self.store.seed("org.config:global")
        self.store.artifact("https://central.example.org", "org.other:resolved", ["1"])
        client = FixtureClient({metadata_url(url, "org.example:vendor"): response(metadata_fixture("org.example:vendor"))})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.store.state["artifacts"]["org.example:vendor"]["repository"], url)
        self.assertEqual(self.store.repositories[0]["capabilities"], ["mirror"])

    def test_given_stale_unattributed_mirror_tasks_when_resumed_then_old_global_and_enumeration_tasks_are_discarded(self):
        url = self.repository(caps=["mirror"])
        self.store.seed("org.example:vendor", url)
        self.store.seed("org.config:global")
        self.store.state["enumerate"] = {"queue": [
            {"repository": url, "kind": "metadata", "coordinate": "org.config:global", "url": metadata_url(url, "org.config:global")},
            {"repository": url, "kind": "listing", "url": url + "/"},
        ]}
        client = FixtureClient({metadata_url(url, "org.example:vendor"): response(metadata_fixture("org.example:vendor"))})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(client.calls), 1)

    def test_given_legacy_global_tasks_without_current_seeds_when_resumed_then_unattributed_metadata_requests_are_discarded(self):
        url = self.repository()
        self.store.state["enumerate"] = {"queue": [
            {"repository": url, "kind": "metadata", "coordinate": "org.old:dependency", "url": metadata_url(url, "org.old:dependency")}]}
        client = FixtureClient()
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(client.calls, [])

    def test_given_repository_failing_three_probes_when_probed_then_it_is_dropped_for_this_cycle(self):
        url = self.repository()
        with patch("indexlib.repositories.capabilities", side_effect=OSError("fixture offline")) as call:
            for _ in range(3):
                probe(self.store, FixtureClient())
        self.assertEqual(call.call_count, 3)
        self.assertFalse(Store(self.root).state.get("probe"))
        self.assertEqual(next(item for item in Store(self.root).repositories if item["url"] == url)["capabilities"], ["metadata"])

    def test_given_metadata_answered_with_401_when_enumerated_then_task_is_treated_as_missing(self):
        url = self.repository()
        self.store.seed("org.example:sdk", url)
        client = FixtureClient({metadata_url(url, "org.example:sdk"): response(b"", status=401)})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(Store(self.root).state["enumerate"]["queue"], [])
        self.assertNotIn("org.example:sdk", Store(self.root).state.get("artifacts", {}))

    def test_given_finished_enumeration_in_current_cycle_when_rerun_then_only_new_repositories_are_requested(self):
        first = self.repository()
        self.store.seed("org.example:sdk", first)
        self.assertTrue(enumerate_artifacts(self.store, FixtureClient({metadata_url(first, "org.example:sdk"): response(metadata_fixture())})))
        second = self.repository("https://second.example.org/repo")
        self.store.seed("org.other:sdk", second)
        client = FixtureClient({metadata_url(second, "org.other:sdk"): response(metadata_fixture("org.other:sdk"))})
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual([url for url, _ in client.calls], [metadata_url(second, "org.other:sdk")])

    def test_given_cycle_older_than_a_week_when_enumerated_then_known_urls_are_requested_again(self):
        url = self.repository()
        self.store.seed("org.example:sdk", url)
        routes = {metadata_url(url, "org.example:sdk"): response(metadata_fixture())}
        self.assertTrue(enumerate_artifacts(self.store, FixtureClient(dict(routes))))
        self.store.state["enumerate"]["cycleStartedAt"] = "2026-01-01T00:00:00Z"
        client = FixtureClient(dict(routes))
        self.assertTrue(enumerate_artifacts(self.store, client))
        self.assertEqual(len(client.calls), 1)
