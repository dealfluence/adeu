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
