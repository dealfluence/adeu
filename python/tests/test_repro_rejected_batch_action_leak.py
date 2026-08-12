"""
BUG 2026-08-12 — a REJECTED batch left its review actions in the document.

Observed on the Node twin over MCP (`reply` + a failing `modify`, retried
twice, then a success → THREE replies on one reviewer comment), but the defect
is in the shared batch pipeline: `_process_batch_internal` applied the batch's
actions (accept / reject / reply) BEFORE capturing the pre-batch snapshot the
edit loop rolls back to. The snapshot therefore already contained the action's
mutation, so "the run restores the pre-batch snapshot and rejects everything"
was true of the edits and false of the actions. An action-only batch was worse
still: the `skipped_actions` raise happened before any snapshot existed.

Python only escaped the visible triple-reply because its MCP layer re-reads the
file from disk on every call — the engine contract is broken either way, and
every long-lived engine (serve daemon, CLI batch, library user) sees it.

The contract under test: a batch is ONE transaction. If it is rejected, the
engine is exactly what it was before the call — actions included.

Written test-first: every case here fails on pre-fix main.
"""

import io
import re
import zipfile

import pytest
from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.models import AcceptChange, ModifyText, ReplyComment
from adeu.redline.engine import BatchValidationError, RedlineEngine

REVIEWER = "Sarah Chen"
AGENT = "Adeu AI (PY)"
BODY = (
    "Discovery Material may be disclosed to outside counsel of record and to "
    "any person to whom disclosure is reasonably necessary for this litigation."
)
ANCHOR = "reasonably necessary"
REVIEW_NOTE = "Please add an attorneys'-eyes-only tier."
REPLY = "Updated - added the AEO tier per your 28 July note."
NO_SUCH_TEXT = "TEXT THAT IS NOT ANYWHERE IN THIS DOCUMENT"


def _stream(paragraphs):
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _reviewed_engine(author=AGENT):
    """An engine over a document carrying exactly one reviewer comment."""
    seed = RedlineEngine(_stream([BODY]), author=REVIEWER)
    seed.apply_edits([ModifyText(target_text=ANCHOR, new_text=ANCHOR, comment=REVIEW_NOTE)])
    return RedlineEngine(seed.save_to_stream(), author=author)


def _revised_engine(author=AGENT):
    """An engine over a document carrying one pending tracked change."""
    seed = RedlineEngine(_stream([BODY]), author=REVIEWER)
    seed.apply_edits([ModifyText(target_text="outside counsel", new_text="outside trial counsel")])
    return RedlineEngine(seed.save_to_stream(), author=author)


# Word does not fix the comments part's name (`comments.xml`, `comments1.xml`,
# … depending on which generator created it), so it is located by pattern.
_COMMENTS_PART = re.compile(r"^word/comments\d*\.xml$")


def _comments_part_name(names) -> str:
    matches = [n for n in names if _COMMENTS_PART.match(n)]
    return matches[0] if matches else ""


def _comment_texts(engine) -> list:
    """Comment bodies of the engine's CURRENT tree, read through a save."""
    data = io.BytesIO(engine.save_to_stream().getvalue())
    with zipfile.ZipFile(data) as z:
        name = _comments_part_name(z.namelist())
        if not name:
            return []
        xml = z.read(name).decode("utf-8")
    return [
        "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", body)).strip()
        for body in re.findall(r"<w:comment\b[^>]*>(.*?)</w:comment>", xml, re.S)
    ]


def _document_text(engine) -> str:
    return extract_text_from_stream(io.BytesIO(engine.save_to_stream().getvalue()), clean_view=False)


def _comment_id(engine) -> str:
    match = re.search(r"\[Com:(\d+)\]", _document_text(engine))
    assert match, "fixture precondition: no comment in the document"
    return match.group(1)


def _empty_comment_body(package: bytes, comment_id: str) -> bytes:
    """
    Strip every block-level child from one `<w:comment>`, on the package rather
    than through python-docx: `EG_BlockLevelElts` is `minOccurs="0"`, so this is
    schema-legal, and it is the one shape where no paragraph identity can be
    minted — i.e. where threading a reply is genuinely impossible.
    """
    with zipfile.ZipFile(io.BytesIO(package)) as z:
        items = {name: z.read(name) for name in z.namelist()}

    part = _comments_part_name(items)
    assert part, "fixture precondition: no comments part in the package"
    xml = items[part].decode("utf-8")
    pattern = re.compile(
        r'(<w:comment\b[^>]*\bw:id="%s"[^>]*>)(.*?)(</w:comment>)' % re.escape(comment_id),
        re.S,
    )
    xml, hits = pattern.subn(lambda m: m.group(1) + m.group(3), xml)
    assert hits == 1, f"fixture precondition: expected one <w:comment w:id={comment_id}>, found {hits}"
    items[part] = xml.encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in items.items():
            z.writestr(name, blob)
    return out.getvalue()


def test_reply_is_rolled_back_when_a_later_edit_fails():
    engine = _reviewed_engine()
    before = _comment_texts(engine)
    assert before == [REVIEW_NOTE]
    cid = _comment_id(engine)

    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ReplyComment(target_id=f"Com:{cid}", text=REPLY),
                ModifyText(target_text=NO_SUCH_TEXT, new_text="x"),
            ]
        )

    assert _comment_texts(engine) == before, "the rejected batch's reply survived the rollback"


def test_accept_is_rolled_back_when_a_later_edit_fails():
    engine = _revised_engine()
    pending = _document_text(engine)
    chg = re.search(r"\[Chg:(\d+)", pending)
    assert chg, "fixture precondition: no tracked change in the document"

    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                AcceptChange(target_id=f"Chg:{chg.group(1)}"),
                ModifyText(target_text=NO_SUCH_TEXT, new_text="x"),
            ]
        )

    assert _document_text(engine) == pending, "the rejected batch committed the reviewer's tracked change anyway"


