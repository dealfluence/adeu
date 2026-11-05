"""
Each per-edit report names its position in the caller's change list.

`edits[]` holds reports for text edits only — review actions are filtered out —
so in a mixed batch `edits[0]` can describe change #1, and nothing in the
report said so. `failed[]` already carried the caller's index; every report now
does too, using the same 0-based numbering.

Node twin: node/packages/core/src/edit-report-index.test.ts
"""

import io

from docx import Document

from adeu.models import AcceptChange, ModifyText
from adeu.payloads import shrink_batch_stats
from adeu.redline.engine import RedlineEngine


def _stream(*paragraphs: str) -> io.BytesIO:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def _doc_with_one_tracked_change() -> io.BytesIO:
    first = RedlineEngine(_stream("Bonus in March.", "Governing law is the State of Delaware."), author="A")
    first.apply_edits([ModifyText(target_text="in March", new_text="in January")])
    return first.save_to_stream()


def test_report_carries_the_callers_index_in_a_mixed_batch():
    engine = RedlineEngine(_doc_with_one_tracked_change(), author="B")
    result = engine.process_batch(
        [AcceptChange(target_id="Chg:1"), ModifyText(target_text="State of Delaware", new_text="State of New York")]
    )

    assert result["actions_applied"] == 1
    assert [r["index"] for r in result["edits"]] == [1]


def test_failed_and_applied_reports_carry_the_same_index_as_failed():
    engine = RedlineEngine(_stream("The termination fee is twenty five thousand dollars."), author="T")
    result = engine.process_batch(
        [
            ModifyText(target_text="twenty five thousand", new_text="fifty thousand"),
            ModifyText(target_text="twenty five thousand", new_text="ten thousand"),
        ],
        partial=True,
    )

    assert [(r["index"], r["status"]) for r in result["edits"]] == [(0, "applied"), (1, "failed")]
    assert [f["index"] for f in result["failed"]] == [1]


def test_minimal_report_keeps_the_index_on_failed_edits_only():
    engine = RedlineEngine(_stream("The termination fee is twenty five thousand dollars."), author="T")
    result = engine.process_batch(
        [
            ModifyText(target_text="twenty five thousand", new_text="fifty thousand"),
            ModifyText(target_text="twenty five thousand", new_text="ten thousand"),
        ],
        partial=True,
    )

    applied, failed = shrink_batch_stats(result)["edits"]
    assert "index" not in applied
    assert failed["index"] == 1
