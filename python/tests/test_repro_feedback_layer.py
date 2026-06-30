import io

from docx import Document
from docx.oxml import parse_xml

from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine
from adeu.redline.mapper import DocumentMapper


def _build_simple_doc(text: str = "Hello world.") -> io.BytesIO:
    doc = Document()
    doc.add_paragraph(text)
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def test_process_batch_returns_detailed_edit_reports():
    """
    Verifies that process_batch returns a detailed edits list
    containing contextual previews, status, and engine versioning.
    """
    stream = _build_simple_doc("The quick brown fox jumps over the lazy dog.")
    engine = RedlineEngine(stream, author="Reviewer AI")

    stats = engine.process_batch([ModifyText(target_text="quick brown fox", new_text="fast red fox")])

    # Check baseline presence
    assert "edits" in stats
    assert len(stats["edits"]) == 1

    edit_report = stats["edits"][0]
    assert edit_report["status"] == "applied"
    assert edit_report["target_text"] == "quick brown fox"
    assert edit_report["new_text"] == "fast red fox"

    # Contextual check (should include surrounding words)
    assert "{--quick brown--}{++fast red++} fox" in edit_report["critic_markup"]
    assert "The " in edit_report["critic_markup"]
    assert " jumps over" in edit_report["critic_markup"]

    assert "The fast red fox jumps over" in edit_report["clean_text"]
    assert "engine" in stats and stats["engine"] == "python"
    assert "version" in stats


def test_punctuation_anchor_no_warning_on_clean_apply():
    """
    A punctuated anchor (underscores/hyphens) that matches and applies cleanly
    must NOT raise the tokenization-splitting warning. The redline preview
    (critic_markup/clean_text) already reports the change; a warning here is a
    false positive that pushes agents into needless "cleaner anchor" retries.
    """
    stream = _build_simple_doc("Refer to sample_term_name in Section 4.")
    engine = RedlineEngine(stream, author="Reviewer AI")

    stats = engine.process_batch([ModifyText(target_text="sample_term_name", new_text="validated_term_name")])

    edit_report = stats["edits"][0]
    assert edit_report["status"] == "applied"
    assert edit_report["warning"] is None

    # Same expectation under dry_run (validation + simulation path).
    stream2 = _build_simple_doc("Refer to sample_term_name in Section 4.")
    engine2 = RedlineEngine(stream2, author="Reviewer AI")
    dry_stats = engine2.process_batch(
        [ModifyText(target_text="sample_term_name", new_text="validated_term_name")],
        dry_run=True,
    )
    dry_report = dry_stats["edits"][0]
    assert dry_report["status"] == "applied"
    assert dry_report["warning"] is None


def test_punctuation_anchor_warns_only_when_match_fails():
    """
    When a punctuated anchor fails to match, the warning IS surfaced as
    recovery context (the punctuation may be why the match missed).
    """
    stream = _build_simple_doc("Refer to sample_term_name in Section 4.")
    engine = RedlineEngine(stream, author="Reviewer AI")

    # 'phantom_term-x' is short, space-free, punctuated, and absent from the doc.
    stats = engine.process_batch(
        [ModifyText(target_text="phantom_term-x", new_text="anything")],
        dry_run=True,
    )

    edit_report = stats["edits"][0]
    assert edit_report["status"] == "failed"
    assert edit_report["warning"] is not None
    assert "punctuation" in edit_report["warning"].lower()
    assert "phantom_term-x" in edit_report["warning"]


def test_dry_run_does_not_mutate_and_reports_safely():
    """
    Verifies that dry_run=True does not mutate the document
    and reports validation errors gracefully without raising exceptions.
    """
    stream = _build_simple_doc("Baseline text.")
    engine = RedlineEngine(stream, author="Reviewer AI")

    # 1. Valid Dry Run
    stats = engine.process_batch([ModifyText(target_text="Baseline", new_text="Modified Preview")], dry_run=True)

    assert stats["edits_applied"] == 1
    assert stats["edits"][0]["status"] == "applied"
    assert "Modified Preview" in stats["edits"][0]["clean_text"]

    # Verify original document was NOT mutated
    out_doc = Document(engine.save_to_stream())
    para_text = out_doc.paragraphs[0].text
    assert "Modified Preview" not in para_text
    assert "Baseline text" in para_text

    # 2. Invalid Dry Run (should report without throwing)
    stats_invalid = engine.process_batch([ModifyText(target_text="NON_EXISTENT", new_text="fail")], dry_run=True)

    assert stats_invalid["edits_skipped"] == 1
    assert stats_invalid["edits"][0]["status"] == "failed"
    assert stats_invalid["edits"][0]["error"] is not None
    assert "not found" in stats_invalid["edits"][0]["error"].lower()


