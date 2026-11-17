"""
`commented_change_granularity="word"`: a commented modify keeps the surgical,
word-level redline an uncommented modify gets, without reintroducing the
2026-07-22 bug #1 (see test_repro_comment_fragment_reject.py).

That bug came from anchoring the comment to ONE fragment, so rejecting that
fragment removed the comment's whole anchor and the comment with it. In "word"
mode the edit's single comment spans from its first fragment to its last, with
the range markers outside every w:ins/w:del: rejecting any one fragment leaves
the rest of the anchor, and the comment, in place. The default ("atomic")
is unchanged and is pinned here too.
"""

import io
import re
import zipfile
from pathlib import Path

import pytest
from docx import Document
from ooxml_integrity import Severity, check

from adeu.models import ModifyText, RejectChange
from adeu.redline.engine import RedlineEngine

CLAUSE = "Either party may terminate this Agreement upon thirty (30) days' written notice to the other party."
COMMENT_TEXT = "Client requested a longer notice period; please confirm with legal."
WORD = "word"


def _docx(*paragraphs: str, table: list[list[str]] | None = None, header: str | None = None) -> io.BytesIO:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    if table:
        t = doc.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, text in enumerate(row):
                t.cell(r, c).text = text
    if header:
        doc.sections[0].header.paragraphs[0].text = header
    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return out


def _apply(source: io.BytesIO, edits: list, granularity: str | None = WORD) -> tuple[io.BytesIO, dict]:
    kwargs = {} if granularity is None else {"commented_change_granularity": granularity}
    engine = RedlineEngine(io.BytesIO(source.getvalue()), author="Reviewer", **kwargs)
    result = engine.process_batch(edits)
    return engine.save_to_stream(), result


def _document_xml(stream: io.BytesIO, part: str = "word/document.xml") -> str:
    return zipfile.ZipFile(io.BytesIO(stream.getvalue())).read(part).decode("utf-8")


def _comment_texts(stream: io.BytesIO) -> list[str]:
    zf = zipfile.ZipFile(io.BytesIO(stream.getvalue()))
    texts: list[str] = []
    for name in zf.namelist():
        if re.match(r"word/comments\d*\.xml$", name):
            for body in re.findall(r"<w:comment\b.*?</w:comment>", zf.read(name).decode("utf-8"), re.S):
                texts.append("".join(re.findall(r"<w:t[^>]*>([^<]*)<", body)))
    return texts


def _revision_ids(xml: str) -> tuple[list[str], list[str]]:
    ins = re.findall(r"<w:ins\b[^>]*\bw:id=\"(\d+)\"", xml)
    dels = re.findall(r"<w:del\b[^>]*\bw:id=\"(\d+)\"", xml)
    return sorted(set(ins), key=int), sorted(set(dels), key=int)


def _anchored_sequence(xml: str) -> list[str]:
    """Range markers and revisions of the body, in document order."""
    tokens = []
    for m in re.finditer(
        r'<w:commentRangeStart w:id="(\d+)"/>|<w:commentRangeEnd w:id="(\d+)"/>|<w:(ins|del)\b[^>]*>', xml
    ):
        if m.group(1):
            tokens.append("start")
        elif m.group(2):
            tokens.append("end")
        else:
            tokens.append(m.group(3))
    return tokens


def _commented_clause() -> io.BytesIO:
    edited, _ = _apply(
        _docx(CLAUSE), [ModifyText(target_text="thirty (30) days'", new_text="sixty (60) days'", comment=COMMENT_TEXT)]
    )
    return edited


def test_default_is_atomic():
    """No kwarg: one deletion and one insertion, exactly as before this option."""
    edited, _ = _apply(
        _docx(CLAUSE),
        [ModifyText(target_text="thirty (30) days'", new_text="sixty (60) days'", comment=COMMENT_TEXT)],
        granularity=None,
    )

    ins, dels = _revision_ids(_document_xml(edited))
    assert (len(ins), len(dels)) == (1, 1)


def test_unknown_granularity_is_refused():
    with pytest.raises(ValueError, match="commented_change_granularity"):
        RedlineEngine(_docx(CLAUSE), commented_change_granularity="sentence")


