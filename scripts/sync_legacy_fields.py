#!/usr/bin/env python3
"""
sync_legacy_fields.py — mirror the newest build into the legacy app-level fields

The AltStore source format has two generations. Modern clients read the
`versions` array on each app. Older ones (AltStore Classic and several forks)
read flat fields on the app itself:

    version, versionDate, versionDescription, downloadURL, size, minOSVersion

We only published the modern array, so those clients had no release notes to
show and fell back to rendering something like "Stremio 2.0.6 build 21" —
which is what users actually saw in the app, even though the real changelog
was sitting in the JSON one level down.

This copies the newest version of each app up into the legacy fields, so both
generations see the same build with the same notes. It must run after
anything that changes `versions` (the updater, the prune, the notes capture),
and validate_source.py enforces that the two stay in agreement — old and new
clients pointing at different builds would be worse than not shipping the
legacy fields at all.

Usage:
    python3 scripts/sync_legacy_fields.py             # write
    python3 scripts/sync_legacy_fields.py --dry-run   # show what would change

Exit codes:
    0 — in sync (or successfully synced)
    1 — --dry-run and something is out of sync
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES = ["stremio-ios.json", "stremio-tvos.json"]

# app-level legacy key -> key on the version object
LEGACY_MAP = {
    "version": "version",
    "versionDate": "date",
    "versionDescription": "localizedDescription",
    "downloadURL": "downloadURL",
    "size": "size",
    "minOSVersion": "minOSVersion",
}


def newest_version(app: dict) -> dict | None:
    versions = [v for v in app.get("versions", []) if isinstance(v, dict)]
    if not versions:
        return None

    def key(v: dict) -> tuple:
        parts = [int(c) if str(c).isdigit() else -1
                 for c in str(v.get("version", "")).split(".")]
        try:
            build = int(v.get("buildVersion", 0))
        except (TypeError, ValueError):
            build = 0
        return (parts, build)

    return max(versions, key=key)


def legacy_payload(v: dict) -> dict:
    return {legacy: v.get(src) for legacy, src in LEGACY_MAP.items() if v.get(src) is not None}


def sync_app(app: dict) -> list[str]:
    """Apply the legacy mirror; returns the names of fields that changed."""
    v = newest_version(app)
    if v is None:
        return []
    changed = []
    for key, value in legacy_payload(v).items():
        if app.get(key) != value:
            app[key] = value
            changed.append(key)
    # Legacy clients look for screenshotURLs, which is a flat list of URLs —
    # the modern field is keyed by device and may hold {imageURL,...} objects,
    # so it has to be flattened rather than copied across.
    urls = flatten_screenshots(app.get("screenshots"))
    if urls and app.get("screenshotURLs") != urls:
        app["screenshotURLs"] = urls
        changed.append("screenshotURLs")
    return changed


def flatten_screenshots(shots: object) -> list[str]:
    """Modern `screenshots` (device-keyed, possibly objects) -> plain URL list."""
    buckets: list = []
    if isinstance(shots, dict):
        # Phone shots lead: the legacy flat gallery has no device context, and
        # it is overwhelmingly viewed on a phone. Ordering is fixed rather than
        # dict order so the mirror does not churn between runs.
        def rank(device: str) -> tuple:
            priority = {"iphone": 0, "ipad": 1, "appletv": 2, "tv": 2}
            return (priority.get(device.lower(), 9), device.lower())

        for device in sorted(shots, key=rank):
            items = shots[device]
            if isinstance(items, list):
                buckets.extend(items)
    elif isinstance(shots, list):
        buckets = shots

    urls: list[str] = []
    for item in buckets:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict) and isinstance(item.get("imageURL"), str):
            urls.append(item["imageURL"])
    return urls


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = ap.parse_args()

    out_of_sync = 0
    for fname in SOURCES:
        path = REPO / fname
        data = json.loads(path.read_text(encoding="utf-8"))
        file_changed = False

        for app in data.get("apps", []):
            v = newest_version(app)
            if v is None:
                print(f"  [SKIP] {fname}: {app.get('name')} has no versions")
                continue
            changed = sync_app(app)
            label = f"{app.get('name')} {v.get('version')} build {v.get('buildVersion')}"
            if changed:
                out_of_sync += 1
                file_changed = True
                print(f"  [SYNC] {fname}: {label} → {', '.join(changed)}")
            else:
                print(f"  [ok  ] {fname}: {label} already mirrored")

        if file_changed and not args.dry_run:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            print(f"[WRITE] {fname} updated")

    if not out_of_sync:
        print("[OK] Legacy fields already match the newest build.")
        return 0
    if args.dry_run:
        print(f"[DRY-RUN] {out_of_sync} app(s) would be updated.")
        return 1
    print(f"[DONE] Mirrored the newest build into {out_of_sync} app(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
