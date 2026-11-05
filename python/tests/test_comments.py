import io
import re
import zipfile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from adeu.models import ModifyText
from adeu.redline.comments import CommentsManager
from adeu.redline.engine import RedlineEngine


def test_native_comment_creation_and_linking():
    doc = Document()
    doc.add_paragraph("The quick brown fox.")

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    edit = ModifyText(target_text="quick", new_text="slow", comment="Foxes are not always quick.")

    engine = RedlineEngine(stream)
    engine.apply_edits([edit])

    result_stream = engine.save_to_stream()
    doc = Document(result_stream)

    doc_xml = doc.element.xml
    assert "w:commentRangeStart" in doc_xml
    assert "w:commentRangeEnd" in doc_xml
    assert "w:commentReference" in doc_xml

    id_match = re.search(r'<w:commentRangeStart[^>]*w:id="(\d+)"', doc_xml)
    assert id_match
    comment_id = id_match.group(1)

    comments_part = None
    for rel in doc.part.rels.values():
        if rel.reltype == RT.COMMENTS:
            comments_part = rel.target_part
            break

    assert comments_part is not None
    comments_xml = comments_part.blob.decode("utf-8")
    assert "Foxes are not always quick." in comments_xml
    assert f'w:id="{comment_id}"' in comments_xml


def test_multiple_comments_ids():
    doc = Document()
    doc.add_paragraph("First sentence.")
    doc.add_paragraph("Second sentence.")

    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    edit1 = ModifyText(target_text="First", new_text="First The ", comment="Comment One")
    edit2 = ModifyText(target_text="Second", new_text="Second The ", comment="Comment Two")

    engine = RedlineEngine(stream)
    engine.apply_edits([edit1, edit2])

    result_stream = engine.save_to_stream()
    doc = Document(result_stream)

    comments_part = None
    for rel in doc.part.rels.values():
        if rel.reltype == RT.COMMENTS:
            comments_part = rel.target_part
            break

    comments_xml = comments_part.blob.decode("utf-8")

    assert "Comment One" in comments_xml
    assert "Comment Two" in comments_xml

    ids = re.findall(r'<w:comment\s+[^>]*w:id="(\d+)"', comments_xml)
    assert len(set(ids)) == 2


# --- An existing comments.xml root keeps its namespace declarations ----------
# Counterparty documents arrive with comments
# whose root declares more than the namespaces Adeu needs; Word puts
# hyperlinks (r:id) in comment bodies.

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16SE_NS = "http://schemas.microsoft.com/office/word/2015/wordml/symex"


def _docx_with_existing_comments(paragraph: str, comments_root_attrs: str, comment_body: str) -> io.BytesIO:
    doc = Document()
    doc.add_paragraph(paragraph)
    source = io.BytesIO()
    doc.save(source)

    comments_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:comments {comments_root_attrs}><w:comment w:id="0" w:author="Counterparty">{comment_body}</w:comment>'
        "</w:comments>"
    )
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source.getvalue())) as src, zipfile.ZipFile(result, "w") as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    b'<Override PartName="/word/comments.xml" ContentType="application/'
                    b'vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/></Types>',
                )
            if item.filename == "word/_rels/document.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    f'<Relationship Id="rIdComments" Type="{RT.COMMENTS}" Target="comments.xml"/>'
                    f'<Relationship Id="rIdLink" Type="{RT.HYPERLINK}" Target="https://example.com" '
                    'TargetMode="External"/></Relationships>'.encode(),
                )
            out.writestr(item, data)
        out.writestr("word/comments.xml", comments_xml)
    result.seek(0)
    return result


def _apply_to_existing_comments(comments_root_attrs: str, comment_body: str) -> str:
    stream = _docx_with_existing_comments("The quick brown fox.", comments_root_attrs, comment_body)
    engine = RedlineEngine(stream, author="Jane Doe")
    engine.apply_edits([ModifyText(target_text="quick", new_text="slow", comment="Foxes are not always quick.")])
    result = Document(engine.save_to_stream())
    return next(rel.target_part for rel in result.part.rels.values() if rel.reltype == RT.COMMENTS).blob.decode()


def _ignorable(comments_xml: str) -> list:
    match = re.search(r'mc:Ignorable="([^"]*)"', comments_xml)
    assert match, "comments root carries no mc:Ignorable"
    return match.group(1).split()


def test_comment_beside_an_existing_hyperlinked_comment_keeps_the_r_namespace():
    hyperlinked = '<w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>see policy</w:t></w:r></w:hyperlink></w:p>'

    comments_xml = _apply_to_existing_comments(f'xmlns:w="{_W_NS}" xmlns:r="{_R_NS}"', hyperlinked)

    assert f'xmlns:r="{_R_NS}"' in comments_xml
    assert 'r:id="rIdLink"' in comments_xml
    assert "see policy" in comments_xml
    assert "Foxes are not always quick." in comments_xml
    assert "w14:paraId" in comments_xml
    assert "ns0:" not in comments_xml


def test_existing_ignorable_prefixes_survive_alongside_the_added_ones():
    root_attrs = f'xmlns:w="{_W_NS}" xmlns:mc="{_MC_NS}" xmlns:w16se="{_W16SE_NS}" mc:Ignorable="w16se"'

    comments_xml = _apply_to_existing_comments(root_attrs, "<w:p><w:r><w:t>Earlier note</w:t></w:r></w:p>")

    assert _ignorable(comments_xml) == ["w16se", "w14", "w15", "w16cid", "w16cex"]
    assert f'xmlns:w16se="{_W16SE_NS}"' in comments_xml


def test_w14_bound_to_another_uri_is_rebound_so_new_comments_keep_the_w14_prefix():
    comments_xml = _apply_to_existing_comments(
        f'xmlns:w="{_W_NS}" xmlns:w14="urn:other"', "<w:p><w:r><w:t>Earlier note</w:t></w:r></w:p>"
    )

    assert f'xmlns:w14="{_W14_NS}"' in comments_xml
    assert "w14:paraId" in comments_xml
    assert "ns0:paraId" not in comments_xml


def test_a_root_already_declaring_w14_and_w15_as_ignorable_is_left_untouched():
    root_attrs = (
        f'xmlns:w="{_W_NS}" xmlns:w14="{_W14_NS}" xmlns:w15="{_W15_NS}" xmlns:mc="{_MC_NS}" mc:Ignorable="w14 w15"'
    )
    doc = Document(_docx_with_existing_comments("Text.", root_attrs, "<w:p><w:r><w:t>Earlier</w:t></w:r></w:p>"))
    manager = CommentsManager(doc)
    original_root = manager._get_or_create_comments_part().element

    assert manager.comments_part.element is original_root
