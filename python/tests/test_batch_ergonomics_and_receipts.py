from pathlib import Path

import docx

from adeu.mcp_components.tools.document import process_document_batch
from adeu.models import AcceptChange, ModifyText, ReplyComment
from tests.utils import extract_content, get_mock_ctx, run_async


def test_derivative_filenames_edit_in_place_by_default(tmp_path: Path):
    for filename in [
        "master-supply-agreement.redline.docx",
        "contract-redline.docx",
        "agreement_markup.docx",
        "terms.marked.docx",
    ]:
        doc_path = tmp_path / filename
        doc = docx.Document()
        doc.add_paragraph("The Supplier shall deliver the goods within 30 days.")
        doc.save(str(doc_path))

        res = run_async(
            process_document_batch(
                reasoning="test in-place derivative filename",
                original_docx_path=str(doc_path),
                author_name="Tester",
                ctx=get_mock_ctx(),
                changes=[
                    ModifyText(
                        target_text="30 days",
                        new_text="45 days",
                    )
                ],
            )
        )
        content = extract_content(res)
        assert f"Saved to: {doc_path}" in content
        spurious = tmp_path / f"{doc_path.stem}_processed.docx"
        assert not spurious.exists(), f"Should not have created {spurious}"


def test_batch_receipt_includes_minted_ids_action_receipts_and_document_state(tmp_path: Path):
    doc_path = tmp_path / "contract_redlined.docx"
    doc = docx.Document()
    doc.add_paragraph("The Supplier shall pay 30 days net.")
    doc.add_paragraph("Governing law shall be the State of Delaware.")
    doc.save(str(doc_path))

    # Step 1: Apply two edits (one with a comment)
    res1 = run_async(
        process_document_batch(
            reasoning="step 1 edits",
            original_docx_path=str(doc_path),
            author_name="Reviewer",
            ctx=get_mock_ctx(),
            changes=[
                ModifyText(
                    target_text="30 days",
                    new_text="45 days",
                    comment="Extend payment term.",
                ),
                ModifyText(
                    target_text="State of Delaware",
                    new_text="State of New York",
                ),
            ],
        )
    )
    text1 = extract_content(res1)
    assert "**IDs:**" in text1
    assert "`Chg:1`" in text1
    assert "`Com:1`" in text1
    assert "Document State:" in text1
    assert "comment(s) remaining" in text1

    # Step 2: Accept Chg:1, Reply to Com:1, and check action receipts
    res2 = run_async(
        process_document_batch(
            reasoning="step 2 actions",
            original_docx_path=str(doc_path),
            author_name="Reviewer",
            ctx=get_mock_ctx(),
            changes=[
                AcceptChange(target_id="Chg:1"),
                ReplyComment(target_id="Com:1", text="Agreed on 45 days."),
            ],
        )
    )
    text2 = extract_content(res2)
    assert "Applied Actions:" in text2
    assert "`Chg:1`" in text2
    assert "`Com:1`" in text2
    assert "Document State:" in text2
