from adeu.diff import generate_edits_from_text


def test_start_of_doc_insertion_duplication_bug():
    """
    Regression Test: Start-of-document insertion generates duplicate edits.
    Logic was falling through from special case handling to standard handling.
    """
    original = "Contract Agreement"
    modified = "Big Contract Agreement"

    edits = generate_edits_from_text(original, modified)

    # Current buggy behavior returns 2 edits:
    # 1. Target="Contract", New="Big Contract" (Heuristic)
    # 2. Target="", New="Big " (Standard)

    # We want exactly 1 semantic edit to represent this change.
    assert len(edits) == 1, f"Expected 1 edit, got {len(edits)}: {edits}"

    # Also verify the content is sane (whichever strategy wins, it must be valid)
    edit = edits[0]
    if edit.target_text == "":
        assert edit.new_text.strip() == "Big"
    else:
        assert "Contract" in edit.target_text
        assert "Big" in edit.new_text