def test_applied_actions_roll_back_when_a_later_ACTION_fails():
    """
    An action-only batch: no edits, so pre-fix there was no snapshot at all and
    the `skipped_actions` raise stranded whatever the batch had already applied.

    Action 2 has to pass validation and fail at APPLY time — a `<w:comment>`
    with no paragraph (schema-legal, `EG_BlockLevelElts` is minOccurs="0") has
    no paragraph identity to thread onto, so its reply is skipped rather than
    silently rooting a new top-level comment.
    """
    seed = RedlineEngine(_stream([BODY]), author=REVIEWER)
    seed.apply_edits(
        [
            ModifyText(target_text=ANCHOR, new_text=ANCHOR, comment=REVIEW_NOTE),
            ModifyText(
                target_text="outside counsel",
                new_text="outside counsel",
                comment="And define who counts as counsel of record.",
            ),
        ]
    )
    package = seed.save_to_stream().getvalue()

    ids = sorted(set(re.findall(r"\[Com:(\d+)\]", extract_text_from_stream(io.BytesIO(package)))), key=int)
    assert len(ids) == 2, f"fixture precondition: expected two comments, got {ids}"
    good, unthreadable = ids[0], ids[-1]

    engine = RedlineEngine(io.BytesIO(_empty_comment_body(package, unthreadable)), author=AGENT)

    before = _comment_texts(engine)
    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ReplyComment(target_id=f"Com:{good}", text=REPLY),
                ReplyComment(target_id=f"Com:{unthreadable}", text="Noted."),
            ]
        )

    assert _comment_texts(engine) == before, "action 1 stayed applied after action 2 rejected the batch"


def test_reported_run_two_rejected_retries_then_a_success_leave_one_reply():
    """The MCP shape: one long-lived engine, the same reply re-sent every time."""
    engine = _reviewed_engine()
    cid = _comment_id(engine)

    for _attempt in (1, 2):
        with pytest.raises(BatchValidationError):
            engine.process_batch(
                [
                    ReplyComment(target_id=f"Com:{cid}", text=REPLY),
                    ModifyText(target_text=NO_SUCH_TEXT, new_text="x"),
                ]
            )

    stats = engine.process_batch(
        [
            ReplyComment(target_id=f"Com:{cid}", text=REPLY),
            ModifyText(target_text=ANCHOR, new_text="strictly necessary"),
        ]
    )
    assert stats["actions_applied"] == 1
    assert stats["edits_applied"] == 1

    texts = _comment_texts(engine)
    assert texts.count(REPLY) == 1, f"the rejected retries left duplicate replies behind: {texts}"
    assert texts == [REVIEW_NOTE, REPLY]


def test_successful_action_plus_edit_batch_still_applies_both():
    """The fix must not over-roll-back: a batch that passes keeps everything."""
    engine = _reviewed_engine()
    cid = _comment_id(engine)

    stats = engine.process_batch(
        [
            ReplyComment(target_id=f"Com:{cid}", text=REPLY),
            ModifyText(target_text=ANCHOR, new_text="strictly necessary"),
        ]
    )

    assert stats["actions_applied"] == 1
    assert stats["edits_applied"] == 1
    assert _comment_texts(engine) == [REVIEW_NOTE, REPLY]
    text = _document_text(engine)
    assert "{--reasonably--}" in text
    assert "{++strictly++}" in text


def test_partial_mode_still_keeps_applied_actions():
    """
    `--partial` is the explicit opt-out of transactional rejection: failures are
    reported per item and whatever applied stays applied. The fix tightens the
    ALL-OR-NOTHING path only.
    """
    engine = _reviewed_engine()
    cid = _comment_id(engine)

    stats = engine.process_batch(
        [
            ReplyComment(target_id=f"Com:{cid}", text=REPLY),
            ModifyText(target_text=NO_SUCH_TEXT, new_text="x"),
        ],
        partial=True,
    )

    assert stats["status"] == "partial"
    assert stats["actions_applied"] == 1
    assert _comment_texts(engine) == [REVIEW_NOTE, REPLY]


# ---------------------------------------------------------------------------
# The invariant behind the fix
# ---------------------------------------------------------------------------


def test_rollback_verified_is_true_after_a_clean_rejection():
    engine = _reviewed_engine()
    cid = _comment_id(engine)
    assert engine.rollback_verified is True

    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ReplyComment(target_id=f"Com:{cid}", text=REPLY),
                ModifyText(target_text=NO_SUCH_TEXT, new_text="x"),
            ]
        )
    assert engine.rollback_verified is True


def test_rollback_verified_goes_false_when_the_restore_does_not_restore():
    """
    Defeat the restore to stand in for any future regression in it: a document
    a rejected batch mutated must never be reported as a verified rollback —
    that flag is what a caching caller keys its document reuse off.
    """
    engine = _reviewed_engine()
    cid = _comment_id(engine)
    engine._restore_from_snapshot = lambda snapshot: None  # type: ignore[assignment]

    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ReplyComment(target_id=f"Com:{cid}", text=REPLY),
                ModifyText(target_text=NO_SUCH_TEXT, new_text="x"),
            ]
        )

    assert engine.rollback_verified is False


def test_rollback_verified_resets_per_batch():
    engine = _reviewed_engine()
    cid = _comment_id(engine)
    engine.rollback_verified = False

    engine.process_batch([ReplyComment(target_id=f"Com:{cid}", text=REPLY)])
    assert engine.rollback_verified is True
