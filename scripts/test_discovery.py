from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from indexlib.discovery import add_repository, bootstrap, discover, split_shard
from indexlib.http import DeadlineReached
from indexlib.parsing import BUILTINS
from indexlib.storage import Store
from test_support import FixtureClient, FixtureTest, response


class DiscoveryTests(FixtureTest):
    def test_given_overfull_size_shard_when_bisected_then_children_are_disjoint_complete_and_restart_at_page_one(self):
        shard = {"file": "build.gradle", "low": 100, "high": 200, "page": 3, "host": ""}
        children = split_shard(shard, 1001)
        self.assertEqual([(item["low"], item["high"], item["page"]) for item in children], [(100, 150, 1), (151, 200, 1)])
        self.assertEqual(split_shard(shard, 1000), [])
        self.assertEqual(split_shard({**shard, "low": 200}, 1001), [])

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
        self.store.state["discover"] = {"queue": [{"file": "build.gradle", "low": 1, "high": 10, "page": 1, "host": ""}]}
        client = FixtureClient(handler=lambda *args, **kwargs: response({"total_count": 1500, "items": []}), token="fixture")
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks > 2:
                raise DeadlineReached()
        client.check = check
        with self.assertRaises(DeadlineReached):
            discover(self.store, client)
        shards = Store(self.root).state["discover"]["queue"]
        self.assertEqual([(shard["low"], shard["high"]) for shard in shards], [(1, 5), (6, 10)])
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
        self.assertIn('"sdk.example.org" filename:settings.gradle', queries)

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
        self.assertIn("https://pom.example.org/repo", Store(self.root).state["probe"]["queue"])

    def test_given_unsplittable_overfull_shard_when_searched_then_it_is_retained_instead_of_silently_losing_hits(self):
        shard = {"file": "build.gradle", "low": 10, "high": 10, "page": 1, "host": ""}
        self.store.state["discover"] = {"queue": [shard]}
        client = FixtureClient(handler=lambda *args, **kwargs: response({"total_count": 1001, "items": []}), token="fixture")
        with self.assertRaisesRegex(ValueError, "Unsplittable"):
            discover(self.store, client)
        self.assertEqual(Store(self.root).state["discover"]["queue"], [shard])
