import copy
import json
from defusedxml.common import DefusedXmlException
from indexlib.evidence import ATTRIBUTES, components_at, merge, parse_manifest
from indexlib.storage import write_json
from indexlib.versions import MavenVersion, ordered
from test_support import FIXTURES, FixtureTest


class VersionTests(FixtureTest):
    def test_given_numeric_versions_when_sorted_then_numbers_take_precedence_over_lexical_order(self):
        self.assertEqual(ordered(["1.10", "1.2", "2", "1.0.1", "1"]), ["1", "1.0.1", "1.2", "1.10", "2"])

    def test_given_maven_qualifiers_when_sorted_then_prereleases_release_and_service_pack_follow_maven_order(self):
        versions = ["1-sp", "1", "1-snapshot", "1-rc2", "1-rc10", "1-milestone1", "1-beta1", "1-alpha1"]
        self.assertEqual(ordered(versions), ["1-alpha1", "1-beta1", "1-milestone1", "1-rc2", "1-rc10", "1-snapshot", "1", "1-sp"])

    def test_given_aliases_and_zero_padding_when_compared_then_equivalent_releases_compare_equal(self):
        for value in ["1.0", "1.0.0", "1-ga", "1-final", "1-release"]:
            self.assertEqual(MavenVersion("1"), MavenVersion(value))
        self.assertEqual(MavenVersion("1-a1"), MavenVersion("1-alpha1"))
        self.assertEqual(MavenVersion("1-cr1"), MavenVersion("1-rc1"))
        self.assertEqual(MavenVersion("1.0-beta"), MavenVersion("1-beta"))

    def test_given_unknown_qualifiers_when_compared_then_they_sort_after_known_qualifiers(self):
        self.assertEqual(ordered(["1-zeta", "1-sp", "1-preview"]), ["1-sp", "1-preview", "1-zeta"])

    def test_given_maven_hyphen_and_digit_transitions_when_compared_then_numeric_builds_and_qualifiers_preserve_precedence(self):
        self.assertLess(MavenVersion("1.0-1"), MavenVersion("1.0.1"))
        self.assertLess(MavenVersion("1.0.rc2"), MavenVersion("1.0-rc3"))
        self.assertLess(MavenVersion("1.0-rc3"), MavenVersion("1.0.1"))
        self.assertEqual(MavenVersion("1.0alpha1"), MavenVersion("1-alpha1"))
        self.assertEqual(MavenVersion("1.0.0-0"), MavenVersion("1"))


class EvidenceTests(FixtureTest):
    def setUp(self):
        super().setUp()
        self.components = parse_manifest((FIXTURES / "manifest.xml").read_bytes())

    def test_given_relative_alias_and_placeholder_names_when_extracted_then_names_types_and_attributes_are_exact(self):
        by_name = {item["name"]: item for item in self.components}
        self.assertEqual(len(by_name), 5)
        screen = by_name["org.example.sdk.Screen"]
        self.assertEqual(set(screen), set(ATTRIBUTES))
        self.assertIs(screen["exported"], False)
        self.assertEqual(screen["process"], ":sdk")
        self.assertEqual(screen["permission"], "org.example.ACCESS")
        self.assertIsNone(screen["authorities"])
        self.assertEqual(screen["actions"], ["sdk.CLOSE", "sdk.OPEN"])
        self.assertEqual(screen["categories"], ["sdk.A", "sdk.Z"])
        self.assertEqual(screen["metaData"], ["sdk.a", "sdk.z"])
        self.assertEqual(by_name["org.example.sdk.Alias"]["type"], "activity")
        self.assertIs(by_name["org.example.sdk.Alias"]["exported"], True)
        self.assertIsNone(by_name["${serviceClass}"]["exported"])
        self.assertEqual(by_name["org.example.sdk.Provider"]["authorities"], "${applicationId}.provider;org.example.second")

    def test_given_entity_expansion_when_manifest_extracted_then_unsafe_xml_is_rejected(self):
        with self.assertRaises(DefusedXmlException):
            parse_manifest(b'<!DOCTYPE manifest [<!ENTITY x SYSTEM "file:///etc/passwd">]><manifest>&x;</manifest>')

    def add(self, evidence, version, components):
        return merge(evidence, "org.example:sdk", "https://sdk.example.org", version,
                     "2026-10-22T23:50:00Z", "a" * 64, ["org.dep:x:1", "org.dep:x:1"], components)

    def test_given_component_disappears_and_reappears_when_versions_merged_then_ranges_preserve_the_gap(self):
        evidence = self.add(None, "1", self.components)
        evidence = self.add(evidence, "3", self.components)
        self.assertEqual(evidence["components"][0]["versionRanges"], [["1", "3"]])
        evidence = self.add(evidence, "2", [])
        self.assertEqual(evidence["components"][0]["versionRanges"], [["1", "1"], ["3", "3"]])
        self.assertEqual(evidence["versions"]["2"]["dependencies"], ["org.dep:x:1"])
        self.assertEqual(components_at(evidence, "2"), [])

    def test_given_attributes_change_when_versions_merged_then_entries_split_and_same_attributes_merge_contiguously(self):
        before = [self.components[-1]]
        after = copy.deepcopy(before)
        after[0]["exported"] = True
        evidence = self.add(None, "1", before)
        evidence = self.add(evidence, "2", after)
        evidence = self.add(evidence, "3", after)
        self.assertEqual(len(evidence["components"]), 2)
        by_exported = {item["exported"]: item["versionRanges"] for item in evidence["components"]}
        self.assertEqual(by_exported, {False: [["1", "1"]], True: [["2", "3"]]})

    def test_given_empty_manifest_when_merged_then_checked_version_is_recorded_without_components(self):
        components = parse_manifest(b'<manifest package="org.empty"><application/></manifest>')
        evidence = self.add(None, "1", components)
        self.assertEqual(evidence["components"], [])
        self.assertEqual(list(evidence["versions"]), ["1"])
        self.assertEqual(set(evidence), {"coordinate", "repository", "versions", "components"})
        self.assertEqual(set(evidence["versions"]["1"]), {"fetchedAt", "sha256", "dependencies"})

    def test_given_versions_arrive_in_different_orders_when_written_then_output_is_identical_and_has_only_approved_keys(self):
        outputs = []
        for versions in [("1", "2", "10"), ("10", "1", "2")]:
            evidence = None
            for version in versions:
                evidence = self.add(evidence, version, self.components if version != "2" else [])
            path = self.root / "evidence.json"
            write_json(path, evidence)
            outputs.append(path.read_bytes())
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(outputs[0].endswith(b"\n"))
        decoded = json.loads(outputs[0])
        self.assertEqual(list(decoded["versions"]), ["1", "2", "10"])
        self.assertEqual(list(decoded["components"][0]), ["name", "type", "versionRanges", *ATTRIBUTES[2:]])
