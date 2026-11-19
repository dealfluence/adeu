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

import asyncio
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from ooxml_integrity import Severity, check

import adeu
from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText, RejectChange
from adeu.redline.engine import RedlineEngine
from tests.utils import run_cli

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


def _wrapped_clause(tag: str) -> io.BytesIO:
    """A clause whose only run sits inside a w:hyperlink or another author's pending w:ins."""
    doc = Document()
    paragraph = doc.add_paragraph("terminate upon thirty (30) days written notice")
    run = paragraph._p.findall(qn("w:r"))[0]
    wrapper = OxmlElement(tag)
    if tag == "w:hyperlink":
        wrapper.set(qn("w:anchor"), "notice")
    else:
        wrapper.set(qn("w:id"), "900")
        wrapper.set(qn("w:author"), "Counterparty")
        wrapper.set(qn("w:date"), "2024-01-01T00:00:00Z")
    run.addprevious(wrapper)
    wrapper.append(run)
    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return out


@pytest.mark.parametrize("tag", ["w:hyperlink", "w:ins"])
def test_fragments_nested_in_a_wrapper_still_get_one_comment(tag: str):
    """The range markers must climb to the paragraph's direct child, as every other anchor does."""
    edited, result = _apply(
        _wrapped_clause(tag),
        [ModifyText(target_text="thirty (30) days written", new_text="sixty (60) days prior written", comment="why")],
    )

    assert result["edits_applied"] == 1
    assert _comment_texts(edited) == ["why"]


def test_the_report_names_the_group_comment():
    """An agent replies to its own comment by the Com id the report gives it."""
    _edited, result = _apply(
        _docx(CLAUSE), [ModifyText(target_text="thirty (30) days'", new_text="sixty (60) days'", comment=COMMENT_TEXT)]
    )

    assert result["edits"][0].get("comment_ids") == ["Com:1"]


def test_a_rollback_keeps_the_granularity():
    engine = RedlineEngine(_docx(CLAUSE), author="Reviewer", commented_change_granularity=WORD)

    engine._restore_from_snapshot(_docx(CLAUSE))

    assert engine.commented_change_granularity == WORD


def test_package_root_exports_granularity_constants():
    assert adeu.COMMENTED_CHANGE_GRANULARITIES == ("atomic", "word")
    assert hasattr(adeu, "CommentedChangeGranularity")


def test_word_mode_spans_comment_across_kept_cross_reference():
    """Changes on both sides of a kept [~2.1~](#_Ref1) field share one comment that survives partial reject."""
    doc = Document()
    p = doc.add_paragraph()
    p.add_run("See Section ")
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), " REF _Ref1 \\h ")
    fld_run = OxmlElement("w:r")
    fld_text = OxmlElement("w:t")
    fld_text.text = "2.1"
    fld_run.append(fld_text)
    fld.append(fld_run)
    p._p.append(fld)
    p.add_run(" for New York governing law.")
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    edited, result = _apply(
        buf,
        [
            ModifyText(
                target_text="See Section [~2.1~](#_Ref1) for New York governing law.",
                new_text="Under Section [~2.1~](#_Ref1) for Delaware governing law.",
                comment=COMMENT_TEXT,
            )
        ],
    )
    assert result["edits_applied"] == 1
    assert _comment_texts(edited) == [COMMENT_TEXT]
    assert "w:fldSimple" in _document_xml(edited)

    ins_ids, _ = _revision_ids(_document_xml(edited))
    engine = RedlineEngine(io.BytesIO(edited.getvalue()), author="Reviewer")
    reject_res = engine.process_batch([RejectChange(target_id=f"Chg:{ins_ids[0]}")])
    after_reject = engine.save_to_stream()

    assert reject_res["actions_applied"] == 1
    assert _comment_texts(after_reject) == [COMMENT_TEXT]
    clean = extract_text_from_stream(io.BytesIO(after_reject.getvalue()), clean_view=True)
    assert "See Section" in clean
    assert "Delaware governing law." in clean


def test_cli_apply_commented_change_granularity_word(tmp_path: Path):
    doc_path = tmp_path / "clause.docx"
    edits_path = tmp_path / "edits.json"
    out_path = tmp_path / "clause_word.docx"
    doc_path.write_bytes(_docx(CLAUSE).getvalue())
    edits_path.write_text(
        json.dumps(
            [
                {
                    "type": "modify",
                    "target_text": "thirty (30) days'",
                    "new_text": "sixty (60) days'",
                    "comment": COMMENT_TEXT,
                }
            ]
        ),
        encoding="utf-8",
    )

    res = run_cli(
        "apply",
        str(doc_path),
        str(edits_path),
        "--commented-change-granularity",
        "word",
        "-o",
        str(out_path),
    )
    assert res.returncode == 0, res.stderr
    saved = io.BytesIO(out_path.read_bytes())
    ins_ids, del_ids = _revision_ids(_document_xml(saved))
    assert len(ins_ids) >= 2 and len(del_ids) >= 2
    assert _comment_texts(saved) == [COMMENT_TEXT]


def test_cli_apply_live_refuses_word_granularity(tmp_path: Path):
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(
        json.dumps(
            [
                {
                    "type": "modify",
                    "target_text": "thirty (30) days'",
                    "new_text": "sixty (60) days'",
                    "comment": COMMENT_TEXT,
                }
            ]
        ),
        encoding="utf-8",
    )

    res = run_cli(
        "apply",
        "--live",
        str(edits_path),
        "--commented-change-granularity",
        "word",
        "--json",
    )
    assert res.returncode != 0
    payload = json.loads(res.stdout)
    assert payload["error"] == "unsupported"
    assert "commented-change-granularity" in payload["message"] or "commented_change_granularity" in payload["message"]


def test_mcp_process_document_batch_word_granularity(tmp_path: Path):
    from fastmcp import Client

    from adeu.server import mcp

    doc_path = tmp_path / "mcp_clause.docx"
    out_path = tmp_path / "mcp_clause_word.docx"
    doc_path.write_bytes(_docx(CLAUSE).getvalue())

    async def _call():
        async with Client(mcp) as client:
            return await client.call_tool(
                "process_document_batch",
                {
                    "original_docx_path": str(doc_path),
                    "author_name": "Reviewer",
                    "output_path": str(out_path),
                    "commented_change_granularity": "word",
                    "changes": [
                        {
                            "type": "modify",
                            "target_text": "thirty (30) days'",
                            "new_text": "sixty (60) days'",
                            "comment": COMMENT_TEXT,
                        }
                    ],
                },
            )

    result = asyncio.run(_call())
    assert out_path.exists(), result
    saved = io.BytesIO(out_path.read_bytes())
    ins_ids, del_ids = _revision_ids(_document_xml(saved))
    assert len(ins_ids) >= 2 and len(del_ids) >= 2
    assert _comment_texts(saved) == [COMMENT_TEXT]


@pytest.mark.skipif(sys.platform != "win32", reason="Live Word path only registered on win32")
def test_mcp_live_word_refuses_word_granularity():
    from adeu.server import mcp

    result = asyncio.run(
        mcp.call_tool(
            "process_document_batch",
            {
                "author_name": "Reviewer",
                "commented_change_granularity": "word",
                "changes": [
                    {
                        "type": "modify",
                        "target_text": "thirty (30) days'",
                        "new_text": "sixty (60) days'",
                        "comment": COMMENT_TEXT,
                    }
                ],
            },
        )
    )
    text = result.content[0].text
    assert "Error:" in text
    assert "commented_change_granularity" in text
    assert "Live Word" in text or "disk" in text
