import sys
from unittest.mock import MagicMock, patch

import pytest

from adeu.models import ModifyText


@pytest.mark.skipif(sys.platform != "win32", reason="Live Word is Windows only")
def test_live_word_trims_common_context():
    """
    Bug #8: Live Word COM engine replaces the exact target substring wholesale,
    but it must trim common context like the disk engine does.
    """
    import adeu.mcp_components.tools.live_word as lw

    doc_mock = MagicMock()
    doc_mock.Content.Text = "The quick brown fox jumps."
    doc_mock.Revisions.Count = 0
    doc_mock.Content.End = 100

    rng_mock = MagicMock()
    rng_mock.Find.Execute.return_value = True
    rng_mock.Start = 4  # Index of 'quick'
    doc_mock.Range.return_value = rng_mock

    app_mock = MagicMock()
    app_mock.ActiveDocument = doc_mock

    with (
        patch("win32com.client.GetActiveObject", return_value=app_mock),
        patch("adeu.mcp_components.tools.live_word._apply_com_replacement") as mock_apply,
    ):
        changes = [ModifyText(target_text="The quick brown fox", new_text="The fast brown fox", comment="Speed up")]

        lw._process_active_word_batch_core(changes, "Reviewer")

        assert mock_apply.called
        args, kwargs = mock_apply.call_args

        # args = (doc, app, target_rng, new_text, comment_text)
        passed_new_text = args[3]

        assert passed_new_text == "fast", f"Expected trimmed 'fast', got '{passed_new_text}'"
