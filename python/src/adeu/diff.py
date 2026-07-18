import re
from typing import Dict, List, Tuple

import structlog
from diff_match_patch import diff_match_patch

from adeu.models import ModifyText

logger = structlog.get_logger(__name__)


def _count_standalone_underscores(s: str) -> int:
    count = 0
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "_":
            # Is it part of "__"?
            is_double = False
            if (i > 0 and s[i - 1] == "_") or (i < n - 1 and s[i + 1] == "_"):
                is_double = True

            # Is it intra-word?
            is_intra = False
            if (i > 0 and s[i - 1].isalnum()) and (i < n - 1 and s[i + 1].isalnum()):
                is_intra = True

            if not is_double and not is_intra:
                count += 1
        i += 1
    return count


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
        u1_count = _count_standalone_underscores(left)

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
                    is_intra = (idx > 0 and left[idx - 1].isalnum()) and (
                        idx < len(left) - 1 and left[idx + 1].isalnum()
                    )
                    if not is_intra:
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
        u1_count = _count_standalone_underscores(right)

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
                    is_intra = (idx_in_right > 0 and right[idx_in_right - 1].isalnum()) and (
                        idx_in_right < len(right) - 1 and right[idx_in_right + 1].isalnum()
                    )
                    if not is_intra:
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

    # Suffix trimming stays active even when the trimmed new_text contains
    # newlines. The engine implements no suffix "transplant" into new
    # paragraph structure: keeping the full suffix inside target_text makes
    # multi-paragraph structured replacements (where target_text spans a
    # paragraph break) produce orphan fragments and standalone reinsertions
    # of the surviving suffix. With the suffix trimmed, the engine sees a
    # clean (target='', new=insertion) pure-insertion edit at the paragraph
    # boundary and handles it via the INSERTION path
    # (get_insertion_anchor + track_insert).

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

    for _, (op, text) in enumerate(diffs):
        if op == 0:  # Equal
            # Flush pending delete if any
            if pending_delete:
                idx, del_txt = pending_delete
                edit = ModifyText(
                    type="modify",
                    target_text=del_txt,
                    new_text="",
                    comment="Diff: Text deleted",
                )
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
                edit = ModifyText(
                    type="modify",
                    target_text=del_txt,
                    new_text=text,
                    comment="Diff: Replacement",
                )
                edit._match_start_index = idx
                edits.append(edit)
                pending_delete = None
            else:
                # Pure Insertion: target_text="" so the engine treats this as a
                # true insertion (op=INSERTION, anchored via get_insertion_anchor).
                # _match_start_index points at the insertion point in the original
                # text. This matches the contract used by every other producer of
                # _match_start_index in the codebase.
                #
                # Never bake an anchor into target_text instead: that yields a
                # contradictory edit object — a target_text claiming to live at
                # [anchor_start : current_original_index] while _match_start_index
                # points at current_original_index — and apply_edits trusts
                # _match_start_index when set, silently corrupting the document.
                #
                # _match_start_index=0 with target_text="" needs no special case:
                # get_insertion_anchor anchors it to the first run of the first
                # paragraph.
                edit = ModifyText(
                    type="modify",
                    target_text="",
                    new_text=text,
                    comment="Diff: Text inserted",
                )
                edit._match_start_index = current_original_index
                edits.append(edit)

    # Flush trailing delete
    if pending_delete:
        idx, del_txt = pending_delete
        edit = ModifyText(
            type="modify",
            target_text=del_txt,
            new_text="",
            comment="Diff: Text deleted",
        )
        edit._match_start_index = idx
        edits.append(edit)

    # Adjacent hunks are deliberately NOT coalesced here. Bridging the gap by
    # appending "gap + edit.target_text" yields semantically meaningless edit
    # objects whenever an adjacent edit is a pure insertion — duplicated text
    # in the rendered diff and silent corruption in the engine's output.
    # Grouped/merged-hunk presentation belongs in the rendering layer, never
    # in these ModifyText objects.
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


def _prev_word_boundary(text: str, pos: int) -> int:
    """Index of the start of the word preceding `pos` (whitespace included)."""
    i = pos
    while i > 0 and text[i - 1].isspace():
        i -= 1
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    return i


