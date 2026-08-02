#!/usr/bin/env python3
"""
fetch_screenshots.py — give the source entries actual screenshots

Our apps ship with an empty `screenshots` array, so they render as a bare
name and icon in signing apps while other sources show a proper gallery.
Stremio's own AltStore source carries the real App Store screenshots, keyed
by device, and those are just image URLs — usable as-is.

Unlike release notes, screenshots are stable rather than a rolling window,
so this runs with the weekly audit instead of every six hours.

Note on tvOS: upstream only publishes iphone and ipad screenshots. Putting
phone screenshots on an Apple TV entry would misrepresent the app, so the
tvOS source is deliberately left without any.

The URLs are third-party input that signing apps will load, so every one is
checked to be https before it is accepted.

Usage:
    python3 scripts/fetch_screenshots.py             # capture
    python3 scripts/fetch_screenshots.py --dry-run   # preview

Exit codes:
    0 — done (or nothing to do)
    2 — upstream could not be fetched or parsed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ipa_plist import http_request  # noqa: E402

UPSTREAM = "https://dl.strem.io/apple/altstore/source.json"

# Which upstream device buckets belong in which of our sources. tvOS is absent
# on purpose: upstream has no Apple TV screenshots and phone ones would lie.
DEVICE_KEYS = {"stremio-ios.json": ("iphone", "ipad")}
MAX_PER_DEVICE = 8


def _https(url: object) -> bool:
    return isinstance(url, str) and urlparse(url).scheme == "https" and bool(urlparse(url).netloc)


def clean_bucket(items: object) -> list:
    """Keep entries that are a plain https URL or a {imageURL,...} object."""
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:MAX_PER_DEVICE]:
        if _https(item):
            out.append(item)
        elif isinstance(item, dict) and _https(item.get("imageURL")):
            entry = {"imageURL": item["imageURL"]}
            for dim in ("width", "height"):
                if isinstance(item.get(dim), int):
                    entry[dim] = item[dim]
            out.append(entry)
    return out


def upstream_screenshots() -> dict[str, dict]:
    """Map bundleIdentifier -> {device: [screenshot, ...]}."""
    resp = http_request(UPSTREAM, timeout=20)
    if resp.status != 200 or not resp.body:
        raise RuntimeError(f"fetch failed: HTTP {resp.status}")
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"upstream is not valid JSON: {e}") from e

    found: dict[str, dict] = {}
    for app in data.get("apps", []):
        bundle = app.get("bundleIdentifier")
        shots = app.get("screenshots")
        if not bundle or not isinstance(shots, dict):
            continue
        cleaned = {dev: clean_bucket(items) for dev, items in shots.items()}
        found[bundle] = {dev: items for dev, items in cleaned.items() if items}
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = ap.parse_args()

    try:
        available = upstream_screenshots()
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    print(f"[INFO] Upstream publishes screenshots for {len(available)} app(s).")

    updated = 0
    for fname, devices in DEVICE_KEYS.items():
        path = REPO / fname
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = False

        for app in data.get("apps", []):
            shots = available.get(app.get("bundleIdentifier"))
            if not shots:
                continue
            wanted = {dev: shots[dev] for dev in devices if dev in shots}
            if not wanted or app.get("screenshots") == wanted:
                continue
            counts = ", ".join(f"{dev}: {len(v)}" for dev, v in wanted.items())
            print(f"  [SET] {fname}: {app.get('name')} → {counts}")
            if not args.dry_run:
                app["screenshots"] = wanted
            changed = True
            updated += 1

        if changed and not args.dry_run:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            print(f"[WRITE] {fname} updated")

    skipped = [f for f in ("stremio-tvos.json",) if f not in DEVICE_KEYS]
    for f in skipped:
        print(f"[SKIP ] {f}: upstream has no Apple TV screenshots; "
              f"phone ones would misrepresent the app")

    if not updated:
        print("[OK] Screenshots already up to date.")
    elif args.dry_run:
        print(f"[DRY-RUN] {updated} app(s) would change.")
    else:
        print(f"[DONE] Updated screenshots for {updated} app(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
