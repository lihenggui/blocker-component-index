from unittest.mock import patch
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from indexlib.discovery import add_repository, bootstrap, discover, split_shard
from indexlib.evidence import merge
from indexlib.http import DeadlineReached
from indexlib.parsing import BUILTINS
from indexlib.storage import Store, write_json
from test_http import Clock
from test_support import FixtureClient, FixtureTest, response


class DiscoveryTests(FixtureTest):
    def test_given_overfull_size_shard_when_bisected_then_children_are_disjoint_complete_and_restart_at_page_one(self):
        shard = {"file": "build.gradle", "low": 100, "high": 1124, "page": 3, "host": ""}
        children = split_shard(shard, 1001)
        self.assertEqual([(item["low"], item["high"], item["page"]) for item in children], [(100, 612, 1), (613, 1124, 1)])
        self.assertEqual(split_shard(shard, 1000), [])
        self.assertEqual(split_shard({**shard, "low": 1124}, 1001), [])

    def test_given_missing_tokens_when_discovery_runs_then_builtins_seeds_and_pom_feedback_persist_with_warning(self):
        (self.root / "config/seeds.yaml").write_text('repositories: ["http://SDK.Example.org/maven/"]\ncoordinates: ["org.example:sdk"]\n')
        self.store.state["pom_repositories"] = ["https://pom.example.org/repo"]
        client = FixtureClient()
        with self.assertLogs("indexlib.discovery", level="WARNING"):
            self.assertTrue(discover(self.store, client))
        self.assertEqual(len(client.calls), 0)
        saved = Store(self.root)
        self.assertEqual(len(saved.repositories), 4)
        self.assertEqual(saved.state["coordinates"]["*"], ["org.example:sdk"])
        self.assertEqual(next(item for item in saved.repositories if item["url"] == "https://pom.example.org/repo")["discoveredBy"], ["pom"])
        for item in saved.repositories:
            self.assertEqual(set(item), {"url", "capabilities", "discoveredBy", "occurrences", "lastChecked"})

    def test_given_discovery_sightings_when_merged_then_provenance_deduplicates_and_occurrences_accumulate(self):
        add_repository(self.store, "https://SDK.Example.org/repo/", "github")
        add_repository(self.store, "https://sdk.example.org/repo", "pom")
        add_repository(self.store, "https://sdk.example.org/repo", "github")
        self.assertEqual(len(self.store.repositories), 1)
        self.assertEqual(self.store.repositories[0]["discoveredBy"], ["github", "pom"])
        self.assertEqual(self.store.repositories[0]["occurrences"], 3)

    def test_given_overfull_search_result_when_discovery_runs_then_bisected_queue_is_saved_before_next_request(self):
        self.store.state["discover"] = {"queue": [{"file": "build.gradle", "low": 0, "high": 1023, "page": 1, "host": ""}]}
        client = FixtureClient(handler=lambda *args, **kwargs: response({"total_count": 1500, "items": []}), token="fixture")
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks > 2:
                raise DeadlineReached()
        client.check = check
        self.assertFalse(discover(self.store, client))
        shards = Store(self.root).state["discover"]["queue"]
        self.assertEqual([(shard["low"], shard["high"]) for shard in shards], [(0, 511), (512, 1023)])
        self.assertEqual(client.calls[0][1]["headers"]["Accept"], "application/vnd.github.text-match+json")
        self.assertTrue(client.calls[0][1]["search"])

    def test_given_github_hit_when_discovered_then_host_queries_and_tree_coordinate_seeds_are_persisted(self):
        self.store.state["discover"] = {"queue": [{"file": "build.gradle.kts", "low": 0, "high": 10, "page": 1, "host": ""}]}
        hit = {"html_url": "https://github.com/o/r/blob/main/build.gradle.kts", "repository": {"full_name": "o/r"},
               "text_matches": [{"fragment": 'maven(url = "https://sdk.example.org/repo/")'}]}

        def handler(url, **kwargs):
            if "/search/code?" in url:
                query = parse_qs(urlsplit(url).query)["q"][0]
                return response({"total_count": 1, "items": [hit]} if query.startswith("maven url") else {"total_count": 0, "items": []})
            if url == "https://api.github.com/repos/o/r":
                return response({"default_branch": "main"})
            if "/git/trees/main?recursive=1" in url:
                return response({"tree": [{"path": "build.gradle.kts", "type": "blob"}, {"path": "gradle/libs.versions.toml", "type": "blob"}]})
            if url.endswith("build.gradle.kts"):
                return response('implementation("org.example:sdk:1.0")')
            if url.endswith("libs.versions.toml"):
                return response('[libraries]\nother = {module="org.other:lib", version="1"}')
            raise AssertionError(url)
        client = FixtureClient(handler=handler, token="fixture")
        self.assertTrue(discover(self.store, client))
        saved = Store(self.root)
        self.assertEqual(saved.state["coordinates"]["https://sdk.example.org/repo"], ["org.example:sdk", "org.other:lib"])
        self.assertEqual(saved.state["discover"]["sources"]["https://sdk.example.org/repo"], ["o/r"])
        queries = [parse_qs(urlsplit(url).query)["q"][0] for url, _ in client.calls if "/search/code?" in url]
        self.assertIn('"sdk.example.org" google filename:settings.gradle', queries)

    def test_given_more_than_thirty_source_repositories_when_discovered_then_source_cap_is_persistent(self):
        self.store.state["discover"] = {"queue": [{"file": "build.gradle", "low": 0, "high": 10, "page": 1, "host": ""}]}
        items = [{"html_url": f"https://github.com/o/r{i}/blob/main/build.gradle", "repository": {"full_name": f"o/r{i}"},
                  "text_matches": [{"fragment": 'maven { url "https://sdk.example.org/repo" }'}]} for i in range(31)]
        client = FixtureClient(handler=lambda url, **kwargs: response({"total_count": 31, "items": items}) if "maven+url" in url else response({"total_count": 0, "items": []}), token="fixture")
        with patch("indexlib.discovery._tree_seeds"):
            discover(self.store, client)
        self.assertEqual(len(Store(self.root).state["discover"]["sources"]["https://sdk.example.org/repo"]), 30)

    def test_given_new_pom_repository_during_resume_when_bootstrapped_then_it_enters_the_pending_probe_queue(self):
        bootstrap(self.store)
        self.store.state["probe"] = {"queue": [BUILTINS[0]]}
        self.store.state["pom_repositories"] = ["https://pom.example.org/repo"]
        bootstrap(self.store)
        self.store.save(force=True)
        self.assertIn("https://pom.example.org/repo", Store(self.root).state["probe"]["queue"])

    def test_given_unsplittable_overfull_shard_when_searched_then_first_ten_pages_are_taken_and_discovery_moves_on(self):
        shard = {"file": "build.gradle", "low": 10, "high": 10, "page": 1, "host": ""}
        self.store.state["discover"] = {"queue": [shard]}
        pages = []

        def handler(url, **kwargs):
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            pages.append(page)
            return response({"total_count": 1001, "items": [], "incomplete_results": True})
        client = FixtureClient(handler=handler, token="fixture")
        with self.assertLogs("indexlib.discovery", level="WARNING") as logs:
            self.assertTrue(discover(self.store, client))
        self.assertEqual(pages, list(range(1, 11)))
        self.assertIn("taking first 1000", " ".join(logs.output))
        self.assertNotIn("queue", Store(self.root).state["discover"])
        self.assertIn("broadCompletedAt", Store(self.root).state["discover"])

    def test_given_broad_and_host_shards_when_at_their_size_floors_then_only_host_shards_continue_below_256_bytes(self):
        broad = {"file": "build.gradle", "low": 0, "high": 256, "page": 1, "host": ""}
        self.assertEqual(split_shard(broad, 1001), [])
        self.assertTrue(split_shard({**broad, "host": "sdk.example.org"}, 1001))
        self.assertEqual(split_shard({**broad, "host": "sdk.example.org", "high": 0}, 1001), [])

    def test_given_uneven_broad_size_bucket_when_recursively_bisected_then_no_child_range_is_narrower_than_256_bytes(self):
        pending = [{"file": "build.gradle", "low": 1024, "high": 4095, "page": 1, "host": ""}]
        leaves = []
        while pending:
            shard = pending.pop()
            children = split_shard(shard, 1001)
            if children:
                pending.extend(children)
            else:
                leaves.append(shard)
        self.assertTrue(all(shard["high"] - shard["low"] + 1 >= 256 for shard in leaves))
        self.assertEqual(sum(shard["high"] - shard["low"] + 1 for shard in leaves), 3072)

    def test_given_old_oversized_broad_queue_when_resumed_then_only_sizes_up_to_65535_are_searched(self):
        self.store.state["discover"] = {"queue": [
            {"file": "build.gradle", "low": 65536, "high": 393215, "page": 1, "host": ""},
            {"file": "build.gradle.kts", "low": 16384, "high": 393215, "page": 1, "host": ""},
        ]}
        client = FixtureClient(handler=lambda *args, **kwargs: response({"total_count": 0, "items": []}), token="fixture")
        self.assertTrue(discover(self.store, client))
        queries = [parse_qs(urlsplit(url).query)["q"][0] for url, _ in client.calls]
        self.assertEqual(queries, ['maven url "com.android" filename:build.gradle.kts size:16384..65535'])

    def test_given_host_shard_at_single_size_when_overfull_then_ten_pages_are_taken_without_blocking_the_queue(self):
        self.store.state["discover"] = {"broadCompletedAt": "2026-10-22T00:00:00Z", "queue": [
            {"file": "build.gradle", "low": 5, "high": 5, "page": 1, "host": "sdk.example.org"}]}
        client = FixtureClient(handler=lambda *args, **kwargs: response({"total_count": 5000, "items": []}), token="fixture")
        epoch = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()
        with patch("indexlib.discovery.time.time", return_value=epoch), self.assertLogs("indexlib.discovery", level="WARNING"):
            self.assertTrue(discover(self.store, client))
        self.assertEqual(len(client.calls), 10)
        self.assertTrue(all('"sdk.example.org" "com.android" filename:build.gradle size:5..5' == parse_qs(urlsplit(url).query)["q"][0] for url, _ in client.calls))

    def test_given_completed_broad_sweep_when_called_before_and_after_28_days_then_only_due_sweeps_restart(self):
        epoch = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()
        client = FixtureClient(handler=lambda *args, **kwargs: response({"total_count": 0, "items": []}), token="fixture")
        with patch("indexlib.discovery.time.time", return_value=epoch), patch("indexlib.discovery.timestamp", return_value="2026-10-22T00:00:00Z"):
            self.assertTrue(discover(self.store, client))
        self.assertEqual(len(client.calls), 16)
        saved = Store(self.root)
        self.assertEqual(saved.state["discover"]["broadCompletedAt"], "2026-10-22T00:00:00Z")
        client.calls.clear()
        with patch("indexlib.discovery.time.time", return_value=epoch + 28 * 86400 - 1):
            self.assertTrue(discover(saved, client))
        self.assertEqual(client.calls, [])
        with patch("indexlib.discovery.time.time", return_value=epoch + 28 * 86400):
            self.assertTrue(discover(saved, client))
        self.assertEqual(len(client.calls), 16)

    def test_given_broad_cooldown_when_new_host_and_completed_tree_exist_then_host_search_runs_without_tree_refresh(self):
        epoch = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()
        repository = "https://sdk.example.org/repo"
        add_repository(self.store, repository, "pom")
        self.store.state["discover"] = {"broadCompletedAt": "2026-10-22T00:00:00Z", "trees": {
            "o/r": {"repositories": [repository], "done": True, "paths": [], "cursor": 0}}}
        client = FixtureClient(handler=self._tree_handler, token="fixture")
        with patch("indexlib.discovery.time.time", return_value=epoch):
            self.assertTrue(discover(self.store, client))
            searches = [url for url, _ in client.calls if "/search/code?" in url]
            self.assertEqual(len(searches), 4)
            self.assertTrue(all(parse_qs(urlsplit(url).query)["q"][0].startswith('"sdk.example.org"') for url in searches))
        self.assertFalse(any("raw.githubusercontent.com" in url for url, _ in client.calls))
        self.assertTrue(self.store.state["discover"]["trees"]["o/r"]["done"])

    def test_given_due_broad_cycle_when_it_starts_then_completed_trees_are_harvested_again(self):
        repository = "https://sdk.example.org/repo"
        add_repository(self.store, repository, "pom")
        self.store.state["discover"] = {"broadCompletedAt": "2026-09-01T00:00:00Z", "hosts": ["sdk.example.org"], "trees": {
            "o/r": {"repositories": [repository], "done": True, "paths": [], "cursor": 0}}}
        client = FixtureClient(handler=self._tree_handler, token="fixture")
        epoch = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()
        with patch("indexlib.discovery.time.time", return_value=epoch):
            self.assertTrue(discover(self.store, client))
        self.assertEqual(len([url for url, _ in client.calls if "raw.githubusercontent.com" in url]), 1)
        self.assertEqual(self.store.state["coordinates"][repository], ["org.example:sdk"])

    def test_given_broad_cycle_in_progress_when_discovery_resumes_then_completed_trees_are_not_harvested_again(self):
        repository = "https://sdk.example.org/repo"
        add_repository(self.store, repository, "pom")
        self.store.state["discover"] = {"hosts": ["sdk.example.org"], "queue": [
            {"file": "build.gradle", "low": 5, "high": 5, "page": 1, "host": ""}], "trees": {
            "o/r": {"repositories": [repository], "done": True, "paths": [], "cursor": 0}}}
        client = FixtureClient(handler=self._tree_handler, token="fixture")
        self.assertTrue(discover(self.store, client))
        self.assertFalse(any("raw.githubusercontent.com" in url for url, _ in client.calls))
        self.assertTrue(self.store.state["discover"]["trees"]["o/r"]["done"])

    @staticmethod
    def _tree_handler(url, **kwargs):
        if "/search/code?" in url:
            return response({"total_count": 0, "items": []})
        if url == "https://api.github.com/repos/o/r":
            return response({"default_branch": "main"})
        if "/git/trees/" in url:
            return response({"tree": [{"type": "blob", "path": "build.gradle"}]})
        return response('implementation "org.example:sdk:1"')

    def test_given_discovery_budget_when_search_remains_then_it_returns_unfinished_restores_deadline_and_saves_progress(self):
        clock = Clock()

        def handler(url, **kwargs):
            clock.now += 30
            return response({"total_count": 0, "items": []})
        client = FixtureClient(handler=handler, token="fixture", clock=clock, deadline=180)
        self.assertFalse(discover(self.store, client, max_minutes=6))
        self.assertEqual(clock.now, 60)
        self.assertEqual(client.deadline, 180)
        progress = Store(self.root).state["discover"]
        self.assertEqual(len(progress["queue"]), 14)
        self.assertNotIn("broadCompletedAt", progress)
        first_pending = dict(progress["queue"][0])
        client.calls.clear()
        self.assertFalse(discover(Store(self.root), client, max_minutes=6))
        self.assertIn(f'size:{first_pending["low"]}..{first_pending["high"]}', parse_qs(urlsplit(client.calls[0][0]).query)["q"][0])

    def test_given_large_github_tree_when_harvested_then_at_most_fifty_files_prefer_settings_root_catalog_and_shallow_paths(self):
        repository = "https://sdk.example.org/repo"
        paths = [f"deep/child/module{i}/build.gradle" for i in range(70)] + [f"module{i}/build.gradle.kts" for i in range(60)]
        paths += ["settings.gradle.kts", "build.gradle", "gradle/libs.versions.toml"]
        self.store.state["discover"] = {"trees": {"o/r": {"repositories": [repository]}}}

        def handler(url, **kwargs):
            if "/search/code?" in url:
                return response({"total_count": 0, "items": []})
            if url == "https://api.github.com/repos/o/r":
                return response({"default_branch": "main"})
            if "/git/trees/" in url:
                return response({"tree": [{"type": "blob", "path": path} for path in paths]})
            return response('implementation "org.example:sdk:1"')
        client = FixtureClient(handler=handler, token="fixture")
        self.assertTrue(discover(self.store, client))
        fetched = [url.split("/main/", 1)[1] for url, _ in client.calls if "raw.githubusercontent.com" in url]
        self.assertEqual(len(fetched), 50)
        self.assertEqual(fetched[:3], ["settings.gradle.kts", "build.gradle", "gradle/libs.versions.toml"])
        self.assertTrue(all(not path.startswith("deep/") for path in fetched))

    def test_given_legacy_global_dependency_seeds_when_bootstrapped_then_only_config_seeds_stay_global_and_dependencies_are_reattributed(self):
        (self.root / "config/seeds.yaml").write_text('repositories: []\ncoordinates: ["org.config:explicit"]\n')
        self.store.seed("org.dep:compile")
        self.store.seed("org.config:explicit")
        coordinate = "org.example:sdk"
        evidence = merge(None, coordinate, "https://one.example.org", "1", "2026-10-22T00:00:00Z", "a" * 64, ["org.dep:compile:1"], [])
        write_json(self.store.evidence_path(coordinate), evidence)
        self.store.state["extractions"] = [{"repository": "https://two.example.org", "dependencies": ["org.dep:module:1"]}]
        bootstrap(self.store)
        self.store.save(force=True)
        seeds = Store(self.root).state["coordinates"]
        self.assertEqual(seeds["*"], ["org.config:explicit"])
        self.assertEqual(seeds["https://one.example.org"], ["org.dep:compile"])
        self.assertEqual(seeds["https://two.example.org"], ["org.dep:module"])

    def test_given_search_rate_limit_persists_when_discovering_then_discovery_is_unfinished_not_failed(self):
        self.store.state["discover"] = {"queue": [{"file": "build.gradle", "low": 0, "high": 1023, "page": 1, "host": ""}],
                                        "broadCompletedAt": "2026-10-24T00:00:00Z"}

        def limited(*args, **kwargs):
            raise OSError("HTTP 429: https://api.github.com/search/code")
        client = FixtureClient(handler=limited, token="fixture")
        self.assertFalse(discover(self.store, client))
        self.assertEqual(len(Store(self.root).state["discover"]["queue"]), 1)

    def test_given_pending_trees_and_search_rate_limit_when_discovering_with_deadline_then_trees_are_harvested_rarest_repository_first(self):
        self.store.state["discover"] = {"queue": [{"file": "build.gradle", "low": 0, "high": 10, "page": 1, "host": ""}],
                                        "broadCompletedAt": "2026-10-24T00:00:00Z",
                                        "trees": {"o/common": {"repositories": ["https://common.example.org/repo"]},
                                                  "o/vendor": {"repositories": ["https://vendor.example.org/repo"]}}}
        self.store.state["coordinates"] = {"https://common.example.org/repo": ["a:b", "c:d"]}
        order = []

        def handler(url, **kwargs):
            if "/search/code?" in url:
                raise OSError("HTTP 429: https://api.github.com/search/code")
            if url.startswith("https://api.github.com/repos/") and "/git/trees/" not in url:
                order.append(url.rsplit("/", 1)[-1])
                return response({"default_branch": "main"})
            if "/git/trees/" in url:
                return response({"tree": [{"path": "build.gradle", "type": "blob"}]})
            return response('implementation "org.vendor:ads:1"')
        clock = Clock()
        client = FixtureClient(handler=handler, token="fixture", clock=clock, deadline=6000)
        self.assertFalse(discover(self.store, client, max_minutes=100))
        self.assertEqual(order, ["vendor", "common"])
        self.assertIn("org.vendor:ads", self.store.state["coordinates"]["https://vendor.example.org/repo"])

    def test_given_empty_repository_tree_when_harvested_then_tree_is_marked_done_without_failing(self):
        self.store.state["discover"] = {"queue": [], "broadCompletedAt": "2026-10-24T00:00:00Z",
                                        "trees": {"o/empty": {"repositories": ["https://vendor.example.org/repo"]}}}

        def handler(url, **kwargs):
            if "/git/trees/" in url:
                return response({"message": "Git Repository is empty."}, status=409)
            if url.startswith("https://api.github.com/repos/"):
                return response({"default_branch": "main"})
            return response({"total_count": 0, "items": []})
        self.assertTrue(discover(self.store, FixtureClient(handler=handler, token="fixture")))
        self.assertTrue(self.store.state["discover"]["trees"]["o/empty"]["done"])
