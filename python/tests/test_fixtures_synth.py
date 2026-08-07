import io
from pathlib import Path

from adeu.ingest import extract_text_from_stream
from adeu.redline.engine import RedlineEngine
from tests.fixtures_synth import build_long_docx, build_multi_author_docx


def test_multi_author_fixture_shape(tmp_path: Path) -> None:
    docx_path = tmp_path / "multi_author.docx"
    res_path = build_multi_author_docx(docx_path)
    assert res_path == docx_path
    assert docx_path.exists()

    engine = RedlineEngine(io.BytesIO(docx_path.read_bytes()))

    assert len(engine._existing_change_ids()) >= 6
    assert len(engine._existing_comment_ids()) >= 3

    text = extract_text_from_stream(io.BytesIO(docx_path.read_bytes()))

    assert "Jane Doe" in text
    assert "Bob Ross" in text
    assert "Adeu AI" in text
    assert "(pairs with Chg:" in text


def test_long_docx_fixture_shape(tmp_path: Path) -> None:
    docx_path = tmp_path / "long.docx"
    res_path = build_long_docx(docx_path, pages=6)
    assert res_path == docx_path
    assert docx_path.exists()