def test_mcp_tool_feedback_formatting():
    import asyncio
    import tempfile
    from unittest.mock import AsyncMock

    from adeu.mcp_components.tools.document import _process_document_batch_disk

    class DummyContext:
        def __init__(self):
            self.info = AsyncMock()
            self.debug = AsyncMock()
            self.warning = AsyncMock()
            self.error = AsyncMock()

    stream = _build_simple_doc("The quick brown fox jumps over the lazy dog.")
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(stream.getvalue())
        tmp_path = tmp.name

    ctx = DummyContext()
    changes = [ModifyText(target_text="quick brown fox", new_text="fast red fox")]

    res = asyncio.run(
        _process_document_batch_disk(
            original_docx_path=tmp_path,
            author_name="Reviewer AI",
            ctx=ctx,
            changes=changes,
            output_path=None,
            dry_run=True,
        )
    )

    assert "Dry-run simulation complete." in res
    assert "Detailed Edit Reports:" in res
    assert "✅ [applied]" in res
    assert "The {--quick brown--}{++fast red++} fox jumps over" in res


def test_preview_self_consistency():
    """
    Verifies that the clean_text preview generated by process_batch matches
    the actual clean text of the modified document (written to disk/stream).
    Guards against paragraph-separator loss on underscore/hyphenated terms.
    """
    stream = _build_simple_doc("ANCHOR_LINE governs the interpretation of this Agreement.")
    engine = RedlineEngine(stream, author="Reviewer AI")

    stats = engine.process_batch(
        [
            ModifyText(
                target_text="ANCHOR_LINE governs the interpretation of this Agreement.",
                new_text="NEW_PARA inserted before.\n\nANCHOR_LINE governs the interpretation of this Agreement.",
            )
        ]
    )

    out_doc = Document(engine.save_to_stream())
    clean_mapper = DocumentMapper(out_doc, clean_view=True)
    clean_doc_text = clean_mapper.full_text

    preview_clean = stats["edits"][0]["clean_text"]
    assert preview_clean is not None
    preview_clean_stripped = preview_clean.strip(".")
    assert preview_clean_stripped in clean_doc_text


def test_preview_no_garbling_duplicates():
    """
    Scenario 2: Duplicated/overlapping text scenario (like 23-5) to guard
    against off-by-one / duplicate garbling (DUPDUP / eat L).
    """
    doc = Document()
    # Paragraph 1
    doc.add_paragraph("Payment Terms")

    # Paragraph 2 (Deleted copy via raw w:delText)
    p2 = doc.add_paragraph()
    p2_xml = parse_xml(
        r'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        r'<w:del w:id="900" w:author="Reviewer" w:date="2026-06-01T00:00:00Z">'
        r"<w:r>"
        r'<w:delText xml:space="preserve">DUP_PHRASE shall be paid within thirty days of invoice.</w:delText>'
        r"</w:r>"
        r"</w:del>"
        r"</w:p>"
    )
    p2._element.getparent().replace(p2._element, p2_xml)

    # Paragraph 3 (Live copy)
    doc.add_paragraph("DUP_PHRASE shall be paid within thirty days of invoice.")

    # Paragraph 4 (Immediate neighbor)
    doc.add_paragraph("Late payments accrue interest at the statutory rate.")

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    engine = RedlineEngine(stream, author="Reviewer AI")
    stats = engine.process_batch(
        [
            ModifyText(
                target_text="DUP_PHRASE shall be paid within thirty days of invoice.",
                new_text="DUP_PHRASE shall be paid within sixty days of invoice.",
            )
        ]
    )

    out_doc = Document(engine.save_to_stream())
    clean_mapper = DocumentMapper(out_doc, clean_view=True)
    clean_doc_text = clean_mapper.full_text

    preview_clean = stats["edits"][0]["clean_text"]
    assert preview_clean is not None
    preview_clean_stripped = preview_clean.strip(".")
    assert preview_clean_stripped in clean_doc_text
