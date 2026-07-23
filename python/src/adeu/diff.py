import re
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

import structlog
from diff_match_patch import diff_match_patch

from adeu.models import DeleteTableRow, InsertTableRow, ModifyText

if TYPE_CHECKING:
    from adeu.ingest import ExtractStructure, TableGeometry

logger = structlog.get_logger(__name__)

DiffEdit = Union[ModifyText, InsertTableRow, DeleteTableRow]


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


def make_edits_self_contained(
    edits: List[ModifyText],
    original_text: str,
    part_ranges: Optional[List[Tuple[int, int, str]]] = None,
) -> List[ModifyText]:
    """
    Rewrites diff-generated edits so each one can be re-applied by TEXT MATCHING
    alone against the document — including against the document state produced
    by the EARLIER edits of the same batch.

    Diff output resolves positions via the private `_match_start_index`, which
    does not survive JSON serialization (`adeu diff --json`). Without it, an
    atomic edit like target_text="2" is ambiguous — and the match_mode
    fallbacks ('first'/'all') can silently modify unrelated occurrences.

    For every edit whose target_text is empty (pure insertion) or does not
    occur exactly once, this widens the edit with surrounding words from the
    original document until the target is unique, mirroring the added context
    into new_text so the change itself is untouched. The engine's
    trim_common_context re-trims that shared context at apply time, so the
    resulting redline is identical to the positional one.

    Uniqueness is evaluated against a SIMULATION of the sequentially applied
    batch, not against `original_text` alone: batches apply one edit at a
    time, and an earlier edit's replacement text can duplicate a later edit's
    target — the paragraph-swap diff shape, where edit 1 writes paragraph B's
    text over paragraph A and edit 2 then finds B's text twice
    (QA 2026-07-19 ADEU-QA-002 C). A target unique in the original document
    is NOT unique mid-batch.

    Two hard rules keep the widened batch applicable:

      - Expansion never crosses an OPC part boundary (`part_ranges`): widening
        a body edit into footer text produced exactly the cross-part targets
        that corrupted documents in QA 2026-07-18 C1.

      - Expansion never absorbs ANOTHER edit's hunk. Batches apply
        sequentially, so a later edit whose anchor context contains an earlier
        edit's original text can only match inside that edit's tracked
        deletion — apply rightly rejects it, and `diff --json` output stops
        being applicable by its own `apply` (QA 2026-07-19 F-01). This clamp
        is also what keeps the simulation sound: a candidate never overlaps
        another hunk, so its text reads identically in the original and in
        every intermediate state until its own edit applies. When uniqueness
        cannot be reached without crossing a neighboring hunk, the two edits
        are COALESCED into one (hunk + stable gap + hunk), which is both
        unambiguous and sequentially applicable.

    Returns the surviving edit objects (mutated in place); coalescing removes
    absorbed edits from the returned list, so callers must use the return
    value rather than the input list.
    """
    n = len(original_text)

    def _bounds_for(idx: int) -> Tuple[int, int]:
        ranges = [(s, e) for s, e, _k in (part_ranges or []) if e > s]
        prev: Optional[Tuple[int, int]] = None
        for p_start, p_end in ranges:
            if idx < p_start:
                # Inside the separator before this part: the offset belongs to
                # the PREVIOUS part (an insertion there appends to it).
                return prev if prev is not None else (p_start, p_end)
            if idx <= p_end:
                return p_start, p_end
            prev = (p_start, p_end)
        if prev is not None:
            return prev
        return 0, n

    # Snapshot each pinned edit's RAW hunk (pre-widening coordinates). Edits
    # without a trustworthy pin pass through untouched and never block others.
    class _Hunk:
        __slots__ = ("edit", "idx", "target", "new")

        def __init__(self, edit: ModifyText, idx: int, target: str, new: str):
            self.edit = edit
            self.idx = idx
            self.target = target
            self.new = new

        @property
        def end(self) -> int:
            return self.idx + len(self.target)

    hunks: List[_Hunk] = []
    for edit in edits:
        idx = edit._match_start_index
        if idx is None:
            continue  # No positional info; nothing safe to do.
        target = edit.target_text or ""
        # Guard against a stale/mismatched index: only expand when the target
        # really lives at idx (always true for our own diff generators).
        if target and original_text[idx : idx + len(target)] != target:
            continue
        hunks.append(_Hunk(edit, idx, target, edit.new_text or ""))
    hunks.sort(key=lambda h: h.idx)

    dropped: set = set()

    def _widen_in(h: _Hunk, h_pos: int, sim_text: str) -> "Tuple[int, int, Optional[_Hunk]]":
        """
        Computes the widened (start, end) for hunk `h` (at index h_pos in
        `hunks`), clamped at part bounds and neighboring hunks. Uniqueness is
        checked against `sim_text` — the document text as it reads when this
        edit applies (earlier hunks already replaced). Returns (start, end,
        blocking_neighbor); blocking_neighbor is the adjacent hunk that
        prevented uniqueness, or None when the candidate is unique (or only
        part bounds constrain it).
        """
        min_start, max_end = _bounds_for(h.idx)
        left = hunks[h_pos - 1] if h_pos > 0 else None
        right = hunks[h_pos + 1] if h_pos + 1 < len(hunks) else None
        if left is not None:
            min_start = max(min_start, left.end)
        if right is not None:
            max_end = min(max_end, right.idx)

        # Pure insertions widen LEFT-ONLY (right expansion only at a hard
        # left edge): mirrored right context rides AFTER the inserted text in
        # new_text, and trim_common_context resolves prefixes first — when
        # the following text begins like the insertion (e.g. the projection's
        # "1. " list marker), the mirrored suffix is consumed as prefix and
        # the anchor lands INSIDE the marker, splitting it
        # (QA 2026-07-19 ADEU-QA-002 A, list-item swap shape).
        insertion = not h.target

        def _extend_right(pos: int) -> int:
            if insertion and pos == h.idx:
                # First right step for an insertion at a hard left edge:
                # absorb the whole following line so the anchor edge is a
                # line boundary, never the interior of a projected marker.
                nl = original_text.find("\n", pos)
                return max_end if nl == -1 else min(nl, max_end)
            return min(max_end, _next_word_boundary(original_text, pos))

        start, end = h.idx, min(h.end, max_end)
        for _ in range(_MAX_CONTEXT_EXPANSIONS):
            candidate = original_text[start:end]
            if candidate and sim_text.count(candidate) == 1:
                return start, end, None
            if start == min_start and end == max_end:
                break
            if start > min_start:
                start = max(min_start, _prev_word_boundary(original_text, start))
                if insertion:
                    continue
            if not insertion or start == min_start:
                end = _extend_right(end)

        candidate = original_text[start:end]
        if candidate and sim_text.count(candidate) == 1:
            return start, end, None

        # Ambiguous. Prefer coalescing with the closer clamping neighbor.
        blockers: List[Tuple[int, _Hunk]] = []
        if left is not None and start == left.end:
            blockers.append((h.idx - left.end, left))
        if right is not None and end == right.idx:
            blockers.append((right.idx - h.end, right))
        blockers.sort(key=lambda pair: pair[0])
        return start, end, blockers[0][1] if blockers else None

    # Sequential simulation: process hunks in document order (the order our
    # generators emit and the engine applies), widening each against the
    # CURRENT text, then applying its replacement before moving on. When
    # widening dead-ends against a neighboring hunk, coalesce and restart —
    # membership changed, so earlier simulation state is void.
    staged: List[Tuple[_Hunk, int, int]] = []
    while True:
        coalesce_pair: "Optional[Tuple[_Hunk, _Hunk]]" = None
        staged = []
        sim_text = original_text
        delta = 0  # cumulative length shift from already-applied hunks
        for pos, h in enumerate(hunks):
            start, end, blocker = _widen_in(h, pos, sim_text)
            if blocker is not None:
                coalesce_pair = (h, blocker)
                break
            staged.append((h, start, end))
            prefix = original_text[start : h.idx]
            suffix = original_text[h.end : end]
            new_full = f"{prefix}{h.new}{suffix}"
            cur_start = start + delta
            cur_end = end + delta
            sim_text = sim_text[:cur_start] + new_full + sim_text[cur_end:]
            delta += len(new_full) - (end - start)
        if coalesce_pair is None:
            break
        # Coalesce h with its blocking neighbor: hunk + stable gap + hunk.
        h, blocker = coalesce_pair
        first, second = (blocker, h) if blocker.idx <= h.idx else (h, blocker)
        gap = original_text[first.end : second.idx]
        merged = _Hunk(
            first.edit,
            first.idx,
            f"{first.target}{gap}{second.target}",
            f"{first.new}{gap}{second.new}",
        )
        merged.edit.comment = first.edit.comment or second.edit.comment
        dropped.add(id(second.edit))
        pos_first = hunks.index(first)
        hunks[pos_first] = merged
        hunks.remove(second)

    # Write the widened hunks back onto the surviving edit objects.
    for h, start, end in staged:
        prefix = original_text[start : h.idx]
        suffix = original_text[h.end : end]
        if prefix or suffix:
            h.edit.target_text = original_text[start:end]
            h.edit.new_text = f"{prefix}{h.new}{suffix}"
            h.edit._match_start_index = start
        else:
            h.edit.target_text = h.target
            h.edit.new_text = h.new
            h.edit._match_start_index = h.idx

    if not dropped:
        return edits
    return [e for e in edits if id(e) not in dropped]


