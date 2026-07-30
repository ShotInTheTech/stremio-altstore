#!/usr/bin/env python3
"""
test_derived_data.py — the scripts that rewrite files from data we already have

render_readme, sync_legacy_fields, fetch_release_notes and add_hashes all edit
published files in place. Their failure mode is quiet: a bad marker match eats
part of the README, a sloppy sanitiser puts raw control bytes in front of
users, a rebuilt version object drops the sha256 that took a 70 MB download to
compute. None of that raises an exception, so only assertions catch it.

Run:
    python3 scripts/test_derived_data.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import add_hashes  # noqa: E402
import fetch_release_notes as notes  # noqa: E402
import render_readme  # noqa: E402
import sync_legacy_fields as legacy  # noqa: E402


def version(ver, build, **extra):
    v = {"version": ver, "buildVersion": build, "date": "2026-07-22",
         "localizedDescription": f"Stremio {ver} (build {build}).",
         "downloadURL": f"https://dl.strem.io/apple/{ver}b{build}/ios/stremio_iOS.ipa",
         "size": 75942223, "minOSVersion": "13.0"}
    v.update(extra)
    return v


def doc(versions):
    return {"name": "Stremio iOS", "identifier": "x",
            "sourceURL": "https://example.com/stremio-ios.json",
            "apps": [{"name": "Stremio", "bundleIdentifier": "com.stremio.pal",
                      "versions": versions}]}


# --------------------------------------------------------------------------
# render_readme
# --------------------------------------------------------------------------

TEMPLATE = """# Title