def _next_word_boundary(text: str, pos: int) -> int:
    """Index just past the word following `pos` (whitespace included)."""
    i = pos
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    while i < n and not text[i].isspace():
        i += 1
    return i


# Backstop against pathological documents (e.g. one token repeated thousands
# of times): after this many word-by-word expansions we give up and emit the
# widest candidate found. 60 words of context on each side is far beyond what
# any real ambiguity requires.
_MAX_CONTEXT_EXPANSIONS = 60


def make_edits_self_contained(edits: List[ModifyText], original_text: str) -> List[ModifyText]:
    """
    Rewrites diff-generated edits so each one can be re-applied by TEXT MATCHING
    alone against `original_text`.

    Diff output resolves positions via the private `_match_start_index`, which
    does not survive JSON serialization (`adeu diff --json`). Without it, an
    atomic edit like target_text="2" is ambiguous — and the match_mode
    fallbacks ('first'/'all') can silently modify unrelated occurrences.

    For every edit whose target_text is empty (pure insertion) or occurs more
    than once in `original_text`, this widens the edit with surrounding words
    from the original document until the target is unique, mirroring the added
    context into new_text so the change itself is untouched. The engine's
    trim_common_context re-trims that shared context at apply time, so the
    resulting redline is identical to the positional one.

    Returns the same edit objects, mutated in place (target_text, new_text and
    _match_start_index updated).
    """
    n = len(original_text)
    for edit in edits:
        idx = edit._match_start_index
        if idx is None:
            continue  # No positional info; nothing safe to do.
        target = edit.target_text or ""
        # Guard against a stale/mismatched index: only expand when the target
        # really lives at idx (always true for our own diff generators).
        if target and original_text[idx : idx + len(target)] != target:
            continue
        if target and original_text.count(target) == 1:
            continue  # Already unambiguous.

        start, end = idx, idx + len(target)
        for _ in range(_MAX_CONTEXT_EXPANSIONS):
            candidate = original_text[start:end]
            if candidate and original_text.count(candidate) == 1:
                break
            if start == 0 and end == n:
                break
            start = _prev_word_boundary(original_text, start)
            end = _next_word_boundary(original_text, end)

        prefix = original_text[start:idx]
        suffix = original_text[idx + len(target) : end]
        if not prefix and not suffix:
            continue
        edit.target_text = original_text[start:end]
        edit.new_text = f"{prefix}{edit.new_text or ''}{suffix}"
        edit._match_start_index = start

    return edits


def generate_edits_via_paragraph_alignment(original_text: str, modified_text: str) -> List[ModifyText]:
    """
    Aligns original and modified text by paragraph (using SequenceMatcher),
    and then performs precise word-level diffing on replaced blocks of text.
    This prevents localized changes from degrading to a single whole-document block.
    """
    import difflib

    orig_paragraphs = original_text.split("\n\n")
    mod_paragraphs = modified_text.split("\n\n")

    # Compute paragraph offsets
    orig_offsets = []
    current_offset = 0
    for p in orig_paragraphs:
        orig_offsets.append(current_offset)
        current_offset += len(p) + 2

    matcher = difflib.SequenceMatcher(None, orig_paragraphs, mod_paragraphs)
    edits: List[ModifyText] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        offset = orig_offsets[i1] if i1 < len(orig_offsets) else len(original_text)

        if tag == "delete":
            deleted_text = "\n\n".join(orig_paragraphs[i1:i2])
            edit = ModifyText(
                type="modify",
                target_text=deleted_text,
                new_text="",
                comment="Diff: Text deleted",
            )
            edit._match_start_index = offset
            edits.append(edit)

        elif tag == "insert":
            inserted_text = "\n\n".join(mod_paragraphs[j1:j2])
            edit = ModifyText(
                type="modify",
                target_text="",
                new_text=inserted_text,
                comment="Diff: Text inserted",
            )
            edit._match_start_index = offset
            edits.append(edit)

        elif tag == "replace":
            orig_chunk = "\n\n".join(orig_paragraphs[i1:i2])
            mod_chunk = "\n\n".join(mod_paragraphs[j1:j2])
            chunk_edits = generate_edits_from_text(orig_chunk, mod_chunk)
            for ce in chunk_edits:
                ce._match_start_index = (ce._match_start_index or 0) + offset
                edits.append(ce)

    return edits