_IMAGE_MARKER_RE = re.compile(r"!\[[^\]]*\]\(docx-image:[^)]*\)")


def _drop_marker_interior_hunks(
    edits: List[ModifyText],
    text_orig: str,
    warnings: List[str],
) -> List[ModifyText]:
    """
    Removes generated hunks whose pinned range cuts INTO a read-only image
    marker without covering it whole — the shape an alt-text-only difference
    produces (`RED` -> `BLUE` inside `![RED logo](docx-image:1)`). The engine
    categorically rejects such edits, so emitting them makes diff output
    unappliable by apply (QA 2026-07-19 F-04/F-14). Hunks that contain
    complete markers are judged by _drop_image_marker_hunks instead.
    """
    marker_spans = [(m.start(), m.end()) for m in _IMAGE_MARKER_RE.finditer(text_orig)]
    if not marker_spans:
        return edits

    kept: List[ModifyText] = []
    warned = False
    for e in edits:
        idx = e._match_start_index
        if idx is None:
            kept.append(e)
            continue
        end = idx + len(e.target_text or "")
        cuts_into_marker = any(
            m_start < end and idx < m_end and not (idx <= m_start and m_end <= end) for m_start, m_end in marker_spans
        )
        if not cuts_into_marker:
            kept.append(e)
            continue
        if not warned:
            warnings.append(
                "An image's alternative text differs between the documents. Image markers "
                "(![alt](docx-image:N)) are read-only projections, so this difference cannot be "
                "expressed as a text edit — it was skipped; update the image or its alt text "
                "manually in Word."
            )
            warned = True
    return kept


