#!/usr/bin/env python3
"""
build_news.py — turn captured changelogs into an in-app news feed

Both sources carry an empty `news` array, and signing apps render that array
as a feed inside the app. We already collect a real changelog per release, so
this converts them into news items: a user opens AltStore and sees that a new
Stremio build landed and what changed, instead of having to notice a version
number went up.

Two things drive the design, both about not being obnoxious:

  * `notify: true` sends a push notification. Item identifiers are therefore
    derived only from the release itself, so they are stable across runs and
    a client notifies at most once. Regenerating the feed must never look
    like a batch of brand new stories.
  * Exactly one item — the newest release, and only while it is still recent
    — is allowed to notify. A backfill of ten historical releases must not
    fire ten notifications at somebody who just added the source.

Only releases with a real changelog get an item; "Stremio 2.0.4 (build 19)."
is not news. Older releases predating changelog capture simply have none.

Usage:
    python3 scripts/build_news.py             # rewrite the news arrays
    python3 scripts/build_news.py --dry-run   # show what would change

Exit codes:
    0 — feed already correct, or rewritten
    1 — --dry-run and the feed is out of date
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES = {
    "stremio-ios.json": "iPhone & iPad",
    "stremio-tvos.json": "Apple TV",
}

TINT = "7055D9"          # AltStore's own feed omits the leading '#'
MAX_ITEMS = 12           # the feed is a highlight reel, not an archive
# A push is only justified for a genuinely fresh release. The updater runs
# every 6 hours, so a new build is captured within hours — well inside this
# window. Anything older is something users already have, and waking their
# phone for it (including the first time this feed is published) is noise.
NOTIFY_WINDOW_DAYS = 7
MAX_CAPTION = 140

# What merge_version() writes when it has no real changelog to record.
PLACEHOLDER_RE = re.compile(r"^\s*.+\s+\d+(\.\d+)*\s+\(build\s+\d+\)\.\s*$")
SLUG_RE = re.compile(r"[^a-z0-9]+")


def is_real_changelog(text: object) -> bool:
    return (isinstance(text, str) and bool(text.strip())
            and not PLACEHOLDER_RE.match(text))


def _version_key(v: dict) -> tuple:
    parts = [int(c) if str(c).isdigit() else -1 for c in str(v.get("version", "")).split(".")]
    try:
        build = int(v.get("buildVersion", 0))
    except (TypeError, ValueError):
        build = 0
    return (parts, build)


def caption_from(changelog: str) -> str:
    """First meaningful line of the changelog, trimmed to a caption."""
    for line in changelog.splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= MAX_CAPTION else line[:MAX_CAPTION - 1].rstrip() + "…"
    return ""


def _slug(text: str) -> str:
    return SLUG_RE.sub("-", text.lower()).strip("-")


def build_items(data: dict, platform_label: str, *, today: date) -> list[dict]:
    """News items for one source, newest first."""
    candidates: list[tuple[tuple, dict, dict]] = []
    for app in data.get("apps", []):
        for v in app.get("versions", []):
            if is_real_changelog(v.get("localizedDescription")):
                candidates.append((_version_key(v), app, v))
    candidates.sort(key=lambda c: c[0], reverse=True)

    cutoff = today - timedelta(days=NOTIFY_WINDOW_DAYS)
    items: list[dict] = []
    for index, (_key, app, v) in enumerate(candidates[:MAX_ITEMS]):
        version, build = v.get("version"), v.get("buildVersion")
        try:
            released = datetime.strptime(str(v.get("date")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            released = None

        # Only the newest release may notify, and only while it is fresh.
        notify = index == 0 and released is not None and released >= cutoff

        item = {
            "title": f"{app.get('name', 'Stremio')} {version}",
            # Stable: derived from the release and platform, nothing else.
            "identifier": _slug(f"{app.get('bundleIdentifier', 'app')}-{version}-b{build}-"
                               f"{platform_label}"),
            "caption": caption_from(v["localizedDescription"]),
            "tintColor": TINT,
            "date": v.get("date"),
            "notify": notify,
        }
        if app.get("bundleIdentifier"):
            item["appID"] = app["bundleIdentifier"]   # tapping opens the app entry
        if app.get("iconURL"):
            item["imageURL"] = app["iconURL"]
        items.append(item)
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = ap.parse_args()

    today = date.today()
    stale = 0

    for fname, label in SOURCES.items():
        path = REPO / fname
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = build_items(data, label, today=today)

        if data.get("news") == items:
            print(f"  [ok  ] {fname}: {len(items)} item(s), already current")
            continue

        stale += 1
        notified = next((i["title"] for i in items if i["notify"]), None)
        print(f"  [SET ] {fname}: {len(items)} item(s)"
              + (f", notifying for {notified}" if notified else ", no notification"))
        for item in items:
            print(f"           · {item['title']} — {item['caption'][:60]}")

        if not args.dry_run:
            data["news"] = items
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            print(f"[WRITE] {fname} updated")

    if not stale:
        print("[OK] News feed already up to date.")
        return 0
    if args.dry_run:
        print(f"[DRY-RUN] {stale} source(s) would change.")
        return 1
    print(f"[DONE] Rebuilt the news feed in {stale} source(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
