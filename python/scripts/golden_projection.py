"""Golden capture/compare for the Python projection pipeline.

Protocol from docs/PERFORMANCE.md §2/§3.6: before touching projection code,
capture every view this engine can emit; after the change, byte-compare. The
projection is a contract with downstream agents (anchors, ids, offsets), so
performance work is only safe when the bytes are provably unchanged.

Views captured per document:
  reader_raw       _extract_text_from_doc(clean_view=False, include_appendix=False)
  reader_clean     _extract_text_from_doc(clean_view=True,  include_appendix=False)
  reader_appendix  _extract_text_from_doc(clean_view=False, include_appendix=True)
  mapper_raw       DocumentMapper(clean_view=False).full_text
  mapper_clean     DocumentMapper(clean_view=True).full_text
  pagination       page count + per-page lengths + page boundary offsets
  outline          one line per OutlineNode, all fields

The twin contract (reader_raw == mapper_raw, reader_clean == mapper_clean)
is asserted on every capture, so a run that breaks it fails loudly even
before the byte-compare.

Usage:
  python scripts/golden_projection.py verify [--manifest PATH]
  python scripts/golden_projection.py capture <outdir>
  python scripts/golden_projection.py compare <baseline_dir> <new_dir>

Default inputs are portable synthetic fixtures. Private runs require an explicit
--document PATH, and private verification also requires --manifest PATH. Private
captures contain document text: keep them local and out of version control.

`verify` is the durable gate: it compares against the committed
tests/golden_manifest.txt (hashes only — no multi-MB golden text in git) and
is what tests/test_projection_goldens.py runs. Use `capture` + `compare` when
you need to SEE a diff, since those keep the full text side by side.

Regenerating the manifest is an explicit, reviewable act — do it only when a
projection change is intended, and say so in the commit:
  python scripts/golden_projection.py capture tmp/g && \
      cp tmp/g/MANIFEST.txt tests/golden_manifest.txt
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import time
from pathlib import Path
from zipfile import BadZipFile

from docx import Document

from adeu.ingest import _extract_text_from_doc
from adeu.outline import extract_outline
from adeu.pagination import paginate, split_structural_appendix
from adeu.redline.mapper import DocumentMapper
from adeu.utils.docx import strip_bom_from_docx_bytes

# Default inputs are synthetic and identical on every checkout.
DOCS = [("cells", None), ("revisions", None)]
VIEW_NAMES = ("reader_raw", "reader_clean", "reader_appendix", "mapper_raw", "mapper_clean", "outline", "pagination")


def build_cells_fixture() -> bytes:
    """Anchor/table fixture exercising every cell-anchor branch (§3.6):
    a labeled cell, a text cell without id, empty cells with unlabeled
    paragraphs, an empty cell with NO paragraph, a nested table with empty
    cells, and a second table."""
    from docx.oxml.ns import qn

    doc = Document()
    doc.add_paragraph("Heading for cells fixture", style="Heading 1")
    doc.add_paragraph("Body text before the table.")

    t = doc.add_table(rows=3, cols=2)
    t.cell(0, 0).text = "Labeled"
    t.cell(0, 1).text = "Text cell without id"
    # (1,0) empty with an unlabeled paragraph (default from add_table)
    # (1,1): strip its only paragraph -> empty cell with NO paragraph
    tc = t.cell(1, 1)._tc
    for p in tc.findall(qn("w:p")):
        tc.remove(p)
    # nested table inside (2,0) with empty cells
    inner = t.cell(2, 0).add_table(rows=2, cols=2)
    inner.cell(0, 0).text = "inner"

    doc.add_paragraph("Between tables.")
    t2 = doc.add_table(rows=2, cols=2)
    t2.cell(0, 0).text = "second table"

    doc.add_paragraph("Trailing paragraph.")
    p = doc.add_paragraph()
    r = p.add_run("bold text")
    r.bold = True
    r2 = p.add_run(" italic")
    r2.italic = True

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_revisions_fixture() -> bytes:
    """Independent insertion/deletion with fixed metadata and differing views."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document()
    doc.add_paragraph("Tracked change fixture", style="Heading 1")
    for tag, revision_id, value in (
        ("del", "1", "removed wording"),
        ("ins", "2", "added wording"),
    ):
        paragraph = doc.add_paragraph()
        revision = OxmlElement(f"w:{tag}")
        revision.set(qn("w:id"), revision_id)
        revision.set(qn("w:author"), "Regression Fixture")
        revision.set(qn("w:date"), "2026-01-01T00:00:00Z")
        run = OxmlElement("w:r")
        text = OxmlElement("w:delText" if tag == "del" else "w:t")
        text.text = value
        run.append(text)
        revision.append(run)
        paragraph._p.append(revision)
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def load_bytes(name, path):
    if path is not None:
        return path.read_bytes()
    return {"cells": build_cells_fixture, "revisions": build_revisions_fixture}[name]()


