# FILE: verify_bug3_fix.py
"""
Verifies Bug 3 fix: Live Word path now runs validate_edit_strings (Category A)
and RedlineEngine.validate_edits (Category B) before applying any edits.

We can't run the real Live Word COM path without Word, but we CAN:
1. Verify validate_edit_strings produces the expected messages for each rule.
2. Verify validate_edit_strings produces identical messages to what
   RedlineEngine.validate_edits emits (parity check).
3. Verify the disk path's validate_edits still works end-to-end.

Self-contained.
"""

import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from docx import Document

from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine, validate_edit_strings


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def make_minimal_docx():
    doc = Document()
    doc.add_paragraph("This is the body of the document.")
    doc.add_paragraph("Microsoft Teams remains essential to how people meet.")
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)
    return stream


def disk_validate(edits):
    stream = make_minimal_docx()
    engine = RedlineEngine(stream, author="Test")
    return engine.validate_edits(edits)


# ---------------------------------------------------------------------------
# Test 1: Category A rules — all six should fire from validate_edit_strings.
# ---------------------------------------------------------------------------

CATEGORY_A_CASES = [
    (
        "Manual CriticMarkup",
        ModifyText(
            type="modify",
            target_text="Microsoft Teams",
            new_text="{++HALLUCINATED++} Microsoft Teams",
        ),
        "Do not manually write CriticMarkup tags",
    ),
    (
        "Heading level > 6",
        ModifyText(
            type="modify",
            target_text="Microsoft Teams",
            new_text="####### Subsection\n\nMicrosoft Teams",
        ),
        "Heading level 7 is not supported",
    ),
    (
        "Inserting footnote marker",
        ModifyText(
            type="modify",
            target_text="Microsoft Teams",
            new_text="Microsoft Teams[^fn-99]",
        ),
        "Cannot insert footnote/endnote markers",
    ),
    (
        "Inserting hyperlink",
        ModifyText(
            type="modify",
            target_text="Microsoft Teams",
            new_text="[Microsoft Teams](https://example.com)",
        ),
        "Cannot insert hyperlinks via text replace",
    ),
    (
        "Inserting cross-reference",
        ModifyText(
            type="modify",
            target_text="Microsoft Teams",
            new_text="[~Section 1~](#sec1) Microsoft Teams",
        ),
        "Cannot insert cross-references",
    ),
    (
        "Inserting internal anchor",
        ModifyText(
            type="modify",
            target_text="Microsoft Teams",
            new_text="{#anchor1} Microsoft Teams",
        ),
        "Cannot modify or insert internal anchor markers",
    ),
]

section("Test 1: validate_edit_strings catches all Category A rules")
all_pass = True
for label, edit, expected_substring in CATEGORY_A_CASES:
    errors = validate_edit_strings([edit])
    if errors and expected_substring in errors[0]:
        print(f"  [PASS] {label}")
        print(f"         {errors[0]}")
    else:
        print(f"  [FAIL] {label}")
        print(f"         expected substring: {expected_substring!r}")
        print(f"         got: {errors!r}")
        all_pass = False
print(
    f"\n{'All Category A checks PASS.' if all_pass else 'Some Category A checks FAILED.'}"
)


# ---------------------------------------------------------------------------
# Test 2: Parity — validate_edit_strings(edits) == disk_validate(edits) for
# Category A inputs (since Category A errors fire before Category B logic
# even runs against the document).
# ---------------------------------------------------------------------------

section("Test 2: Disk-vs-helper parity for Category A inputs")
parity_ok = True
for label, edit, _ in CATEGORY_A_CASES:
    helper_errors = validate_edit_strings([edit])
    disk_errors = disk_validate([edit])
    # Disk errors may include Category B error too (target text not found, etc.)
    # but we expect the Category A error to appear at the top.
    if not helper_errors:
        print(f"  [SKIP] {label} — no Category A error")
        continue
    helper_msg = helper_errors[0]
    found_in_disk = any(helper_msg == err for err in disk_errors)
    if found_in_disk:
        print(f"  [PASS] {label}: helper message appears verbatim in disk output")
    else:
        print(f"  [FAIL] {label}: messages differ")
        print(f"         helper: {helper_msg!r}")
        print(f"         disk:   {disk_errors!r}")
        parity_ok = False
print(f"\n{'Parity confirmed.' if parity_ok else 'Parity FAILED.'}")


# ---------------------------------------------------------------------------
# Test 3: Empty/well-formed edit batch produces no errors.
# ---------------------------------------------------------------------------

section("Test 3: Well-formed edits produce no Category A errors")
good_edits = [
    ModifyText(type="modify", target_text="Microsoft Teams", new_text="Teams"),
    ModifyText(type="modify", target_text="meet", new_text="collaborate"),
]
errors = validate_edit_strings(good_edits)
if errors:
    print(f"  [FAIL] Expected no errors, got: {errors}")
else:
    print("  [PASS] No errors for well-formed edits.")


# ---------------------------------------------------------------------------
# Test 4: Empty list returns empty errors.
# ---------------------------------------------------------------------------

section("Test 4: Empty edit list returns empty error list")
errors = validate_edit_strings([])
if errors:
    print(f"  [FAIL] Expected no errors, got: {errors}")
else:
    print("  [PASS] Empty list → empty errors.")


# ---------------------------------------------------------------------------
# Test 5: Edit numbering — the i+1 indexing should reflect position in the batch.
# ---------------------------------------------------------------------------

section("Test 5: Edit numbering reflects position in the batch")
mixed = [
    ModifyText(type="modify", target_text="ok", new_text="ok2"),  # Edit 1, valid
    ModifyText(
        type="modify", target_text="bad", new_text="{++bad++}"
    ),  # Edit 2, Cat-A fail
    ModifyText(type="modify", target_text="ok3", new_text="ok4"),  # Edit 3, valid
    ModifyText(
        type="modify", target_text="bad2", new_text="####### bad"
    ),  # Edit 4, Cat-A fail
]
errors = validate_edit_strings(mixed)
print(f"  Errors emitted: {len(errors)}")
for e in errors:
    print(f"    {e}")
if len(errors) == 2 and "Edit 2" in errors[0] and "Edit 4" in errors[1]:
    print("\n  [PASS] Edit numbering correct: positions 2 and 4 flagged.")
else:
    print("\n  [FAIL] Edit numbering does not match expected positions.")


# ---------------------------------------------------------------------------
# Test 6: Disk path still works end-to-end (regression check).
# ---------------------------------------------------------------------------

section("Test 6: Disk path validate_edits still produces correct messages")
edit = ModifyText(
    type="modify",
    target_text="Microsoft Teams",
    new_text="{++HALLUCINATED++} Microsoft Teams",
)
errors = disk_validate([edit])
print(f"  Errors: {errors}")
if errors and "Do not manually write CriticMarkup" in errors[0]:
    print("  [PASS] Disk path still rejects manual CriticMarkup.")
else:
    print("  [FAIL] Disk path validation regressed.")
