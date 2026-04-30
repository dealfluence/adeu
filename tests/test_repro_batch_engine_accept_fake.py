import io

import docx
import pytest

from adeu import AcceptChange, RedlineEngine
from adeu.redline.engine import BatchValidationError


def test_batch_engine_accept_fake_id_attribute_error():
    """
    Reproduces a bug where accepting/rejecting a non-existent change ID
    crashes with a Pydantic AttributeError on `_match_start_index` rather
    than a clean missing target / validation error.
    """
    doc = docx.Document()
    doc.add_paragraph("Test")
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    engine = RedlineEngine(stream, author="QA Bot")

    with pytest.raises(BatchValidationError):
        engine.process_batch([AcceptChange(target_id="Chg:999")])
