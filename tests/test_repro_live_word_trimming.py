import sys
from unittest.mock import MagicMock

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

    import win32com.client

    original_get = getattr(win32com.client, "GetActiveObject", None)
    win32com.client.GetActiveObject = lambda name: app_mock
    try:
        changes = [ModifyText(target_text="The quick brown fox", new_text="The fast brown fox", comment="Speed up")]
        stats = lw._process_active_word_batch_core(changes, "Reviewer")

        assert stats["failed"] == 0, f"Batch failed with: {stats.get('skipped_details', [])}"

        # The real _apply_com_replacement will set target_rng.Text
        assert rng_mock.Text == "fast", f"Expected trimmed 'fast', got '{rng_mock.Text}'"
    finally:
        if original_get:
            win32com.client.GetActiveObject = original_get
