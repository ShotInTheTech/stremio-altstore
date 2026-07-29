#!/usr/bin/env python3
"""
prune_dead.py — find (and optionally remove) versions whose IPA is gone

When Stremio pulls an old build from the CDN, our JSON keeps pointing at it
and users just get "failed to download". This script HEAD-checks every listed
downloadURL and separates the two kinds of problem:

  * Mechanical — an old build was pulled while the app still has other
    working versions. Safe to remove automatically.
  * Judgement — every version of an app is gone, or something looks
    CDN-wide. Removing a whole app (or reacting to an outage) is a product
    decision, so these are only flagged, never auto-applied.

Removing entries reaches users within minutes of the push, so the checks are
deliberately conservative:

  * Only 404/410 counts as dead. Timeouts, 5xx and connection errors are
    reported as UNKNOWN and never pruned — a flaky network must not delete
    a perfectly good release.
  * Every suspected 404 is re-checked (--retries) before it counts.
  * If the newest version in a source is gone, nothing is pruned: that is a
    CDN-wide signal, not an individual pull.
  * If more than --max-prune versions look dead at once, nothing is pruned.
  * An app is never left with zero versions, and an app is never deleted.

Usage:
    python3 scripts/prune_dead.py                 # report only (safe default)
    python3 scripts/prune_dead.py --apply         # remove the safe cases
    python3 scripts/prune_dead.py --max-prune 5   # raise the safety cap
    python3 scripts/prune_dead.py --retries 3

Exit codes:
    0 — everything healthy, or the dead entries were pruned cleanly
    1 — dead entries found in report mode (nothing written)
    2 — needs a human: an app has no live versions left, the newest version
        is gone, or the safety cap was exceeded. Any safe prunes still ran.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ipa_plist import http_request  # noqa: E402  (shared HTTP helper)

SOURCES = ["stremio-ios.json", "stremio-tvos.json"]
DEFAULT_MAX_PRUNE = 3
DEFAULT_RETRIES = 2

# Statuses that mean "this file is genuinely gone", as opposed to
# "we could not reach it right now".
GONE_STATUSES = {404, 410}


def _version_key(v: dict) -> tuple:
    parts = [int(c) if c.isdigit() else -1 for c in str(v.get("version", "")).split(".")]
    try:
        build = int(v.get("buildVersion", 0))
    except (TypeError, ValueError):
        build = 0
    return (parts, build)


def _probe(url: str, retries: int) -> tuple[str, object]:
    """Returns ("live"|"dead"|"unknown", last_status)."""
    last = None
    for attempt in range(retries + 1):
        resp = http_request(url, method="HEAD", timeout=20)
        last = resp.status
        if last == 200:
            return ("live", last)
        if last not in GONE_STATUSES:
            continue  # transient-looking: retry, but never conclude "dead"
        if attempt == retries:
            return ("dead", last)
    return ("dead" if last in GONE_STATUSES else "unknown", last)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Remove the safe dead versions (default: report only)")
    ap.add_argument("--max-prune", type=int, default=DEFAULT_MAX_PRUNE,
                    help=f"Prune nothing if more than N look dead (default: {DEFAULT_MAX_PRUNE})")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"Re-checks before calling a URL dead (default: {DEFAULT_RETRIES})")
    args = ap.parse_args()

    sources = {f: json.loads((REPO / f).read_text(encoding="utf-8")) for f in SOURCES}

    dead: list[dict] = []
    unknown: list[dict] = []
    source_newest_dead: list[str] = []
    checked = 0

    print("=== Checking every listed IPA ===")
    for fname, data in sources.items():
        apps = data.get("apps", [])
        all_versions = [v for a in apps for v in a.get("versions", [])]
        source_newest = max(all_versions, key=_version_key) if all_versions else None

        for app in apps:
            for v in app.get("versions", []):
                url = v.get("downloadURL")
                if not url:
                    continue
                checked += 1
                state, status = _probe(url, args.retries)
                label = f"{app.get('name', '?')} {v.get('version')} build {v.get('buildVersion')}"
                if state == "live":
                    print(f"  [ok  ] {fname}: {label}")
                    continue
                print(f"  [{'DEAD' if state == 'dead' else '????'}] {fname}: {label} → HTTP {status}")
                rec = {"file": fname, "app": app, "version": v, "label": label, "status": status}
                if state == "dead":
                    if v is source_newest:
                        source_newest_dead.append(f"{fname}: {label}")
                    dead.append(rec)
                else:
                    unknown.append(rec)

    live = checked - len(dead) - len(unknown)
    print(f"\nChecked {checked} versions: {len(dead)} dead, {len(unknown)} unreachable, {live} live.")
    for rec in unknown:
        print(f"[WARN] Unreachable, left untouched — {rec['file']}: {rec['label']} (HTTP {rec['status']})")

    if not dead:
        print("[OK] No dead versions.")
        return 0

    # ---- global safety gates: prune nothing at all ---------------------
    if source_newest_dead:
        print("\n[CRITICAL] The newest version in a source is gone from the CDN:")
        for d in source_newest_dead:
            print(f"  - {d}")
        print("Pruning nothing — this looks CDN-wide rather than an individual pull.")
        return 2
    if len(dead) > args.max_prune:
        print(f"\n[CRITICAL] {len(dead)} versions look dead, over the safety cap of {args.max_prune}.")
        print("Pruning nothing — a mass 404 usually means the CDN layout changed.")
        return 2

    # ---- split into mechanical vs judgement ----------------------------
    dead_ids = {id(r["version"]) for r in dead}
    prunable, orphaned = [], []
    for rec in dead:
        remaining = [v for v in rec["app"].get("versions", []) if id(v) not in dead_ids]
        (prunable if remaining else orphaned).append(rec)

    if orphaned:
        print("\n[ACTION NEEDED] These apps have no working versions left:")
        for rec in orphaned:
            print(f"  - {rec['file']}: {rec['app'].get('name')} "
                  f"({rec['app'].get('bundleIdentifier')}) — only {rec['label']}, and it is gone")
        print("Not removing them automatically: dropping a whole app from a published")
        print("source is a product decision. Remove the app entry by hand, or wait in")
        print("case Stremio restores the build.")

    if not args.apply:
        if prunable:
            print("\nDead versions that can be pruned (re-run with --apply):")
            for rec in prunable:
                print(f"  - {rec['file']}: {rec['label']}")
        return 2 if orphaned else 1

    # ---- apply ---------------------------------------------------------
    changed: set[str] = set()
    for rec in prunable:
        app, v = rec["app"], rec["version"]
        app["versions"] = [x for x in app["versions"] if x is not v]
        changed.add(rec["file"])
        print(f"[PRUNE] {rec['file']}: removed {rec['label']}")

    for fname in sorted(changed):
        (REPO / fname).write_text(
            json.dumps(sources[fname], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[WRITE] {fname} updated")

    print(f"[DONE] Pruned {len(prunable)} dead version(s).")
    return 2 if orphaned else 0


if __name__ == "__main__":
    sys.exit(main())
