#!/usr/bin/env python3
"""
test_trim_versions.py — the rules that stop retention from stranding anyone

Trimming removes options users can still install, so the value of this script
is in its exceptions: a build outside the keep window survives when it is the
last one a given device or release line can use. Those exceptions do nothing
on today's data — every build wants the same OS and sits in one minor line —
so without tests they could be broken for months without anyone noticing, and
would only surface the day a release raised the minimum OS and stranded older
devices.

Run:
    python3 scripts/test_trim_versions.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import trim_versions  # noqa: E402


def v(version: str, build: str, min_os: str = "13.0") -> dict:
    return {"version": version, "buildVersion": build, "date": "2026-07-22",
            "minOSVersion": min_os,
            "downloadURL": f"https://dl.strem.io/apple/{version}b{build}/ios/stremio_iOS.ipa",
            "size": 75000000}


def names(versions) -> list[str]:
    return [f"{x['version']}b{x['buildVersion']}" for x in versions]


class KeepWindow(unittest.TestCase):
    def test_keeps_the_newest_n(self):
        versions = [v("2.0.%d" % i, str(i)) for i in range(6)]
        kept, dropped, _ = trim_versions.plan(versions, keep=3)
        self.assertEqual(names(kept), ["2.0.5b5", "2.0.4b4", "2.0.3b3"])
        self.assertEqual(len(dropped), 3)

    def test_nothing_dropped_when_list_is_short(self):
        versions = [v("2.0.1", "1"), v("2.0.2", "2")]
        kept, dropped, _ = trim_versions.plan(versions, keep=12)
        self.assertEqual(dropped, [])
        self.assertEqual(len(kept), 2)

    def test_result_stays_newest_first(self):
        versions = [v("2.0.0", "1"), v("2.0.9", "9"), v("2.0.5", "5")]
        kept, _, _ = trim_versions.plan(versions, keep=12)
        self.assertEqual(names(kept), ["2.0.9b9", "2.0.5b5", "2.0.0b1"])

    def test_orders_by_build_not_string(self):
        versions = [v("2.0.0", "9"), v("2.0.0", "21")]
        kept, _, _ = trim_versions.plan(versions, keep=1)
        self.assertEqual(names(kept), ["2.0.0b21"])


class ProtectiveRules(unittest.TestCase):
    """The exceptions are the reason this is safe to run at all."""

    def test_last_build_for_an_older_os_survives_the_window(self):
        # A device on iOS 13 has exactly one option left; dropping it for
        # being old would strand that device entirely.
        versions = [v("2.1.%d" % i, str(20 + i), min_os="15.0") for i in range(5)]
        legacy = v("2.0.0", "11", min_os="13.0")
        versions.append(legacy)
        kept, dropped, reasons = trim_versions.plan(versions, keep=2)
        self.assertIn("2.0.0b11", names(kept))
        self.assertNotIn("2.0.0b11", names(dropped))
        self.assertIn("minOS 13.0", reasons[id(legacy)])

    def test_last_build_of_a_minor_line_survives(self):
        versions = [v("2.1.%d" % i, str(20 + i)) for i in range(5)]
        old_line = v("1.3.6", "7")
        versions.append(old_line)
        kept, _, reasons = trim_versions.plan(versions, keep=2)
        self.assertIn("1.3.6b7", names(kept))
        self.assertIn("1.3 line", reasons[id(old_line)])

    def test_only_the_newest_of_a_protected_bucket_is_kept(self):
        # The rule preserves one option per bucket, not the whole history.
        versions = [v("2.1.%d" % i, str(20 + i), min_os="15.0") for i in range(5)]
        newer_legacy = v("2.0.1", "16", min_os="13.0")
        older_legacy = v("2.0.0", "11", min_os="13.0")
        versions += [newer_legacy, older_legacy]
        kept, dropped, _ = trim_versions.plan(versions, keep=2)
        self.assertIn("2.0.1b16", names(kept))
        self.assertIn("2.0.0b11", names(dropped))

    def test_every_bucket_keeps_an_option(self):
        versions = [
            v("3.0.0", "40", min_os="17.0"), v("3.0.1", "41", min_os="17.0"),
            v("2.1.0", "30", min_os="15.0"),
            v("1.3.6", "7", min_os="12.0"),
        ]
        kept, _, _ = trim_versions.plan(versions, keep=1)
        surviving_os = {x["minOSVersion"] for x in kept}
        self.assertEqual(surviving_os, {"17.0", "15.0", "12.0"},
                         "no operating system may lose its last option")

    def test_a_version_is_never_reported_as_both_kept_and_dropped(self):
        versions = [v("2.1.%d" % i, str(20 + i), min_os="15.0") for i in range(5)]
        versions.append(v("2.0.0", "11", min_os="13.0"))
        kept, dropped, _ = trim_versions.plan(versions, keep=2)
        self.assertEqual(len(kept) + len(dropped), len(versions))
        self.assertFalse(set(names(kept)) & set(names(dropped)))


class Robustness(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(trim_versions.plan([], keep=5), ([], [], {}))

    def test_malformed_entries_do_not_raise(self):
        versions = [v("2.0.6", "21"), {"version": None}, {"buildVersion": "x"}]
        kept, dropped, _ = trim_versions.plan(versions, keep=12)
        self.assertEqual(len(kept) + len(dropped), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
