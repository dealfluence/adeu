#!/usr/bin/env python3
"""Scan corpus documents for content-control facts; refresh manifest sdt_facts.

Companion to scripts/fetch_corpus.py.
When an upstream document revision triggers the fetcher's sha256-drift warning, run:

    python scripts/scan_corpus_facts.py            # print facts vs manifest
    python scripts/scan_corpus_facts.py --write    # rewrite sdt_facts + sha256/bytes

Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "shared" / "corpus" / "manifest.json"

# Detection order matters: first match wins (mirrors spec-projection.md §1).
CLASS_MARKERS = [
    ("checkbox", "<w14:checkbox"),
    ("dropdown", "<w:dropDownList"),
    ("combobox", "<w:comboBox"),
    ("date", "<w:date"),
    ("picture", "<w:picture"),
    ("building_block", "<w:docPartObj"),
    ("building_block", "<w:docPartList"),
    ("repeating", "<w15:repeatingSection/"),
    ("repeating_item", "<w15:repeatingSectionItem"),
    ("group", "<w:group"),
    ("text", "<w:text"),
]

PART_RE = re.compile(r"word/(document|header\d*|footer\d*|glossary/document)\.xml$")


def scan(path: Path) -> dict:
    z = zipfile.ZipFile(path)
    facts: dict = {"total": 0}
    locks: dict[str, int] = {}
    for name in z.namelist():
        if not PART_RE.match(name):
            continue
        xml = z.read(name).decode("utf-8", "replace")
        for pr in re.findall(r"<w:sdtPr>.*?</w:sdtPr>", xml, re.S):
            facts["total"] += 1
            for cls, marker in CLASS_MARKERS:
                if marker in pr:
                    facts[cls] = facts.get(cls, 0) + 1
                    break
            else:
                facts["richtext"] = facts.get("richtext", 0) + 1
            lock = re.search(r'<w:lock w:val="([^"]+)"', pr)
            if lock:
                locks[lock.group(1)] = locks.get(lock.group(1), 0) + 1
            if "<w:showingPlcHdr" in pr:
                facts["placeholders"] = facts.get("placeholders", 0) + 1
            if "<w:dataBinding" in pr:
                facts["bound"] = facts.get("bound", 0) + 1
            if "<w:temporary" in pr:
                facts["temporary"] = facts.get("temporary", 0) + 1
    facts["locks"] = locks
    doc = z.read("word/document.xml").decode("utf-8", "replace")
    cell = len(re.findall(r"<w:sdtContent>\s*<w:tc[ >]", doc))
    row = len(re.findall(r"<w:sdtContent>\s*<w:tr[ >]", doc))
    if cell:
        facts["cell_level"] = cell
    if row:
        facts["row_level"] = row
    try:
        settings = z.read("word/settings.xml").decode("utf-8", "replace")
        m = re.search(r'<w:documentProtection[^>]*w:edit="([^"]+)"[^>]*?(w:enforcement="(1|true)")?[^>]*/?>', settings)
        if m:
            enforced = 'w:enforcement="1"' in m.group(0) or 'w:enforcement="true"' in m.group(0)
            hashed = "w:hash=" in m.group(0)
            facts["protection"] = f"edit={m.group(1)} enforcement={1 if enforced else 0}" + (
                " (password hash present)" if hashed else ""
            )
        else:
            facts["protection"] = None
    except KeyError:
        facts["protection"] = None
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="update manifest sdt_facts, sha256 and bytes in place")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    corpus = MANIFEST.parent
    changed = False
    for key, entry in manifest["documents"].items():
        path = corpus / entry["file"]
        if not path.exists():
            print(f"{key:32} MISSING (fetch first)")
            continue
        facts = scan(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        drift = []
        if sha != entry.get("sha256"):
            drift.append("sha256")
        if facts != {k: v for k, v in entry.get("sdt_facts", {}).items() if k not in ("tags", "negative_id")} and facts != entry.get("sdt_facts"):
            drift.append("sdt_facts")
        print(f"{key:32} total={facts['total']:5} {'DRIFT: ' + ','.join(drift) if drift else 'matches manifest'}")
        if args.write and drift:
            preserved = {k: entry["sdt_facts"][k] for k in ("tags", "negative_id") if k in entry.get("sdt_facts", {})}
            entry["sdt_facts"] = {**facts, **preserved}
            entry["sha256"] = sha
            entry["bytes"] = path.stat().st_size
            changed = True
    if changed:
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("manifest updated — review the diff and re-check A5 floors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
