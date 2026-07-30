#!/usr/bin/env python3
"""
test_ipa_plist.py — exercise the HTTP-Range IPA reader against real ZIP bytes

ipa_plist.py walks raw ZIP structures by byte offset (end-of-central-directory,
central directory, local file header, deflate stream) to pull one Info.plist out
of a 70 MB remote archive. That is the most fragile code in the repo and every
consumer depends on it: the updater reads minOSVersion with it, the weekly audit
verifies bundle identifiers with it.

So these tests build genuine ZIP archives with zipfile, serve them through a fake
HTTP layer that honours Range requests exactly like a CDN would, and check the
parser picks the right entry and decodes it.

Run:
    python3 scripts/test_ipa_plist.py
"""

from __future__ import annotations

import io
import plistlib
import sys
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ipa_plist  # noqa: E402

MAIN_PLIST = {
    "CFBundleIdentifier": "com.stremio.pal",
    "CFBundleShortVersionString": "2.0.6",
    "CFBundleVersion": "21",
    "MinimumOSVersion": "15.0",
}
FRAMEWORK_PLIST = {"CFBundleIdentifier": "org.videolan.vlckit"}
APPEX_PLIST = {"CFBundleIdentifier": "com.stremio.pal.share"}
WATCH_PLIST = {"CFBundleIdentifier": "com.stremio.pal.watchkitapp"}


def build_ipa(compression=zipfile.ZIP_DEFLATED, *, plist_format=plistlib.FMT_BINARY) -> bytes:
    """A miniature but structurally real IPA."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        # Decoys the parser must skip, written first so they are not simply
        # "the first Info.plist we happen to find".
        z.writestr("Payload/Stremio.app/Frameworks/VLCKit.framework/Info.plist",
                   plistlib.dumps(FRAMEWORK_PLIST))
        z.writestr("Payload/Stremio.app/PlugIns/Share.appex/Info.plist",
                   plistlib.dumps(APPEX_PLIST))
        z.writestr("Payload/Stremio.app/Watch/Watch.app/Info.plist",
                   plistlib.dumps(WATCH_PLIST))
        z.writestr("Payload/Stremio.app/Info.plist",
                   plistlib.dumps(MAIN_PLIST, fmt=plist_format))
        z.writestr("Payload/Stremio.app/embedded.mobileprovision", b"\x00" * 512)
    return buf.getvalue()


class FakeCDN:
    """Serves a byte buffer over the subset of HTTP the parser uses."""

    def __init__(self, data: bytes, *, head_status: int = 200, drop_content_length: bool = False):
        self.data = data
        self.head_status = head_status
        self.drop_content_length = drop_content_length
        self.range_requests: list[tuple[int, int]] = []

    def __call__(self, url, *, method="GET", headers=None, timeout=15):
        headers = headers or {}
        if method == "HEAD":
            h = {} if self.drop_content_length else {"Content-Length": str(len(self.data))}
            return ipa_plist.HttpResp(self.head_status, h, None)
        rng = headers.get("Range")
        if not rng:
            return ipa_plist.HttpResp(200, {}, self.data)
        start_s, _, end_s = rng.replace("bytes=", "").partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else len(self.data) - 1
        end = min(end, len(self.data) - 1)
        self.range_requests.append((start, end))
        return ipa_plist.HttpResp(206, {}, self.data[start:end + 1])


class Parsing(unittest.TestCase):
    def _read(self, data: bytes, **kw):
        cdn = FakeCDN(data, **kw)
        ipa_plist.http_request = cdn
        return ipa_plist.get_main_app_info_plist("https://dl.strem.io/fake.ipa"), cdn

    def setUp(self):
        self._real = ipa_plist.http_request

    def tearDown(self):
        ipa_plist.http_request = self._real

    def test_reads_binary_plist_from_deflated_ipa(self):
        res, _ = self._read(build_ipa())
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["plist"], MAIN_PLIST)

    def test_reads_xml_plist(self):
        res, _ = self._read(build_ipa(plist_format=plistlib.FMT_XML))
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["plist"]["MinimumOSVersion"], "15.0")

    def test_reads_stored_uncompressed_entry(self):
        # method 0 takes the other branch of the decompressor.
        res, _ = self._read(build_ipa(compression=zipfile.ZIP_STORED))
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["plist"], MAIN_PLIST)

    def test_picks_the_main_app_not_a_framework_appex_or_watch_app(self):
        # The whole point: a framework's bundle id must never be reported as
        # the app's, which is what the audit compares against the JSON.
        res, _ = self._read(build_ipa())
        self.assertEqual(res["name"], "Payload/Stremio.app/Info.plist")
        self.assertEqual(res["plist"]["CFBundleIdentifier"], "com.stremio.pal")

    def test_downloads_far_less_than_the_whole_archive(self):
        # The reason this parser exists: a few KB instead of ~70 MB.
        data = build_ipa()
        _, cdn = self._read(data)
        fetched = sum(end - start + 1 for start, end in cdn.range_requests)
        self.assertLess(fetched, len(data) * 3,
                        "range reads should not add up to repeatedly pulling the file")


class Failures(unittest.TestCase):
    """Every failure must be reported, never raised — callers check ok/error."""

    def setUp(self):
        self._real = ipa_plist.http_request

    def tearDown(self):
        ipa_plist.http_request = self._real

    def test_head_404(self):
        ipa_plist.http_request = FakeCDN(build_ipa(), head_status=404)
        res = ipa_plist.get_main_app_info_plist("https://dl.strem.io/gone.ipa")
        self.assertFalse(res["ok"])
        self.assertIn("404", res["error"])

    def test_missing_content_length(self):
        ipa_plist.http_request = FakeCDN(build_ipa(), drop_content_length=True)
        res = ipa_plist.get_main_app_info_plist("https://dl.strem.io/fake.ipa")
        self.assertFalse(res["ok"])
        self.assertIn("Content-Length", res["error"])

    def test_not_a_zip(self):
        ipa_plist.http_request = FakeCDN(b"this is not a zip archive" * 100)
        res = ipa_plist.get_main_app_info_plist("https://dl.strem.io/fake.ipa")
        self.assertFalse(res["ok"])
        self.assertIn("EOCD", res["error"])

    def test_zip_without_any_app_info_plist(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Payload/Stremio.app/readme.txt", b"nothing useful here")
        ipa_plist.http_request = FakeCDN(buf.getvalue())
        res = ipa_plist.get_main_app_info_plist("https://dl.strem.io/fake.ipa")
        self.assertFalse(res["ok"])
        self.assertIn("Info.plist", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
