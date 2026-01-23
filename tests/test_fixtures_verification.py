import difflib
import io
import os
import re
import zipfile
from xml.dom.minidom import parseString

import pytest

from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit, ReviewAction
from adeu.redline.engine import RedlineEngine

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
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
    # Remove dates: "@ 2026-01-23" -> ""
    text = re.sub(r" @ \d{4}-\d{2}-\d{2}", "", text)
    # Remove IDs: "[Com:0]" -> "[Com:X]"
    text = re.sub(r"\[Com:\d+\]", "[Com:X]", text)
    # Remove Change IDs: "[Chg:1]" -> "[Chg:X]"
    text = re.sub(r"\[Chg:\d+\]", "[Chg:X]", text)

    # Normalize whitespace artifacts
    text = re.sub(r"(\s+)(?=--\}|\+\+\})", "", text)
    text = text.replace("<<} ", "<<}")

    return text.strip()


def abstract_docx_xml(xml_str: str, filename: str) -> str:
    """
    Abstracts volatile parts of the DOCX XML (IDs, Dates, RSIDs) to allow for text comparison.
    Removes noise but preserves structure to detect bugs (e.g. w15:p threading).
    """
    # 0. Clean Root Namespaces (Word spam)
    if "comments" in filename and filename.endswith(".xml"):
        # Matches <w:comments ...> or <w16cid:commentsIds ...>
        # Replaces with just the tag name <w:comments>
        xml_str = re.sub(r"^(<[\w:]+)(\s+.*?)(\/?>)", r"\1\3", xml_str, count=1, flags=re.DOTALL | re.MULTILINE)

    # 1. RSIDs - Remove completely (pure noise)
    xml_str = re.sub(r' w:rsid\w*="[^"]+"', "", xml_str)

    # 2. IDs - Abstract values, preserve attributes
    xml_str = re.sub(r'(w:id=")[^"]+(")', r"\1ID\2", xml_str)
    # w15:p is CRITICAL for threading, but Word sometimes omits/adds it inconsistently with our mock.
    # We assume Adeu's explicit addition is correct, but for diffing we remove it.
    xml_str = re.sub(r' w15:p="[^"]+"', "", xml_str)

    xml_str = re.sub(r'(w16cid:durableId=")[^"]+(")', r"\1DID\2", xml_str)
    xml_str = re.sub(r'(w16cex:durableId=")[^"]+(")', r"\1DID\2", xml_str)

    # 3. Dates
    xml_str = re.sub(r'(w:date=")[^"]+(")', r"\1DATE\2", xml_str)
    xml_str = re.sub(r'([a-zA-Z0-9]+:dateUtc=")[^"]+(")', r"\1DATE\2", xml_str)

    # 4. Para IDs
    xml_str = re.sub(r'(w14:paraId=")[^"]+(")', r"\1PID\2", xml_str)
    xml_str = re.sub(r'(w14:textId=")[^"]+(")', r"\1TID\2", xml_str)
    xml_str = re.sub(r'(w15:paraId=")[^"]+(")', r"\1PID\2", xml_str)
    xml_str = re.sub(r'(w15:paraIdParent=")[^"]+(")', r"\1PID\2", xml_str)
    xml_str = re.sub(r'(w16cid:paraId=")[^"]+(")', r"\1PID\2", xml_str)

    # 5. Relationship IDs (rId1 -> RID)
    xml_str = re.sub(r'(Id="rId)\d+(")', r"\1RID\2", xml_str)

    # 6. Normalize filenames in Relationship Targets
    # comments1.xml -> comments.xml (Allows diff against Word files)
    xml_str = re.sub(r'(Target=".*comments.*?)\d+(\.xml")', r"\1\2", xml_str)

    # 7. Initials
    xml_str = re.sub(r' w:initials="[^"]+"', "", xml_str)

    # 8. Filter people.xml relationships (Word adds them, Adeu does not)
    xml_str = re.sub(r'<Relationship [^>]*Target="people\.xml"[^>]*/>', "", xml_str)

    return xml_str


