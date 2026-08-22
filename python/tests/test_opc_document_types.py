# FILE: tests/test_opc_document_types.py
"""CC-11 — Adeu opens every WordprocessingML flavour, and preserves which one it was.

`python-docx` accepts exactly one main-part content type. Templates (`.dotx`)
and macro-enabled documents (`.docm`, `.dotm`) are the same format with a
different declaration, and `@adeu/core` has always read them, so refusing them
was a dual-engine parity break as well as a product gap — templates are the
native habitat of content controls.

These tests use SYNTHETIC packages on purpose. The corpus is optional (it is
gitignored and fetched on demand), so a corpus-only guard is no guard at all:
CI without `shared/corpus/` would skip straight past a regression. The corpus
`.dotx` is exercised separately in `test_corpus_validation.py::test_a5_7_*`.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from adeu.ingest import extract_text_from_stream
from adeu.redline.engine import RedlineEngine
from adeu.utils.opc import (
    WML_DOCUMENT_MACRO_MAIN,
    WML_DOCUMENT_MAIN,
    WML_TEMPLATE_MACRO_MAIN,
    WML_TEMPLATE_MAIN,
    is_template,
    load_document,
    suggested_extension,
)
from tests.sdt_fixtures import build_sdt_docx, para

# (content type, the extension a user would see, is it a template?)
FLAVOURS = [
    pytest.param(WML_DOCUMENT_MAIN, ".docx", False, id="docx"),
    pytest.param(WML_TEMPLATE_MAIN, ".dotx", True, id="dotx"),
    pytest.param(WML_DOCUMENT_MACRO_MAIN, ".docm", False, id="docm"),
    pytest.param(WML_TEMPLATE_MACRO_MAIN, ".dotm", True, id="dotm"),
]


def _build(tmp_path: Path, content_type: str, extension: str) -> Path:
    return build_sdt_docx(
        tmp_path / f"flavour{extension}",
        para("Contractor shall complete the work.") + para("Second paragraph."),
        main_content_type=content_type,
    )


def _declared_main_content_type(data: bytes) -> str:
    """The `[Content_Types].xml` override for `/word/document.xml`."""
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        content_types = package.read("[Content_Types].xml").decode("utf-8")
    marker = '<Override PartName="/word/document.xml" ContentType="'
    start = content_types.index(marker) + len(marker)
    return content_types[start : content_types.index('"', start)]


@pytest.mark.parametrize(("content_type", "extension", "expect_template"), FLAVOURS)
def test_every_wordprocessingml_flavour_opens(
    tmp_path: Path, content_type: str, extension: str, expect_template: bool
) -> None:
    document = load_document(str(_build(tmp_path, content_type, extension)))

    assert [p.text for p in document.paragraphs] == [
        "Contractor shall complete the work.",
        "Second paragraph.",
    ]
    assert is_template(document) is expect_template
    assert suggested_extension(document) == extension


@pytest.mark.parametrize(("content_type", "extension", "expect_template"), FLAVOURS)
def test_projection_is_identical_across_flavours(
    tmp_path: Path, content_type: str, extension: str, expect_template: bool
) -> None:
    """The declared content type is metadata; it must not reach the text projection."""
    data = _build(tmp_path, content_type, extension).read_bytes()
    baseline = _build(tmp_path, WML_DOCUMENT_MAIN, "-baseline.docx").read_bytes()

    assert extract_text_from_stream(io.BytesIO(data)) == extract_text_from_stream(io.BytesIO(baseline))


@pytest.mark.parametrize(("content_type", "extension", "expect_template"), FLAVOURS)
def test_save_preserves_the_flavour(tmp_path: Path, content_type: str, extension: str, expect_template: bool) -> None:
    """A `.dotx` in is a `.dotx` out.

    This is the whole reason CC-11 registers content types with `PartFactory`
    instead of rewriting `[Content_Types].xml` on load. `python-docx`
    serialises the content-type map from the parts' own `content_type`, so a
    load-time rewrite would follow the document into `save()` and silently
    convert the user's template into a document — fidelity loss inflicted by a
    read.
    """
    data = _build(tmp_path, content_type, extension).read_bytes()

    saved = RedlineEngine(io.BytesIO(data)).save_to_stream().getvalue()

    assert _declared_main_content_type(saved) == content_type


def test_a_genuinely_wrong_format_still_fails_but_teaches(tmp_path: Path) -> None:
    """The refusal must name what it found and what it accepts.

    `python-docx` raises a bare "is not a Word file", which the CLI surfaced as
    an unhandled traceback. A user handed an `.odt` deserves to be told to
    convert it.
    """
    odt_main = "application/vnd.oasis.opendocument.text"
    path = _build(tmp_path, odt_main, ".odt")

    with pytest.raises(ValueError) as excinfo:
        load_document(str(path))

    message = str(excinfo.value)
    assert odt_main in message, "the error must name the content type it actually found"
    assert ".dotx" in message and ".docm" in message, "the error must list what is accepted"
    assert "convert it to .docx" in message


def test_load_document_requires_an_argument() -> None:
    """`docx.Document()` defaults to its bundled empty template.

    That default turns a forgotten argument into a silently empty document
    rather than an error, which is never what Adeu wants.
    """
    with pytest.raises(TypeError):
        load_document()  # type: ignore[call-arg]
