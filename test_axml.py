#!/usr/bin/env python3
"""Tests for the AXML versionCode reader and the catalog it feeds.

Run: python3 -m pytest test_axml.py -q   (or: python3 test_axml.py)

The parser is the only thing standing between the store and a catalog whose
entries carry no ordering information, so it is tested against real APKs
rather than synthetic fixtures wherever possible.
"""
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from axml import version_code_from_apk, version_code_from_axml

# Real published APKs, when available (pulled off the store PVC). Paths are
# overridable so the suite still runs on a machine without them.
APK_DIR = Path(os.environ.get("APK_DIR", "/tmp/apks/apk"))
CASES = [
    ("SutliffApps/app-release.apk", 1),
    ("Proto/app-release.apk", 56),
    ("Talaria/talaria-release.apk", 56),
    ("Jellyfin/jellyfin-android-v2.7.1-sutliff.1-proprietary-release.apk", 2070101),
]


def build_apk(tmp: Path, manifest: bytes, name: str = "app.apk") -> Path:
    p = tmp / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", b"not really a dex")
    return p


class TestRealApks(unittest.TestCase):
    """Against artifacts the store actually serves."""

    def test_version_codes_match_expected(self):
        available = [(f, v) for f, v in CASES if (APK_DIR / f).exists()]
        if not available:
            self.skipTest(f"no APKs under {APK_DIR}")
        for rel, expected in available:
            with self.subTest(apk=rel):
                self.assertEqual(version_code_from_apk(str(APK_DIR / rel)), expected)

    def test_unsigned_and_prev_variants_are_distinct(self):
        # Guards against the parser silently returning a value from the wrong
        # archive entry or a cached/previous build.
        unsigned = APK_DIR / "Proto/app-release-unsigned.apk"
        signed = APK_DIR / "Proto/app-release.apk"
        if unsigned.exists() and signed.exists():
            self.assertNotEqual(
                version_code_from_apk(str(unsigned)),
                version_code_from_apk(str(signed)),
            )


class TestMalformedInput(unittest.TestCase):
    """Every one of these must return None, never raise and never guess."""

    def test_missing_file(self):
        self.assertIsNone(version_code_from_apk("/nonexistent/nope.apk"))

    def test_not_a_zip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "notazip.apk"
            p.write_bytes(b"this is not a zip file at all")
            self.assertIsNone(version_code_from_apk(str(p)))

    def test_zip_without_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            p = build_apk(Path(d), b"", "nomanifest.apk")
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("classes.dex", b"x")
            self.assertIsNone(version_code_from_apk(str(p)))

    def test_truncated_manifest(self):
        self.assertIsNone(version_code_from_axml(b""))
        self.assertIsNone(version_code_from_axml(b"\x03\x00"))
        self.assertIsNone(version_code_from_axml(b"\x00\x00\x80\x03" + b"\xff" * 4))

    def test_plain_text_manifest_is_not_axml(self):
        # A text AndroidManifest (some tooling emits one) must not be read as
        # binary — it has no valid magic and must yield None, not a digit.
        self.assertIsNone(version_code_from_axml(b"<manifest versionCode='9'/>"))

    def test_random_bytes_do_not_crash(self):
        for blob in (bytes(64), b"\x03\x00\x80\x03" + bytes(range(256)), b"\xff" * 512):
            with self.subTest(n=len(blob)):
                self.assertIsNone(version_code_from_axml(blob))


class TestCatalogIntegration(unittest.TestCase):
    """The catalog must expose a usable versionCode for a served APK."""

    def test_catalog_backfills_versioncode_and_persists(self):
        src = APK_DIR / "Proto/app-release.apk"
        if not src.exists():
            self.skipTest(f"no APK under {APK_DIR}")
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / "apk/Proto").mkdir(parents=True)
            (data / "version").mkdir()
            (data / "apk/Proto/app-release.apk").write_bytes(src.read_bytes())
            # A manifest exactly as an un-upgraded CI script would leave it:
            # no versionCode, no sha256.
            (data / "version/com.example.json").write_text(json.dumps({
                "app": "com.example",
                "name": "Example",
                "version": "1",
                "apk": "apk/Proto/app-release.apk",
                "size": src.stat().st_size,
                "built": "2026-01-01T00:00:00+00:00",
            }))

            import server
            old = server.DATA
            server.DATA = data
            try:
                out = server.catalog()
                self.assertEqual(len(out), 1)
                self.assertTrue(out[0]["exists"])
                # This is the whole point: an app published by a CI script that
                # never sent versionCode still gets one.
                self.assertEqual(out[0]["versionCode"], 56)
                self.assertEqual(len(out[0]["sha256"]), 64)
                # And it is written back, so the second call is cheap and stable.
                persisted = json.loads((data / "version/com.example.json").read_text())
                self.assertEqual(persisted["versionCode"], 56)
                self.assertEqual(server.catalog()[0]["versionCode"], 56)
            finally:
                server.DATA = old

    def test_missing_apk_reports_exists_false(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / "version").mkdir()
            (data / "version/com.gone.json").write_text(json.dumps({
                "app": "com.gone",
                "version": "1",
                "apk": "apk/Nope/app.apk",
            }))
            import server
            old = server.DATA
            server.DATA = data
            try:
                out = server.catalog()
                self.assertFalse(out[0]["exists"])
                # No bytes -> no facts. Absent, never fabricated.
                self.assertIsNone(out[0].get("versionCode"))
            finally:
                server.DATA = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