def test_a_commented_modify_word_splits_under_one_comment():
    edited = _commented_clause()
    xml = _document_xml(edited)

    ins, dels = _revision_ids(xml)
    assert len(ins) >= 2 and len(dels) >= 2, "a commented modify should fan out like an uncommented one"
    assert _comment_texts(edited) == [COMMENT_TEXT]
    sequence = _anchored_sequence(xml)
    assert sequence[0] == "start" and sequence[-1] == "end", sequence
    assert sequence.count("start") == 1


@pytest.mark.parametrize("position", ["first", "last"])
def test_rejecting_one_fragment_keeps_the_comment(position: str):
    """The 2026-07-22 bug #1, in word mode: no fragment owns the comment's whole anchor."""
    edited = _commented_clause()
    ins, dels = _revision_ids(_document_xml(edited))
    ids = sorted({*ins, *dels}, key=int)
    target = ids[0] if position == "first" else ids[-1]

    engine = RedlineEngine(io.BytesIO(edited.getvalue()), author="Reviewer")
    result = engine.process_batch([RejectChange(target_id=f"Chg:{target}")])
    out = engine.save_to_stream()

    assert result["actions_applied"] == 1
    assert _comment_texts(out) == [COMMENT_TEXT]
    assert "removed" not in "\n".join(result["skipped_details"]).lower()


def test_a_multi_paragraph_modify_spans_every_paragraph():
    first = "The Supplier shall deliver the goods within ten days."
    second = "The Customer shall pay within thirty days of delivery."
    edited, result = _apply(
        _docx(first, second),
        [
            ModifyText(
                target_text=f"{first}\n\n{second}",
                new_text=f"{first.replace('ten', 'five')}\n\n{second.replace('thirty', 'forty-five')}",
                comment="Faster delivery, longer payment.",
            )
        ],
    )
    xml = _document_xml(edited)
    paragraphs = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)

    assert result["edits_applied"] == 1
    assert "commentRangeStart" in paragraphs[0] and "commentRangeEnd" not in paragraphs[0]
    assert "commentRangeEnd" in paragraphs[1]
    assert _comment_texts(edited) == ["Faster delivery, longer payment."]


def test_a_table_cell_modify_word_splits_under_one_comment():
    edited, _ = _apply(
        _docx(table=[["Fee: one hundred dollars", "Due: on signing"]]),
        [
            ModifyText(
                target_text="Fee: one hundred dollars", new_text="Fee: two hundred US dollars", comment="Rate card."
            )
        ],
    )
    xml = _document_xml(edited)

    ins, _dels = _revision_ids(xml)
    assert len(ins) >= 2
    assert _comment_texts(edited) == ["Rate card."]
    assert _anchored_sequence(xml)[0] == "start"


def test_every_occurrence_of_match_mode_all_gets_its_own_comment():
    edited, _ = _apply(
        _docx("Acme Corp shall pay.", "Acme Corp shall deliver."),
        [ModifyText(target_text="Acme Corp", new_text="Acme Holdings Inc", comment="Name change.", match_mode="all")],
    )

    assert _comment_texts(edited) == ["Name change.", "Name change."]


def test_a_header_edit_drops_its_comment_with_the_same_warning_as_atomic():
    """Word has no comments in headers; the tracked change still applies."""
    source = _docx("Body.", header="Acme Corp Confidential Draft")
    edit = ModifyText(target_text="Acme Corp Confidential Draft", new_text="Acme Holdings Final", comment="Rename.")

    word, word_result = _apply(source, [edit])
    _atomic, atomic_result = _apply(source, [edit], granularity="atomic")

    assert word_result["edits_applied"] == 1
    assert "w:ins" in _document_xml(word, "word/header1.xml")
    assert _comment_texts(word) == []
    warning = [d for d in word_result["skipped_details"] if "NOT attached" in d]
    assert warning and warning == [d for d in atomic_result["skipped_details"] if "NOT attached" in d]


def test_word_mode_output_is_valid_ooxml(tmp_path: Path):
    output = tmp_path / "output.docx"
    output.write_bytes(_commented_clause().getvalue())

    errors = [f for f in check(output) if f.severity is Severity.ERROR]
    assert not errors, "\n".join(str(f) for f in errors)
