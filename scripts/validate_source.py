#!/usr/bin/env python3
"""
validate_source.py — last gate before a source is published

Five different scripts now write to the JSON sources (the updater, the hash
backfill, the dead-version prune, the metadata repair, plus anything done by
hand), and whatever they produce reaches users within minutes of the push.
This checks that the result is still a valid, safe AltStore source, and is
meant to run immediately before the commit step in CI: if it fails, nothing
is published.

It deliberately checks more than "is this JSON": the invariants below are the
ones that would actually break a signing app or mislead a user.

  * Structure — the keys AltStore-format consumers require, with the right
    types (buildVersion must be a string, size a positive int, ...).
  * Identity — no duplicate bundle identifiers in one source, no duplicate
    version+build inside an app, and sourceURL pointing at this very file
    (a swapped pair would make one platform serve the other's apps).
  * Safety — every downloadURL must be https and on the expected CDN host.
    A source that sends people to an arbitrary host to download an
    unsigned IPA is the worst thing this repo could ship.
  * Sanity — plausible sizes and dates, well-formed sha256 hashes; catches
    a corrupted write such as "size": 1.

Warnings (missing hash, unsorted versions, no screenshots) are printed but
do not fail the run.

Usage:
    python3 scripts/validate_source.py                     # both sources
    python3 scripts/validate_source.py stremio-ios.json     # one file
    python3 scripts/validate_source.py --strict             # warnings fail too

Exit codes:
    0 — valid (warnings may be present)
    1 — invalid: do not publish
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
SOURCES = ["stremio-ios.json", "stremio-tvos.json"]

# IPAs must come from Stremio's own CDN. Anything else in a sideloading
# source means users would be downloading an unsigned app from somewhere
# nobody vetted.
ALLOWED_IPA_HOSTS = {"dl.strem.io"}

BUNDLE_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z0-9-]+$")
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MIN_IPA_BYTES = 1 * 1024 * 1024        # 1 MB — anything smaller is corruption
MAX_IPA_BYTES = 500 * 1024 * 1024      # 500 MB — anything larger is suspicious

# Release notes come from upstream; the harvester caps them at 4000, so this
# leaves headroom while still refusing anything absurd.
MAX_DESCRIPTION_CHARS = 6000
# Anything but tab and newline: a changelog keeps its line breaks, nothing else.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


def _version_key(v: dict) -> tuple:
    parts = [int(c) if str(c).isdigit() else -1 for c in str(v.get("version", "")).split(".")]
    try:
        build = int(v.get("buildVersion", 0))
    except (TypeError, ValueError):
        build = 0
    return (parts, build)


def _check_url(rep: Report, where: str, url: object, *, field: str,
               hosts: set[str] | None = None) -> None:
    if not isinstance(url, str) or not url:
        rep.error(where, f"{field} is missing or not a string")
        return
    parsed = urlparse(url)
    if parsed.scheme != "https":
        rep.error(where, f"{field} must use https, got {parsed.scheme or 'no scheme'!r}")
    if not parsed.netloc:
        rep.error(where, f"{field} has no host: {url!r}")
    elif hosts is not None and parsed.netloc.lower() not in hosts:
        rep.error(where, f"{field} host {parsed.netloc!r} is not allowed "
                         f"(expected one of {sorted(hosts)})")


def validate_version(rep: Report, where: str, v: object) -> None:
    if not isinstance(v, dict):
        rep.error(where, "version entry is not an object")
        return

    ver = v.get("version")
    if not isinstance(ver, str) or not VERSION_RE.match(ver or ""):
        rep.error(where, f"version must look like 1.2.3, got {ver!r}")

    build = v.get("buildVersion")
    # A number here silently breaks consumers that compare it as text.
    if not isinstance(build, str) or not build.strip():
        rep.error(where, f"buildVersion must be a non-empty string, got {build!r}")

    d = v.get("date")
    if not isinstance(d, str) or not DATE_RE.match(d or ""):
        rep.error(where, f"date must be YYYY-MM-DD, got {d!r}")
    else:
        try:
            parsed_date = datetime.strptime(d, "%Y-%m-%d").date()
            if parsed_date > date.today():
                rep.warn(where, f"date {d} is in the future")
        except ValueError:
            rep.error(where, f"date {d!r} is not a real date")

    _check_url(rep, where, v.get("downloadURL"), field="downloadURL", hosts=ALLOWED_IPA_HOSTS)

    size = v.get("size")
    if not isinstance(size, int) or isinstance(size, bool):
        rep.error(where, f"size must be an integer, got {size!r}")
    elif size < MIN_IPA_BYTES:
        rep.error(where, f"size {size} is implausibly small — likely a corrupted write")
    elif size > MAX_IPA_BYTES:
        rep.error(where, f"size {size} is implausibly large")

    if not isinstance(v.get("minOSVersion"), str) or not v.get("minOSVersion"):
        rep.error(where, "minOSVersion is missing or not a string")

    # Release notes are harvested from Stremio's own source, i.e. third-party
    # text that signing apps will render. Keep it a plausible string.
    desc = v.get("localizedDescription")
    if desc is not None:
        if not isinstance(desc, str):
            rep.error(where, f"localizedDescription must be a string, got {type(desc).__name__}")
        elif len(desc) > MAX_DESCRIPTION_CHARS:
            rep.error(where, f"localizedDescription is {len(desc)} chars, "
                             f"over the {MAX_DESCRIPTION_CHARS} limit")
        elif CONTROL_CHARS_RE.search(desc):
            rep.error(where, "localizedDescription contains control characters")

    sha = v.get("sha256")
    if sha is None:
        rep.warn(where, "no sha256 yet (the backfill fills these in over time)")
    elif not isinstance(sha, str) or not SHA256_RE.match(sha):
        rep.error(where, f"sha256 must be 64 lowercase hex chars, got {sha!r}")


def validate_app(rep: Report, where: str, app: object) -> str | None:
    if not isinstance(app, dict):
        rep.error(where, "app entry is not an object")
        return None

    name = app.get("name")
    if not isinstance(name, str) or not name.strip():
        rep.error(where, f"app name is missing or empty, got {name!r}")
    label = name if isinstance(name, str) and name else "<unnamed>"
    where = f"{where} · {label}"

    bundle = app.get("bundleIdentifier")
    if not isinstance(bundle, str) or not BUNDLE_RE.match(bundle or ""):
        rep.error(where, f"bundleIdentifier must be reverse-DNS, got {bundle!r}")

    versions = app.get("versions")
    if not isinstance(versions, list) or not versions:
        # This is exactly the empty shell the updater used to re-create for an
        # app whose builds were all pulled — it shows up broken in signing apps.
        rep.error(where, "app has no versions; it must not be published")
        return bundle if isinstance(bundle, str) else None

    seen: dict[tuple, int] = {}
    for i, v in enumerate(versions):
        vwhere = f"{where} · version[{i}]"
        validate_version(rep, vwhere, v)
        if isinstance(v, dict):
            key = (str(v.get("version")), str(v.get("buildVersion")))
            if key in seen:
                rep.error(vwhere, f"duplicate of version[{seen[key]}] ({key[0]} build {key[1]})")
            else:
                seen[key] = i

    ordered = sorted([v for v in versions if isinstance(v, dict)], key=_version_key, reverse=True)
    if ordered != [v for v in versions if isinstance(v, dict)]:
        rep.warn(where, "versions are not newest-first; some signing apps show the first entry")

    if not app.get("screenshots"):
        rep.warn(where, "no screenshots — the entry looks bare in signing apps")

    return bundle if isinstance(bundle, str) else None


def validate_source(rep: Report, path: Path) -> None:
    where = path.name
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        rep.error(where, f"cannot read file: {e}")
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        rep.error(where, f"not valid JSON: {e}")
        return
    if not isinstance(data, dict):
        rep.error(where, "top level must be an object")
        return

    for key in ("name", "identifier"):
        if not isinstance(data.get(key), str) or not data.get(key, "").strip():
            rep.error(where, f"{key} is missing or empty")

    # sourceURL must name this very file: if the two sources were ever swapped,
    # one platform would silently serve the other's apps.
    src = data.get("sourceURL")
    _check_url(rep, where, src, field="sourceURL")
    if isinstance(src, str) and src and not src.rstrip("/").endswith(path.name):
        rep.error(where, f"sourceURL points at {src.rsplit('/', 1)[-1]!r}, expected {path.name!r}")

    for key in ("iconURL", "headerURL", "website"):
        if data.get(key) is not None:
            _check_url(rep, where, data.get(key), field=key)

    news = data.get("news")
    if news is not None and not isinstance(news, list):
        rep.error(where, "news must be a list when present")

    apps = data.get("apps")
    if not isinstance(apps, list) or not apps:
        rep.error(where, "apps must be a non-empty list")
        return

    bundles: dict[str, int] = {}
    for i, app in enumerate(apps):
        bundle = validate_app(rep, f"{where} · apps[{i}]", app)
        if bundle:
            if bundle in bundles:
                # Most signing apps refuse a source that lists one bundle id twice —
                # the very reason this repo splits iOS and tvOS into two files.
                rep.error(where, f"bundleIdentifier {bundle!r} appears in apps[{bundles[bundle]}] "
                                 f"and apps[{i}]; signing apps reject duplicates")
            else:
                bundles[bundle] = i


def main() -> int:
    argv = sys.argv[1:]
    strict = "--strict" in argv
    argv = [a for a in argv if a != "--strict"]

    paths = [Path(a) for a in argv] if argv else [REPO / f for f in SOURCES]
    paths = [p if p.is_absolute() else REPO / p for p in paths]

    rep = Report()
    for p in paths:
        validate_source(rep, p)

    for w in rep.warnings:
        print(f"[WARN ] {w}")
    for e in rep.errors:
        print(f"[ERROR] {e}")

    checked = ", ".join(p.name for p in paths)
    print(f"\n=== Validation summary ===")
    print(f"Checked: {checked}")
    print(f"Errors: {len(rep.errors)}, warnings: {len(rep.warnings)}")

    if rep.errors:
        print("\nINVALID — refusing to publish. Fix the errors above.")
        return 1
    if strict and rep.warnings:
        print("\nWarnings present and --strict was given.")
        return 1
    print("Sources are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
