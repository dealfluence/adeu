"""Block content may not land in a page header or footer by accident; edits outside the body say where they landed.

final-2's indenture runs anchored an executive summary (heading + table) on the document title, which was the page
header's text: the summary was written into word/header1.xml and repeated on every page. Twin:
node/packages/core/src/header_block_guard.test.ts.
"""

from io import BytesIO

import pytest
from docx import Document

from adeu.models import ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine

TITLE = "Pinnacle Health Systems, Inc. Indenture"


def _doc() -> bytes:
    d = Document()
    sec = d.sections[0]
    sec.header.is_linked_to_previous = False
    sec.header.paragraphs[0].text = TITLE
    sec.footer.is_linked_to_previous = False
    sec.footer.paragraphs[0].text = "Confidential draft"
    d.add_paragraph("ARTICLE 1 DEFINITIONS")
    d.add_paragraph("Consolidated EBITDA means net income plus addbacks.")
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def _batch(changes):
    eng = RedlineEngine(BytesIO(_doc()), author="Purchaser Counsel")
    return eng, eng.process_batch(changes)


@pytest.mark.parametrize(
    "new_text",
    [
        f"# Executive Summary\n\n{TITLE}",
        f"| # | Section | Position |\n|---|---|---|\n| 1 | Art. 1 | Cap at 25% |\n\n{TITLE}",
    ],
)
def test_heading_or_table_into_the_page_header_is_refused_with_the_first_body_paragraph(new_text):
    with pytest.raises(BatchValidationError) as exc:
        _batch([ModifyText(target_text=TITLE, new_text=new_text)])
    msg = "\n".join(exc.value.errors)
    assert "page header" in msg
    assert "header1.xml" in msg
    assert "ARTICLE 1 DEFINITIONS" in msg


def test_a_repeated_first_body_paragraph_hint_says_how_to_target_it():
    # The indenture's first body line ("## **INDENTURE**") recurs later; a hint that walks into the ambiguity guard
    # is no hint at all.
    d = Document()
    sec = d.sections[0]
    sec.header.is_linked_to_previous = False
    sec.header.paragraphs[0].text = TITLE
    d.add_paragraph("INDENTURE")
    d.add_paragraph("This INDENTURE is dated as of the Issue Date.")
    d.add_paragraph("INDENTURE")
    b = BytesIO()
    d.save(b)
    eng = RedlineEngine(BytesIO(b.getvalue()), author="Purchaser Counsel")
    with pytest.raises(BatchValidationError) as exc:
        eng.process_batch([ModifyText(target_text=TITLE, new_text=f"# Executive Summary\n\n{TITLE}")])
    msg = "\n".join(exc.value.errors)
    assert "'INDENTURE' (it appears " in msg
    assert 'times: pass match_mode="first")' in msg


def test_table_into_the_footer_points_at_the_last_body_paragraph():
    with pytest.raises(BatchValidationError) as exc:
        _batch([ModifyText(target_text="Confidential draft", new_text="Confidential draft\n\n| a | b |\n|---|---|")])
    msg = "\n".join(exc.value.errors)
    assert "page footer" in msg
    assert "Consolidated EBITDA means net income plus addbacks." in msg


def test_wording_change_in_the_header_applies_and_the_receipt_names_the_part():
    eng, stats = _batch([ModifyText(target_text="Indenture", new_text="Indenture (Purchaser Draft)")])
    assert (stats["edits_applied"], stats["edits_skipped"]) == (1, 0)
    assert stats["edits"][0]["part"] == "header (word/header1.xml)"


def test_body_edit_receipt_has_no_part_label():
    eng, stats = _batch([ModifyText(target_text="net income", new_text="consolidated net income")])
    assert stats["edits"][0]["part"] == ""
