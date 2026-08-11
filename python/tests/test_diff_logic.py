from adeu.diff import generate_edits_from_text, generate_edits_via_paragraph_alignment


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


def test_paragraph_alignment_emits_no_empty_hunk():
    """
    Regression (P1 pinned round-trip fuzz): when the word-level hunk over a
    multi-paragraph replace block is exactly "A\\n\\nB\\n\\n" -> "", the
    cross-paragraph split emitted one deletion per paragraph AND a leftover
    final piece with an empty target and empty new_text. The engine can only
    read that as an insertion of nothing, so it fails the edit: one skipped
    edit for a change that was already fully expressed.
    """
    original = "0 0.\n\n0 0 0.\n\n0.\n\n0 0 0 0 0."
    modified = "0 0.\n\n0 0 0 0 0. 0"

    edits = generate_edits_via_paragraph_alignment(original, modified)

    assert all(e.target_text or e.new_text for e in edits), (
        f"no-op edit emitted: {[(e.target_text, e.new_text) for e in edits]}"
    )
