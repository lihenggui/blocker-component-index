import json
from indexlib.parsing import coordinates, gradle_repositories, metadata, module_info, normalize_repository, pom
from indexlib.storage import valid_coordinate
from test_support import FIXTURES, FixtureTest, metadata_fixture


class ParsingTests(FixtureTest):
    def test_given_groovy_maven_syntax_when_parsed_then_urls_normalize_and_ignored_hosts_are_excluded(self):
        self.assertEqual(gradle_repositories((FIXTURES / "repositories.gradle").read_text()),
                         ["http://downloads.example.org/maven", "https://sdk.example.org/releases"])

    def test_given_kts_maven_syntax_when_parsed_then_function_uri_and_seturl_forms_are_found(self):
        self.assertEqual(gradle_repositories((FIXTURES / "repositories.gradle.kts").read_text()),
                         ["https://other.example.org/android", "https://sdk.example.org/releases", "https://third.example.org/repository"])

    def test_given_groovy_kts_and_catalog_coordinates_when_parsed_then_generic_coordinates_are_extracted(self):
        self.assertEqual(coordinates((FIXTURES / "repositories.gradle").read_text()), ["org.example:one", "org.example:two"])
        self.assertEqual(coordinates((FIXTURES / "repositories.gradle.kts").read_text()), ["org.example:four", "org.example:three"])
        self.assertEqual(coordinates((FIXTURES / "libs.versions.toml").read_text(), catalog=True),
                         ["org.example:one", "org.example:three", "org.example:two"])

    def test_given_local_ip_interpolated_and_plugin_urls_when_normalized_then_they_are_rejected(self):
        for value in ["http://localhost/repo", "http://127.0.0.1/repo", "http://[::1]/repo", "file:/maven", "https://${host}/maven", "https://plugins.gradle.org/m2", "https://user:pass@repo.example/maven"]:
            self.assertIsNone(normalize_repository(value))
        self.assertEqual(normalize_repository("http://SDK.Example:8080/path/"), "http://sdk.example:8080/path")

    def test_given_pom_dependencies_when_parsed_then_only_nonoptional_compile_runtime_and_managed_versions_remain(self):
        packaging, dependencies, repositories = pom((FIXTURES / "artifact.pom").read_bytes())
        self.assertEqual(packaging, "aar")
        self.assertEqual(dependencies, ["org.dep:compile:2.1", "org.dep:managed:4.0", "org.dep:runtime:3.0"])
        self.assertEqual(repositories, ["https://pom.example.org/maven"])

    def test_given_module_aar_variant_when_parsed_then_aar_file_and_runtime_dependencies_are_returned(self):
        value = {"variants": [
            {"attributes": {"org.gradle.usage": "java-runtime"}, "files": [{"name": "sdk-1.aar", "url": "sdk-1.aar"}],
             "dependencies": [{"group": "org.dep", "module": "runtime", "version": {"requires": "1"}}]},
            {"files": [{"name": "sdk-1.jar"}], "dependencies": [{"group": "org.dep", "module": "jar", "version": {"requires": "2"}}]},
        ]}
        self.assertEqual(module_info(json.dumps(value)), (["sdk-1.aar"], ["org.dep:runtime:1"]))

    def test_given_unsorted_metadata_when_parsed_then_versions_are_sorted_and_coordinate_is_returned(self):
        self.assertEqual(metadata(metadata_fixture(versions=("10", "2", "1", "2"))), ("org.example:sdk", ["1", "2", "10"]))

    def test_given_unsafe_coordinate_when_evidence_path_built_then_it_cannot_escape_artifacts_directory(self):
        self.assertFalse(valid_coordinate("../../tmp:x"))
        with self.assertRaises(ValueError):
            self.store.evidence_path("org.example:../x")

    def test_given_custom_gradle_configuration_and_multiline_maps_when_parsed_then_map_coordinates_are_not_limited_to_builtin_configurations(self):
        text = '''
        fossImplementation group: 'org.example', name: 'one', version: '1'
        customSdk(
            name = "two",
            group = "org.example",
            version = "2"
        )
        '''
        self.assertEqual(coordinates(text), ["org.example:one", "org.example:two"])

    def test_given_empty_call_parentheses_when_parsed_then_coordinates_are_still_extracted(self):
        text = '''
        repositories { google() }
        implementation 'org.example:one:1'
        '''
        self.assertEqual(coordinates(text), ["org.example:one"])

    def test_given_module_with_null_fields_when_parsed_then_nulls_are_treated_as_absent(self):
        raw = json.dumps({"variants": [None, {"attributes": None, "files": [None, {"name": None, "url": "sdk.aar"}],
                                              "dependencies": [None, {"group": None, "module": "x", "version": None},
                                                               {"group": "org.dep", "module": "lib", "version": {"requires": "1"}}]}]})
        self.assertEqual(module_info(raw.encode()), (["sdk.aar"], ["org.dep:lib:1"]))
