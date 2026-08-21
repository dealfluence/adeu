#!/usr/bin/env python3
"""Fetch the content-controls test corpus from official government sources.

The corpus documents are real public-sector files that are deliberately NOT
committed (see specs/content-controls/spec-corpus.md). This script downloads
whatever is missing into shared/corpus/ (or $ADEU_CORPUS_DIR), verifies each
file is a real OOXML package (PK zip magic), and warns on sha256/size drift
against the manifest snapshot without failing the fetch.

Stdlib only. Exit 0 iff every requested document is present on disk afterwards.

Usage:
    python scripts/fetch_corpus.py [--list] [--force] [--only KEY[,KEY...]]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "shared" / "corpus" / "manifest.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def corpus_dir() -> Path:
    override = os.environ.get("ADEU_CORPUS_DIR")
    return Path(override) if override else REPO_ROOT / "shared" / "corpus"


def load_manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manual_instructions(key: str, entry: dict, dest: Path) -> str:
    lines = [f"  MANUAL FETCH NEEDED for '{key}':"]
    if entry.get("url"):
        lines.append(f"    1. Open in a browser: {entry['url']}")
    if entry.get("landing_page"):
        lines.append(f"       (landing page: {entry['landing_page']})")
    if not entry.get("url"):
        lines.append(f"    1. Locate \"{entry.get('title', key)}\" on the {entry.get('org', 'issuer')} website")
    lines.append(f"    2. Save the file as: {dest}")
    return "\n".join(lines)


def fetch_one(key: str, entry: dict, dest: Path, force: bool) -> tuple[bool, str]:
    """Returns (present_on_disk, message)."""
    if dest.exists() and not force:
        drift = ""
        try:
            actual = sha256_of(dest)
            if entry.get("sha256") and actual != entry["sha256"]:
                drift = " [WARN: sha256 differs from manifest snapshot — upstream revision or manual copy; refresh manifest if intended]"
        except OSError as exc:  # unreadable file: treat as missing
            return False, f"unreadable ({exc})"
        return True, f"present{drift}"

    url = entry.get("url")
    if not url:
        return dest.exists(), "no automated URL — manual fetch required"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return dest.exists(), f"download failed ({exc}) — likely bot protection; fetch manually"

    if not data.startswith(b"PK\x03\x04"):
        return dest.exists(), (
            "response is not an OOXML package (got "
            f"{data[:32]!r}...) — the source likely served an HTML page; fetch manually"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, dest)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    msg = f"fetched {len(data):,} bytes"
    if entry.get("sha256") and sha256_of(dest) != entry["sha256"]:
        msg += " [WARN: sha256 differs from manifest snapshot — upstream revision; refresh manifest]"
    elif entry.get("bytes") and len(data) != entry["bytes"]:
        msg += " [WARN: size differs from manifest snapshot]"
    return True, msg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="show manifest keys and on-disk status, fetch nothing")
    parser.add_argument("--force", action="store_true", help="re-download even when present")
    parser.add_argument("--only", metavar="KEY[,KEY...]", help="restrict to these manifest keys")
    args = parser.parse_args()

    manifest = load_manifest()
    docs: dict = manifest["documents"]
    keys = list(docs)
    if args.only:
        requested = [k.strip() for k in args.only.split(",") if k.strip()]
        unknown = [k for k in requested if k not in docs]
        if unknown:
            print(f"error: unknown manifest key(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"known keys: {', '.join(keys)}", file=sys.stderr)
            return 2
        keys = requested

    cdir = corpus_dir()
    if args.list:
        for key in keys:
            dest = cdir / docs[key]["file"]
            status = "present" if dest.exists() else "missing"
            auto = "auto" if docs[key].get("url") else "manual-only"
            print(f"{key:32} {status:8} {auto:12} {dest.name}")
        return 0

    all_present = True
    manual_needed: list[str] = []
    for key in keys:
        entry = docs[key]
        dest = cdir / entry["file"]
        present, message = fetch_one(key, entry, dest, args.force)
        print(f"{key:32} {message}")
        if not present:
            all_present = False
            manual_needed.append(manual_instructions(key, entry, dest))

    if manual_needed:
        print("\nSome documents need a manual download (bot protection or no automated URL):")
        for block in manual_needed:
            print(block)
        print("\nRe-run this script afterwards to verify; tests skip missing documents cleanly.")

    return 0 if all_present else 1


if __name__ == "__main__":
    sys.exit(main())
