#!/usr/bin/env python3
"""
test_prune_dead.py — the safety gates on the one script that deletes data

prune_dead.py removes versions from a published source, and a push reaches
users within minutes. Its value is entirely in what it *refuses* to do, and
those refusals are invisible in normal operation — nothing would look wrong
until the day a CDN blip quietly deleted good releases.

Every gate gets a test here: only 404/410 counts as gone, a flaky URL is
retried before being condemned, a missing newest build or a mass failure
stops the run outright, and an app whose builds are all gone is flagged for a
human rather than silently emptied.

Run:
    python3 scripts/test_prune_dead.py
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import ipa_plist  # noqa: E402
import prune_dead  # noqa: E402

IOS = "stremio-ios.json"


def version(ver: str, build: str, url: str) -> dict:
    return {
        "version": ver, "buildVersion": build, "date": "2026-07-22",
        "localizedDescription": f"Stremio {ver} (build {build}).",
        "downloadURL": url, "size": 75000000, "minOSVersion": "13.0",
    }


def source(apps: list[dict]) -> dict:
    return {
        "name": "Stremio iOS", "identifier": "com.gorlev.stremio-ios",
        "sourceURL": "https://gorlev.github.io/stremio-altstore/stremio-ios.json",
        "apps": apps,
    }


def app(name: str, bundle: str, versions: list[dict]) -> dict:
    return {"name": name, "bundleIdentifier": bundle, "versions": versions}


def url(tag: str) -> str:
    return f"https://dl.strem.io/apple/{tag}/ios/stremio_iOS.ipa"


class PruneCase(unittest.TestCase):
    """Runs prune_dead against a temp source with a scripted CDN."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = (prune_dead.REPO, prune_dead.SOURCES, prune_dead.http_request)
        prune_dead.REPO = self.dir
        prune_dead.SOURCES = [IOS]

    def tearDown(self):
        prune_dead.REPO, prune_dead.SOURCES, prune_dead.http_request = self._orig
        self._tmp.cleanup()

    def write(self, doc: dict) -> None:
        (self.dir / IOS).write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def read(self) -> dict:
        return json.loads((self.dir / IOS).read_text(encoding="utf-8"))

    def serve(self, statuses: dict[str, object]) -> None:
        """statuses maps a URL substring to a status, or to a list of statuses
        returned in order (to model a flaky endpoint)."""
        state = {k: list(v) if isinstance(v, list) else None for k, v in statuses.items()}

        def fake(u, *, method="GET", headers=None, timeout=15):
            for frag, val in statuses.items():
                if frag in u:
                    if state[frag] is not None:
                        code = state[frag].pop(0) if state[frag] else val[-1]
                    else:
                        code = val
                    return ipa_plist.HttpResp(code, {}, None)
            return ipa_plist.HttpResp(200, {}, None)

        prune_dead.http_request = fake

    def run_prune(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        old = sys.argv
        sys.argv = ["prune_dead.py", *argv]
        try:
            with contextlib.redirect_stdout(out):
                rc = prune_dead.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()


class HealthyAndMechanical(PruneCase):
    def test_all_live_changes_nothing(self):
        doc = source([app("Stremio", "com.stremio.pal",
                          [version("2.0.6", "21", url("2.0.6b21")),
                           version("2.0.5", "20", url("2.0.5b20"))])])
        self.write(copy.deepcopy(doc))
        self.serve({})
        rc, out = self.run_prune()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), doc)

    def test_report_mode_never_writes(self):
        doc = source([app("Stremio", "com.stremio.pal",
                          [version("2.0.6", "21", url("2.0.6b21")),
                           version("2.0.0", "11", url("2.0.0b11"))])])
        self.write(copy.deepcopy(doc))
        self.serve({"2.0.0b11": 404})
        rc, out = self.run_prune()
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.read(), doc, "report mode must leave the file untouched")

    def test_apply_removes_only_the_dead_build(self):
        self.write(source([app("Stremio", "com.stremio.pal",
                               [version("2.0.6", "21", url("2.0.6b21")),
                                version("2.0.0", "11", url("2.0.0b11"))])]))
        self.serve({"2.0.0b11": 404})
        rc, out = self.run_prune("--apply")
        self.assertEqual(rc, 0, out)
        left = [v["version"] for v in self.read()["apps"][0]["versions"]]
        self.assertEqual(left, ["2.0.6"])


