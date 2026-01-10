import pytest
from adeu.diff import generate_edits_from_text
from adeu.models import EditOperationType

def _get_change_summary(edits):
    """Helper to visualize what the diff engine produced."""
    summary = []
    for e in edits:
        if e.operation == EditOperationType.DELETION:
            summary.append(f"[-'{e.target_text}']")
        elif e.operation == EditOperationType.INSERTION:
            summary.append(f"[+'{e.new_text}']")
        elif e.operation == EditOperationType.MODIFICATION:
            summary.append(f"['{e.target_text}'->'{e.new_text}']")
    return " ".join(summary)

def test_legal_term_replacement_tenant_lessee():
    """
    Scenario: "Tenant" -> "Lessee"
    Failure Mode: Character diff keeps 'en' or 'e'.
    """
    original = "The Tenant shall pay."
    modified = "The Lessee shall pay."
    
    edits = generate_edits_from_text(original, modified)
    print(f"\nTenant->Lessee Edits: {_get_change_summary(edits)}")
    
    # Strict check: Should be 1 MOD: "Tenant" -> "Lessee"
    target_edits = [e for e in edits if "enant" in e.target_text or "essee" in (e.new_text or "")]
    
    assert len(target_edits) == 1, f"Fragmented diffs found: {_get_change_summary(edits)}"
    assert target_edits[0].target_text == "Tenant"
    assert target_edits[0].new_text == "Lessee"

def test_corporate_name_change():
    """
    Scenario: "NordicTech Solutions Inc." -> "NordicTech Services LLC"
    Failure Mode: Stitches 'S...s' together.
    """
    original = "Signed by NordicTech Solutions Inc. today."
    modified = "Signed by NordicTech Services LLC today."
    
    edits = generate_edits_from_text(original, modified)
    print(f"\nCorp Name Edits: {_get_change_summary(edits)}")
    
    solutions_edits = [e for e in edits if "Solutions" in e.target_text]
    
    assert len(solutions_edits) > 0, \
        f"The word 'Solutions' was not fully captured. Result: {_get_change_summary(edits)}"

def test_date_change_fragmentation():
    """
    Scenario: "October 24, 2025" -> "November 1, 2025"
    Failure Mode: Keeps 'er', '2', etc.
    """
    original = "Date: October 24, 2025"
    modified = "Date: November 1, 2025"
    
    edits = generate_edits_from_text(original, modified)
    print(f"\nDate Edits: {_get_change_summary(edits)}")
    
    oct_edits = [e for e in edits if "October" in e.target_text]
    assert len(oct_edits) > 0, f"October should be replaced as a whole word. Result: {_get_change_summary(edits)}"

def test_indemnification_clause_rewrite():
    """
    Scenario: Complete clause structural rewrite.
    Original: "Customer shall indemnify Provider against claims."
    Modified: "Customer shall hold Provider harmless from claims."
    """
    original = "Customer shall indemnify Provider against claims."
    modified = "Customer shall hold Provider harmless from claims."
    
    edits = generate_edits_from_text(original, modified)
    print(f"\nIndemnity Edits: {_get_change_summary(edits)}")
    
    # Specifically check that "indemnify" is removed wholly
    indemnify_edit = [e for e in edits if e.target_text == "indemnify"]
    
    assert len(indemnify_edit) == 1, \
        f"Expected atomic deletion of 'indemnify', got fragmentation: {_get_change_summary(edits)}"

def test_multi_word_phrase_change():
    """
    Scenario: Changing a multi-word phrase where words share letters.
    Original: "The quick brown fox"
    Modified: "The slow red fox"
    
    Current (Char): 'q'->'s', 'uick'->'low', 'b'->'r', 'rown'->'ed' (likely messy)
    Desired (Word): "quick brown" -> "slow red" (One big modification)
    """
    original = "The quick brown fox jumped."
    modified = "The slow red fox jumped."
    
    edits = generate_edits_from_text(original, modified)
    print(f"\nPhrase Edits: {_get_change_summary(edits)}")
    
    # We want ONE modification that handles "quick brown" -> "slow red"
    # Or clean DEL "quick brown" INS "slow red"
    
    phrase_edits = [e for e in edits if "quick" in e.target_text]
    
    assert len(phrase_edits) == 1, "Should group adjacent word changes"
    # Depending on merge logic, it might be MOD or DEL.
    # If MOD, text should be "quick brown"
    if phrase_edits[0].operation == EditOperationType.MODIFICATION:
        assert "brown" in phrase_edits[0].target_text
        assert "slow" in phrase_edits[0].new_text
        assert "red" in phrase_edits[0].new_text