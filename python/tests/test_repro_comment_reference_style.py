"""
Comment reference typography: the styles Adeu's comment XML has always
referenced but never defined.

`word/document.xml` wraps every `w:commentReference` in
`<w:rStyle w:val="CommentReference"/>` (engine.py `_attach_comment`,
`_attach_comment_spanning`, `_anchor_reply_comment`) and every comment
paragraph in `word/comments.xml` carries `<w:pStyle w:val="CommentText"/>`
(comments.py `add_comment`). When `word/styles.xml` defines neither, Word and
LibreOffice silently ignore the dangling reference and render the marker at the
surrounding body size (11-12pt) instead of the 8pt superscript Word writes
natively, and extraction tools warn that both style ids are referenced but not
defined.

Twin of node/packages/core/src/repro.comment-reference-style.test.ts — the
emitted definitions must be identical in both engines.
"""

import io
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn

from adeu.models import ModifyText
from adeu.redline.comments import CommentsManager
from adeu.redline.engine import RedlineEngine
from adeu.utils.docx import _get_style_cache

FIXTURES = Path(__file__).resolve().parents[2] / "shared" / "fixtures"
BASE = FIXTURES / "base.docx"


def _edit(target_text: str, new_text: str) -> ModifyText:
    """A commenting edit, freshly built for one `apply_edits` call.

    Never a module-level constant. `ModifyText` inherits `_EditState`, whose
    PrivateAttrs are the engine's per-edit scratch space, and `apply_edits`
    resets only three of them (`engine.py:4132-4135`) — `_resolved_start_idx`
    and `_match_start_index` survive. Reusing one instance across tests
    therefore carries a resolved offset from a PREVIOUS document into the next
    run, which made the golden.docx guard below report `(1, 0)` in a full-file
    run and `(0, 1)` when run alone.
    """
    return ModifyText(
        target_text=target_text,
        new_text=new_text,
        comment="Aligning this with the SLA wording.",
    )


# base.docx defines Normal but NOT DefaultParagraphFont and NOT either comment
# style, so it exercises injection AND the conditional w:basedOn at once.
BASE_EDIT_TARGET = ("reasonable skill and care", "reasonable skill, care and diligence")

# golden.docx is Word-authored and holds none of base.docx's contract wording;
# its whole body is "This is the golden document". The target only has to make
# the engine attach a comment — the assertions are about styles.xml.
GOLDEN_EDIT_TARGET = ("golden", "gilded")


def _styles_root(source):
    """The `<w:styles>` element of a saved DOCX (stream or bytes)."""
    with zipfile.ZipFile(source) as z:
        return parse_xml(z.read("word/styles.xml"))


def _styles_by_id(styles_root, style_id: str) -> list:
    return [s for s in styles_root.findall(qn("w:style")) if s.get(qn("w:styleId")) == style_id]


def _style_by_id(styles_root, style_id: str):
    found = _styles_by_id(styles_root, style_id)
    return found[0] if found else None


def _child_tags(style_el) -> list:
    return [child.tag for child in style_el]


def _run_comment_edit(stream, target=BASE_EDIT_TARGET) -> io.BytesIO:
    engine = RedlineEngine(stream)
    applied, skipped = engine.apply_edits([_edit(*target)])
    assert (applied, skipped) == (1, 0)
    return engine.save_to_stream()


def test_comment_styles_are_injected_when_missing():
    root = _styles_root(_run_comment_edit(io.BytesIO(BASE.read_bytes())))

    ref = _style_by_id(root, "CommentReference")
    assert ref is not None, "CommentReference must be defined in word/styles.xml"
    assert ref.get(qn("w:type")) == "character"
    assert ref.find(qn("w:name")).get(qn("w:val")) == "annotation reference"
    assert ref.find(qn("w:uiPriority")).get(qn("w:val")) == "99"
    assert ref.find(qn("w:semiHidden")) is not None
    assert ref.find(qn("w:unhideWhenUsed")) is not None
    ref_rpr = ref.find(qn("w:rPr"))
    assert ref_rpr.find(qn("w:sz")).get(qn("w:val")) == "16"
    assert ref_rpr.find(qn("w:szCs")).get(qn("w:val")) == "16"

    txt = _style_by_id(root, "CommentText")
    assert txt is not None, "CommentText must be defined in word/styles.xml"
    assert txt.get(qn("w:type")) == "paragraph"
    assert txt.find(qn("w:name")).get(qn("w:val")) == "annotation text"
    assert txt.find(qn("w:uiPriority")).get(qn("w:val")) == "99"
    assert txt.find(qn("w:semiHidden")) is not None
    assert txt.find(qn("w:unhideWhenUsed")) is not None
    txt_rpr = txt.find(qn("w:rPr"))
    assert txt_rpr.find(qn("w:sz")).get(qn("w:val")) == "20"
    assert txt_rpr.find(qn("w:szCs")).get(qn("w:val")) == "20"


