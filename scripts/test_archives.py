import hashlib
import re
import zipfile
from indexlib.archives import eocd, manifest_entry, read_manifest
from indexlib.http import DownloadTooLarge, HttpClient
from test_support import FIXTURES, FixtureClient, FixtureTest, aar, response


class ArchiveTests(FixtureTest):
    def test_given_stored_deflated_and_commented_zips_when_directory_parsed_then_manifest_is_located(self):
        for method in [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]:
            for comment in [b"", b"comment with fake signature PK\x05\x06 inside"]:
                with self.subTest(method=method, comment=comment):
                    raw = aar(method=method, comment=comment)
                    end = eocd(raw)
                    entry = manifest_entry(raw[end["offset"]:end["offset"] + end["size"]], end["count"])
                    self.assertEqual(entry["method"], method)
                    self.assertEqual(entry["size"], (FIXTURES / "manifest.xml").stat().st_size)

    def test_given_range_server_when_manifest_read_then_only_ranges_are_used_and_hash_is_of_decompressed_manifest(self):
        for method in [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]:
            raw = aar(method=method, comment=b"fixture comment")

            def handler(url, headers, **kwargs):
                value = headers["Range"]
                if value == "bytes=-65536":
                    first, last = max(0, len(raw) - 65536), len(raw) - 1
                else:
                    first, last = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", value).groups())
                return response(raw[first:last + 1], 206, {"Content-Range": f"bytes {first}-{last}/{len(raw)}"})
            client = FixtureClient(handler=handler)
            manifest, digest = read_manifest(client, "https://sdk.example/a.aar", 1024 * 1024)
            self.assertEqual(manifest, (FIXTURES / "manifest.xml").read_bytes())
            self.assertEqual(digest, hashlib.sha256(manifest).hexdigest())
            self.assertTrue(all("Range" in kwargs["headers"] for _, kwargs in client.calls))
            self.assertFalse(list((self.root / ".cache").glob("*.aar")))

    def test_given_range_ignored_when_manifest_read_then_full_bytes_are_hashed_without_persisting_the_aar(self):
        raw = aar()
        client = FixtureClient({"https://sdk.example/a.aar": response(raw)})
        manifest, digest = read_manifest(client, "https://sdk.example/a.aar", 1024 * 1024)
        self.assertEqual(manifest, (FIXTURES / "manifest.xml").read_bytes())
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(list(self.root.rglob("*.aar")))
        self.assertFalse((self.root / ".cache/downloads").exists())

    def test_given_range_supported_only_for_tail_when_manifest_read_then_full_download_fallback_succeeds(self):
        raw = aar()
        calls = 0

        def handler(url, **kwargs):
            nonlocal calls
            calls += 1
            return (response(raw, 206, {"Content-Range": f"bytes 0-{len(raw) - 1}/{len(raw)}"})
                    if calls == 1 else response(raw))
        manifest, digest = read_manifest(FixtureClient(handler=handler), "https://sdk.example/a.aar", 1024 * 1024)
        self.assertEqual(manifest, (FIXTURES / "manifest.xml").read_bytes())
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_given_server_rejects_suffix_range_longer_than_file_when_manifest_read_then_full_download_is_used(self):
        raw = aar()

        def handler(url, headers=None, **kwargs):
            if headers and "Range" in headers:
                return response(b"", 416, {"Content-Range": f"bytes */{len(raw)}"})
            return response(raw)
        manifest, _ = read_manifest(FixtureClient(handler=handler), "https://sdk.example/a.aar", 1024 * 1024)
        self.assertEqual(manifest, (FIXTURES / "manifest.xml").read_bytes())

    def test_given_oversized_full_download_when_server_ignores_range_then_download_is_rejected(self):
        raw = aar()
        client = HttpClient(transport=lambda *args: response(raw))
        with self.assertRaises(DownloadTooLarge):
            read_manifest(client, "https://sdk.example/a.aar", 32)

    def test_given_truncated_directory_or_non_zip_when_parsed_then_invalid_archive_is_rejected(self):
        with self.assertRaises(ValueError):
            eocd(b"not a zip")
        with self.assertRaises(ValueError):
            manifest_entry(b"PK\x01\x02", 1)
