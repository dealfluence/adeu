from io import BytesIO

from adeu import ModifyText, RedlineEngine


def test_disk_engine_drops_comment_on_multipara_insertion():
    """
    Reproduces a bug where the Disk Engine (RedlineEngine) drops
    the `comment` parameter if the `new_text` contains a multi-paragraph
    insertion (\n\n).
    """
    from docx import Document

    doc = Document()
    doc.add_paragraph("This is paragraph 1. It is short.")
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)

    edit = ModifyText(
        target_text="This is paragraph 1. It is short.",
        new_text="This is paragraph 1.\n\nIt is short and now has a second paragraph.",
        comment="This comment will be dropped by the disk engine!",
    )

    engine = RedlineEngine(stream, author="QA Disk Bot")
    engine.process_batch([edit])

    out_stream = engine.save_to_stream()

    import zipfile

    out_stream.seek(0)
    z = zipfile.ZipFile(out_stream)

    # Bug manifestation: word/comments.xml might not even exist!
    has_comments_part = any("word/comments" in f and f.endswith(".xml") for f in z.namelist())
    assert has_comments_part, "word/comments.xml was not created, comment was completely dropped."

    comments_xml = ""
    for f in z.namelist():
        if "word/comments" in f and f.endswith(".xml"):
            comments_xml = z.read(f).decode("utf-8")
            break

    assert "This comment will be dropped" in comments_xml, "Comment text not found in comments.xml"