def render_outline(doc):
    """Mirrors the production path exactly (doc_cache._fill_view): the reader
    is re-run with return_paragraph_offsets=True because the outline consumes
    those offsets, so this golden covers that branch too."""
    text, offsets = _extract_text_from_doc(doc, clean_view=False, include_appendix=False, return_paragraph_offsets=True)
    body, _ = split_structural_appendix(text)
    pg = paginate(body, structural_appendix="")
    nodes = extract_outline(doc, body, pg.body_pages, pg.body_page_offsets, paragraph_offsets=offsets)
    lines = []
    for n in nodes:
        lines.append(
            f"L{n.level}\tp{n.page}\tend={n.end_page}\tstyle={n.style}\t"
            f"table={n.has_table}\tfn={','.join(n.footnote_ids)}\t{n.text}"
        )
    return "\n".join(lines), pg, text


def render_pagination(pg):
    lines = [
        f"total_pages={pg.total_pages}",
        f"body_pages={len(pg.body_pages)}",
        f"body_page_offsets={','.join(str(o) for o in pg.body_page_offsets)}",
    ]
    for i, p in enumerate(pg.pages, 1):
        h = hashlib.sha256(p.page_content.encode("utf-8")).hexdigest()[:16]
        lines.append(f"page {i}\tlen={len(p.page_content)}\ttracked={p.tracked_change_count}\tsha={h}")
    return "\n".join(lines)


def compute_views(name: str, sanitized: bytes, verbose: bool = False) -> dict[str, str]:
    """Every projection view for one document, plus the invariant assertions.

    Importable, so the pytest gate (tests/test_projection_goldens.py) runs the
    exact same computation this CLI does — one implementation, no drift.
    """

    def _t(label, fn):
        t = time.perf_counter()
        out = fn()
        if verbose:
            print(f"  {label:16s} {time.perf_counter() - t:6.2f}s")
        return out

    views: dict[str, str] = {}
    views["reader_raw"] = _t(
        "reader_raw",
        lambda: _extract_text_from_doc(Document(io.BytesIO(sanitized)), clean_view=False, include_appendix=False),
    )
    views["reader_clean"] = _t(
        "reader_clean",
        lambda: _extract_text_from_doc(Document(io.BytesIO(sanitized)), clean_view=True, include_appendix=False),
    )
    views["reader_appendix"] = _t(
        "reader_appendix",
        lambda: _extract_text_from_doc(Document(io.BytesIO(sanitized)), clean_view=False, include_appendix=True),
    )
    views["mapper_raw"] = _t(
        "mapper_raw",
        lambda: DocumentMapper(Document(io.BytesIO(sanitized)), clean_view=False).full_text,
    )
    views["mapper_clean"] = _t(
        "mapper_clean",
        lambda: DocumentMapper(Document(io.BytesIO(sanitized)), clean_view=True).full_text,
    )

    # Twin contract (§7.3.3) — asserted on EVERY computation, so drift fails
    # loudly even when hashes are not being compared.
    assert views["reader_raw"] == views["mapper_raw"], f"{name}: TWIN DRIFT (raw view)"
    assert views["reader_clean"] == views["mapper_clean"], f"{name}: TWIN DRIFT (clean view)"

    outline_txt, pg, offsets_text = _t("outline+paginate", lambda: render_outline(Document(io.BytesIO(sanitized))))
    views["outline"] = outline_txt
    views["pagination"] = render_pagination(pg)

    # Requesting paragraph offsets must not change the projected text.
    assert offsets_text == views["reader_raw"], f"{name}: reader text differs when paragraph offsets are requested"
    return views


