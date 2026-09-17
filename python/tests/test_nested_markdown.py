# FILE: tests/test_nested_markdown.py

from io import BytesIO

from docx import Document

from adeu.redline.engine import RedlineEngine


def _parse_and_check(engine, text, expected_tokens):
    """
    Helper to run the engine's internal parser and check output structure.
    expected_tokens: list of (text, props_dict)
    """
    tokens = engine._parse_inline_markdown(text)

    # Debug print if failure
    if tokens != expected_tokens:
        print(f"\nFailed: '{text}'")
        print("Expected:", expected_tokens)
        print("Actual:  ", tokens)

    assert tokens == expected_tokens


def test_recursive_nested_styles():
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    # Case 1: Simple Bold inside Italic
    # "Outer _Inner **Deep** Inner_ Outer"
    text = "A _B **C** B_ A"
    expected = [
        ("A ", {}),
        ("B ", {"italic": True}),
        ("C", {"italic": True, "bold": True}),
        (" B", {"italic": True}),
        (" A", {}),
    ]
    _parse_and_check(engine, text, expected)


def test_complex_llm_failure_case():
    """
    Reproduces the user reported failure:
    _Either Party... **(i)** ..._
    """
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    text = "_Start **Bold** End_"
    expected = [
        ("Start ", {"italic": True}),
        ("Bold", {"italic": True, "bold": True}),
        (" End", {"italic": True}),
    ]
    _parse_and_check(engine, text, expected)


def test_sequential_tags():
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    text = "**Bold**_Italic_"
    expected = [("Bold", {"bold": True}), ("Italic", {"italic": True})]
    _parse_and_check(engine, text, expected)


def test_underscore_runs_preserved():
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    _parse_and_check(engine, "__", [("__", {})])
    _parse_and_check(engine, "____", [("____", {})])
    _parse_and_check(engine, "__________", [("__________", {})])
    _parse_and_check(engine, "___", [("___", {})])
    _parse_and_check(engine, "_____", [("_____", {})])


def test_intra_word_underscores():
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    _parse_and_check(engine, "foo_bar_baz", [("foo_bar_baz", {})])
    _parse_and_check(engine, "foo__bar", [("foo__bar", {})])
    _parse_and_check(engine, "_foo_bar_", [("foo_bar", {"italic": True})])


def test_empty_delimiters_preserved():
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    _parse_and_check(engine, "****", [("****", {})])
    _parse_and_check(engine, "__", [("__", {})])


def test_backslash_escaping():
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    _parse_and_check(engine, r"\_", [("_", {})])
    _parse_and_check(engine, r"\_\_", [("__", {})])
    _parse_and_check(engine, r"foo\_bar", [("foo_bar", {})])
    _parse_and_check(engine, r"\*", [("*", {})])
    _parse_and_check(engine, r"\*\*", [("**", {})])
    _parse_and_check(engine, r"\**", [("**", {})])
    _parse_and_check(engine, r"\_italic_", [("_italic_", {})])
    _parse_and_check(engine, r"_foo\_bar_", [("foo_bar", {"italic": True})])


def test_fill_in_blank_with_surrounding_text():
    doc = Document()
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    _parse_and_check(engine, "Name: __________", [("Name: __________", {})])
    _parse_and_check(
        engine,
        "Fill _name_: __________",
        [
            ("Fill ", {}),
            ("name", {"italic": True}),
            (": __________", {}),
        ],
    )
    _parse_and_check(
        engine,
        r"This is a line with __________ blank and **bold** and _italic_ and \_escaped\_ and foo_bar_baz",
        [
            ("This is a line with __________ blank and ", {}),
            ("bold", {"bold": True}),
            (" and ", {}),
            ("italic", {"italic": True}),
            (" and _escaped_ and foo_bar_baz", {}),
        ],
    )


def test_tracked_insert_underscore_run_survives():
    doc = Document()
    doc.add_paragraph("Name: [blank]")
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    from adeu.models import ModifyText

    stats = engine.process_batch(
        [ModifyText(type="modify", target_text="Name: [blank]", new_text="Name: __________", comment=None)]
    )
    assert stats["edits_applied"] == 1

    # Verify XML structure has w:ins containing run with __________
    saved_stream = engine.save_to_stream()
    out_doc = Document(saved_stream)
    p = out_doc.paragraphs[0]
    xml_str = p._element.xml
    assert "w:ins" in xml_str
    assert "__________" in xml_str

    # Verify clean text extraction includes __________
    from adeu.ingest import extract_text_from_stream

    clean_text = extract_text_from_stream(engine.save_to_stream(), clean_view=True)
    assert "Name: __________" in clean_text


def test_tracked_insert_escaped_characters_survive():
    doc = Document()
    doc.add_paragraph("Original: placeholder")
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    engine = RedlineEngine(stream)

    from adeu.models import ModifyText

    stats = engine.process_batch(
        [
            ModifyText(
                type="modify",
                target_text="Original: placeholder",
                new_text=r"Original: foo\_bar and \*not bold\*",
                comment=None,
            )
        ]
    )
    assert stats["edits_applied"] == 1

    # Verify XML structure has w:ins containing run with unescaped text
    saved_stream = engine.save_to_stream()
    out_doc = Document(saved_stream)
    p = out_doc.paragraphs[0]
    xml_str = p._element.xml
    assert "w:ins" in xml_str
    assert "foo_bar" in xml_str
    assert "*not bold*" in xml_str

    from adeu.ingest import extract_text_from_stream

    clean_text = extract_text_from_stream(engine.save_to_stream(), clean_view=True)
    assert "Original: foo_bar and *not bold*" in clean_text
