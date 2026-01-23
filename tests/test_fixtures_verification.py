import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

import pytest

from adeu.ingest import extract_text_from_stream
from adeu.models import DocumentEdit, ReviewAction
from adeu.redline.engine import RedlineEngine

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
INITIAL_DOC = os.path.join(FIXTURES_DIR, "initial.docx")
GOLDEN_DOC = os.path.join(FIXTURES_DIR, "golden.docx")
RESULT_DOC = os.path.join(FIXTURES_DIR, "test_result.docx")

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}


@pytest.fixture
def clean_result_file():
    yield
    if os.path.exists(RESULT_DOC):
        try:
            os.remove(RESULT_DOC)
        except PermissionError:
            pass


class SemanticDocxComparator:
    """
    Compares two DOCX files semantically, ignoring volatile data like
    timestamps, RSIDs, and random internal IDs.
    """

    def __init__(self, golden_path, actual_path):
        self.golden_path = golden_path
        self.actual_path = actual_path
        self.golden_zip = zipfile.ZipFile(golden_path, "r")
        self.actual_zip = zipfile.ZipFile(actual_path, "r")

    def _get_xml_root(self, zf, filename):
        if filename not in zf.namelist():
            return None
        xml_content = zf.read(filename)
        return ET.fromstring(xml_content)

    def compare(self):
        self._check_zip_structure()
        self._check_comments_semantics()
        self.golden_zip.close()
        self.actual_zip.close()
        # We can add _check_document_body if needed, but comments are the main risk area right now.

    def _check_zip_structure(self):
        """
        Ensures no duplicate parts (The Invisible Comment Bug).
        """
        actual_files = self.actual_zip.namelist()

        # Fail if we see duplicated parts (Base AND Numbered)
        # e.g. commentsIds.xml AND commentsIds1.xml
        for base_name in ["word/commentsIds.xml", "word/commentsExtended.xml"]:
            if base_name in actual_files:
                # Check for numbered variants
                prefix = base_name.replace(".xml", "")
                duplicates = [f for f in actual_files if f.startswith(prefix) and f != base_name and f.endswith(".xml")]
                # specifically check for "1.xml" pattern if strict, but any numbered suffix suggests duplication if base exists
                assert not duplicates, f"Duplicate parts found: {base_name} AND {duplicates}"

        # Ensure essential parts from golden exist in actual
        golden_files = self.golden_zip.namelist()
        for f in golden_files:
            if "comments" in f and f.endswith(".xml"):
                # Normalize names for comparison (remove numbers) to handle 1.xml vs .xml valid differences
                norm_golden = f.replace("1.xml", ".xml").replace("2.xml", ".xml")

                found = any(af.replace("1.xml", ".xml").replace("2.xml", ".xml") == norm_golden for af in actual_files)
                assert found, f"Expected part type {norm_golden} missing from actual output"

    def _extract_comments(self, zf):
        """
        Parses comments.xml into a list of simplified dicts:
        [{'id': '1', 'text': '...', 'author': '...', 'parent_id': '0'}]
        Handles both Legacy (w15:p) and Modern (commentsExtended.xml) threading.
        """
        # Find the main comments part (comments.xml or comments1.xml)
        # Exclude Ids/Extended/Extensible
        candidates = [
            f
            for f in zf.namelist()
            if f.startswith("word/comments")
            and f.endswith(".xml")
            and "Ids" not in f
            and "Extended" not in f
            and "Extensible" not in f
        ]
        if not candidates:
            return {}

        root = self._get_xml_root(zf, candidates[0])
        if root is None:
            return {}

        comments = {}
        para_id_map = {}  # paraId -> comment_id (for modern threading)

        for node in root.findall(".//w:comment", NS):
            c_id = node.get(f"{{{NS['w']}}}id")
            author = node.get(f"{{{NS['w']}}}author")

            # Extract text
            text_parts = []
            for t in node.findall(".//w:t", NS):
                if t.text:
                    text_parts.append(t.text)
            text = "".join(text_parts)

            # Extract Parent (Legacy w15:p)
            parent_id = node.get(f"{{{NS['w15']}}}p")

            # Map paraId for Modern Threading
            # Find first paragraph in comment to get its paraId
            p_node = node.find(".//w:p", NS)
            if p_node is not None:
                para_id = p_node.get(f"{{{NS['w14']}}}paraId")
                if para_id:
                    para_id_map[para_id] = c_id

            comments[c_id] = {"author": author, "text": text, "parent_id": parent_id}

        # Resolving Modern Threading (commentsExtended.xml)
        # If legacy parent_id is missing, look here.
        ext_candidates = [f for f in zf.namelist() if "commentsExtended" in f and f.endswith(".xml")]
        if ext_candidates:
            ext_root = self._get_xml_root(zf, ext_candidates[0])
            if ext_root is not None:
                for ex in ext_root.findall(".//w15:commentEx", NS):
                    pid = ex.get(f"{{{NS['w15']}}}paraId")
                    parent_pid = ex.get(f"{{{NS['w15']}}}paraIdParent")

                    if pid and parent_pid and pid in para_id_map:
                        c_id = para_id_map[pid]
                        # Only overwrite if legacy link was missing or to prefer modern?
                        # Word prefers Modern. Let's populate if missing.
                        if comments[c_id]["parent_id"] is None:
                            if parent_pid in para_id_map:
                                comments[c_id]["parent_id"] = para_id_map[parent_pid]

        return comments

    def _check_comments_semantics(self):
        golden_comments = self._extract_comments(self.golden_zip)
        actual_comments = self._extract_comments(self.actual_zip)

        print(f"\nGolden Comments: {len(golden_comments)}")
        print(f"Actual Comments: {len(actual_comments)}")

        # Map by content hash to handle ID drift
        # Hash = (Author, Text, ParentText)
        # Since ParentID might differ, we resolve ParentID -> ParentText

        def build_semantic_map(comments_dict):
            semantic_map = []
            for c in comments_dict.values():
                parent_text = None
                if c["parent_id"] and c["parent_id"] in comments_dict:
                    parent_text = comments_dict[c["parent_id"]]["text"]

                # We relax author check slightly if names vary (e.g. "Adeu AI" vs "Mikko"),
                # but for this test we expect exact match or controlled inputs.
                semantic_map.append(
                    {
                        "text": c["text"],
                        "parent_text_snippet": parent_text[:10] if parent_text else None,
                        # Author check can be added if fixture is consistent
                    }
                )
            return semantic_map

        g_map = build_semantic_map(golden_comments)
        a_map = build_semantic_map(actual_comments)

        # Sort by text to ensure order independence
        g_map.sort(key=lambda x: x["text"])
        a_map.sort(key=lambda x: x["text"])

        # Compare
        for g, a in zip(g_map, a_map):
            assert g["text"] == a["text"], f"Comment text mismatch: Expected '{g['text']}', got '{a['text']}'"
            assert g["parent_text_snippet"] == a["parent_text_snippet"], (
                f"Threading mismatch for comment '{g['text']}': Expected parent '{g['parent_text_snippet']}', got '{a['parent_text_snippet']}'"
            )

        assert len(g_map) == len(a_map), "Comment count mismatch"