def test_injected_styles_follow_ct_style_child_order():
    # ISO/IEC 29500 CT_Style sequence: name, basedOn, uiPriority, semiHidden,
    # unhideWhenUsed, rPr. Word tolerates a lot; the schema does not.
    root = _styles_root(_run_comment_edit(io.BytesIO(BASE.read_bytes())))

    # base.docx has no DefaultParagraphFont, so CommentReference gets no basedOn.
    assert _child_tags(_style_by_id(root, "CommentReference")) == [
        qn("w:name"),
        qn("w:uiPriority"),
        qn("w:semiHidden"),
        qn("w:unhideWhenUsed"),
        qn("w:rPr"),
    ]
    # base.docx DOES define Normal, so CommentText keeps its basedOn.
    assert _child_tags(_style_by_id(root, "CommentText")) == [
        qn("w:name"),
        qn("w:basedOn"),
        qn("w:uiPriority"),
        qn("w:semiHidden"),
        qn("w:unhideWhenUsed"),
        qn("w:rPr"),
    ]
    assert _style_by_id(root, "CommentText").find(qn("w:basedOn")).get(qn("w:val")) == "Normal"


def test_based_on_is_written_when_the_base_style_exists():
    # initial.docx DOES define DefaultParagraphFont, so the full requirement
    # XML applies there.
    doc = Document(io.BytesIO((FIXTURES / "initial.docx").read_bytes()))
    CommentsManager(doc).add_comment("Adeu AI", "Typography check.")

    styles_root = doc.part.styles.element
    ref = _style_by_id(styles_root, "CommentReference")
    assert ref.find(qn("w:basedOn")).get(qn("w:val")) == "DefaultParagraphFont"
    txt = _style_by_id(styles_root, "CommentText")
    assert txt.find(qn("w:basedOn")).get(qn("w:val")) == "Normal"


def test_existing_comment_styles_are_neither_duplicated_nor_rewritten():
    # golden.docx already carries Word's own definitions, complete with the
    # w:rsid / w:link children this engine never writes.
    src = io.BytesIO((FIXTURES / "golden.docx").read_bytes())
    before = _styles_root(io.BytesIO(src.getvalue()))
    assert _style_by_id(before, "CommentReference").find(qn("w:rsid")) is not None
    assert _style_by_id(before, "CommentText").find(qn("w:link")) is not None

    root = _styles_root(_run_comment_edit(src, GOLDEN_EDIT_TARGET))

    assert len(_styles_by_id(root, "CommentReference")) == 1
    assert len(_styles_by_id(root, "CommentText")) == 1
    # Word's own definitions survive untouched: an injected copy would have no
    # w:rsid and no w:link.
    assert _style_by_id(root, "CommentReference").find(qn("w:rsid")) is not None
    assert _style_by_id(root, "CommentText").find(qn("w:link")) is not None


def test_no_injection_when_the_annotation_names_exist_under_other_style_ids():
    doc = Document(io.BytesIO(BASE.read_bytes()))
    styles_el = doc.part.styles.element
    # Deliberately mixed case: the name match is case-insensitive.
    for style_id, s_type, name in (
        ("AnnotRef", "character", "Annotation Reference"),
        ("AnnotTxt", "paragraph", "ANNOTATION TEXT"),
    ):
        style = OxmlElement("w:style")
        style.set(qn("w:type"), s_type)
        style.set(qn("w:styleId"), style_id)
        name_el = OxmlElement("w:name")
        name_el.set(qn("w:val"), name)
        style.append(name_el)
        styles_el.append(style)
    prepared = io.BytesIO()
    doc.save(prepared)
    prepared.seek(0)

    root = _styles_root(_run_comment_edit(prepared))

    assert _style_by_id(root, "CommentReference") is None
    assert _style_by_id(root, "CommentText") is None


def test_styles_part_is_created_when_the_package_has_none():
    stripped = io.BytesIO()
    with zipfile.ZipFile(BASE) as zin, zipfile.ZipFile(stripped, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "word/styles.xml":
                continue
            data = zin.read(item.filename)
            if item.filename == "word/_rels/document.xml.rels":
                data = re.sub(
                    rb'<Relationship[^>]*Target="styles\.xml"[^>]*/>',
                    b"",
                    data,
                )
            zout.writestr(item, data)
    stripped.seek(0)

    saved = _run_comment_edit(stripped)

    with zipfile.ZipFile(saved) as z:
        assert "word/styles.xml" in z.namelist()
        content_types = z.read("[Content_Types].xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    assert 'PartName="/word/styles.xml"' in content_types
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml" in content_types
    assert 'Target="styles.xml"' in rels

    root = _styles_root(saved)
    assert _style_by_id(root, "CommentReference") is not None
    assert _style_by_id(root, "CommentText") is not None


def test_style_cache_is_invalidated_after_injection():
    doc = Document(io.BytesIO(BASE.read_bytes()))
    package = doc.part.package

    stale, _ = _get_style_cache(doc.part)
    assert "CommentReference" not in stale
    assert hasattr(package, "_adeu_style_cache")

    CommentsManager(doc).add_comment("Adeu AI", "Check this figure.")

    assert not hasattr(package, "_adeu_style_cache"), (
        "the projection's style cache was built from the styles.xml we just changed"
    )
    fresh, _ = _get_style_cache(doc.part)
    assert fresh["CommentReference"]["name"] == "annotation reference"
    assert fresh["CommentText"]["name"] == "annotation text"
