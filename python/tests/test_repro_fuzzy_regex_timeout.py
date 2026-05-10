# FILE: tests/test_repro_fuzzy_regex_timeout.py
"""
Regression test for the ITB 32.1 timeout case.

Background
----------
While exercising the Live Word redline path on an 18-page DOCX, sending a
ModifyText with an underline-heavy + markdown-wrapped target_text caused the
MCP server to hang (timed out after 4 minutes on a single edit).

Hypothesis: the pathology is in the fuzzy-match regex building, not in
document size. If the same target_text triggers the same behavior on a tiny
in-memory document, the bug is reproducible regardless of scale and we have
a small fixture to debug against.

What this test does
-------------------
Builds a minimal DOCX containing the exact ITB 32.1 line, then attempts the
exact same ModifyText that hung in production. The test simply observes the
engine's behaviour:

  * Engine returns normally with edit applied  -> PASS
  * Engine returns normally with edit skipped  -> PASS (still informative)
  * Engine raises any exception                 -> the test will surface it
  * Engine hangs                                -> pytest will hang too,
    which is the desired diagnostic. (Run with `pytest --timeout=30` or a
    SIGALRM wrapper if you want a hard kill; we deliberately avoid imposing
    a wall-clock budget here so the failure mode is faithful.)

We exercise the DISK path because it's cross-platform and uses the same
fuzzy-regex code path (mapper._make_fuzzy_regex) that backs the Live Word
disk-fallback. If the disk path passes, the next step is a
Windows-only sibling test against the Live Word fuzzy matcher in
mcp_components/tools/live_word.markup._make_fuzzy_regex.
"""

import io
import zipfile

from adeu.models import ModifyText
from adeu.redline.engine import BatchValidationError, RedlineEngine

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

# The exact line that appears in the BDS table for ITB 32.1 in the SPD doc.
# Intentionally preserves: 8x underscore run, **_..._** wrappers, parenthetical
# "(e.g., the Central Bank in the Employer's Country)" with comma + period
# inside the brackets. These are the regex tokens that may have caused the
# fuzzy regex to backtrack pathologically.
_ITB_32_1_LINE = (
    "The source of exchange rate shall be: ________ "
    "[Insert name of the source of exchange rates "
    "(e.g., the Central Bank in the Employer\u2019s Country).]"
)

# Pathological target_text (matches the original failing call). Note the
# embedded markdown wrappers and the long underscore run.
_PATHOLOGICAL_TARGET = (
    "The source of exchange rate shall be: ________"
    "**_ [Insert name of the source of exchange rates "
    "(e.g., the Central Bank in the Employer\u2019s Country).]_**"
)

_NEW_TEXT = "The source of exchange rate shall be: Helsinki"


def _build_minimal_docx_with_line(line: str) -> io.BytesIO:
    """Builds a tiny .docx containing exactly one paragraph with the given line.

    We bypass python-docx's Document() constructor here so the run boundaries
    match what the SPD doc would emit: a single run containing the literal
    underscores and bracket placeholder. python-docx would otherwise split
    the text across multiple runs based on style application.
    """
    # Render the line as one <w:r><w:t> with xml:space=preserve so the
    # underscore run survives whitespace normalization.
    escaped = line.replace("&", "&amp;").replace("<", "&lt;")
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {W_NS}>\n"
        "  <w:body>\n"
        "    <w:p>\n"
        f'      <w:r><w:t xml:space="preserve">{escaped}</w:t></w:r>\n'
        "    </w:p>\n"
        "    <w:sectPr/>\n"
        "  </w:body>\n"
        "</w:document>"
    ).encode("utf-8")

    rels = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1"'
        b' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        b' Target="word/document.xml"/>'
        b"</Relationships>"
    )

    ct = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels"'
        b' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Override PartName="/word/document.xml"'
        b' ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc_xml)
    buf.seek(0)
    return buf


def test_pathological_target_text_does_not_hang():
    """The pathological target should resolve in finite time, pass or fail.

    We do not assert success/failure of the edit itself — only that the call
    returns. Whether it applies, validates and rejects, or raises a
    structured BatchValidationError is all acceptable. What is NOT acceptable
    is the engine spinning on a regex backtrack and never returning.
    """
    stream = _build_minimal_docx_with_line(_ITB_32_1_LINE)
    engine = RedlineEngine(stream, author="Adeu Test")

    edit = ModifyText(
        type="modify",
        target_text=_PATHOLOGICAL_TARGET,
        new_text=_NEW_TEXT,
        comment=None,
    )

    # The call must return (one way or another) within reasonable time.
    # We accept any of: applied, skipped with detail, or raised
    # BatchValidationError. We reject only "hangs forever."
    try:
        stats = engine.process_batch([edit])
    except BatchValidationError:
        # Acceptable failure mode — validator made a deterministic decision.
        return

    assert "edits_applied" in stats
    assert stats["edits_applied"] + stats["edits_skipped"] == 1


def test_simplified_target_text_succeeds_on_same_doc():
    """Sanity check: with the markdown stripped from the target, the edit
    should apply cleanly. This is the exact rewrite that worked in
    production (Live Word path) and serves as the contrast case.
    """
    stream = _build_minimal_docx_with_line(_ITB_32_1_LINE)
    engine = RedlineEngine(stream, author="Adeu Test")

    edit = ModifyText(
        type="modify",
        target_text="The source of exchange rate shall be:",
        new_text=_NEW_TEXT,
        comment=None,
    )

    stats = engine.process_batch([edit])
    assert stats["edits_applied"] == 1, stats
    assert stats["edits_skipped"] == 0, stats