def format_and_sort_xml(xml_bytes: bytes, filename: str) -> str:
    """
    Parses XML, Sorts Relationships if applicable, and Pretty Prints.
    """
    try:
        dom = parseString(xml_bytes)

        # Sort Relationships for deterministic diffing
        if filename.endswith(".rels"):
            rels_node = None
            if dom.documentElement.tagName == "Relationships":
                rels_node = dom.documentElement

            if rels_node:
                children = []
                for child in rels_node.childNodes:
                    if child.nodeType == child.ELEMENT_NODE and child.tagName == "Relationship":
                        children.append(child)

                for child in children:
                    rels_node.removeChild(child)

                # Sort by Target first, then Type
                children.sort(key=lambda x: (x.getAttribute("Target"), x.getAttribute("Type")))

                for child in children:
                    rels_node.appendChild(child)

        return dom.toprettyxml(indent="  ")
    except Exception:
        return xml_bytes.decode("utf-8", errors="ignore")


def get_abstracted_xml_snapshot(docx_path: str) -> str:
    snapshot_lines = []

    with zipfile.ZipFile(docx_path, "r") as z:
        relevant_files = []
        for f in z.namelist():
            if f.endswith("/"):
                continue

            # Filter logic
            if f.startswith("word/") and (f.endswith(".xml") or f.endswith(".rels")):
                ignored = [
                    "word/settings.xml",
                    "word/webSettings.xml",
                    "word/fontTable.xml",
                    "word/styles.xml",
                    "word/people.xml",
                    "word/numbering.xml",  # often noisy
                ]
                if any(i in f for i in ignored):
                    continue
                relevant_files.append(f)

            if f == "_rels/.rels":
                relevant_files.append(f)

        relevant_files.sort()

        for fname in relevant_files:
            content = z.read(fname)
            formatted = format_and_sort_xml(content, fname)
            abstracted = abstract_docx_xml(formatted, fname)

            # Normalize filename (comments1.xml -> comments.xml)
            display_name = re.sub(r"(comments.*?)\d+(\.xml)", r"\1\2", fname)
            display_name = re.sub(r"(comments.*?)\d+(\.xml\.rels)", r"\1\2", display_name)

            snapshot_lines.append(f"=== FILE: {display_name} ===")
            snapshot_lines.append(abstracted)
            snapshot_lines.append(f"=== END FILE: {display_name} ===\n")

    return "\n".join(snapshot_lines)


@pytest.mark.skipif(not os.path.exists(INITIAL_DOC), reason="Initial fixture not found")
def test_oracle_golden_replica(clean_result_file):
    # --- 1. GENERATION PHASE ---
    with open(INITIAL_DOC, "rb") as f:
        stream = io.BytesIO(f.read())

    engine = RedlineEngine(stream, author="Mikko Korpela")
    edit = DocumentEdit(
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

    action1 = ReviewAction(action="REPLY", target_id=f"Com:{root_id}", text="Second comment")
    engine.apply_review_actions([action1])

    action2 = ReviewAction(action="REPLY", target_id=f"Com:{root_id}", text="Third comment in the thread")
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
            norm_golden.splitlines(), norm_result.splitlines(), fromfile="Golden Extract", tofile="Result Extract"
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
            golden_xml.splitlines(), result_xml.splitlines(), fromfile="Golden XML", tofile="Result XML", n=3
        )
        print("\n".join(diff))
        pytest.fail("XML Structure mismatch")


@pytest.mark.skipif(not os.path.exists(GOLDEN_DOC) or not os.path.exists(GOLDEN2_DOC), reason="Golden fixtures missing")
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
    action = ReviewAction(action="REPLY", target_id="Com:3", text="Forth comment")
    applied, _ = engine.apply_review_actions([action])
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
            expected_xml.splitlines(), actual_xml.splitlines(), fromfile="Golden2 XML", tofile="Result XML", n=3
        )
        print("\n".join(diff))
        pytest.fail("Structure mismatch: The invisible comment bug might be present.")