def normalize_adeu_extract(text):
    # Remove dates: "@ 2026-01-23" -> ""
    text = re.sub(r" @ \d{4}-\d{2}-\d{2}", "", text)
    # Remove IDs: "[Com:0]" -> "[Com:X]"
    text = re.sub(r"\[Com:\d+\]", "[Com:X]", text)
    # Remove Change IDs: "[Chg:1]" -> "[Chg:X]"
    text = re.sub(r"\[Chg:\d+\]", "[Chg:X]", text)

    # Normalize whitespace artifacts (Abstracting selection differences)
    # 1. Remove trailing space inside tags: {--word --} -> {--word--}
    text = re.sub(r"(\s+)(?=--\}|\+\+\})", "", text)
    # 2. Collapse space between comment block and following text
    # <<} document -> <<}document
    text = text.replace("<<} ", "<<}")

    return text


@pytest.mark.skipif(not os.path.exists(INITIAL_DOC), reason="Initial fixture not found")
def test_oracle_golden_replica(clean_result_file):
    """
    The Oracle Test:
    1. Reads initial.docx
    2. Performs the exact sequence of edits to replicate golden.docx
    3. Compares the resulting XML structure semantically.
    """
    # 1. Load Initial
    with open(INITIAL_DOC, "rb") as f:
        stream = io.BytesIO(f.read())

    # 2. Generation Sequence (Matches repro_golden.py logic)
    # The fixture initial.docx likely contains "Original placeholder text"

    # Step A: Root Comment
    # "initial " -> "golden "
    # Comment: "Start of comment thread"
    engine = RedlineEngine(stream, author="Mikko Korpela")
    edit = DocumentEdit(
        target_text="initial ",
        new_text="golden ",
        comment="Start of comment thread",
    )
    # Note: If target text isn't found exactly, we might need adjustments,
    # but let's assume fixtures match repro script assumptions.
    applied, _ = engine.apply_edits([edit])
    assert applied == 1, "Failed to apply root edit"

    # Get Root ID
    comments = engine.comments_manager.extract_comments_data()
    # Find the comment we just added
    root_id = None
    for cid, data in comments.items():
        if data["text"] == "Start of comment thread":
            root_id = cid
            break
    assert root_id, "Root comment not found after application"

    # Step B: Reply 1
    # Action: REPLY "Second comment"
    action1 = ReviewAction(action="REPLY", target_id=f"Com:{root_id}", text="Second comment")
    applied, _ = engine.apply_review_actions([action1])
    assert applied == 1

    # Step C: Reply 2
    # Action: REPLY "Third comment in the thread" (Threaded to root, based on golden structure)
    action2 = ReviewAction(action="REPLY", target_id=f"Com:{root_id}", text="Third comment in the thread")
    applied, _ = engine.apply_review_actions([action2])
    assert applied == 1

    # 3. Save Result
    with open(RESULT_DOC, "wb") as f:
        f.write(engine.save_to_stream().getvalue())

        print(f"Generated {RESULT_DOC}")

        # 4. Semantic Comparison - Extract View (Adeu's View)
        if not os.path.exists(GOLDEN_DOC):
            pytest.skip("Golden docx not found, cannot run oracle comparison")

        with open(GOLDEN_DOC, "rb") as f:
            golden_text = extract_text_from_stream(io.BytesIO(f.read()))
        with open(RESULT_DOC, "rb") as f:
            result_text = extract_text_from_stream(io.BytesIO(f.read()))

        norm_golden = normalize_adeu_extract(golden_text)
        norm_result = normalize_adeu_extract(result_text)

        assert norm_golden == norm_result, f"Extract mismatch!\nExpected:\n{norm_golden}\nActual:\n{norm_result}"

        # 5. Semantic Comparison - XML Structure (Zip/Relationships)
        comparator = SemanticDocxComparator(GOLDEN_DOC, RESULT_DOC)
        comparator.compare()


@pytest.mark.skipif(not os.path.exists(GOLDEN_DOC), reason="Golden fixture not found")
def test_no_duplicate_parts_on_reply_golden():
    """
    Verifies that replying to the REAL golden document (which has existing comments)
    does not trigger the duplicate part bug.
    """
    with open(GOLDEN_DOC, "rb") as f:
        stream = io.BytesIO(f.read())

    engine = RedlineEngine(stream, author="Adeu Verifier")

    # 1. Identify an existing comment
    comments = engine.comments_manager.extract_comments_data()
    assert len(comments) > 0, "Golden fixture must have comments"
    root_id = list(comments.keys())[0]

    # 2. Apply Reply
    action = ReviewAction(action="REPLY", target_id=f"Com:{root_id}", text="Verification Reply")
    engine.apply_review_actions([action])

    result_stream = engine.save_to_stream()

    # 3. Verify Structure via ZipFile (Lightweight check)
    with zipfile.ZipFile(result_stream) as z:
        names = z.namelist()
        duplicates = [n for n in names if "commentsIds1" in n or "commentsExtended1" in n]
        assert not duplicates, f"Bug Detected! Duplicates found: {duplicates}"