def collect_media_difference_warnings(original_docx: bytes, modified_docx: bytes) -> List[str]:
    """
    Compares embedded media bytes (word/media/*) between two DOCX packages.
    Adeu's diff is a text comparison: two documents whose images differ but
    whose projections agree produce an empty diff, which reads as "visually
    identical" unless the caller says otherwise (QA 2026-07-19 F-04). Returns
    warning strings describing changed/added/removed media members; empty
    when the media sets are byte-identical (or either package is unreadable —
    the caller has already surfaced package-level errors).
    """
    import hashlib
    import zipfile
    from io import BytesIO

    def media_hashes(data: bytes) -> Dict[str, str]:
        hashes: Dict[str, str] = {}
        try:
            with zipfile.ZipFile(BytesIO(data)) as z:
                for name in z.namelist():
                    if name.startswith("word/media/"):
                        hashes[name] = hashlib.sha256(z.read(name)).hexdigest()
        except Exception:
            return {}
        return hashes

    hashes_orig = media_hashes(original_docx)
    hashes_mod = media_hashes(modified_docx)
    if hashes_orig == hashes_mod:
        return []

    changed = sorted(n for n in hashes_orig.keys() & hashes_mod.keys() if hashes_orig[n] != hashes_mod[n])
    added = sorted(hashes_mod.keys() - hashes_orig.keys())
    removed = sorted(hashes_orig.keys() - hashes_mod.keys())
    if not (changed or added or removed):
        return []

    parts = []
    if changed:
        parts.append(f"{len(changed)} changed")
    if added:
        parts.append(f"{len(added)} added")
    if removed:
        parts.append(f"{len(removed)} removed")
    return [
        f"The documents' embedded media differ ({', '.join(parts)}: "
        f"{', '.join((changed + added + removed)[:5])}). This diff compares TEXT only — an empty "
        "edit list does not mean the documents are visually identical. Image changes must be "
        "applied manually in Word."
    ]


def _drop_image_marker_hunks(edits: List[ModifyText], warnings: List[str]) -> List[ModifyText]:
    """
    Removes generated edits whose image-marker multisets differ between
    target and new text. The engine (validate_edit_strings) rightly refuses
    such edits — markers are read-only projections — so emitting them would
    make the diff pipeline reject its own output. An added/removed image is
    reported as a warning instead of an unappliable edit.
    """

    def _normalized(s: str) -> str:
        return re.sub(r"\s+", " ", _IMAGE_MARKER_RE.sub("", s)).strip()

    kept: List[ModifyText] = []
    for e in edits:
        t_imgs = sorted(_IMAGE_MARKER_RE.findall(e.target_text or ""))
        n_imgs = sorted(_IMAGE_MARKER_RE.findall(e.new_text or ""))
        if t_imgs == n_imgs:
            kept.append(e)
            continue
        if _normalized(e.target_text or "") == _normalized(e.new_text or ""):
            warnings.append(
                "An inline image was added or removed between the documents. Images cannot be "
                "transferred by text edits — the image-only difference was skipped; apply it "
                "manually in Word."
            )
        else:
            warnings.append(
                "A text change overlapping an added/removed inline image was skipped — images "
                "cannot be transferred by text edits. Apply that section manually in Word."
            )
    return kept


