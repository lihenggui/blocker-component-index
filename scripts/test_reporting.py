import json
from unittest.mock import patch
from indexlib.discovery import add_repository
from indexlib.evidence import merge, parse_manifest
from indexlib.http import DeadlineReached
from indexlib.prefixes import prefixes
from indexlib.reporting import group, group_label, matching_rules, report
from indexlib.storage import read_json, write_json
from test_support import FIXTURES, FixtureClient, FixtureTest


class ReportingTests(FixtureTest):
    def test_given_mixed_case_rule_keywords_when_matching_then_substring_semantics_match_the_app(self):
        rules = json.loads((FIXTURES / "general.json").read_text())
        self.assertEqual(matching_rules("org.example.sdk.Screen", rules), ["example"])
        self.assertEqual(matching_rules("org.example.OtherProvider", rules), ["42"])
        self.assertEqual(matching_rules("org.example.Unmatched", rules), [])

    def test_given_sdk_prefix_overrides_when_grouped_then_longest_matching_prefix_wins_without_changing_evidence(self):
        self.assertEqual(group_label("org.example.sdk", {"Broad": ["org."], "Specific": ["org.example"]}), "Specific")
        self.assertEqual(group_label("org.other", {}), "org.other")
        self.assertTrue(group(self.store, FixtureClient()))
        self.assertFalse((self.root / "data/artifacts").exists())

    def test_given_components_rules_and_unseeded_repository_when_reported_then_totals_coverage_misses_and_rule_ids_are_deterministic(self):
        rules = self.rules()
        evidence = merge(None, "org.example:sdk", "https://sdk.example.org/repo", "1", "2026-10-22T00:00:00Z", "a" * 64, [],
                         parse_manifest((FIXTURES / "manifest.xml").read_bytes()))
        write_json(self.store.evidence_path("org.example:sdk"), evidence)
        item = add_repository(self.store, "https://sdk.example.org/repo", "seeds")
        item["capabilities"] = ["metadata"]
        add_repository(self.store, "https://unseeded.example.org/repo", "github")["capabilities"] = ["metadata"]
        self.store.artifact(item["url"], "org.example:sdk", ["1"])
        self.assertTrue(report(self.store, FixtureClient(), rules_dir=rules))
        path = self.root / "reports/coverage.md"
        before = path.read_bytes()
        text = before.decode()
        self.assertIn("Artifacts: 1", text)
        self.assertIn("Artifacts with components: 1", text)
        self.assertIn("Components: 5", text)
        self.assertIn("| org.example | 5 | 3 | 2 |", text)
        self.assertIn("42, example", text)
        self.assertIn("- https://unseeded.example.org/repo", text)
        self.assertNotIn("- https://sdk.example.org/repo", text)
        self.assertTrue(report(self.store, FixtureClient(), rules_dir=rules))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.evidence("org.example:sdk"), evidence)

    def test_given_missing_rules_checkout_when_reported_then_no_misleading_zero_coverage_report_is_written(self):
        with self.assertRaises(ValueError):
            report(self.store, FixtureClient(), rules_dir=self.root / "missing")
        self.assertFalse((self.root / "reports/coverage.md").exists())

    def test_given_prefix_snapshot_when_reported_then_candidates_are_capped_and_cooccurrences_follow_the_sdk_table(self):
        entries = [{"prefix": f"org.candidate.p{index:03}", "components": 201 - index,
                    "groups": ["a", "b"], "examples": [f"org.candidate.p{index:03}.Screen"],
                    "coveredBy": [], "cooccursWith": []} for index in range(201)]
        entries.extend([
            {"prefix": "org.vendor.related", "components": 5, "groups": ["vendor"], "examples": [],
             "coveredBy": [], "cooccursWith": [{"rule": 2, "ratio": 0.8, "artifacts": 4},
                                                {"rule": 10, "ratio": 0.8, "artifacts": 4}]},
            {"prefix": "org.vendor.covered", "components": 10, "groups": [], "examples": [],
             "coveredBy": [2], "cooccursWith": [{"rule": 2, "ratio": 1.0, "artifacts": 2}]},
        ])
        write_json(self.root / "data/prefixes.json", list(reversed(entries)))
        report(self.store, FixtureClient(), rules_dir=self.rules())
        text = (self.root / "reports/coverage.md").read_text()
        self.assertLess(text.index("| SDK group |"), text.index("## Candidate prefixes"))
        self.assertLess(text.index("## Candidate prefixes"), text.index("## Co-occurring uncovered prefixes"))
        self.assertLess(text.index("## Co-occurring uncovered prefixes"), text.index("## Repositories without"))
        self.assertIn("| org.candidate.p000 | 201 | 2 | org.candidate.p000.Screen |", text)
        self.assertIn("| org.candidate.p199 |", text)
        self.assertNotIn("| org.candidate.p200 |", text)
        self.assertIn("| org.vendor.related | 5 | 2 | 0.80 | 4 |", text)
        self.assertIn("| org.vendor.related | 5 | 10 | 0.80 | 4 |", text)
        self.assertNotIn("org.vendor.covered", text)

    def test_given_functional_prefixes_in_rules_checkout_when_reported_then_they_and_their_children_leave_review_sections(self):
        entry = {"groups": ["a"], "examples": [], "coveredBy": [], "cooccursWith": []}
        write_json(self.root / "data/prefixes.json", [
            dict(entry, prefix="org.useful", components=3), dict(entry, prefix="org.useful.ui", components=2),
            dict(entry, prefix="org.usefulness", components=1),
            dict(entry, prefix="org.bundled", components=1, cooccursWith=[{"rule": 2, "ratio": 1.0, "artifacts": 2}])])
        rules_dir = self.rules()
        (rules_dir / "upstream/component-index").mkdir(parents=True)
        (rules_dir / "upstream/component-index/functional.yaml").write_text(
            "- prefix: org.useful\n  sdk: Useful\n  reason: Reviewed\n- prefix: org.bundled\n  sdk: Bundled\n  reason: Reviewed\n")
        report(self.store, FixtureClient(), rules_dir=rules_dir)
        text = (self.root / "reports/coverage.md").read_text()
        self.assertNotIn("| org.useful |", text)
        self.assertNotIn("| org.useful.ui |", text)
        self.assertNotIn("| org.bundled |", text)
        self.assertIn("| org.usefulness | 1 |", text)

    def test_given_deadline_during_report_when_artifacts_scanned_then_remaining_files_are_not_loaded_past_budget(self):
        rules = self.rules()
        for artifact in ["one", "two", "three"]:
            coordinate = "org.example:" + artifact
            write_json(self.store.evidence_path(coordinate), merge(None, coordinate, "https://sdk.example.org", "1", "2026-10-22T00:00:00Z", "a" * 64, [], []))
        client = FixtureClient()
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks > 2:
                raise DeadlineReached()
        client.check = check
        with patch("indexlib.reporting.read_json", wraps=read_json) as reads:
            with self.assertRaises(DeadlineReached):
                report(self.store, client, rules_dir=rules)
        self.assertEqual(reads.call_count, 2)
        self.assertFalse((self.root / "reports/coverage.md").exists())


