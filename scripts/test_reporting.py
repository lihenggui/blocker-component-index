import json
from unittest.mock import patch
from indexlib.discovery import add_repository
from indexlib.evidence import merge, parse_manifest
from indexlib.http import DeadlineReached
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
