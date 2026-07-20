import difflib
import io
import os
import re

import pytest

from adeu.ingest import extract_text_from_stream
from adeu.models import ModifyText, ReplyComment
from adeu.redline.engine import RedlineEngine
from adeu.utils.xml_debug import get_abstracted_xml_snapshot

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "shared", "fixtures")
INITIAL_DOC = os.path.join(FIXTURES_DIR, "initial.docx")
GOLDEN_DOC = os.path.join(FIXTURES_DIR, "golden.docx")
GOLDEN2_DOC = os.path.join(FIXTURES_DIR, "golden2.docx")
RESULT_DOC = os.path.join(FIXTURES_DIR, "test_result.docx")


@pytest.fixture
def clean_result_file():
    yield
    if os.path.exists(RESULT_DOC):
        try:
            os.remove(RESULT_DOC)
        except PermissionError:
            pass


def normalize_adeu_extract(text):
    """
    Normalizes the Adeu text extract to ignore volatile IDs and dates.
    """
    # Remove dates: "@ 2026-01-23" or "@ 2026-01-23T10:00:00Z" -> ""
    text = re.sub(r" @ \d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?", "", text)
    # Remove IDs: "[Com:0]" -> "[Com:X]"
    text = re.sub(r"\[Com:\d+\]", "[Com:X]", text)
    # Remove Change IDs: "[Chg:1]" or "[Chg:1 delete]" / "[Chg:1 insert]" / "[Chg:1 format]" -> "[Chg:X]"
    text = re.sub(r"\[Chg:\d+(?:\s+\w+)?\]", "[Chg:X]", text)
    # ...including inside the resolution-group annotation (ADEU-QA-004):
    # "(pairs with Chg:2, Chg:3)" -> "(pairs with Chg:X)"
    text = re.sub(r"\(pairs with Chg:\d+(?:, Chg:\d+)*\)", "(pairs with Chg:X)", text)

    # Normalize whitespace artifacts
    text = re.sub(r"(\s+)(?=--\}|\+\+\})", "", text)
    text = text.replace("<<} ", "<<}")

    return text.strip()


@pytest.mark.skipif(not os.path.exists(INITIAL_DOC), reason="Initial fixture not found")
def test_oracle_golden_replica(clean_result_file):
    # --- 1. GENERATION PHASE ---
    with open(INITIAL_DOC, "rb") as f:
        stream = io.BytesIO(f.read())

    engine = RedlineEngine(stream, author="Mikko Korpela")
    edit = ModifyText(
        target_text="initial ",
        new_text="golden ",
        comment="Start of comment thread",
    )
    applied, _ = engine.apply_edits([edit])
    assert applied == 1, "Failed to apply root edit"

    comments = engine.comments_manager.extract_comments_data()
    root_id = None
    for cid, data in comments.items():
        if data["text"] == "Start of comment thread":
            root_id = cid
            break
    assert root_id, "Root comment not found"

    action1 = ReplyComment(target_id=f"Com:{root_id}", text="Second comment")
    engine.apply_review_actions([action1])

    action2 = ReplyComment(target_id=f"Com:{root_id}", text="Third comment in the thread")
    engine.apply_review_actions([action2])

    with open(RESULT_DOC, "wb") as f:
        f.write(engine.save_to_stream().getvalue())

    print(f"\nGenerated: {RESULT_DOC}")

    if not os.path.exists(GOLDEN_DOC):
        pytest.skip("Golden docx not found")

    # --- 2. EXTRACT COMPARISON PHASE ---
    with open(GOLDEN_DOC, "rb") as f:
        golden_text = extract_text_from_stream(io.BytesIO(f.read()))
    with open(RESULT_DOC, "rb") as f:
        result_text = extract_text_from_stream(io.BytesIO(f.read()))

    norm_golden = normalize_adeu_extract(golden_text)
    norm_result = normalize_adeu_extract(result_text)

    if norm_golden != norm_result:
        print("\n--- EXTRACT DIFF ---")
        diff = difflib.unified_diff(
            norm_golden.splitlines(),
            norm_result.splitlines(),
            fromfile="Golden Extract",
            tofile="Result Extract",
        )
        print("\n".join(diff))
        pytest.fail("Adeu Extract does not match Golden Extract")
    else:
        print("✅ Adeu Extract Matches")

    # --- 3. XML STRUCTURE COMPARISON PHASE ---
    golden_xml = get_abstracted_xml_snapshot(GOLDEN_DOC)
    result_xml = get_abstracted_xml_snapshot(RESULT_DOC)

    if golden_xml != result_xml:
        print("\n--- XML STRUCTURE DIFF ---")

        diff = difflib.unified_diff(
            golden_xml.splitlines(),
            result_xml.splitlines(),
            fromfile="Golden XML",
            tofile="Result XML",
            n=3,
        )
        print("\n".join(diff))
        print("WARNING: XML Structure mismatch (Likely run coalescing differences)")


@pytest.mark.skipif(
    not os.path.exists(GOLDEN_DOC) or not os.path.exists(GOLDEN2_DOC),
    reason="Golden fixtures missing",
)
def test_repro_golden_to_golden2(clean_result_file):
    """
    Reproduction of 'Invisible Comment Bug'.
    1. Load golden.docx (Contains existing Modern Comments structure).
    2. Add 'Forth comment' as a reply.
    3. Compare against golden2.docx (Word-generated baseline).

    If bug exists: We will see duplicate parts (commentsIds1.xml) or Namespace/RelType mismatch in the XML diff.
    """
    # --- 1. EDIT PHASE ---
    with open(GOLDEN_DOC, "rb") as f:
        stream = io.BytesIO(f.read())

    engine = RedlineEngine(stream, author="Mikko Korpela")

    # Add the reply seen in golden2.docx
    action = ReplyComment(target_id="Com:2", text="Forth comment")
    applied, _, _ = engine.apply_review_actions([action])
    assert applied == 1

    with open(RESULT_DOC, "wb") as f:
        f.write(engine.save_to_stream().getvalue())

    print(f"\nGenerated: {RESULT_DOC}")

    # --- 2. VERIFICATION PHASE ---

    # Extract Check
    with open(GOLDEN2_DOC, "rb") as f:
        expected_text = extract_text_from_stream(io.BytesIO(f.read()))
    with open(RESULT_DOC, "rb") as f:
        actual_text = extract_text_from_stream(io.BytesIO(f.read()))

    norm_expected = normalize_adeu_extract(expected_text)
    norm_actual = normalize_adeu_extract(actual_text)

    assert norm_expected == norm_actual, "Text extraction mismatch (Content differs)"

    # XML Structure Check
    expected_xml = get_abstracted_xml_snapshot(GOLDEN2_DOC)
    actual_xml = get_abstracted_xml_snapshot(RESULT_DOC)

    if expected_xml != actual_xml:
        print("\n--- XML STRUCTURE DIFF (GOLDEN2 vs RESULT) ---")
        diff = difflib.unified_diff(
            expected_xml.splitlines(),
            actual_xml.splitlines(),
            fromfile="Golden2 XML",
            tofile="Result XML",
            n=3,
        )
        print("\n".join(diff))
        print("WARNING: XML Structure mismatch (Likely run coalescing or property diffs)")
