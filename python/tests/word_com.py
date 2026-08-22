# FILE: tests/word_com.py
"""
Word COM oracle for tests about ids Word reinterprets on load.

Why this exists: for `w14:paraId`, `w16cid:durableId` and friends the XML tells
you nothing. A package can be schema-valid, internally consistent, and exactly
what the writer intended, and Word will still silently rewrite the ids and drop
every relationship keyed on them. Word is the only oracle that sees the
difference (BUG_paraId_signed_int32_thread_collapse.md).

Usage from a test::

    def test_something(word_app, tmp_path):
        path = tmp_path / "out.docx"
        path.write_bytes(engine.save_to_stream().getvalue())
        comments = read_comments(word_app, path)
        assert thread_of(comments, "the reply") == "the root"

Conventions this module keeps (each one cost real debugging time):

* **Open read-only, never save.** A COM round-trip REWRITES ids, so a check
  that saved would destroy the evidence it was collecting.
* **`Comment.Ancestor` may THROW instead of returning None** for a root comment
  on some builds. It is always caught and treated as top-level.
* **`Comments.Item(i)` is anchor order, not `w:id` order** and gives no stable
  identity. Comments are matched by BODY TEXT; `Comment.Date` is
  minute-precision and cannot disambiguate replies written seconds apart.
* **Word raises on a package it will not open.** That is a real (and the most
  severe) outcome of a bad id, so it is surfaced as `WordRefusedDocument`
  rather than an error.
* **Word keys documents by BASE NAME, not by path.** Two tests using
  `tmp_path / "out.docx"` are, to Word, the same document; the second one
  silently gets the first one's state. Every open here goes through a
  uniquely-named staging copy, which also keeps the caller's file untouched.
* **Every python-docx document has the SAME `w14:docId`** (`24062061`, baked
  into the default template). That is Word's document identity, so an entire
  test suite's fixtures are one document as far as Word is concerned — which
  showed up as one test deterministically poisoning the next (Word stopped
  stamping `w14:paraId` at all). Staging assigns a fresh docId.
"""

from __future__ import annotations

import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

WD_DO_NOT_SAVE_CHANGES = 0
# wdFormatDocumentDefault. NOT wdFormatXMLDocument (12) — that is flat OPC, and
# with a .docx extension Word writes it anyway, producing a file no zip reader
# can open.
WD_FORMAT_DOCUMENT_DEFAULT = 16


class WordRefusedDocument(Exception):
    """Word would not open the package at all ("The file appears to be corrupted")."""


@dataclass(frozen=True)
class CommentView:
    """One comment exactly as Word — not the XML — reports it."""

    index: int
    author: str
    body: str
    ancestor_index: Optional[int]
    reply_count: int
    scope_start: Optional[int]
    scope_end: Optional[int]
    scope_text: str

    @property
    def is_top_level(self) -> bool:
        return self.ancestor_index is None

    @property
    def is_anchored(self) -> bool:
        """False when Word collapsed the anchor to a zero-length point (B3)."""
        return self.scope_start is not None and self.scope_end is not None and self.scope_end > self.scope_start


_DOC_ID_RE = re.compile(r'(<w14:docId\s+w14:val=")([0-9A-Fa-f]{8})(")')
_W15_DOC_ID_RE = re.compile(r'(<w15:docId\s+w15:val=")\{[^}]*\}(")')


def _stage(path: Path) -> Path:
    """A copy of `path` that Word cannot confuse with any other document.

    Three collisions to defeat, all of which have produced silently wrong
    measurements here: the file NAME (Word's document identity for open
    documents), the `w14:docId` (Word's identity across saves — constant across
    every python-docx document) and the caller's file itself, which Word would
    otherwise lock and possibly rewrite.
    """
    staged = path.parent / f"{uuid.uuid4().hex[:12]}_{path.name}"

    # A fresh docId is itself an ST_LongHexNumber: keep it in the legal range,
    # or the staging copy would inject the very defect under test.
    doc_id = f"{uuid.uuid4().int % 0x7FFFFFFF + 1:08X}"
    guid = f"{{{uuid.uuid4()}}}".upper()

    source = zipfile.ZipFile(path)
    with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "word/settings.xml":
                text = data.decode("utf-8")
                text = _DOC_ID_RE.sub(rf"\g<1>{doc_id}\g<3>", text)
                text = _W15_DOC_ID_RE.sub(rf"\g<1>{guid}\g<2>", text)
                data = text.encode("utf-8")
            target.writestr(item, data)
    source.close()
    return staged


def _open_read_only(app, path: Path):
    try:
        return app.Documents.Open(str(path), False, True, False)
    except Exception as exc:  # pywintypes.com_error
        raise WordRefusedDocument(
            f"Word refused to open {path.name}: {exc}. An out-of-range id is one cause — "
            "w14:paraId=00000000 makes Word report the file as corrupted."
        ) from exc


