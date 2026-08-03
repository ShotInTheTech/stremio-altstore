#!/usr/bin/env python3
"""
trim_versions.py — keep the version list useful instead of merely long

Nothing ever left these sources except builds Stremio pulled, so the list
grows by roughly one entry a week forever. Signing apps render it as a
version picker, and a picker with fifty near-identical 2.0.x rows is worse
than one with a dozen.

Trimming is a real loss, though: a version removed here is one a user can no
longer install from this source, even though the IPA still exists. So the
rule is not simply "keep the newest N" — a build is also kept whenever it is
the last remaining option for someone:

  * the newest --keep builds, which covers downgrading after a bad release;
  * the newest build of each distinct minOSVersion, so a device stuck on an
    older OS never loses its last compatible option (today every build wants
    the same OS, but a future release raising the floor would strand those
    devices otherwise);
  * the newest build of each minor line (1.3.x, 2.0.x, 2.1.x), so an entire
    release line never disappears at once.

Report-only by default, like every other destructive step here.

Usage:
    python3 scripts/trim_versions.py              # report what would go
    python3 scripts/trim_versions.py --apply      # actually remove them
    python3 scripts/trim_versions.py --keep 20    # widen the window

Exit codes:
    0 — nothing to trim, or trimmed successfully
    1 — versions are trimmable (report mode; nothing written)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES = ["stremio-ios.json", "stremio-tvos.json"]
DEFAULT_KEEP = 12


def _version_key(v: dict) -> tuple:
    parts = [int(c) if str(c).isdigit() else -1 for c in str(v.get("version", "")).split(".")]
    try:
        build = int(v.get("buildVersion", 0))
    except (TypeError, ValueError):
        build = 0
    return (parts, build)


def _minor_line(v: dict) -> str:
    return ".".join(str(v.get("version", "")).split(".")[:2])


def plan(versions: list[dict], keep: int) -> tuple[list[dict], list[dict], dict[int, str]]:
    """Split versions into (kept, dropped, why-kept-by-id)."""
    ordered = sorted([v for v in versions if isinstance(v, dict)], key=_version_key, reverse=True)
    reasons: dict[int, str] = {}

    for v in ordered[:keep]:
        reasons.setdefault(id(v), f"among the {keep} newest")

    for label, attr in (("last for minOS {}", "minOSVersion"), ("last of the {} line", None)):
        seen: set[str] = set()
        for v in ordered:
            bucket = str(v.get(attr)) if attr else _minor_line(v)
            if bucket in seen:
                continue
            seen.add(bucket)
            reasons.setdefault(id(v), label.format(bucket))

    kept = [v for v in ordered if id(v) in reasons]
    dropped = [v for v in ordered if id(v) not in reasons]
    return kept, dropped, reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Remove the trimmable versions")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help=f"How many newest builds to always keep (default: {DEFAULT_KEEP})")
    args = ap.parse_args()

    if args.keep < 1:
        print("[ERROR] --keep must be at least 1", file=sys.stderr)
        return 1

    total_dropped = 0
    changed: set[str] = set()
    sources = {f: json.loads((REPO / f).read_text(encoding="utf-8")) for f in SOURCES}

    for fname, data in sources.items():
        for app in data.get("apps", []):
            versions = app.get("versions", [])
            kept, dropped, reasons = plan(versions, args.keep)
            label = f"{fname}: {app.get('name')}"
            if not dropped:
                print(f"  [ok  ] {label}: {len(kept)} version(s), nothing to trim")
                continue

            total_dropped += len(dropped)
            changed.add(fname)
            print(f"  [TRIM] {label}: keeping {len(kept)}, dropping {len(dropped)}")
            for v in dropped:
                print(f"           - {v.get('version')} build {v.get('buildVersion')} "
                      f"({v.get('date')})")
            # Show what the protective rules saved, not just the count.
            for v in kept:
                why = reasons[id(v)]
                if not why.startswith("among the"):
                    print(f"           ~ kept {v.get('version')} build {v.get('buildVersion')}"
                          f" — {why}")
            if args.apply:
                app["versions"] = kept

        if args.apply and fname in changed:
            (REPO / fname).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
            print(f"[WRITE] {fname} updated")

    if not total_dropped:
        print("[OK] Every listed version still earns its place.")
        return 0
    if not args.apply:
        print(f"\n{total_dropped} version(s) could be trimmed. Re-run with --apply to remove them.")
        return 1
    print(f"[DONE] Trimmed {total_dropped} version(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