def _rows_are_plain(text: str, table: "TableGeometry") -> bool:
    """
    True when every projected row reads exactly as its cells joined by " | ".
    Rows wrapped in tracked-row CriticMarkup ({++ … ++} / {-- … --}) do not,
    and such tables are diffed as plain text rather than as row structures.
    """
    for row in table.rows:
        if text[row.start : row.end] != " | ".join(row.cells):
            return False
    return True


def _table_row_opcodes(rows_o, rows_m):
    """
    Row-level alignment opcodes between two tables, or None when the row sets
    differ only cell-internally (no rows added/removed) — the caller then
    keeps fine-grained word-level text edits instead of row operations.
    """
    import difflib

    keys_o = ["\x1f".join(r.cells) for r in rows_o]
    keys_m = ["\x1f".join(r.cells) for r in rows_m]
    opcodes = difflib.SequenceMatcher(None, keys_o, keys_m, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in opcodes:
        if tag in ("insert", "delete") or (tag == "replace" and (i2 - i1) != (j2 - j1)):
            return opcodes
    return None


def _row_ops_for_table(
    table_o: "TableGeometry",
    table_m: "TableGeometry",
    opcodes,
    warnings: List[str],
) -> List[DiffEdit]:
    """
    Emits structured operations (delete_row / insert_row / per-row modify)
    that transform table_o's row set into table_m's, following the alignment
    `opcodes` from _table_row_opcodes (QA 2026-07-18 C2: a generic text edit
    cannot add or remove rows — it writes fake pipe text into one cell or is
    rejected by the cell-count validator).
    """
    rows_o = table_o.rows
    rows_m = table_m.rows
    keys_o = ["\x1f".join(r.cells) for r in rows_o]

    def row_text(r) -> str:
        return " | ".join(r.cells)

    # Duplicate row texts make text-anchored row operations ambiguous. The
    # engine fails closed with a strict-mode ambiguity error at apply time;
    # tell the user up front so the rejection is not a surprise.
    if len(set(keys_o)) != len(keys_o):
        warnings.append(
            "A table contains rows with identical text; the generated row operations anchor by "
            "row text and may be rejected as ambiguous at apply time. If that happens, apply the "
            "row changes with explicit insert_row/delete_row edits."
        )

    # Rows removed by the transformation (deletes + surplus rows of shrinking
    # replaces) can never anchor an insert.
    removed: set = set()
    replaced_new_text: Dict[int, str] = {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "delete":
            removed.update(range(i1, i2))
        elif tag == "replace":
            pairs = min(i2 - i1, j2 - j1)
            removed.update(range(i1 + pairs, i2))
            for k in range(pairs):
                replaced_new_text[i1 + k] = row_text(rows_m[j1 + k])

    surviving = [i for i in range(len(rows_o)) if i not in removed]

    def anchor_text(orig_idx: int) -> str:
        # Anchor on the row's FINAL text: modified rows are matched via the
        # engine's clean-view fallback after their own edit applies.
        return replaced_new_text.get(orig_idx, row_text(rows_o[orig_idx]))

    def insert_ops(new_rows, at_orig_index: int) -> List[DiffEdit]:
        ops: List[DiffEdit] = []
        before = [i for i in surviving if i < at_orig_index]
        after = [i for i in surviving if i >= at_orig_index]
        if before:
            # Insert below the preceding surviving row. Emitting in reverse
            # keeps the final order: below-A(B), then below-A(C) yields A,C,B —
            # so emit C first.
            anchor_idx = before[-1]
            anchor = anchor_text(anchor_idx)
            for r in reversed(new_rows):
                ins_op = InsertTableRow(type="insert_row", target_text=anchor, position="below", cells=list(r.cells))
                # Pin to the anchor row's offset: text anchors alone are
                # ambiguous when tables share identical rows. Pins do not
                # survive JSON round-trips (the strict text match applies
                # then, failing closed with the duplicate-row warning above).
                ins_op._match_start_index = rows_o[anchor_idx].start
                ops.append(ins_op)
        elif after:
            anchor_idx = after[0]
            anchor = anchor_text(anchor_idx)
            for r in new_rows:
                ins_op = InsertTableRow(type="insert_row", target_text=anchor, position="above", cells=list(r.cells))
                ins_op._match_start_index = rows_o[anchor_idx].start
                ops.append(ins_op)
        else:
            warnings.append(
                "A table gained rows but no original row survives to anchor them; "
                "these row insertions were skipped — add them with explicit insert_row operations."
            )
        return ops

    ops: List[DiffEdit] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if tag == "replace":
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                o_txt = row_text(rows_o[i1 + k])
                m_txt = row_text(rows_m[j1 + k])
                if o_txt != m_txt:
                    row_edit = ModifyText(
                        type="modify",
                        target_text=o_txt,
                        new_text=m_txt,
                        comment="Diff: Table row modified",
                    )
                    row_edit._is_table_edit = True
                    ops.append(row_edit)
            for k in range(i1 + pairs, i2):
                del_op = DeleteTableRow(type="delete_row", target_text=row_text(rows_o[k]))
                del_op._match_start_index = rows_o[k].start
                ops.append(del_op)
            surplus_new = [rows_m[j] for j in range(j1 + pairs, j2)]
            if surplus_new:
                ops.extend(insert_ops(surplus_new, i2))
        elif tag == "delete":
            for k in range(i1, i2):
                del_op = DeleteTableRow(type="delete_row", target_text=row_text(rows_o[k]))
                del_op._match_start_index = rows_o[k].start
                ops.append(del_op)
        elif tag == "insert":
            ops.extend(insert_ops([rows_m[j] for j in range(j1, j2)], i1))
    return ops


def generate_structured_edits(
    text_orig: str,
    struct_orig: "ExtractStructure",
    text_mod: str,
    struct_mod: "ExtractStructure",
) -> Tuple[List[DiffEdit], List[str]]:
    """
    DOCX-to-DOCX diff over projections extracted with return_structure=True.

    Improvements over a flat generate_edits_from_text pass:
      - each OPC part (header/body/footer/notes) diffs against its
        counterpart, so no edit can span a part boundary and content that
        moved between parts is reported instead of hidden (QA 2026-07-18 C1);
      - table row insertions/deletions become structured insert_row /
        delete_row operations instead of pipe-text edits (QA C2);
      - every ModifyText is widened with in-part context until unique.

    Returns (edits, warnings). Warnings describe fallbacks the caller should
    surface (differing part layouts, unanchorable rows, …).
    """
    warnings: List[str] = []
    edits: List[DiffEdit] = []

    kinds_o = [k for s, e, k in struct_orig.part_ranges if e > s]
    kinds_m = [k for s, e, k in struct_mod.part_ranges if e > s]

    if kinds_o != kinds_m:
        warnings.append(
            f"The documents have different part layouts ({' + '.join(kinds_o) or 'none'} vs "
            f"{' + '.join(kinds_m) or 'none'}); comparing flattened text instead. Header/footer "
            "additions or removals cannot be expressed as text edits."
        )
        flat = _drop_image_marker_hunks(generate_edits_via_paragraph_alignment(text_orig, text_mod), warnings)
        flat = _drop_marker_interior_hunks(flat, text_orig, warnings)
        return list(make_edits_self_contained(flat, text_orig, part_ranges=struct_orig.part_ranges)), warnings

    ranges_o = [(s, e) for s, e, k in struct_orig.part_ranges if e > s]
    ranges_m = [(s, e) for s, e, k in struct_mod.part_ranges if e > s]

    for (po_start, po_end), (pm_start, pm_end) in zip(ranges_o, ranges_m, strict=True):
        tables_o = [t for t in struct_orig.tables if po_start <= t.start and t.end <= po_end]
        tables_m = [t for t in struct_mod.tables if pm_start <= t.start and t.end <= pm_end]

        tables_alignable = (
            len(tables_o) == len(tables_m)
            and all(_rows_are_plain(text_orig, t) for t in tables_o)
            and all(_rows_are_plain(text_mod, t) for t in tables_m)
        )
        if len(tables_o) != len(tables_m):
            warnings.append(
                f"A {kinds_o[ranges_o.index((po_start, po_end))]} part has {len(tables_o)} table(s) in the "
                f"original but {len(tables_m)} in the modified document; its tables were compared as plain "
                "text. Adding or removing whole tables is not supported via diff/apply."
            )

        if not tables_alignable:
            part_edits = generate_edits_via_paragraph_alignment(text_orig[po_start:po_end], text_mod[pm_start:pm_end])
            for e in part_edits:
                e._match_start_index = (e._match_start_index or 0) + po_start
            edits.extend(part_edits)
            continue

        # Walk interleaved segments: text-before-table, table, text-after…
        boundaries_o = [(po_start, po_start)] + [(t.start, t.end) for t in tables_o] + [(po_end, po_end)]
        boundaries_m = [(pm_start, pm_start)] + [(t.start, t.end) for t in tables_m] + [(pm_end, pm_end)]

        for seg_idx in range(len(boundaries_o) - 1):
            seg_o_start = boundaries_o[seg_idx][1]
            seg_o_end = boundaries_o[seg_idx + 1][0]
            seg_m_start = boundaries_m[seg_idx][1]
            seg_m_end = boundaries_m[seg_idx + 1][0]
            # Paragraph alignment, never a raw word-level diff over the whole
            # segment: dmp legally shifts hunk boundaries across paragraphs
            # sharing a prefix ("...arrears.\n\nThe "), and the engine rightly
            # rejects a deletion with body text on both sides of a paragraph
            # break — every paragraph deletion/reordering in the QA corpus
            # failed replay this way (QA 2026-07-19 ADEU-QA-002 A).
            seg_edits = generate_edits_via_paragraph_alignment(
                text_orig[seg_o_start:seg_o_end], text_mod[seg_m_start:seg_m_end]
            )
            for e in seg_edits:
                e._match_start_index = (e._match_start_index or 0) + seg_o_start
            edits.extend(seg_edits)

            if seg_idx < len(tables_o):
                t_o = tables_o[seg_idx]
                t_m = tables_m[seg_idx]
                row_opcodes = _table_row_opcodes(t_o.rows, t_m.rows)
                if row_opcodes is not None:
                    edits.extend(_row_ops_for_table(t_o, t_m, row_opcodes, warnings))
                else:
                    # Cell-internal changes only (row sets align 1:1). Emit one
                    # ROW-LEVEL edit per differing row — the engine splits it
                    # into per-cell sub-edits along the " | " boundaries. A
                    # word-level diff over the whole table span produces hunks
                    # that start or end inside a cell separator, which apply
                    # into the wrong cell or write literal pipe text. Unpinned
                    # like every other table edit: pinned application bypasses
                    # the cell splitter, and the full row text is the anchor
                    # contract.
                    for r_o, r_m in zip(t_o.rows, t_m.rows, strict=True):
                        o_txt = " | ".join(r_o.cells)
                        m_txt = " | ".join(r_m.cells)
                        if o_txt == m_txt:
                            continue
                        row_edit = ModifyText(
                            type="modify",
                            target_text=o_txt,
                            new_text=m_txt,
                            comment="Diff: Table row modified",
                        )
                        row_edit._is_table_edit = True
                        edits.append(row_edit)

    # Table edits anchor by row text (pins do not survive JSON): if the
    # anchor text also appears elsewhere in the document — e.g. two tables
    # sharing a header row — the strict text match at apply time is
    # ambiguous. In-process consumers ride the pinned offsets; JSON consumers
    # fail closed, so tell the user why up front.
    ambiguous_anchor_warned = False
    for row_op in edits:
        is_table_anchor = isinstance(row_op, (InsertTableRow, DeleteTableRow)) or (
            isinstance(row_op, ModifyText) and row_op._is_table_edit
        )
        if is_table_anchor and not ambiguous_anchor_warned:
            if row_op.target_text and text_orig.count(row_op.target_text) > 1:
                warnings.append(
                    f'The row anchor "{row_op.target_text[:60]}" appears more than once in the document. '
                    "Applying this diff from its JSON output may be rejected as ambiguous — "
                    "make the anchor rows unique, or apply the row changes with explicit "
                    "insert_row/delete_row edits."
                )
                ambiguous_anchor_warned = True

    # Our own output must never trip the engine's read-only image-marker
    # validation: an added/removed image becomes a warning, not an edit —
    # and so does an alt-text change, whose hunk lands INSIDE a marker.
    modify_edits = [e for e in edits if isinstance(e, ModifyText)]
    kept_modifies = set(map(id, _drop_image_marker_hunks(modify_edits, warnings)))
    edits = [e for e in edits if not isinstance(e, ModifyText) or id(e) in kept_modifies]
    modify_edits = [e for e in edits if isinstance(e, ModifyText)]
    kept_modifies = set(map(id, _drop_marker_interior_hunks(modify_edits, text_orig, warnings)))
    edits = [e for e in edits if not isinstance(e, ModifyText) or id(e) in kept_modifies]

    # Table row edits are excluded from context widening: their target is the
    # full row already, and widening would drag " | " separators or "\n" row
    # boundaries from NEIGHBORING rows into the target, misaligning the
    # engine's per-cell splitter.
    text_edits = [
        e for e in edits if isinstance(e, ModifyText) and e._match_start_index is not None and not e._is_table_edit
    ]
    surviving = make_edits_self_contained(text_edits, text_orig, part_ranges=struct_orig.part_ranges)
    if len(surviving) != len(text_edits):
        # Coalescing absorbed neighboring hunks; drop the absorbed edit
        # objects from the outgoing list too.
        surviving_ids = set(map(id, surviving))
        absorbed = {id(e) for e in text_edits if id(e) not in surviving_ids}
        edits = [e for e in edits if id(e) not in absorbed]
    return edits, warnings


def _is_table_blob(block: str) -> bool:
    """
    True when a "\\n\\n"-separated block reads as projected table rows: every
    line carries the " | " cell separator. Table rows are separated by single
    newlines, so a whole table is one block in the paragraph alignment.
    """
    lines = block.split("\n")
    return bool(lines) and all(" | " in line for line in lines)


def _table_blob_row_edits(orig_blob: str, mod_blob: str) -> "List[ModifyText] | None":
    """
    Pairwise row-level edits between two aligned table blobs, or None when
    the blobs cannot be row-aligned (different row counts — a structural
    change the text path hands to the engine's row guards). Row edits are
    unpinned: the full row text is the anchor, and the engine's per-cell
    splitter resolves them — word-level hunks across " | " separators land
    in the wrong cell.
    """
    rows_o = orig_blob.split("\n")
    rows_m = mod_blob.split("\n")
    if len(rows_o) != len(rows_m):
        return None
    row_edits: List[ModifyText] = []
    for r_o, r_m in zip(rows_o, rows_m, strict=True):
        if r_o == r_m:
            continue
        row_edit = ModifyText(
            type="modify",
            target_text=r_o,
            new_text=r_m,
            comment="Diff: Table row modified",
        )
        row_edit._is_table_edit = True
        row_edits.append(row_edit)
    return row_edits


def _split_cross_paragraph_hunks(edits: List[ModifyText]) -> List[ModifyText]:
    """
    Splits generated hunks the engine would reject: an UNBALANCED target
    spanning a paragraph break with body text on both sides ("tail of A\\n\\n
    head of B" — the shape dmp produces when adjacent paragraphs share a
    prefix). Each leading paragraph piece becomes its own separator-carrying
    deletion (an allowed merge shape) and the final piece carries the whole
    replacement text; sequential application produces the identical merged
    result. Without this, one such hunk poisons the entire batch — apply is
    all-or-nothing (QA 2026-07-19 ADEU-QA-002 A).
    """
    out: List[ModifyText] = []
    for e in edits:
        target = e.target_text or ""
        new = e.new_text or ""
        idx = e._match_start_index
        if idx is None or "\n\n" not in target or target.count("\n\n") == new.count("\n\n"):
            out.append(e)
            continue
        parts = target.split("\n\n")
        if len(parts) < 2 or not parts[0].strip() or not parts[-1].strip():
            # One-sided shapes (separator-carrying deletions/insertions) are
            # the engine's supported merge protocol — leave them intact.
            out.append(e)
            continue
        offset = idx
        for piece in parts[:-1]:
            deletion = ModifyText(
                type="modify",
                target_text=piece + "\n\n",
                new_text="",
                comment=e.comment or "Diff: Text deleted",
            )
            deletion._match_start_index = offset
            out.append(deletion)
            offset += len(piece) + 2
        final = ModifyText(type="modify", target_text=parts[-1], new_text=new, comment=e.comment)
        final._match_start_index = offset
        out.append(final)
    return out


def generate_edits_via_paragraph_alignment(original_text: str, modified_text: str) -> List[ModifyText]:
    """
    Aligns original and modified text by paragraph (using SequenceMatcher),
    and then performs precise word-level diffing on replaced blocks of text.
    This prevents localized changes from degrading to a single whole-document block.
    """
    import difflib

    # Normalize trailing whitespace/newlines in modified_text to match original_text
    orig_stripped = original_text.rstrip()
    orig_ws = original_text[len(orig_stripped) :]
    mod_stripped = modified_text.rstrip()
    modified_text = mod_stripped + orig_ws

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
            # A deleted paragraph block must CARRY one adjacent separator, or
            # the deletion leaves an empty paragraph container behind — the
            # clean view then reads "\n\n\n\n" where the supplied text has
            # "\n\n", and the post-apply verification rightly fails
            # (QA 2026-07-19 v8 F-12 fallout; mirrors the insert branch
            # below). Mid-document deletions take the FOLLOWING break;
            # deleting the document's trailing block takes the LEADING one.
            #
            # Multi-paragraph mid-document blocks are emitted as ONE deletion
            # PER paragraph ("A\n\n", "B\n\n"), never "A\n\nB\n\n": the
            # engine's merge protocol supports one deleted paragraph break
            # per edit, and the joint form fails at apply time — the shape
            # that broke paragraph-swap replays (QA 2026-07-19 ADEU-QA-002 A).
            if i2 < len(orig_paragraphs):
                piece_offset = offset
                for k in range(i1, i2):
                    piece = orig_paragraphs[k] + "\n\n"
                    edit = ModifyText(
                        type="modify",
                        target_text=piece,
                        new_text="",
                        comment="Diff: Text deleted",
                    )
                    edit._match_start_index = piece_offset
                    edits.append(edit)
                    piece_offset += len(piece)
            else:
                deleted_text = "\n\n".join(orig_paragraphs[i1:i2])
                if i1 > 0:
                    deleted_text = "\n\n" + deleted_text
                    offset -= 2
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
            # An inserted paragraph must CARRY its paragraph separator, or
            # the engine (rightly) treats the text as an inline insertion and
            # glues it to the neighboring paragraph. Mid-document inserts
            # anchor at the following paragraph's start and keep it separate
            # with a trailing "\n\n" (the same hunk shape word-level diffs
            # emit); end-of-document appends lead with "\n\n" so the new text
            # becomes its own paragraph after the current last one.
            if i1 < len(orig_paragraphs):
                inserted_text = inserted_text + "\n\n"
            else:
                inserted_text = "\n\n" + inserted_text
            edit = ModifyText(
                type="modify",
                target_text="",
                new_text=inserted_text,
                comment="Diff: Text inserted",
            )
            edit._match_start_index = offset
            edits.append(edit)

        elif tag == "replace":
            # Table blobs in equal-count replace blocks pair up positionally
            # and diff as ROW-LEVEL edits; word-level hunks over a table blob
            # start or end inside " | " separators and land in the wrong
            # cell. Prose pairs (and any block whose counts differ) keep the
            # word-level chunk diff.
            if (i2 - i1) == (j2 - j1) and any(
                _is_table_blob(orig_paragraphs[i1 + k]) or _is_table_blob(mod_paragraphs[j1 + k])
                for k in range(i2 - i1)
            ):
                for k in range(i2 - i1):
                    orig_p = orig_paragraphs[i1 + k]
                    mod_p = mod_paragraphs[j1 + k]
                    if orig_p == mod_p:
                        continue
                    pair_offset = orig_offsets[i1 + k]
                    row_edits = (
                        _table_blob_row_edits(orig_p, mod_p)
                        if _is_table_blob(orig_p) and _is_table_blob(mod_p)
                        else None
                    )
                    if row_edits is not None:
                        edits.extend(row_edits)
                        continue
                    pair_edits = generate_edits_from_text(orig_p, mod_p)
                    for ce in pair_edits:
                        ce._match_start_index = (ce._match_start_index or 0) + pair_offset
                        edits.append(ce)
                continue

            orig_chunk = "\n\n".join(orig_paragraphs[i1:i2])
            mod_chunk = "\n\n".join(mod_paragraphs[j1:j2])

            # If the replace block represents a massive truncation, rewrite or insert,
            # do not decompose into fragile, fine-grained character/word diffs.
            # Just generate a single clean replacement edit.
            use_wholesale = False
            if len(orig_chunk) > 120 or len(mod_chunk) > 120:
                m = difflib.SequenceMatcher(None, orig_chunk, mod_chunk)
                if m.real_quick_ratio() < 0.35 and m.ratio() < 0.3:
                    use_wholesale = True

            if use_wholesale:
                edit = ModifyText(
                    type="modify",
                    target_text=orig_chunk,
                    new_text=mod_chunk,
                    comment="Diff: Rewrite",
                )
                edit._match_start_index = offset
                edits.append(edit)
            else:
                chunk_edits = generate_edits_from_text(orig_chunk, mod_chunk)
                for ce in chunk_edits:
                    ce._match_start_index = (ce._match_start_index or 0) + offset
                    edits.append(ce)

    # Word-level diffs over unequal replace chunks can still emit hunks that
    # straddle a paragraph break with body text on both sides; split them
    # into engine-applicable pieces (ADEU-QA-002 A).
    return _split_cross_paragraph_hunks(edits)


def create_unified_diff(
    original_text: str,
    modified_text: str,
    fromfile: str = "original",
    tofile: str = "modified",
) -> str:
    """Produce a standard Git-style unified diff string between two texts."""
    import difflib

    a_lines = original_text.splitlines(keepends=True)
    b_lines = modified_text.splitlines(keepends=True)
    diff = difflib.unified_diff(a_lines, b_lines, fromfile=fromfile, tofile=tofile)
    return "".join(diff)
