#!/usr/bin/env python3
"""
fetch_release_notes.py — give each version its real changelog

Until now every entry carried a placeholder ("Stremio 2.0.4 (build 19)."),
so people picking a version had nothing to go on. Stremio's own AltStore
source publishes a real changelog per release; we cannot use its download
URLs (they are the encrypted marketplace format this repo exists to work
around) but the release notes are perfectly usable.

The important detail: that source is a *rolling window* — it currently
carries only the newest couple of builds. Ours is the long-term archive.
So this runs on the normal 6-hourly cadence to capture each changelog while
it is still published, and never drops a note it has already captured.

Notes are third-party text landing in a file that signing apps render, so
they are sanitised: control characters stripped, line endings normalised,
runs of blank lines collapsed, and length capped.

Usage:
    python3 scripts/fetch_release_notes.py              # capture into both sources
    python3 scripts/fetch_release_notes.py --dry-run    # show what would change
    python3 scripts/fetch_release_notes.py --check      # exit 1 if any are missing

Exit codes:
    0 — done (or nothing to do)
    1 — --check mode and at least one version still has no real notes
    2 — the upstream source could not be fetched or parsed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ipa_plist import http_request  # noqa: E402  (shared HTTP helper)

SOURCES = ["stremio-ios.json", "stremio-tvos.json"]
UPSTREAM = "https://dl.strem.io/apple/altstore/source.json"

MAX_LEN = 4000
# Everything except tab and newline; keeps the changelog's line structure
# while dropping anything that could confuse a renderer.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
BLANKS_RE = re.compile(r"\n{3,}")

# What merge_version() writes when it has nothing better to say.
PLACEHOLDER_RE = re.compile(r"^\s*.+\s+\d+(\.\d+)*\s+\(build\s+\d+\)\.\s*$")


def sanitize(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = CONTROL_RE.sub("", t)
    t = BLANKS_RE.sub("\n\n", t)
    t = "\n".join(line.rstrip() for line in t.split("\n")).strip()
    if not t:
        return None
    if len(t) > MAX_LEN:
        t = t[:MAX_LEN].rstrip() + "\n\n(truncated)"
    return t


def is_placeholder(text: object) -> bool:
    return not isinstance(text, str) or not text.strip() or bool(PLACEHOLDER_RE.match(text))


def fetch_upstream_notes() -> dict[tuple[str, str], str]:
    """Map (version, buildVersion) -> sanitised changelog from Stremio's source."""
    resp = http_request(UPSTREAM, timeout=20)
    if resp.status != 200 or not resp.body:
        raise RuntimeError(f"fetch failed: HTTP {resp.status}")
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"upstream is not valid JSON: {e}") from e

    notes: dict[tuple[str, str], str] = {}
    for app in data.get("apps", []):
        for v in app.get("versions", []):
            text = sanitize(v.get("localizedDescription"))
            if not text or is_placeholder(text):
                continue
            key = (str(v.get("version", "")), str(v.get("buildVersion", "")))
            if key[0] and key[1]:
                notes[key] = text
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    ap.add_argument("--check", action="store_true",
                    help="Report coverage only; exit 1 if any version lacks real notes")
    args = ap.parse_args()

    sources = {f: json.loads((REPO / f).read_text(encoding="utf-8")) for f in SOURCES}

    if args.check:
        missing = [
            f"{f}: {a.get('name')} {v.get('version')} build {v.get('buildVersion')}"
            for f, d in sources.items()
            for a in d.get("apps", [])
            for v in a.get("versions", [])
            if is_placeholder(v.get("localizedDescription"))
        ]
        total = sum(len(a.get("versions", [])) for d in sources.values() for a in d.get("apps", []))
        print(f"[INFO] Release notes: {total - len(missing)}/{total} versions have real notes.")
        for m in missing:
            print(f"  [MISSING] {m}")
        return 1 if missing else 0

    try:
        notes = fetch_upstream_notes()
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    print(f"[INFO] Upstream currently publishes notes for {len(notes)} build(s): "
          f"{', '.join(f'{v}b{b}' for v, b in sorted(notes))}")

    changed_files: set[str] = set()
    updated = 0
    for fname, data in sources.items():
        for app in data.get("apps", []):
            for v in app.get("versions", []):
                key = (str(v.get("version", "")), str(v.get("buildVersion", "")))
                upstream = notes.get(key)
                if not upstream:
                    # Not in the rolling window any more — keep whatever we captured.
                    continue
                if v.get("localizedDescription") == upstream:
                    continue
                label = f"{fname}: {app.get('name')} {key[0]} build {key[1]}"
                first = "captured" if is_placeholder(v.get("localizedDescription")) else "refreshed"
                print(f"  [{first.upper():9}] {label} ({len(upstream)} chars)")
                if not args.dry_run:
                    v["localizedDescription"] = upstream
                changed_files.add(fname)
                updated += 1

    if not updated:
        print("[OK] Every version already carries the newest available notes.")
        return 0

    if args.dry_run:
        print(f"[DRY-RUN] {updated} description(s) would change (not written).")
        return 0

    for fname in sorted(changed_files):
        (REPO / fname).write_text(
            json.dumps(sources[fname], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[WRITE] {fname} updated")
    print(f"[DONE] {updated} description(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
