import re
from typing import Dict, List, Tuple

import structlog
from diff_match_patch import diff_match_patch

from adeu.models import ModifyText

logger = structlog.get_logger(__name__)


def trim_common_context(target: str, new_val: str) -> tuple[int, int]:
    """
    Calculates overlapping prefix/suffix lengths between target and new_val.
    Returns (prefix_len, suffix_len).
    Ensures that we only trim at word boundaries (whitespace) AND
    do not split Markdown style delimiters (bold/italic).
    """
    if not target or not new_val:
        return 0, 0

    # 1. Prefix with Word Boundary Check
    prefix_len = 0
    limit = min(len(target), len(new_val))
    while prefix_len < limit and target[prefix_len] == new_val[prefix_len]:
        prefix_len += 1

    # Backtrack to nearest whitespace if we split a word
    if prefix_len < len(target) and prefix_len < len(new_val):
        while prefix_len > 0:
            target_split = not target[prefix_len - 1].isspace() and not target[prefix_len].isspace()
            new_split = not new_val[prefix_len - 1].isspace() and not new_val[prefix_len].isspace()
            if target_split or new_split:
                prefix_len -= 1
            else:
                break

    # Backtrack prefix to avoid splitting markdown markers or leaving them unbalanced
    while prefix_len > 0:
        if prefix_len < len(target) and target[prefix_len - 1 : prefix_len + 1] in (
            "**",
            "__",
        ):
            prefix_len -= 1
            continue

        left = target[:prefix_len]
        b_count = left.count("**")
        u2_count = left.count("__")
        u1_count = left.replace("__", "").count("_")

        if b_count % 2 != 0:
            prefix_len = left.rfind("**")
            continue
        if u2_count % 2 != 0:
            prefix_len = left.rfind("__")
            continue
        if u1_count % 2 != 0:
            # Safely find the last standalone '_'
            idx = len(left) - 1
            while idx >= 0:
                if (
                    left[idx] == "_"
                    and (idx == 0 or left[idx - 1] != "_")
                    and (idx == len(left) - 1 or left[idx + 1] != "_")
                ):
                    prefix_len = idx
                    break
                idx -= 1
            continue

        # Safety: Backtrack if we consumed a Markdown Header marker (#)
        temp_len = prefix_len
        hit_header = False
        while temp_len > 0:
            char = target[temp_len - 1]
            if char == "#":
                prefix_len = temp_len - 1
                while prefix_len > 0 and target[prefix_len - 1] != "\n":
                    prefix_len -= 1
                hit_header = True
                break
            if char == "\n":
                break
            temp_len -= 1
        if hit_header:
            continue

        break

    # 2. Suffix with Word Boundary Check
    suffix_len = 0
    target_rem_len = len(target) - prefix_len
    new_rem_len = len(new_val) - prefix_len

    limit_suffix = min(target_rem_len, new_rem_len)
    while suffix_len < limit_suffix and target[-(suffix_len + 1)] == new_val[-(suffix_len + 1)]:
        suffix_len += 1

    # Backtrack suffix if we split a word (Bi-directional check)
    if suffix_len > 0:
        while suffix_len > 0:
            target_split = False
            if suffix_len < len(target):
                target_split = not target[-(suffix_len + 1)].isspace() and not target[-suffix_len].isspace()

            new_split = False
            if suffix_len < len(new_val):
                new_split = not new_val[-(suffix_len + 1)].isspace() and not new_val[-suffix_len].isspace()

            if target_split or new_split:
                suffix_len -= 1
            else:
                break

    # Backtrack suffix to avoid splitting markdown markers or leaving them unbalanced
    while suffix_len > 0:
        idx = len(target) - suffix_len
        if idx > 0 and target[idx - 1 : idx + 1] in ("**", "__"):
            suffix_len -= 1
            continue

        right = target[len(target) - suffix_len :]
        b_count = right.count("**")
        u2_count = right.count("__")
        u1_count = right.replace("__", "").count("_")

        if b_count % 2 != 0:
            idx_in_right = right.find("**")
            suffix_len -= idx_in_right + 2
            continue
        if u2_count % 2 != 0:
            idx_in_right = right.find("__")
            suffix_len -= idx_in_right + 2
            continue
        if u1_count % 2 != 0:
            # Safely find the first standalone '_'
            idx_in_right = 0
            while idx_in_right < len(right):
                if (
                    right[idx_in_right] == "_"
                    and (idx_in_right == 0 or right[idx_in_right - 1] != "_")
                    and (idx_in_right == len(right) - 1 or right[idx_in_right + 1] != "_")
                ):
                    suffix_len -= idx_in_right + 1
                    break
                idx_in_right += 1
            continue
        break

    if suffix_len > 0 and target[len(target) - suffix_len :].isspace():
        suffix_len = 0

    # Fix 5.5: Absorb balanced wrappers into prefix/suffix to avoid leaving markers in the diff.
    # This prevents marker leaks and allows exact matches on formatting blocks (like __init__).
    for marker in ["**", "__", "_"]:
        mlen = len(marker)
        tgt_rem = target[prefix_len : len(target) - suffix_len if suffix_len else len(target)]
        new_rem = new_val[prefix_len : len(new_val) - suffix_len if suffix_len else len(new_val)]

        if (
            tgt_rem.startswith(marker)
            and new_rem.startswith(marker)
            and tgt_rem.endswith(marker)
            and new_rem.endswith(marker)
            and len(tgt_rem) >= 2 * mlen
            and len(new_rem) >= 2 * mlen
        ):
            prefix_len += mlen
            suffix_len += mlen

    return prefix_len, suffix_len


def generate_edits_from_text(original_text: str, modified_text: str) -> List[ModifyText]:
    """
    Compares original and modified text to generate structured ModifyText objects.
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
                edit = ModifyText(type="modify", target_text=del_txt, new_text="", comment="Diff: Text deleted")
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
                edit = ModifyText(type="modify", target_text=del_txt, new_text=text, comment="Diff: Replacement")
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

                            edit = ModifyText(
                                type="modify",
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
                edit = ModifyText(
                    type="modify",
                    target_text=anchor,
                    new_text=anchor + text,
                    comment="Diff: Text inserted",
                )
                edit._match_start_index = current_original_index
                edits.append(edit)

    # Flush trailing delete
    if pending_delete:
        idx, del_txt = pending_delete
        edit = ModifyText(type="modify", target_text=del_txt, new_text="", comment="Diff: Text deleted")
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
