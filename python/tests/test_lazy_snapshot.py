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


def test_dry_run_on_mutated_engine_sees_current_state():
    """Dry-run's second engine must be built from CURRENT state: after batch
    1 introduced ALPHA, a dry-run targeting ALPHA must succeed."""
    engine = _engine_for(["alpha beta gamma"])
    engine.process_batch([ModifyText(type="modify", target_text="alpha", new_text="ALPHA")])

    stats = engine.process_batch([ModifyText(type="modify", target_text="ALPHA", new_text="OMEGA")], dry_run=True)
    assert stats["edits_applied"] == 1

    # And the dry-run must not have mutated the real engine.
    assert "OMEGA" not in engine.mapper.full_text
