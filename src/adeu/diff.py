import re
from typing import Dict, List, Tuple

import structlog
from diff_match_patch import diff_match_patch

from adeu.models import DocumentEdit

logger = structlog.get_logger(__name__)


def generate_edits_from_text(original_text: str, modified_text: str) -> List[DocumentEdit]:
    """
    Compares original and modified text to generate structured ComplianceEdit objects.
    Uses Word-Level diffing to ensure natural, readable redlines.
    """
    dmp = diff_match_patch()

    # 1. Word-Level Tokenization & Encoding
    chars1, chars2, token_array = _words_to_chars(original_text, modified_text)

    # 2. Compute Diff on the Encoded Strings
    diffs_encoded = dmp.diff_main(chars1, chars2, False)

    # 3. Semantic Cleanup
    dmp.diff_cleanupSemantic(diffs_encoded)

    # 4. Decode back to Text
    dmp.diff_charsToLines(diffs_encoded, token_array)
    diffs = diffs_encoded

    edits = []
    current_original_index = 0
    pending_delete = None  # Tuple(index, text)

    for i, (op, text) in enumerate(diffs):
        if op == 0:  # Equal
            # Flush pending delete if any
            if pending_delete:
                idx, del_txt = pending_delete
                edit = DocumentEdit(target_text=del_txt, new_text="", comment="Diff: Text deleted")
                edit._match_start_index = idx
                edits.append(edit)
                pending_delete = None

            current_original_index += len(text)

        elif op == -1:  # Delete
            # Defer deletion to check for immediate insertion (Modification)
            pending_delete = (current_original_index, text)
            current_original_index += len(text)

        elif op == 1:  # Insert
            if pending_delete:
                # Merge into Modification (Replace)
                idx, del_txt = pending_delete
                edit = DocumentEdit(target_text=del_txt, new_text=text, comment="Diff: Replacement")
                edit._match_start_index = idx
                edits.append(edit)
                pending_delete = None
            else:
                # Pure Insertion
                # Find Anchor context
                anchor_start = max(0, current_original_index - 50)
                anchor = original_text[anchor_start:current_original_index]

                # Special Case: Start-of-Document with no anchor
                if not anchor and current_original_index == 0:
                    # Check next equal for context (Forward Anchor)
                    if i + 1 < len(diffs) and diffs[i + 1][0] == 0:
                        next_text = diffs[i + 1][1]
                        # Grab first word or chunk
                        anchor_target = next_text.split(" ")[0] if " " in next_text else next_text[:20]
                        if anchor_target:
                            # Convert to Modification of the following text
                            # Target: "Contract" -> New: "Big Contract"
                            logger.info(f"Converting start-of-doc insert to modification of '{anchor_target}'")

                            edit = DocumentEdit(
                                target_text=anchor_target,
                                new_text=text + anchor_target,
                                comment="Diff: Start-of-doc insertion",
                            )
                            edit._match_start_index = current_original_index
                            edits.append(edit)

                            # We consumed the start of the next text conceptually?
                            # Actually, DMP will process the next Equal text normally.
                            # But we claim we modified it. This is slightly overlapping logic.
                            # However, since we track indices, we just want to ensure we target correctly.
                            # BUT, current_original_index matches the start of anchor_target.
                            # So we assume the next Op=0 will advance past it.
                            # This is a bit hacky. For now, let's stick to standard Anchor logic if possible,
                            # or just use empty anchor if allowed.

                            continue

                # Standard Insertion: Target=Anchor, New=Anchor+Text
                edit = DocumentEdit(target_text=anchor, new_text=anchor + text, comment="Diff: Text inserted")
                edit._match_start_index = current_original_index
                edits.append(edit)

    # Flush trailing delete
    if pending_delete:
        idx, del_txt = pending_delete
        edit = DocumentEdit(target_text=del_txt, new_text="", comment="Diff: Text deleted")
        edit._match_start_index = idx
        edits.append(edit)

    return edits


def _words_to_chars(text1: str, text2: str) -> Tuple[str, str, List[str]]:
    """
    Splits text into words/tokens and encodes them as unique Unicode characters.
    """
    token_array: List[str] = []
    token_hash: Dict[str, int] = {}
    split_pattern = r"(\s+|\w+|[^\w\s])"

    def encode_text(text: str) -> str:
        tokens = [t for t in re.split(split_pattern, text) if t]
        encoded_chars = []
        for token in tokens:
            if token in token_hash:
                encoded_chars.append(chr(token_hash[token]))
            else:
                code = len(token_array)
                token_hash[token] = code
                token_array.append(token)
                encoded_chars.append(chr(code))
        return "".join(encoded_chars)

    chars1 = encode_text(text1)
    chars2 = encode_text(text2)
    return chars1, chars2, token_array