def iter_documents(document: Path | None = None):
    """Yield named raw input bytes once, without implicit file discovery."""
    for name, path in [("private", document)] if document is not None else DOCS:
        yield name, load_bytes(name, path)


def capture(outdir: Path, document: Path | None = None) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = []
    outputs = {}
    for name, raw in iter_documents(document):
        print(f"\n=== {name} ({len(raw) / 1e6:.2f} MB) ===")
        views = compute_views(name, strip_bom_from_docx_bytes(raw), verbose=True)
        if set(views) != set(VIEW_NAMES):
            raise ValueError("expected all seven projection views")
        print("  twin contract    OK (raw + clean byte-identical)")
        if document is not None:
            outputs["INPUT_SHA256.txt"] = hashlib.sha256(raw).hexdigest() + "\n"
        for view, text in views.items():
            outputs[f"{name}.{view}.txt"] = text
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            manifest.append(f"{sha}  {len(text):>10}  {name}.{view}.txt")
            print(f"    {view:16s} {len(text):>10} chars  {sha[:16]}")

    if not manifest:
        raise ValueError("no projection views captured")
    outputs["MANIFEST.txt"] = "\n".join(manifest) + "\n"
    if document is not None:
        for filename in outputs:
            destination = outdir / filename
            if destination.exists() and destination.samefile(document):
                raise ValueError("capture outputs must not overwrite the input document")
    for filename, text in outputs.items():
        (outdir / filename).write_text(text, encoding="utf-8", newline="")
    print(f"\ncaptured {len(manifest)} views")


def load_manifest(manifest_path: Path) -> dict[str, str]:
    expected = {}
    allowed_names = {name for name, _ in DOCS} | {"private"}
    allowed_keys = {f"{name}.{view}.txt" for name in allowed_names for view in VIEW_NAMES}
    for number, ln in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        fields = ln.split()
        if len(fields) != 3:
            raise ValueError(f"invalid manifest row {number}")
        sha, size, fname = fields
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha) or not re.fullmatch(r"[0-9]+", size) or fname not in allowed_keys:
            raise ValueError(f"invalid manifest row {number}")
        if fname in expected:
            raise ValueError(f"duplicate manifest row {number}")
        expected[fname] = sha.lower()
    names = {key.split(".", 1)[0] for key in expected}
    if not expected or set(expected) != {f"{name}.{view}.txt" for name in names for view in VIEW_NAMES}:
        raise ValueError("manifest must contain all seven views of each document")
    if "private" in names and names != {"private"}:
        raise ValueError("private manifest cannot contain portable fixtures")
    return expected


def _read_input_sha(manifest_path: Path) -> str:
    digest = (manifest_path.parent / "INPUT_SHA256.txt").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("invalid private input fingerprint")
    return digest.lower()


