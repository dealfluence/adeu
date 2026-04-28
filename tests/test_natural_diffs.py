from adeu.diff import generate_edits_from_text


def _get_change_summary(edits):
    summary = []
    for e in edits:
        if not e.new_text:
            summary.append(f"[-'{e.target_text}']")
        elif not e.target_text:
            summary.append(f"[+'{e.new_text}']")
        else:
            summary.append(f"['{e.target_text}'->'{e.new_text}']")
    return " ".join(summary)


def test_legal_term_replacement_tenant_lessee():
    original = "The Tenant shall pay."
    modified = "The Lessee shall pay."

    edits = generate_edits_from_text(original, modified)
    target_edits = [e for e in edits if "enant" in e.target_text or "essee" in (e.new_text or "")]

    assert len(target_edits) == 1
    assert target_edits[0].target_text == "Tenant"
    assert target_edits[0].new_text == "Lessee"


def test_corporate_name_change():
    original = "Signed by NordicTech Solutions Inc. today."
    modified = "Signed by NordicTech Services LLC today."

    edits = generate_edits_from_text(original, modified)
    solutions_edits = [e for e in edits if "Solutions" in e.target_text]
    assert len(solutions_edits) > 0


def test_date_change_fragmentation():
    original = "Date: October 24, 2025"
    modified = "Date: November 1, 2025"

    edits = generate_edits_from_text(original, modified)
    oct_edits = [e for e in edits if "October" in e.target_text]
    assert len(oct_edits) > 0


def test_indemnification_clause_rewrite():
    original = "Customer shall indemnify Provider against claims."
    modified = "Customer shall hold Provider harmless from claims."

    edits = generate_edits_from_text(original, modified)
    indemnify_edit = [e for e in edits if "indemnify" in (e.target_text or "")]
    assert len(indemnify_edit) == 1


def test_multi_word_phrase_change():
    original = "The quick brown fox jumped."
    modified = "The slow red fox jumped."

    edits = generate_edits_from_text(original, modified)
    phrase_edits = [e for e in edits if "quick" in e.target_text]

    assert len(phrase_edits) == 1
    assert "brown" in phrase_edits[0].target_text
    assert "slow" in phrase_edits[0].new_text
    assert "red" in phrase_edits[0].new_text


def test_val_obs_new_8_diff_coalescing():
    """
    VAL-OBS-NEW-8: Adjacent edits separated only by whitespace or punctuation
    or short runs of stable tokens should be coalesced into a single hunk
    to prevent redline fragmentation across clauses.
    """
    # 4-word gap (" year of the AI ")
    original = "the second year of the AI platform shift"
    modified = "the third year of the AI strategy shift"

    edits = generate_edits_from_text(original, modified)

    # Without coalescing, 'second'->'third' and 'platform'->'strategy' are 2 separate edits.
    assert len(edits) == 1, "Edits should be coalesced into a single hunk"