class NeverPrunedWithoutProof(PruneCase):
    def test_timeout_is_never_treated_as_dead(self):
        # A network error must not delete a perfectly good release.
        doc = source([app("Stremio", "com.stremio.pal",
                          [version("2.0.6", "21", url("2.0.6b21")),
                           version("2.0.0", "11", url("2.0.0b11"))])])
        self.write(copy.deepcopy(doc))
        self.serve({"2.0.0b11": None})
        rc, out = self.run_prune("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("Unreachable", out)
        self.assertEqual(self.read(), doc)

    def test_server_error_is_never_treated_as_dead(self):
        doc = source([app("Stremio", "com.stremio.pal",
                          [version("2.0.6", "21", url("2.0.6b21")),
                           version("2.0.0", "11", url("2.0.0b11"))])])
        self.write(copy.deepcopy(doc))
        self.serve({"2.0.0b11": 503})
        rc, out = self.run_prune("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), doc)

    def test_flaky_404_that_recovers_is_kept(self):
        # One bad response out of three must not condemn a build.
        doc = source([app("Stremio", "com.stremio.pal",
                          [version("2.0.6", "21", url("2.0.6b21")),
                           version("2.0.0", "11", url("2.0.0b11"))])])
        self.write(copy.deepcopy(doc))
        self.serve({"2.0.0b11": [404, 200, 200]})
        rc, out = self.run_prune("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), doc, "a recovered URL must not be pruned")


class GlobalGates(PruneCase):
    def test_newest_build_gone_stops_everything(self):
        # Looks CDN-wide: prune nothing, even the other dead build.
        doc = source([app("Stremio", "com.stremio.pal",
                          [version("2.0.6", "21", url("2.0.6b21")),
                           version("2.0.5", "20", url("2.0.5b20")),
                           version("2.0.0", "11", url("2.0.0b11"))])])
        self.write(copy.deepcopy(doc))
        self.serve({"2.0.6b21": 404, "2.0.0b11": 404})
        rc, out = self.run_prune("--apply")
        self.assertEqual(rc, 2, out)
        self.assertIn("CDN-wide", out)
        self.assertEqual(self.read(), doc)

    def test_mass_failure_exceeds_cap_and_prunes_nothing(self):
        versions = [version("2.0.6", "21", url("2.0.6b21"))] + [
            version(f"2.0.{i}", str(i), url(f"2.0.{i}b{i}")) for i in range(1, 6)
        ]
        doc = source([app("Stremio", "com.stremio.pal", versions)])
        self.write(copy.deepcopy(doc))
        self.serve({f"2.0.{i}b{i}": 404 for i in range(1, 6)})
        rc, out = self.run_prune("--apply", "--max-prune", "3")
        self.assertEqual(rc, 2, out)
        self.assertIn("safety cap", out)
        self.assertEqual(self.read(), doc)


class OrphanedApps(PruneCase):
    def test_app_with_no_surviving_versions_is_flagged_not_emptied(self):
        doc = source([
            app("Stremio", "com.stremio.pal", [version("2.0.6", "21", url("2.0.6b21"))]),
            app("Stremio Lite", "com.stremio.ios", [version("1.3.6", "7", url("1.3.6b7"))]),
        ])
        self.write(copy.deepcopy(doc))
        self.serve({"1.3.6b7": 404})
        rc, out = self.run_prune("--apply")
        self.assertEqual(rc, 2, out)
        self.assertIn("ACTION NEEDED", out)
        # The app keeps its version rather than becoming an empty shell.
        self.assertEqual(self.read(), doc)

    def test_orphan_is_flagged_while_a_sibling_app_is_still_pruned(self):
        doc = source([
            app("Stremio", "com.stremio.pal",
                [version("2.0.6", "21", url("2.0.6b21")),
                 version("2.0.0", "11", url("2.0.0b11"))]),
            app("Stremio Lite", "com.stremio.ios", [version("1.3.6", "7", url("1.3.6b7"))]),
        ])
        self.write(copy.deepcopy(doc))
        self.serve({"2.0.0b11": 404, "1.3.6b7": 404})
        rc, out = self.run_prune("--apply")
        self.assertEqual(rc, 2, out)
        after = self.read()
        pal = [a for a in after["apps"] if a["bundleIdentifier"] == "com.stremio.pal"][0]
        lite = [a for a in after["apps"] if a["bundleIdentifier"] == "com.stremio.ios"][0]
        self.assertEqual([v["version"] for v in pal["versions"]], ["2.0.6"],
                         "the safe removal should still happen")
        self.assertEqual(len(lite["versions"]), 1, "the orphan must be left alone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