class PrefixTests(FixtureTest):
    def evidence(self, coordinate, names):
        write_json(self.store.evidence_path(coordinate), {
            "coordinate": coordinate, "repository": "https://fixture.test", "versions": {},
            "components": [{"name": name, "type": "activity"} for name in names],
        })

    def run_prefixes(self, rules=()):
        rules_dir = self.root / ".rules"
        write_json(rules_dir / "rules/en/general.json", list(rules))
        self.assertTrue(prefixes(self.store, FixtureClient(), rules_dir=rules_dir))
        return read_json(self.root / "data/prefixes.json", [])

    def test_given_rotating_children_with_identical_artifacts_when_attributed_then_parent_collapses_and_own_matches_are_excluded(self):
        names = [f"com.byazt.{child}.Screen" for child in ("cio", "fk", "mw", "ox", "qhj")]
        for coordinate in ("group.z:one", "group.a:two"):
            self.evidence(coordinate, names + ["com.byazt.Root", names[0]])
        result = self.run_prefixes([{"id": 10, "searchKeyword": ["byazt.cio"]}])
        self.assertEqual(result, [{
            "prefix": "com.byazt", "components": 6, "groups": ["group.a", "group.z"],
            "examples": sorted(set(names + ["com.byazt.Root"]))[:5],
            "coveredBy": [10], "cooccursWith": [],
        }])

    def test_given_five_children_with_four_equal_artifact_sets_when_attributed_then_eighty_percent_is_enough_to_collapse(self):
        for index in range(2):
            children = ("a", "b", "c", "d", "e") if index == 0 else ("a", "b", "c", "d")
            self.evidence(f"group.sdk:artifact{index}", [f"com.rotate.{child}.Screen" for child in children])
        result = self.run_prefixes()
        self.assertEqual([(item["prefix"], item["components"]) for item in result], [("com.rotate", 5)])

    def test_given_shared_namespace_children_in_different_artifacts_when_attributed_then_their_prefixes_remain_separate(self):
        for index, child in enumerate(("ads", "auth", "maps", "play", "util")):
            self.evidence(f"com.google:sdk{index}", [f"com.google.{child}.Screen"])
        result = self.run_prefixes()
        self.assertEqual([item["prefix"] for item in result],
                         [f"com.google.{child}" for child in ("ads", "auth", "maps", "play", "util")])

    def test_given_four_children_or_nested_but_unequal_sets_when_attributed_then_neither_namespace_collapses(self):
        for index in range(5):
            names = [f"com.nested.c{child}.Screen" for child in range(index, 5)]
            names.extend(f"com.small.c{child}.Screen" for child in range(4))
            self.evidence(f"group.sdk:a{index}", names)
        result = self.run_prefixes()
        self.assertEqual(len(result), 9)
        self.assertNotIn("com.nested", [item["prefix"] for item in result])
        self.assertNotIn("com.small", [item["prefix"] for item in result])

    def test_given_placeholders_short_names_and_attribute_variants_when_attributed_then_only_valid_unique_class_names_count(self):
        names = ["Screen", "pkg.Screen", "${applicationId}.sdk.Screen", "com.vendor.sdk.Screen",
                 "com.vendor.sdk.Screen", "com.vendor.sdk.Other", "short.pkg.Screen"]
        self.evidence("group.sdk:one", names)
        self.evidence("group.sdk:two", ["com.vendor.sdk.Screen"])
        result = self.run_prefixes([{"id": 10, "searchKeyword": ["SDK.SCREEN"]},
                                    {"id": 2, "searchKeyword": ["vendor.sdk.s"]}])
        self.assertEqual([item["prefix"] for item in result], ["com.vendor.sdk", "short.pkg"])
        self.assertEqual(result[0]["components"], 2)
        self.assertEqual(result[0]["coveredBy"], [2, 10])

    def test_given_rule_matches_inside_and_outside_a_prefix_when_attributed_then_only_outside_matches_meet_ratio_and_artifact_thresholds(self):
        for index in range(5):
            names = ["candidate.target.Screen", "candidate.target.OwnMarker", "candidate.target.SelfOnly",
                     "also.track.Screen"]
            if index < 4:
                names += ["vendor.track.Screen", "outside.owner.OwnMarker"]
            if index < 3:
                names += ["partial.track.Screen"]
            if index < 2:
                names += ["candidate.pair.Screen"]
            if index == 0:
                names += ["candidate.single.Screen"]
            self.evidence(f"group.sdk:a{index}", names)
        result = self.run_prefixes([
            {"id": 10, "searchKeyword": ["vendor.track."]},
            {"id": 2, "searchKeyword": ["also.track."]},
            {"id": 3, "searchKeyword": ["partial.track."]},
            {"id": 99, "searchKeyword": ["OwnMarker"]},
            {"id": 98, "searchKeyword": ["SelfOnly"]},
        ])
        by_prefix = {item["prefix"]: item for item in result}
        self.assertEqual(by_prefix["candidate.target"]["cooccursWith"], [
            {"rule": 2, "ratio": 1.0, "artifacts": 5},
            {"rule": 10, "ratio": 0.8, "artifacts": 4},
            {"rule": 99, "ratio": 0.8, "artifacts": 4},
        ])
        self.assertEqual(by_prefix["candidate.single"]["cooccursWith"], [])
        self.assertIn({"rule": 10, "ratio": 1.0, "artifacts": 2}, by_prefix["candidate.pair"]["cooccursWith"])

    def test_given_shuffled_artifacts_and_components_when_attributed_twice_then_order_examples_and_json_bytes_are_deterministic(self):
        names = [f"org.vendor.sdk.Screen{index}" for index in reversed(range(7))]
        names += ["org.alpha.sdk.Screen"]
        for coordinate in ("group.z:sdk", "group.a:sdk"):
            self.evidence(coordinate, names)
        result = self.run_prefixes()
        path = self.root / "data/prefixes.json"
        before = path.read_bytes()
        for coordinate in ("group.a:sdk", "group.z:sdk"):
            self.evidence(coordinate, list(reversed(names)))
        self.assertEqual(self.run_prefixes(), result)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(before, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
        self.assertEqual(result[0]["examples"], sorted(names[:7])[:5])
        self.assertEqual(result[0]["groups"], ["group.a", "group.z"])
        self.assertEqual(list(result[0]), ["prefix", "components", "groups", "examples", "coveredBy", "cooccursWith"])

    def test_given_ratios_near_the_threshold_when_attributed_then_filtering_precedes_two_decimal_rounding(self):
        for index in range(200):
            names = ["candidate.rounding.Screen"]
            for limit, prefix in ((159, "below.track"), (160, "exact.track"), (166, "rounded.track")):
                if index < limit:
                    names.append(prefix + ".Screen")
            self.evidence(f"group.sdk:a{index}", names)
        result = self.run_prefixes([
            {"id": rule, "searchKeyword": [prefix]} for rule, prefix in
            ((1, "below.track"), (2, "exact.track"), (3, "rounded.track"))
        ])
        target = next(item for item in result if item["prefix"] == "candidate.rounding")
        self.assertEqual(target["cooccursWith"], [{"rule": 3, "ratio": 0.83, "artifacts": 166},
                                                 {"rule": 2, "ratio": 0.8, "artifacts": 160}])

    def test_given_rule_matches_in_a_two_segment_parent_and_its_uncollapsed_child_when_attributed_then_child_matches_are_inside_the_parent(self):
        for index in range(2):
            self.evidence(f"group.sdk:a{index}", ["org.shared.OwnMarker", "org.shared.sdk.OwnMarker"])
        result = self.run_prefixes([{"id": 10, "searchKeyword": ["OwnMarker"]}])
        parent = next(item for item in result if item["prefix"] == "org.shared")
        self.assertEqual(parent["cooccursWith"], [])
        child = next(item for item in result if item["prefix"] == "org.shared.sdk")
        self.assertEqual(child["cooccursWith"], [{"rule": 10, "ratio": 1.0, "artifacts": 2}])

    def test_given_no_rules_directory_when_attributed_then_no_prefix_snapshot_is_written(self):
        with self.assertRaises(ValueError):
            prefixes(self.store, FixtureClient())
        self.assertFalse((self.root / "data/prefixes.json").exists())