def read_comments(app, path: Path) -> List[CommentView]:
    """Every comment in `path` as Word sees it. Opens read-only, never saves."""
    document = _open_read_only(app, _stage(path))
    try:
        views: List[CommentView] = []
        for i in range(1, document.Comments.Count + 1):
            comment = document.Comments.Item(i)

            try:
                ancestor = comment.Ancestor
                ancestor_index = None if ancestor is None else int(ancestor.Index)
            except Exception:
                # Build-dependent: Ancestor throws rather than returning None
                # for a thread root.
                ancestor_index = None

            try:
                reply_count = int(comment.Replies.Count)
            except Exception:
                reply_count = 0

            try:
                scope = comment.Scope
                scope_start: Optional[int] = int(scope.Start)
                scope_end: Optional[int] = int(scope.End)
                scope_text = str(scope.Text or "")
            except Exception:
                scope_start = scope_end = None
                scope_text = ""

            views.append(
                CommentView(
                    index=i,
                    author=str(comment.Author),
                    # Word terminates the comment body with \r; strip it so
                    # tests can compare against the text they wrote.
                    body=str(comment.Range.Text or "").rstrip("\r\x07"),
                    ancestor_index=ancestor_index,
                    reply_count=reply_count,
                    scope_start=scope_start,
                    scope_end=scope_end,
                    scope_text=scope_text,
                )
            )
        return views
    finally:
        document.Close(WD_DO_NOT_SAVE_CHANGES)


def round_trip(app, source: Path, destination: Path) -> Path:
    """Open `source` in Word and save it to `destination`, unmodified.

    The one place saving is correct: it is how you observe which ids Word KEPT.
    Word preserves every `w14:paraId` of a package it accepts and renumbers the
    whole part when it finds one it does not.

    Only meaningful on a document Word itself authored (`author_document`). A
    foreign package gets its paraIds re-stamped wholesale on first save, and a
    package Word considers untouched is often saved without paraIds at all.
    """
    staged_source = _stage(source)
    staged_destination = destination.parent / f"{uuid.uuid4().hex[:12]}_{destination.name}"
    document = app.Documents.Open(str(staged_source), False, False, False)
    try:
        document.SaveAs2(str(staged_destination), WD_FORMAT_DOCUMENT_DEFAULT)
    finally:
        document.Close(WD_DO_NOT_SAVE_CHANGES)
    destination.write_bytes(staged_destination.read_bytes())
    return destination


def edit_and_save(app, source: Path, destination: Path, action, *, track: Optional[bool] = True):
    """Open `source` read-WRITE, run `action(document)`, save to `destination`.

    The oracle for behaviour Word only exhibits while editing: what it does to a
    `w:sdt` when you type into it (CC-6). `round_trip` cannot answer those —
    Word rewrites nothing on an untouched document.

    `track=None` skips touching `TrackRevisions` entirely, and that is not
    optional under `w:documentProtection w:edit="forms"`: **reading** the
    property throws there just as assigning it does ("The TrackRevisions method
    or property is not available because the document is a protected
    document"). A probe that reads it to restore it later dies before it
    measures anything.

    Staging is the same defence as everywhere else in this module, applied
    twice: to the source (Word keys documents by base name) and to the
    destination (an earlier run's file is still Word's idea of that name).
    """
    staged_source = _stage(source)
    document = app.Documents.Open(str(staged_source), False, False, False)
    try:
        if track is not None:
            document.TrackRevisions = bool(track)
        result = action(document)
        staged_destination = destination.parent / f"{uuid.uuid4().hex[:12]}_{destination.name}"
        document.SaveAs2(str(staged_destination), WD_FORMAT_DOCUMENT_DEFAULT)
    finally:
        document.Close(WD_DO_NOT_SAVE_CHANGES)
    destination.write_bytes(staged_destination.read_bytes())
    return result


def author_document(app, destination: Path, rows: int = 8, cols: int = 2) -> Path:
    """A DOCX written from scratch BY Word, carrying Word's own ids.

    The only reliable way to get a baseline of Word-assigned `w14:paraId`s.
    Round-tripping a foreign document does not work: Word decides for itself
    whether an open/save is worth re-stamping paragraph identities on and skips
    it about half the time, so the baseline would come and go. A document it
    authors has them every time (measured 6/6, all high-bit clear — which is
    also the direct form of "Word's own ids are never out of range").
    """
    document = app.Documents.Add()
    try:
        document.Content.Text = "Word-authored baseline.\n"
        table = document.Tables.Add(document.Paragraphs(document.Paragraphs.Count).Range, rows, cols)
        for row in range(1, rows + 1):
            for column in range(1, cols + 1):
                table.Cell(row, column).Range.Text = f"r{row}c{column}"
        document.SaveAs2(str(destination), WD_FORMAT_DOCUMENT_DEFAULT)
    finally:
        document.Close(WD_DO_NOT_SAVE_CHANGES)
    return destination


def thread_map(comments: List[CommentView]) -> Dict[str, Optional[str]]:
    """`{comment body: parent's body or None}` — the assertion surface for
    threading. Bodies, not indices: `Comments.Item(i)` is anchor order and the
    indices shuffle as soon as threading changes."""
    by_index = {c.index: c for c in comments}
    return {c.body: (None if c.ancestor_index is None else by_index[c.ancestor_index].body) for c in comments}