[![Stremio iOS versions](https://img.shields.io/badge/iOS-99%20versions-7055D9)](stremio-ios.json)

Intro paragraph that must survive.

## Available versions

<!-- BEGIN:AVAILABLE_VERSIONS -->
stale content
<!-- END:AVAILABLE_VERSIONS -->

## Footer that must also survive
"""


class RenderReadme(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = (render_readme.REPO, render_readme.PLATFORMS)
        render_readme.REPO = self.dir
        render_readme.PLATFORMS = [{"json": "stremio-ios.json", "heading": "iOS / iPadOS",
                                    "badge": "iOS"}]
        (self.dir / "stremio-ios.json").write_text(
            json.dumps(doc([version("2.0.6", "21"), version("2.0.0", "11")])), encoding="utf-8")
        (self.dir / "README.md").write_text(TEMPLATE, encoding="utf-8")

    def tearDown(self):
        render_readme.REPO, render_readme.PLATFORMS = self._orig
        self._tmp.cleanup()

    def readme(self) -> str:
        return (self.dir / "README.md").read_text(encoding="utf-8")

    def render(self, check: bool = False) -> int:
        """Run the renderer without letting its logging into the test report."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return render_readme.render(check=check)

    def test_only_the_marked_block_is_replaced(self):
        self.assertEqual(self.render(check=False), 0)
        out = self.readme()
        self.assertIn("Intro paragraph that must survive.", out)
        self.assertIn("## Footer that must also survive", out)
        self.assertNotIn("stale content", out)
        self.assertIn("2.0.6", out)

    def test_badge_count_is_synced(self):
        self.render(check=False)
        self.assertIn("iOS-2%20versions", self.readme())
        self.assertNotIn("iOS-99%20versions", self.readme())

    def test_is_idempotent(self):
        self.render(check=False)
        once = self.readme()
        self.assertEqual(self.render(check=False), 0)
        self.assertEqual(self.readme(), once)

    def test_check_reports_stale_without_writing(self):
        before = self.readme()
        self.assertEqual(self.render(check=True), 1)
        self.assertEqual(self.readme(), before)

    def test_check_passes_once_rendered(self):
        self.render(check=False)
        self.assertEqual(self.render(check=True), 0)

    def test_missing_markers_refuses_to_touch_the_file(self):
        # Better to fail loudly than to guess where the block belongs.
        (self.dir / "README.md").write_text("# No markers here\n", encoding="utf-8")
        self.assertEqual(self.render(check=False), 2)
        self.assertEqual(self.readme(), "# No markers here\n")


# --------------------------------------------------------------------------
# sync_legacy_fields
# --------------------------------------------------------------------------

class LegacyMirror(unittest.TestCase):
    def test_newest_version_wins_on_build_not_just_semver(self):
        app = {"versions": [version("2.0.1", "15"), version("2.0.1", "16"),
                            version("2.0.0", "14")]}
        self.assertEqual(legacy.newest_version(app)["buildVersion"], "16")

    def test_double_digit_builds_sort_numerically(self):
        app = {"versions": [version("2.0.0", "9"), version("2.0.0", "21")]}
        self.assertEqual(legacy.newest_version(app)["buildVersion"], "21")

    def test_app_without_versions_has_no_newest(self):
        self.assertIsNone(legacy.newest_version({"versions": []}))

    def test_mirror_copies_the_newest_build_up(self):
        app = {"name": "Stremio", "versions": [version("2.0.6", "21",
                                                       localizedDescription="feat: real notes")]}
        changed = legacy.sync_app(app)
        self.assertIn("versionDescription", changed)
        self.assertEqual(app["version"], "2.0.6")
        self.assertEqual(app["versionDescription"], "feat: real notes")
        self.assertEqual(app["downloadURL"], app["versions"][0]["downloadURL"])

    def test_mirror_is_idempotent(self):
        app = {"name": "Stremio", "versions": [version("2.0.6", "21")]}
        legacy.sync_app(app)
        self.assertEqual(legacy.sync_app(app), [], "second run should change nothing")

    def test_mirror_follows_a_newly_added_build(self):
        app = {"name": "Stremio", "versions": [version("2.0.5", "20")]}
        legacy.sync_app(app)
        app["versions"].insert(0, version("2.0.6", "21"))
        self.assertTrue(legacy.sync_app(app))
        self.assertEqual(app["version"], "2.0.6")


# --------------------------------------------------------------------------
# fetch_release_notes — sanitising third-party text
# --------------------------------------------------------------------------

class Sanitising(unittest.TestCase):
    def test_keeps_changelog_line_structure(self):
        self.assertEqual(notes.sanitize("feat: a\nfix: b"), "feat: a\nfix: b")

    def test_normalises_windows_line_endings(self):
        self.assertEqual(notes.sanitize("feat: a\r\nfix: b"), "feat: a\nfix: b")

    def test_strips_control_characters(self):
        self.assertEqual(notes.sanitize("feat: a\x00\x07b"), "feat: ab")

    def test_collapses_runs_of_blank_lines(self):
        self.assertEqual(notes.sanitize("a\n\n\n\n\nb"), "a\n\nb")

    def test_caps_length(self):
        out = notes.sanitize("x" * 10000)
        self.assertLessEqual(len(out), notes.MAX_LEN + len("\n\n(truncated)"))
        self.assertTrue(out.endswith("(truncated)"))

    def test_empty_and_non_string_become_none(self):
        for bad in ("", "   \n\n ", None, 42, {"a": 1}):
            self.assertIsNone(notes.sanitize(bad), repr(bad))

    def test_placeholder_is_recognised(self):
        self.assertTrue(notes.is_placeholder("Stremio 2.0.4 (build 19)."))
        self.assertTrue(notes.is_placeholder("Stremio Lite 1.3.6 (build 7)."))
        self.assertTrue(notes.is_placeholder(""))
        self.assertTrue(notes.is_placeholder(None))

    def test_real_notes_are_not_mistaken_for_a_placeholder(self):
        self.assertFalse(notes.is_placeholder("feat: new desktop like UI for player"))
        self.assertFalse(notes.is_placeholder("fix: crash on launch\nfix: seek bar"))


# --------------------------------------------------------------------------
# add_hashes
# --------------------------------------------------------------------------

class Hashes(unittest.TestCase):
    def test_valid_hash_recognised(self):
        self.assertTrue(add_hashes._has_valid_hash({"sha256": "a" * 64}))

    def test_bad_hashes_rejected(self):
        for bad in ("A" * 64, "abc", "", None, "g" * 64, "a" * 63):
            self.assertFalse(add_hashes._has_valid_hash({"sha256": bad}), repr(bad))
        self.assertFalse(add_hashes._has_valid_hash({}))

    def test_hash_is_inserted_right_after_size(self):
        # Purely for diff stability: the bot rewrites these files constantly.
        v = version("2.0.6", "21")
        add_hashes._set_sha256(v, "b" * 64)
        keys = list(v.keys())
        self.assertEqual(keys[keys.index("size") + 1], "sha256")

    def test_setting_a_hash_twice_does_not_duplicate_or_reorder(self):
        v = version("2.0.6", "21")
        add_hashes._set_sha256(v, "b" * 64)
        first = list(v.keys())
        add_hashes._set_sha256(v, "c" * 64)
        self.assertEqual(list(v.keys()), first)
        self.assertEqual(v["sha256"], "c" * 64)

    def test_newest_versions_are_hashed_first(self):
        sources = {"stremio-ios.json": doc([version("2.0.0", "11"), version("2.0.6", "21")])}
        missing = add_hashes._missing(sources)
        self.assertEqual(missing[0]["version"], "2.0.6")

    def test_already_hashed_versions_are_not_revisited(self):
        sources = {"stremio-ios.json": doc([version("2.0.6", "21", sha256="d" * 64),
                                            version("2.0.0", "11")])}
        missing = add_hashes._missing(sources)
        self.assertEqual([m["version"] for m in missing], ["2.0.0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
