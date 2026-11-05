"""
`comment` on a modify accepts a list of comments, each with its own author.

A reviewer often needs two notes on one change under different labels — an
internal note and a counterparty-facing one ("Jane Doe (Internal)" / "Jane
Doe (Vendor)"). Each list entry becomes its own comment on the same anchor;
an entry without an author uses the engine author. A plain string comment
behaves exactly as before.

Node twin: node/packages/core/src/comment-list.test.ts
"""

import io
import json
import re

import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from pydantic import TypeAdapter, ValidationError

from adeu.markup import apply_edits_to_markdown
from adeu.models import DocumentChange, EditComment, FlatSchemaDocumentChange, ModifyText
from adeu.redline.engine import RedlineEngine

PARAGRAPH = "The quick brown fox."


def _apply(edit: ModifyText, paragraph: str = PARAGRAPH) -> tuple[dict, str, str]:
    doc = Document()
    doc.add_paragraph(paragraph)
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    engine = RedlineEngine(stream, author="Jane Doe")
    result = engine.process_batch([edit])
    out = Document(engine.save_to_stream())
    comments_part = next(rel.target_part for rel in out.part.rels.values() if rel.reltype == RT.COMMENTS)
    return result, out.element.xml, comments_part.blob.decode("utf-8")


def _authors_by_text(comments_xml: str) -> dict:
    pairs = re.findall(r'<w:comment\b[^>]*w:author="([^"]*)"[^>]*>.*?<w:t[^>]*>([^<]*)</w:t>', comments_xml, re.S)
    return {text: author for author, text in pairs}


def _range_starts(doc_xml: str) -> list:
    return re.findall(r'<w:commentRangeStart[^>]*w:id="(\d+)"', doc_xml)


def test_each_entry_becomes_a_comment_under_its_own_author():
    edit = ModifyText(
        target_text="quick",
        new_text="slow",
        comment=[
            EditComment(text="Internal: foxes vary.", author="Jane Doe (Internal)"),
            EditComment(text="Vendor: please confirm.", author="Jane Doe (Vendor)"),
        ],
    )

    _, _, comments_xml = _apply(edit)

    assert _authors_by_text(comments_xml) == {
        "Internal: foxes vary.": "Jane Doe (Internal)",
        "Vendor: please confirm.": "Jane Doe (Vendor)",
    }


def test_entries_share_one_anchor():
    edit = ModifyText(target_text="quick", new_text="slow", comment=[{"text": "One"}, {"text": "Two"}])

    _, doc_xml, _ = _apply(edit)

    starts = _range_starts(doc_xml)
    ends = re.findall(r'<w:commentRangeEnd[^>]*w:id="(\d+)"', doc_xml)
    assert len(starts) == 2
    assert sorted(ends) == sorted(starts)
    between_starts = doc_xml.split(f'w:id="{starts[0]}"')[1].split(f'w:id="{starts[1]}"')[0]
    assert "<w:t" not in between_starts


def test_entry_without_author_uses_the_engine_author():
    edit = ModifyText(target_text="quick", new_text="slow", comment=[{"text": "No author given"}])

    _, _, comments_xml = _apply(edit)

    assert _authors_by_text(comments_xml) == {"No author given": "Jane Doe"}


def test_string_comment_still_writes_one_comment_under_the_engine_author():
    _, _, comments_xml = _apply(ModifyText(target_text="quick", new_text="slow", comment="Plain comment"))

    assert _authors_by_text(comments_xml) == {"Plain comment": "Jane Doe"}


def test_a_comment_list_survives_a_word_level_split_on_a_comment_only_edit():
    sentence = "The Supplier shall deliver all of the ordered goods to the Customer within thirty days."
    edit = ModifyText(
        target_text=sentence,
        new_text=sentence,
        comment=[EditComment(text="Internal note", author="A"), EditComment(text="Vendor note", author="B")],
    )

    _, doc_xml, comments_xml = _apply(edit, paragraph=sentence)

    assert _authors_by_text(comments_xml) == {"Internal note": "A", "Vendor note": "B"}
    assert len(_range_starts(doc_xml)) == 2


def test_report_echoes_the_list_as_json():
    edit = ModifyText(
        target_text="quick", new_text="slow", comment=[EditComment(text="One", author="A"), EditComment(text="Two")]
    )

    result, _, _ = _apply(edit)

    assert result["edits"][0]["comment"] == [{"text": "One", "author": "A"}, {"text": "Two"}]
    json.dumps(result)


def test_comment_text_joins_the_entries():
    edit = ModifyText(target_text="a", new_text="b", comment=[{"text": "One"}, {"text": "Two"}])

    assert edit.comment_text == "One\n\nTwo"
    assert ModifyText(target_text="a", new_text="b", comment="Solo").comment_text == "Solo"


def test_markdown_preview_shows_the_joined_comment():
    preview = apply_edits_to_markdown(
        "The quick brown fox.",
        [ModifyText(target_text="quick", new_text="slow", comment=[{"text": "One"}, {"text": "Two"}])],
    )

    assert "{>>One\n\nTwo<<}" in preview


def test_malformed_comment_is_rejected_by_validation():
    with pytest.raises(ValidationError):
        TypeAdapter(DocumentChange).validate_python(
            {"type": "modify", "target_text": "a", "new_text": "b", "comment": [{"author": "A"}]}
        )
    with pytest.raises(ValidationError):
        TypeAdapter(DocumentChange).validate_python(
            {"type": "modify", "target_text": "a", "new_text": "b", "comment": 42}
        )


def test_control_characters_in_an_entry_are_rejected():
    doc = Document()
    doc.add_paragraph(PARAGRAPH)
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream, author="Jane Doe")
    result = engine.process_batch(
        [ModifyText(target_text="quick", new_text="slow", comment=[{"text": "bad\x07bell"}])], partial=True
    )

    assert result["edits_applied"] == 0
    assert "control character" in result["failed"][0]["reason"]


def test_published_schemas_inline_the_entry_shape():
    flat = TypeAdapter(list[FlatSchemaDocumentChange]).json_schema()
    assert "$defs" not in flat
    variants = flat["items"]["properties"]["comment"]["anyOf"]
    array = next(v for v in variants if v.get("type") == "array")
    assert array["items"]["required"] == ["text"]
    assert set(array["items"]["properties"]) == {"text", "author"}

    canonical = TypeAdapter(list[DocumentChange]).json_schema()
    assert "EditComment" not in canonical["$defs"]
