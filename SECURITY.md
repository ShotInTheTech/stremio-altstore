# Security Policy

## Supported versions

This repository is a **data/file collection** (JSON sources + Python scripts). "Version" here refers to JSON-tracked IPA references rather than software releases. Instead of a traditional "supported versions" list, the sources themselves are the
record: they are re-checked every six hours, and the install page reports when
that last succeeded.

| Component | Status |
|---|---|
| `stremio-ios.json` | ✅ Actively maintained |
| `stremio-tvos.json` | ✅ Actively maintained |
| `stremio-updater.py` | ✅ Actively maintained |
| Legacy IPA references | ⚠️ Available as long as Stremio keeps them on the CDN |

## Reporting a vulnerability

This repository does not execute code directly; it only collects data and produces JSON. However, if you discover a security issue or suspect something is wrong:

**Please DO NOT open a public Issue.** Instead:

1. Use **GitHub Security Advisories**:
   https://github.com/gorlev/stremio-altstore/security/advisories/new

2. Or contact the maintainer directly via the email on their GitHub profile.

In your report, please include:

- Affected component (e.g. `stremio-updater.py`, `stremio-ios.json`)
- Description of the issue and its potential impact
- Reproduction steps (PoC if available)
- Suggested fix (if any)

**Response time:** initial response within 7 days, assessment within 30 days.

## Known security notes

### Signature expiry

IPAs in this source are distributed with **Stremio's official Apple Developer signature**. When re-signed via any signing app (Feather, AltStore, ESign, etc.):

- With a free Apple ID: expires after **7 days**
- With a paid developer account: expires after **1 year**

If you don't re-sign, the app stops launching. This is Apple's rule, not a repo issue.

### CDN security

`dl.strem.io` belongs to Stremio. This repo only uses **publicly accessible** URLs. If Stremio takes down the CDN or changes its structure, the repo breaks — open an Issue in that case.

### What is enforced before anything is published

Several automated scripts write to these sources, and a push reaches users
within minutes, so `scripts/validate_source.py` runs as a gate before every
commit in CI. A failure fails the job and nothing is published. It refuses, among
other things:

- any `downloadURL` that is not **https on `dl.strem.io`** — a sideloading source
  that sends people to an arbitrary host for an unsigned IPA is the worst thing
  this repo could ship, so the host is an allowlist rather than a convention;
- a malformed `sha256`, an implausible file size, or a duplicate bundle
  identifier;
- an app published with no versions, or legacy fields describing a different
  build than the versions array.

Release notes and screenshots are copied from Stremio's own source; that text and
those URLs are treated as untrusted input — sanitised on the way in, length
capped, and rendered without HTML interpretation.

### Third-party dependencies

This repository has **zero third-party Python dependencies**. It uses only the Python 3.8+ standard library. You will not find `requirements.txt` or `Pipfile` — that's intentional.

### SHA256 verification

Every listed version carries a `sha256` of the IPA it points at. The hashes are
computed by downloading the file in full (`scripts/add_hashes.py`), a few builds
per run so the job stays within its time budget, and they are never recomputed
once recorded.

You can check any download yourself:

```bash
curl -sL "https://dl.strem.io/apple/2.0.6b21/ios/stremio_iOS.ipa" | shasum -a 256
# compare with the sha256 field for that version in stremio-ios.json
```

A mismatch means the file you received is not the one this source vouches for —
please report it.

## Acknowledgements

Thank you to any researchers who report security issues responsibly.
