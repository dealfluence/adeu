"""
Lazy pre-batch snapshot (docs/Performance.md §5.2 ported to Python).

process_batch historically took a full save_to_stream() snapshot before
every real batch. The lazy variant snapshots from the engine's pristine
load-time bytes while nothing has mutated the tree, and only pays the full
serialize+re-zip once the engine has been mutated (actions earlier in the
batch, or a previous batch on the same engine instance). These tests pin
the correctness edge of that optimization: rollback must restore the state
the snapshot was supposed to capture — including on a REUSED engine whose
pristine bytes are stale.
"""

from io import BytesIO

import pytest
from docx import Document

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine


def _engine_for(paragraphs):
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return RedlineEngine(buf, author="Snapshot Tester")


def test_fresh_engine_failed_batch_rolls_back_to_pristine():
    engine = _engine_for(["alpha beta gamma"])
    assert engine._mutated_since_load is False

    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ModifyText(type="modify", target_text="alpha", new_text="ALPHA"),
                ModifyText(type="modify", target_text="THIS TEXT DOES NOT EXIST", new_text="x"),
            ]
        )

    text = engine.mapper.full_text
    assert "ALPHA" not in text
    assert "alpha beta gamma" in text
    # Rollback re-initializes the engine; it must present as unmutated again.
    assert engine._mutated_since_load is False


def test_reused_engine_failed_batch_rolls_back_to_previous_batch_state():
    """
    The stale-pristine hazard: after batch 1 mutates the engine, batch 2's
    snapshot must capture batch-1 state (via save_to_stream), NOT the
    pristine load-time bytes — otherwise batch 2's rollback would silently
    discard batch 1.
    """
    engine = _engine_for(["alpha beta gamma"])

    stats1 = engine.process_batch([ModifyText(type="modify", target_text="alpha", new_text="ALPHA")])
    assert stats1["edits_applied"] == 1
    assert engine._mutated_since_load is True
    text_after_1 = engine.mapper.full_text
    assert "ALPHA" in text_after_1

    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ModifyText(type="modify", target_text="beta", new_text="BETA"),
                ModifyText(type="modify", target_text="THIS TEXT DOES NOT EXIST", new_text="x"),
            ]
        )

    text_after_rollback = engine.mapper.full_text
    assert text_after_rollback == text_after_1


def test_successful_batches_chain_on_reused_engine():
    engine = _engine_for(["alpha beta gamma"])
    s1 = engine.process_batch([ModifyText(type="modify", target_text="alpha", new_text="ALPHA")])
    s2 = engine.process_batch([ModifyText(type="modify", target_text="gamma", new_text="GAMMA")])
    assert s1["edits_applied"] == 1
    assert s2["edits_applied"] == 1
    text = engine.mapper.full_text
    assert "ALPHA" in text and "GAMMA" in text


def _engine_with_pending_revision():
    """A FRESH engine over a document that already carries one pending tracked
    insertion, so _pristine_bytes describes the un-accepted document."""
    engine = _engine_for(["alpha beta gamma"])
    engine.process_batch([ModifyText(type="modify", target_text="gamma", new_text="GAMMA")])
    return RedlineEngine(BytesIO(engine.save_to_stream().getvalue()), author="Snapshot Tester")


def _document_text(engine) -> str:
    """Raw projection of the engine's CURRENT tree. Read through a save rather
    than engine.mapper: the mapper is built at __init__ and accept_all /
    reject_all do not rebuild it."""
    return extract_text_from_stream(BytesIO(engine.save_to_stream().getvalue()), clean_view=False)


def test_accept_all_revisions_marks_the_engine_mutated():
    """accept_all_revisions rewrites the tree, so the pristine load-time bytes
    stop being this engine's state. Without the flag, a later batch snapshots
    those bytes and a validation rollback resurrects every accepted revision.
    """
    engine = _engine_with_pending_revision()
    assert engine._mutated_since_load is False

    engine.accept_all_revisions()
    assert engine._mutated_since_load is True, (
        "accept_all_revisions mutated the tree without flagging it; a later batch would snapshot pre-accept bytes"
    )

    accepted = _document_text(engine)
    assert "{++" not in accepted  # the insertion is now committed body text
    assert "GAMMA" in accepted

    # A failing batch must roll back to the ACCEPTED state, not resurrect the
    # tracked changes accept_all just committed.
    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ModifyText(type="modify", target_text="alpha", new_text="ALPHA"),
                ModifyText(type="modify", target_text="THIS TEXT DOES NOT EXIST", new_text="x"),
            ]
        )
    rolled_back = _document_text(engine)
    assert "{++" not in rolled_back, "rollback resurrected the accepted revisions"
    assert rolled_back == accepted


def test_reject_all_revisions_marks_the_engine_mutated():
    engine = _engine_with_pending_revision()
    assert engine._mutated_since_load is False

    engine.reject_all_revisions()
    assert engine._mutated_since_load is True, (
        "reject_all_revisions mutated the tree without flagging it; a later batch would snapshot pre-reject bytes"
    )

    rejected = _document_text(engine)
    assert "GAMMA" not in rejected  # the proposed insertion is gone

    with pytest.raises(BatchValidationError):
        engine.process_batch(
            [
                ModifyText(type="modify", target_text="alpha", new_text="ALPHA"),
                ModifyText(type="modify", target_text="THIS TEXT DOES NOT EXIST", new_text="x"),
            ]
        )
    rolled_back = _document_text(engine)
    assert "GAMMA" not in rolled_back, "rollback resurrected the rejected revisions"
    assert rolled_back == rejected


def test_dry_run_after_accept_all_sees_accepted_state():
    """Dry-run builds its second engine from _pristine_bytes while unmutated,
    so an unflagged accept_all would make it reason about the wrong document."""
    engine = _engine_with_pending_revision()
    engine.accept_all_revisions()

    # "GAMMA" is committed body text only in the ACCEPTED document; in the
    # pre-accept bytes it is inside a tracked insertion.
    stats = engine.process_batch(
        [ModifyText(type="modify", target_text="GAMMA", new_text="OMEGA")],
        dry_run=True,
    )
    assert stats["edits_applied"] == 1
    assert "OMEGA" not in _document_text(engine)


def test_dry_run_on_mutated_engine_sees_current_state():
    """Dry-run's second engine must be built from CURRENT state: after batch
    1 introduced ALPHA, a dry-run targeting ALPHA must succeed."""
    engine = _engine_for(["alpha beta gamma"])
    engine.process_batch([ModifyText(type="modify", target_text="alpha", new_text="ALPHA")])

    stats = engine.process_batch([ModifyText(type="modify", target_text="ALPHA", new_text="OMEGA")], dry_run=True)
    assert stats["edits_applied"] == 1

    # And the dry-run must not have mutated the real engine.
    assert "OMEGA" not in engine.mapper.full_text