def verify(manifest_path: Path, document: Path | None = None) -> int:
    """Verify all portable views, or an explicit private input and its baseline."""
    expected = load_manifest(manifest_path)
    names = {"private"} if document is not None else {name for name, _ in DOCS}
    if set(expected) != {f"{name}.{view}.txt" for name in names for view in VIEW_NAMES}:
        raise ValueError("manifest must cover exactly the selected documents")
    input_sha = _read_input_sha(manifest_path) if document is not None else None
    checked = 0
    failures = []

    for name, raw in iter_documents(document):
        if input_sha is not None and hashlib.sha256(raw).hexdigest() != input_sha:
            print("FAIL: private input differs from captured baseline")
            return 1
        views = compute_views(name, strip_bom_from_docx_bytes(raw))
        if set(views) != set(VIEW_NAMES):
            raise ValueError("expected all seven projection views")
        for view, text in views.items():
            key = f"{name}.{view}.txt"
            got = hashlib.sha256(text.encode("utf-8")).hexdigest()
            checked += 1
            if got != expected[key]:
                failures.append(f"{key}: expected {expected[key][:16]} got {got[:16]} ({len(text)} chars)")

    if checked != len(expected):
        failures.append("not all manifest views were checked")
    for f in failures:
        print(f"FAIL {f}")
    print(
        f"\n{'GOLDENS VERIFIED' if not failures else 'GOLDEN MISMATCH'} "
        f"({checked} views checked, {len(failures)} failures)"
    )
    return 0 if not failures else 1


def compare(base: Path, new: Path):
    bm = load_manifest(base / "MANIFEST.txt")
    nm = load_manifest(new / "MANIFEST.txt")
    keys = bm.keys() | nm.keys()
    if any(key.startswith("private.") for key in keys):
        if not all(key.startswith("private.") for key in keys):
            raise ValueError("private comparison requires two private baselines")
        if _read_input_sha(base / "MANIFEST.txt") != _read_input_sha(new / "MANIFEST.txt"):
            print("FAIL: private input differs between captures")
            return 1

    only_b = sorted(set(bm) - set(nm))
    only_n = sorted(set(nm) - set(bm))
    diff = sorted(k for k in set(bm) & set(nm) if bm[k] != nm[k])

    for k in only_b:
        print(f"MISSING in new: {k}")
    for k in only_n:
        print(f"EXTRA in new:   {k}")
    for k in diff:
        print(f"DIFFERS: {k}")
        bt = (base / k).read_text(encoding="utf-8")
        nt = (new / k).read_text(encoding="utf-8")
        print(f"   baseline {len(bt)} chars, new {len(nt)} chars")
        # first divergence
        lim = min(len(bt), len(nt))
        i = next((i for i in range(lim) if bt[i] != nt[i]), lim)
        print(f"   first divergence at char {i}:")
        print(f"     baseline: {bt[max(0, i - 60) : i + 60]!r}")
        print(f"     new:      {nt[max(0, i - 60) : i + 60]!r}")

    ok = not (only_b or only_n or diff)
    print(f"\n{'ALL VIEWS BYTE-IDENTICAL' if ok else 'GOLDEN MISMATCH'} ({len(set(bm) & set(nm))} compared)")
    return 0 if ok else 1


DEFAULT_MANIFEST = Path(__file__).resolve().parent.parent / "tests" / "golden_manifest.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="verify portable or explicit private projection hashes")
    check.add_argument("--manifest", type=Path)
    check.add_argument("--document", type=Path)
    snapshot = commands.add_parser("capture", help="capture projection text; keep private outputs local")
    snapshot.add_argument("outdir", type=Path)
    snapshot.add_argument("--document", type=Path)
    diff = commands.add_parser("compare", help="compare local captures, including text excerpts")
    diff.add_argument("base", type=Path)
    diff.add_argument("new", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify" and args.document is not None and args.manifest is None:
        parser.error("private verification requires --manifest")
    try:
        if args.command == "verify":
            return verify(args.manifest or DEFAULT_MANIFEST, args.document)
        if args.command == "capture":
            capture(args.outdir, args.document)
            return 0
        return compare(args.base, args.new)
    except (OSError, UnicodeError, BadZipFile) as exc:
        print(f"ERROR: cannot read/write benchmark inputs or outputs ({type(exc).__name__})", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
