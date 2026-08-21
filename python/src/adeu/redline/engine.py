import datetime
import re
from collections import defaultdict
from copy import deepcopy
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import lxml.etree as etree
import structlog
from docx.oxml import parse_xml
from docx.oxml.ns import nsmap, qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from adeu.diff import generate_edits_from_text, trim_common_context
from adeu.markup import format_ambiguity_error
from adeu.models import (
    AcceptChange,
    DeleteTableRow,
    DocumentChange,
    EditOperationType,
    InsertTableRow,
    ModifyText,
    RejectChange,
    ReplyComment,
    SetField,
)
from adeu.pagination import paginate, split_structural_appendix
from adeu.redline.comments import CommentsManager, CommentThreadingError
from adeu.redline.gates import (
    GateOverrides,
    check_block_merge_across_control,
    check_bound_control,
    check_checkbox_edit,
    check_content_lock,
    check_delete_lock,
    check_group_region,
    check_placeholder_target,
    check_protection_blocks_edit,
    check_protection_blocks_review,
    check_untracked_write,
    crossed_control_walls,
    describe_control,
    overrides_note,
    segmentation_note,
)
from adeu.redline.mapper import DocumentMapper, TextSpan
from adeu.utils.docx import create_attribute, create_element, strip_bom_from_docx_bytes
from adeu.utils.opc import load_document as Document
from adeu.utils.protection import read_document_protection
from adeu.utils.safe_regex import RegexTimeoutError
from adeu.utils.text import (
    PREVIEW_TEXT_CAP,
    REPORT_ECHO_CAP,
    clamp_text,
    has_smart_quotes,
    restore_document_typography,
    truncate_middle,
)

logger = structlog.get_logger(__name__)

# Width of the surrounding-document window shown in redline previews.
PREVIEW_CONTEXT_CHARS = 30

# Character ceiling for the multi-author guard refusal, i.e. the ~70-token
# budget at the 4-chars-per-token estimate the message budget tests use.
GUARD_MESSAGE_CAP = 70 * 4

# Register w16du namespace for dateUtc
w16du_ns = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
if "w16du" not in nsmap:
    nsmap["w16du"] = w16du_ns
# Register the prefix with lxml's serializer as well: when a tracked-change
# write sets a w16du:dateUtc attribute on an element with NO in-scope
# declaration, lxml then auto-declares xmlns:w16du on that element (instead
# of minting an ns0 prefix). Elements under a root that already declares the
# prefix keep using the root declaration — for such documents the output is
# byte-identical. This is what lets __init__ skip the historical eager
# root-stamp of the main part, which serialized and re-parsed the entire
# 45 MB part just to add one namespace declaration.
etree.register_namespace("w16du", w16du_ns)


def _empty_bounds() -> List[Optional[int]]:
    return [None, None]


def _extract_failed_indices(errors: List[str]) -> List[Tuple[int, str]]:
    failed = []
    pattern = re.compile(r"^-\s*(?:Action|Edit|Note: Action)\s+(\d+)\b", re.IGNORECASE)
    for err in errors:
        first_line = err.splitlines()[0] if err else ""
        m = pattern.search(first_line)
        if m:
            idx = int(m.group(1)) - 1
            parts = err.split("Failed: ", 1)
            reason = parts[1].strip() if len(parts) > 1 else err.strip()
            failed.append((idx, reason))
        else:
            failed.append((0, err.strip()))
    return failed


def _trim_shared_trailing_paragraph_mark(target: str, new: str) -> Tuple[str, str]:
    """
    Drop paragraph marks that BOTH sides of a replacement end with (CC-14).

    A "\\n\\n" the target and the replacement share is structural context, not
    text to rewrite, and must never reach the apply layer: that layer
    track-deletes a trailing mark inside a target -- a genuine paragraph merge,
    "A.\\n\\n" -> "Z.", depends on it -- but does not re-create the one the
    replacement asks for. The break silently disappears while the batch still
    reports the edit applied.

    Trims from the END only, so a caller-pinned start index stays valid. The
    real merge shape (target ends with a mark, replacement does not) is left
    alone, as is a shared LEADING mark, which the apply layer handles
    correctly today.
    """
    while target.endswith("\n\n") and new.endswith("\n\n"):
        target = target[:-2]
        new = new[:-2]
    return target, new


class BatchValidationError(Exception):
    """Raised when text edits fail location validation."""

    def __init__(self, errors: List[str], failed: Optional[List[Tuple[int, str]]] = None):
        super().__init__("Batch validation failed:\n" + "\n".join(errors))
        self.errors = errors
        if failed is None:
            failed = _extract_failed_indices(errors)
        self.failed = failed


# Characters XML 1.0 cannot represent: C0 controls except tab/newline/CR.
# lxml refuses to serialize them, so without an up-front check they surfaced
# as a raw "All strings must be XML compatible" traceback from deep inside
# lxml instead of a clean per-edit error (QA 2026-07-17 F11).
XML_ILLEGAL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# CC-1e: content-control anchors, open or close, with or without flag words.
_CC_ANCHOR_SCAN_RE = re.compile(r"\{#(/?)cc:(\d+)(?: [^}]*)?\}")
_CC_ANCHOR_RE = re.compile(r"\{#/?cc:\d+[^}]*\}")

# The sanctioned empty-pair fill target (spec-projection.md §3): an open and
# close anchor for the SAME ordinal with nothing between them but an optional
# placeholder bubble.
_CC_EMPTY_PAIR_RE = re.compile(r"^\{#cc:(\d+)[^}]*\}(?:\{>>placeholder:[^<]*<<\})?\{#/cc:\1\}$")

# Children of a properties container that the corresponding tracked-change
# record cannot store, and which must therefore survive rejecting it.
#
# w:sectPrChange stores a CT_SectPrBase, and per ECMA-376 that type carries no
# EG_HdrFtrReferences — header/footer references exist only on the live
# CT_SectPr. Clearing the container wholesale (correct for w:rPrChange, whose
# stored child is a complete w:rPr) would delete the section's headers and
# footers with nothing to restore them from. CT_SectPr also sequences
# EG_HdrFtrReferences ahead of EG_SectPrContents, so leaving these in place
# keeps the element order valid once the stored properties are appended.
#
# Kept byte-for-byte in step with the Node twin
# (node/packages/core/src/engine.ts PROPS_REVERT_PRESERVED_CHILDREN).
PROPS_REVERT_PRESERVED_CHILDREN: dict[str, tuple[str, ...]] = {
    qn("w:sectPr"): (qn("w:headerReference"), qn("w:footerReference")),
}


def describe_illegal_control_chars(text: str) -> Optional[str]:
    """Human-readable listing of XML-illegal control characters in `text`, or None."""
    if not text:
        return None
    found = sorted({f"0x{ord(c):02x}" for c in XML_ILLEGAL_CHARS_RE.findall(text)})
    if not found:
        return None
    return ", ".join(found)


def validate_review_action_batch(
    actions: List[Union["AcceptChange", "RejectChange", "ReplyComment"]],
    indices: Optional[List[int]] = None,
) -> List[str]:
    """
    Document-context-free validation of review actions (QA 2026-07-19 v8 F-07):

      - A reply's text must not be blank/whitespace-only — Word renders it as
        an empty comment bubble that reads as a data-loss bug to reviewers.
      - The same accept/reject may not name the same target_id twice in one
        batch, and accept + reject may not both name one target_id: the first
        action resolves the change, so the duplicate either double-counts as
        "applied" or conflicts. (Distinct IDs that one action resolves as a
        group — e.g. the del+ins pair of a single modification — remain fine.)
      - Duplicated identical replies (same comment, same text) are the
        double-send shape and are rejected; DIFFERENT replies to one comment
        are a legitimate thread.

    Shared by the disk engine and the Live Word pipeline; the Node engine
    mirrors these checks in `validate_review_actions`.
    """
    errors: List[str] = []
    seen_resolutions: dict = {}
    seen_replies: set = set()
    for i, act in enumerate(actions):
        batch_idx = indices[i] if indices else i
        act_type = getattr(act, "type", "")
        target_id = getattr(act, "target_id", "")
        if act_type == "reply":
            reply_text = (getattr(act, "text", "") or "").strip()
            if not reply_text:
                errors.append(
                    f"- Action {batch_idx + 1} Failed: reply text for {target_id} is empty or "
                    "whitespace-only. Word would show a blank comment bubble — provide the "
                    "reply content in 'text'."
                )
                continue
            reply_key = (target_id, reply_text)
            if reply_key in seen_replies:
                errors.append(
                    f"- Action {batch_idx + 1} Failed: duplicate reply — this batch already replies to "
                    f"{target_id} with the same text. Remove the duplicate action."
                )
            seen_replies.add(reply_key)
        elif act_type in ("accept", "reject"):
            prior = seen_resolutions.get(target_id)
            if prior is not None:
                first_pos, first_type = prior
                first_batch_idx = indices[first_pos] if indices else first_pos
                if first_type == act_type:
                    errors.append(
                        f"- Action {batch_idx + 1} Failed: duplicate action — Action {first_batch_idx + 1} in this "
                        f"batch already applies '{act_type}' to {target_id}. A change can only be "
                        "resolved once; remove the duplicate action."
                    )
                else:
                    errors.append(
                        f"- Action {batch_idx + 1} Failed: conflicting actions — Action {first_batch_idx + 1} in "
                        f"this batch applies '{first_type}' to {target_id}, but this action applies "
                        f"'{act_type}'. Decide the outcome and keep exactly one of them."
                    )
            else:
                seen_resolutions[target_id] = (i, act_type)
    return errors


def validate_edit_strings(
    edits: List[Union["ModifyText", "InsertTableRow", "DeleteTableRow", "SetField"]],
    index_offset: int = 0,
) -> List[str]:
    """
    Performs document-context-free validation on a batch of edits.

    Checks the shape of `target_text` and `new_text` strings for forbidden
    constructs:
      - Manual CriticMarkup tags ({++, {--, {>>, {==) in new_text
      - Heading levels greater than 6 (####### Title)
      - Footnote/endnote marker insertion or deletion via text replace
      - Hyperlink structural manipulation via text replace
      - Cross-reference marker manipulation
      - Internal anchor `{#name}` modification

    These checks need no document context. Both the disk pipeline (via
    `RedlineEngine.validate_edits`) and the Live Word pipeline call this
    function to ensure consistent rejection of malformed edit shapes.

    The remaining document-aware checks (target text not found, ambiguous
    match, modification targeting Structural Appendix, edits overlapping
    foreign-author insertions) live inside `RedlineEngine.validate_edits`
    because they require a loaded Document and DocumentMapper.

    Args:
        edits: list of edit operations to validate.
        index_offset: added to each edit's 0-based position when rendering the
            1-based "Edit N Failed" labels. Callers validating one edit at a
            time (the sequential batch loop) pass the edit's position in the
            full batch so error labels stay correct.

    Returns:
        List of error message strings. Empty if all edits pass these checks.
    """
    errors: List[str] = []

    for i, edit in enumerate(edits, start=index_offset):
        # `set_field` has no target_text - it addresses a control by id rather
        # than by content - but its `value` is written into the document and
        # must clear exactly the same bar as any other inserted string. A
        # value containing `{#cc:3}` or raw CriticMarkup would fabricate
        # anchors and reviewer names as prose (CC-1e), and routing it here is
        # what stops `set_field` becoming a hole in that check.
        t_text = getattr(edit, "target_text", None) or ""
        n_text = getattr(edit, "new_text", None)
        if n_text is None:
            n_text = getattr(edit, "value", None)
        n_text = n_text or ""

        # VAL-CRIT-8: XML-illegal control characters. These can never be
        # written into a DOCX (lxml refuses), so reject them here with a clean
        # per-edit error instead of a raw lxml traceback at apply time.
        checked_fields = [("target_text", t_text), ("new_text", n_text)]
        comment_text = getattr(edit, "comment", None)
        if comment_text:
            checked_fields.append(("comment", comment_text))
        for cell_idx, cell in enumerate(getattr(edit, "cells", []) or []):
            checked_fields.append((f"cells[{cell_idx}]", cell or ""))
        for field_name, field_value in checked_fields:
            described = describe_illegal_control_chars(field_value)
            if described:
                errors.append(
                    f"- Edit {i + 1} Failed: `{field_name}` contains control character(s) ({described}) "
                    "that cannot be stored in a DOCX. Remove them and re-submit."
                )

        # VAL-CRIT-6: CriticMarkup Hallucination Prevention
        if "{++" in n_text or "{--" in n_text or "{>>" in n_text or "{==" in n_text:
            errors.append(
                f"- Edit {i + 1} Failed: Do not manually write CriticMarkup tags "
                "({++, {--, {>>, {==) in `new_text`. The engine handles redlining "
                "automatically. To add a comment, use the `comment` parameter."
            )

        # VAL-CRIT-3 & VAL-CRIT-4: Footnotes/Endnotes Structural Integrity
        if "[^" in t_text or "[^" in n_text:
            t_fns = re.findall(r"\[\^(?:fn|en)-[^\]]+\]", t_text)
            n_fns = re.findall(r"\[\^(?:fn|en)-[^\]]+\]", n_text)
            if sorted(t_fns) != sorted(n_fns):
                if len(n_fns) > len(t_fns) or any(n_fns.count(f) > t_fns.count(f) for f in n_fns):
                    errors.append(
                        f"- Edit {i + 1} Failed: Cannot insert footnote/endnote markers via text replace. "
                        "Markers like `[^fn-N]` are read-only projections. Use Word's References menu."
                    )
                else:
                    errors.append(
                        f"- Edit {i + 1} Failed: Cannot delete footnote/endnote references via text replace. "
                        "The marker corresponds to a structural XML element."
                    )

        # VAL-CRIT-5: Hyperlink Structural Integrity
        if "](" in t_text or "](" in n_text:
            # Exclude cross-references using a negative lookahead for `~` immediately after `[`
            t_links = re.findall(r"\[(?!~)[^\]]+\]\([^)]+\)", t_text)
            n_links = re.findall(r"\[(?!~)[^\]]+\]\([^)]+\)", n_text)
            if len(t_links) != len(n_links):
                if len(n_links) > len(t_links):
                    errors.append(
                        f"- Edit {i + 1} Failed: Cannot insert hyperlinks via text replace. "
                        "Inserting new hyperlinks is not supported; insert the display text "
                        "instead (editing the text or URL of an existing link IS supported)."
                    )
                else:
                    errors.append(
                        f"- Edit {i + 1} Failed: Cannot delete hyperlinks via text replace. "
                        "The marker corresponds to a structural XML element."
                    )
            elif len(t_links) > 1 and sorted(t_links) != sorted(n_links):
                errors.append(
                    f"- Edit {i + 1} Failed: Can only edit or retarget one hyperlink per text replacement. "
                    "Please split into multiple edits."
                )

        # VAL-CRIT-5: Cross-reference Structural Integrity
        if "[~" in t_text or "[~" in n_text:
            t_xrefs_list = re.findall(r"\[~[^~]+~\]\(#[^\)]+\)", t_text)
            n_xrefs_list = re.findall(r"\[~[^~]+~\]\(#[^\)]+\)", n_text)

            if len(t_xrefs_list) != len(n_xrefs_list):
                if len(n_xrefs_list) > len(t_xrefs_list):
                    errors.append(
                        f"- Edit {i + 1} Failed: Cannot insert cross-references via text replace. "
                        "Markers are read-only projections."
                    )
                else:
                    errors.append(
                        f"- Edit {i + 1} Failed: Cannot delete cross-references via text replace. "
                        "The marker corresponds to a structural XML element."
                    )
            else:
                target_xrefs = dict(re.findall(r"\[~([^~]+)~\]\(#([^\)]+)\)", t_text))
                new_xrefs = dict(re.findall(r"\[~([^~]+)~\]\(#([^\)]+)\)", n_text))
                for t_ref_text, t_hash in target_xrefs.items():
                    if t_hash in new_xrefs.values():
                        for n_ref_text, n_hash in new_xrefs.items():
                            if n_hash == t_hash and n_ref_text != t_ref_text:
                                errors.append(
                                    f"- Edit {i + 1} Failed: Cross-reference display text is computed. "
                                    "To change it, edit the heading or paragraph at the target instead."
                                )
                    elif t_ref_text in new_xrefs:
                        if new_xrefs[t_ref_text] != t_hash:
                            errors.append(
                                f"- Edit {i + 1} Failed: Directly retargeting cross-references via text "
                                "replacement is disallowed to prevent dependency corruption."
                            )
                    else:
                        errors.append(
                            f"- Edit {i + 1} Failed: Modifying cross-reference markers is disallowed "
                            "to prevent dependency corruption."
                        )

        # QA 2026-07-18 M5: image markers are read-only projections of
        # w:drawing elements. They cannot be fabricated, duplicated or
        # removed through text replacement.
        if "docx-image:" in t_text or "docx-image:" in n_text:
            t_imgs = re.findall(r"!\[[^\]]*\]\(docx-image:[^)]*\)", t_text)
            n_imgs = re.findall(r"!\[[^\]]*\]\(docx-image:[^)]*\)", n_text)
            if sorted(t_imgs) != sorted(n_imgs):
                errors.append(
                    f"- Edit {i + 1} Failed: image markers (![alt](docx-image:N)) are read-only "
                    "projections of embedded images. They cannot be inserted, altered, or removed "
                    "via text replacement — edit the text around the image instead."
                )

        # VAL-OBS-9: Internal Anchor Structural Integrity
        if "{#" in t_text or "{#" in n_text:
            t_anchors = re.findall(r"\{#[^\}]+\}", t_text)
            n_anchors = re.findall(r"\{#[^\}]+\}", n_text)
            for anchor in n_anchors:
                if n_anchors.count(anchor) > t_anchors.count(anchor):
                    errors.append(
                        f"- Edit {i + 1} Failed: Cannot modify or insert internal anchor markers (`{{#...}}`). "
                        "These represent structural XML bookmarks."
                    )
                    break

        # CC-1e / A1.7: content-control anchors are structural in BOTH
        # directions. VAL-OBS-9 above only counts anchors that GAINED copies,
        # so it catches fabrication and rewriting but not deletion: a target
        # covering `{#/cc:3}` whose new_text omits it passed cleanly and
        # silently unbalanced the pair in the projection.
        #
        # Scoped to `cc` anchors rather than made symmetric for every `{#...}`
        # token, because two anchor classes are deliberate TARGETING surfaces
        # that a symmetric rule would break: `{#cell:paraId}` empty-cell writes
        # (engine.py `^\{#cell:[^}]+\}$`) and the empty pair below.
        if "{#" in t_text and "cc:" in t_text or "cc:" in n_text:
            t_cc = _CC_ANCHOR_RE.findall(t_text)
            n_cc = _CC_ANCHOR_RE.findall(n_text)
            # Sanctioned edit surface #1 (spec-projection.md §3): the empty
            # pair is deliberately matchable and is the text-first fill. The
            # anchors are not being deleted there — the wrapper survives and
            # only the control's CONTENT changes — so the fill must stay legal
            # for CC-4/CC-5 to route through set_field semantics.
            fills_empty_pair = _CC_EMPTY_PAIR_RE.match(t_text.strip()) is not None
            # ORDERED comparison, unlike the footnote/image checks above, which
            # compare multisets. A multiset lets `{#cc:3}A{#/cc:3}` become
            # `{#/cc:3}A{#cc:3}` — same tokens, inverted pair. Text replacement
            # cannot move an sdt wrapper anyway, so reordering controls is never
            # a legitimate edit and order is the honest invariant.
            if t_cc != n_cc and not fills_empty_pair:
                errors.append(
                    f"- Edit {i + 1} Failed: Cannot insert, alter, or remove content-control "
                    "anchor markers (`{#cc:N}` / `{#/cc:N}`). They are read-only projections "
                    "of the control's structure, not text. Edit the content BETWEEN the "
                    "anchors, keeping both tokens in `new_text` exactly as they appear."
                )

        # Heading level > 6 (only meaningful for ModifyText with new_text)
        if isinstance(edit, ModifyText) and edit.new_text:
            for line in edit.new_text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#######"):
                    level = len(stripped) - len(stripped.lstrip("#"))
                    if stripped[level:].startswith(" ") or not stripped[level:]:
                        errors.append(f"- Edit {i + 1} Failed: Heading level {level} is not supported (maximum is 6).")
                        break

        # VAL-OBS-10: Appendix Boundary Structural Integrity
        if (
            "READONLY_BOUNDARY_START" in t_text
            or "READONLY_BOUNDARY_START" in n_text
            or "# Document Structure (Read-Only)" in t_text
            or "# Document Structure (Read-Only)" in n_text
        ):
            errors.append(
                f"- Edit {i + 1} Failed: Modification targets the read-only boundary "
                "(Structural Appendix). This section cannot be edited."
            )

    return errors


class RedlineEngine:
    def __init__(
        self,
        doc_stream: BytesIO,
        author: str = "Adeu AI",
        id_discovery_hint: Optional[str] = None,
        terse_errors: bool = False,
        ignore_control_locks: bool = False,
        ignore_document_protection: bool = False,
        allow_untracked_writes: bool = False,
    ):
        self.terse_errors = terse_errors
        # CC-4 write-gate overrides. Engine kwargs rather than process_batch
        # arguments because the gates run in three places — validate_edits,
        # the resolver and the apply-path backstop — and only the first takes
        # batch arguments today. Same route terse_errors takes.
        self.gate_overrides = GateOverrides(
            ignore_control_locks=ignore_control_locks,
            ignore_document_protection=ignore_document_protection,
            allow_untracked_writes=allow_untracked_writes,
        )
        # Controls whose locks an override actually bypassed, for the report
        # disclosure (spec-gates §5). Reset per batch.
        self._overridden_controls: List[Any] = []
        # Surface-aware advice for "how do I list the current Chg:/Com: ids":
        # the CLI default points at CLI commands; the MCP layer passes a
        # read_docx-based hint because MCP callers cannot run the CLI
        # (QA 2026-07-23 F11).
        self.id_discovery_hint = id_discovery_hint
        doc_stream.seek(0)
        sanitized_bytes = strip_bom_from_docx_bytes(doc_stream.read())
        # Pristine load-time bytes + mutation flag power the LAZY pre-batch
        # snapshot: while nothing has mutated the tree, "snapshot" is these
        # bytes verbatim — no save_to_stream (full serialize + re-zip) needed.
        # apply_review_actions / apply_edits flip the flag on first applied
        # change; __init__ (also the rollback path) resets it.
        self._pristine_bytes = sanitized_bytes
        self._mutated_since_load = False
        # Whether the LAST batch that was rejected provably left the document
        # as it was (see _verify_rollback). A caller that reuses this engine —
        # or its document — after a rejection MUST check it: False means the
        # in-memory state no longer matches the file it was loaded from, and
        # reusing it compounds the damage (BUG 2026-08-12: one comment
        # collected three identical replies that way).
        self.rollback_verified = True
        self.doc = Document(BytesIO(sanitized_bytes))
        # Read once at load (spec-gates §3), not per gate: it lives in
        # word/settings.xml, which nothing else in a batch touches, and the
        # gates, the projection banner and the fields ledger must all report
        # the same state.
        self.protection = read_document_protection(self.doc)

        # No part is stamped with the w16du namespace up front. Tracked-change
        # writes (w16du:dateUtc attributes) self-declare the prefix locally on
        # the element they land on, via the lxml prefix registration at module
        # scope; documents whose root already declares w16du keep using that
        # declaration and serialize byte-identically. The historical eager
        # stamp of the main part serialized + re-parsed the ENTIRE part
        # (seconds on a 45 MB document.xml) just to add one declaration.
        # Untouched parts staying byte-for-byte untouched (report F9 / TC5)
        # is preserved a fortiori — nothing is written anywhere at init.
        part = self.doc.part
        if hasattr(part, "_element"):
            part._adeu_element = part._element  # type: ignore[attr-defined]
        elif not hasattr(part, "_adeu_element"):
            part._adeu_element = parse_xml(part.blob)  # type: ignore[attr-defined]

        self.author = author
        self.timestamp = (
            datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self.current_id = self._scan_existing_ids()
        self.mapper = DocumentMapper(self.doc)
        # Offsets into mapper.full_text; rebuilt whenever the mapper is.
        self._cc_anchor_pairs: "list[tuple[int, int, int]] | None" = None
        # (projection text, ledger rows) - see _field_entries.
        self._field_entries_cache: Optional[Tuple[str, List[Any]]] = None
        self.comments_manager = CommentsManager(self.doc)
        self.clean_mapper: Optional[DocumentMapper] = None
        self.original_mapper: Optional[DocumentMapper] = None
        self.skipped_details: List[str] = []
        self._bullet_num_id: Optional[str] = None
        # Set by _reply_to_comment when a reply's parent could not be threaded,
        # so apply_review_actions reports the real reason instead of the
        # misleading "no comment with that id exists" (B1).
        self._reply_threading_error: Optional[str] = None
        # Comment removals accept_all_revisions actually performed, attributed
        # to their authors (B2).
        self.removed_comment_notes: List[str] = []

    def _check_punctuation_warning(self, target_text: str) -> Optional[str]:
        """Return a hint when a short, single-token anchor contains punctuation
        that can split awkwardly, else None.

        Surface this ONLY for edits that actually failed to match/apply. On a
        successful edit the batch report already carries the redline preview, so
        emitting this would be a false positive: the punctuation (dates,
        ``[_name_]`` placeholders, ``____`` blanks) is frequently the literal
        target and the edit succeeds despite it.
        """
        if not target_text:
            return None
        if len(target_text) > 20 or " " in target_text:
            return None
        if "_" in target_text or "-" in target_text:
            return (
                f"Warning: target_text '{target_text}' contains tokenization-splitting punctuation "
                "('_' or '-'). This can trigger mid-word splits in the diff engine. "
                "Consider using a longer plain-prose anchor."
            )
        return None

    # CriticMarkup wrapper pairs used when tidying preview context windows.
    _PREVIEW_WRAPPER_PAIRS = (("{--", "--}"), ("{++", "++}"), ("{==", "==}"), ("{>>", "<<}"))
    _PREVIEW_META_BLOCK_RE = re.compile(r"\{>>.*?<<\}", re.DOTALL)
    # 1-2 char remnants of a 3-char wrapper token chopped by the window edge.
    _PREVIEW_LEAD_ORPHAN_RE = re.compile(r"^[-+=<>]{0,2}\}")
    _PREVIEW_TAIL_ORPHAN_RE = re.compile(r"\{[-+=<>]{0,2}$")

    @classmethod
    def _tidy_preview_context(cls, snippet: str, side: str) -> str:
        """
        Makes a fixed-width slice of the raw-view projection presentable:
        drops complete {>>...<<} meta blocks (annotations of pre-existing
        changes, not part of this edit) and any wrapper fragments the window
        boundary chopped in half. Without this, previews leak internal
        scaffolding like "[Chg:5 delete]" (QA H1).
        """
        snippet = cls._PREVIEW_META_BLOCK_RE.sub("", snippet)

        for open_tok, close_tok in cls._PREVIEW_WRAPPER_PAIRS:
            if side == "before":
                # Cut through the last closer whose opener lies left of the window.
                depth = 0
                cut = 0
                i = 0
                while i < len(snippet):
                    if snippet.startswith(open_tok, i):
                        depth += 1
                        i += len(open_tok)
                    elif snippet.startswith(close_tok, i):
                        if depth == 0:
                            cut = i + len(close_tok)
                        else:
                            depth -= 1
                        i += len(close_tok)
                    else:
                        i += 1
                snippet = snippet[cut:]
            else:
                # Cut from the first opener whose closer lies right of the window.
                opens: List[int] = []
                i = 0
                while i < len(snippet):
                    if snippet.startswith(open_tok, i):
                        opens.append(i)
                        i += len(open_tok)
                    elif snippet.startswith(close_tok, i):
                        if opens:
                            opens.pop()
                        i += len(close_tok)
                    else:
                        i += 1
                if opens:
                    snippet = snippet[: opens[0]]

        if side == "before":
            snippet = cls._PREVIEW_LEAD_ORPHAN_RE.sub("", snippet)
        else:
            snippet = cls._PREVIEW_TAIL_ORPHAN_RE.sub("", snippet)
        return snippet

    def _capture_preview_context(self, edit: Any) -> None:
        """
        Snapshots the document text around a resolved edit BEFORE anything is
        applied. Previews rendered after the batch mutates the DOM cannot slice
        full_text at the stored offsets: applied edits shift offsets and inject
        tracked-change markup, garbling previews with unrelated edits and
        internal scaffolding (QA H1).
        """
        if not isinstance(edit, ModifyText):
            return
        start_idx = edit._resolved_start_idx
        if start_idx is None:
            return
        active_mapper = edit._active_mapper_ref or self.mapper
        full_text = active_mapper.full_text
        if not full_text:
            return
        length = len(edit.target_text or "")
        before = full_text[max(0, start_idx - PREVIEW_CONTEXT_CHARS) : start_idx]
        after = full_text[start_idx + length : start_idx + length + PREVIEW_CONTEXT_CHARS]
        edit._preview_context = (
            self._tidy_preview_context(before, "before"),
            self._tidy_preview_context(after, "after"),
        )

    def _capture_parent_preview_context(self, parent: Any) -> None:
        """
        Like _capture_preview_context, but snapshots the context around the
        ORIGINAL edit's full matched span (stashed by _pre_resolve_heuristic_edit),
        so the report preview can present the complete logical change of a
        compound modification instead of its first sub-edit.
        """
        if not isinstance(parent, ModifyText):
            return
        if parent._preview_context is not None or parent._preview_span is None:
            return
        start_idx, match_len = parent._preview_span
        active_mapper = parent._preview_mapper_ref or self.mapper
        full_text = active_mapper.full_text
        if not full_text:
            return
        before = full_text[max(0, start_idx - PREVIEW_CONTEXT_CHARS) : start_idx]
        after = full_text[start_idx + match_len : start_idx + match_len + PREVIEW_CONTEXT_CHARS]
        parent._preview_context = (
            self._tidy_preview_context(before, "before"),
            self._tidy_preview_context(after, "after"),
        )

    def _build_full_match_preview(self, edit: ModifyText) -> Tuple[Optional[str], Optional[str]]:
        """
        Renders the preview from the edit's full matched span. The common
        prefix/suffix between matched and replacement text is moved into the
        surrounding context so the {--...--}{++...++} block shows the minimal
        complete change.
        """
        context_before, context_after = edit._preview_context  # type: ignore[misc]
        matched = edit._preview_matched_text or ""
        new_text = edit._preview_new_text if edit._preview_new_text is not None else (edit.new_text or "")

        proxy = getattr(edit, "_resolved_proxy_edit", None)
        if proxy is not None and getattr(proxy, "_internal_op", None) == EditOperationType.PARAGRAPH_REPLACE:
            # Heading markdown prefixes are projection artifacts, not literal
            # document text (see the F4/F5 note in _build_edit_context_previews).
            matched = re.sub(r"^#+\s*", "", matched)
            new_text = re.sub(r"^#+\s*", "", new_text)

        prefix_len, suffix_len = trim_common_context(matched, new_text)
        display_target = matched[prefix_len : len(matched) - suffix_len]
        display_new = new_text[prefix_len : len(new_text) - suffix_len]
        context_before = context_before + matched[:prefix_len]
        if suffix_len:
            context_after = matched[len(matched) - suffix_len :] + context_after

        display_target = truncate_middle(display_target, PREVIEW_TEXT_CAP)
        display_new = truncate_middle(display_new, PREVIEW_TEXT_CAP)
        if not display_target and not display_new:
            # Comment-only edit (text unchanged): highlight the anchor instead
            # of rendering an empty change.
            anchor = truncate_middle(matched, PREVIEW_TEXT_CAP)
            body = f"{{=={anchor}==}}" if anchor else ""
            critic_markup = f"{context_before[: len(context_before) - len(matched)]}{body}{context_after}"
        else:
            deletion = f"{{--{display_target}--}}" if display_target else ""
            insertion = f"{{++{display_new}++}}" if display_new else ""
            critic_markup = f"{context_before}{deletion}{insertion}{context_after}"

        clean_text = critic_markup
        clean_text = re.sub(r"\{>>.*?<<\}", "", clean_text, flags=re.DOTALL)
        clean_text = re.sub(r"\{--.*?--\}", "", clean_text, flags=re.DOTALL)
        clean_text = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", clean_text, flags=re.DOTALL)
        return critic_markup, clean_text

    # Virtual projection tokens the preview window absorbs when extending a
    # modified span outward, so a window never starts/ends between a wrapper
    # token and its content (which the edge tidier would then chop away).
    _PREVIEW_MARKUP_TOKENS = frozenset({"{--", "--}", "{++", "++}", "{==", "==}", "**", "_", "__"})
    # At most this many disjoint windows are rendered per edit; each window is
    # capped at REPORT_ECHO_CAP chars (bounded reports, QA C2).
    _PREVIEW_MAX_WINDOWS = 10

    def _build_post_apply_previews(self, edit: Any) -> Optional[Tuple[str, str]]:
        """
        Builds the report preview by slicing the document's ACTUAL raw
        projection AFTER the edit applied (F6, QA 2026-07-23): the
        critic_markup preview is the window(s) of self.mapper.full_text
        covering EVERY span the edit modified (located via the revision ids it
        wrote — all occurrences of a match_mode="all" fan-out), and the clean
        preview is the same window(s) with markup resolved to the accepted
        state. Synthesizing previews from pre-apply snapshots instead showed
        only the first occurrence, rendered other pending insertions as
        already-accepted text, and nested CriticMarkup on same-author
        re-edits. {>>…<<} meta bubbles are stripped for compactness
        (previews must never leak scaffolding, QA H1).

        Returns None when the edit wrote no revision ids (comment-only edits,
        URL retargets, virtual no-ops) — callers fall back to the snapshot
        path, which is faithful for those shapes because they change no text.
        """
        used_ids = set(getattr(edit, "_used_revision_ids", None) or [])
        if not used_ids:
            return None

        spans = self.mapper.spans
        matched_indices = [
            i
            for i, s in enumerate(spans)
            if s.run is not None and ((s.ins_id and s.ins_id in used_ids) or (s.del_id and s.del_id in used_ids))
        ]
        if not matched_indices:
            return None

        def _absorbable(span) -> bool:
            if span.run is not None:
                return False
            if span.start == span.end:
                return True  # zero-width anchors
            return span.text in self._PREVIEW_MARKUP_TOKENS or span.text.startswith("{>>")

        ranges: List[List[int]] = []
        for i in matched_indices:
            lo, hi = i, i
            while lo - 1 >= 0 and _absorbable(spans[lo - 1]):
                lo -= 1
            while hi + 1 < len(spans) and _absorbable(spans[hi + 1]):
                hi += 1
            ranges.append([spans[lo].start, spans[hi].end])

        # Merge nearby ranges into one window so e.g. the three occurrences of
        # a fan-out over "apple apple apple." render as a single window.
        ranges.sort()
        merged: List[List[int]] = []
        for st, en in ranges:
            if merged and st - merged[-1][1] <= 2 * PREVIEW_CONTEXT_CHARS:
                merged[-1][1] = max(merged[-1][1], en)
            else:
                merged.append([st, en])

        full_text = self.mapper.full_text
        windows = []
        for st, en in merged[: self._PREVIEW_MAX_WINDOWS]:
            ws = max(0, st - PREVIEW_CONTEXT_CHARS)
            we = min(len(full_text), en + PREVIEW_CONTEXT_CHARS)
            window = full_text[ws:we]
            # Drop meta bubbles and any wrapper fragments the window edges
            # chopped in half (same tidy the snapshot path uses).
            window = self._tidy_preview_context(self._tidy_preview_context(window, "before"), "after")
            windows.append(truncate_middle(window, REPORT_ECHO_CAP))
        critic_markup = "\n…\n".join(windows)
        if len(merged) > self._PREVIEW_MAX_WINDOWS:
            critic_markup += f"\n…\n({len(merged) - self._PREVIEW_MAX_WINDOWS} more modified regions not shown)"

        clean_text = critic_markup
        clean_text = re.sub(r"\{>>.*?<<\}", "", clean_text, flags=re.DOTALL)
        clean_text = re.sub(r"\{--.*?--\}", "", clean_text, flags=re.DOTALL)
        clean_text = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", clean_text, flags=re.DOTALL)
        clean_text = re.sub(r"\{==(.*?)==\}", r"\1", clean_text, flags=re.DOTALL)
        return critic_markup, clean_text

    def _build_edit_context_previews(self, edit: Any) -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(edit, ModifyText):
            return None, None
        # Preferred path: slice the actual post-apply projections (F6).
        post_apply = self._build_post_apply_previews(edit)
        if post_apply is not None:
            return post_apply
        if edit._preview_span is not None and edit._preview_context is not None:
            return self._build_full_match_preview(edit)
        if hasattr(edit, "_resolved_proxy_edit") and edit._resolved_proxy_edit is not None:
            edit = edit._resolved_proxy_edit
        start_idx = edit._resolved_start_idx
        if start_idx is None:
            return None, None
        target_text = edit.target_text or ""
        new_text = edit.new_text or ""

        context = getattr(edit, "_preview_context", None)
        if context is not None:
            context_before, context_after = context
        else:
            # Fallback for callers that never went through apply_edits. Only
            # safe while the mapper still reflects the pre-apply document.
            length = len(target_text)
            active_mapper = edit._active_mapper_ref or self.mapper
            full_text = active_mapper.full_text
            if not full_text:
                return None, None
            context_before = self._tidy_preview_context(
                full_text[max(0, start_idx - PREVIEW_CONTEXT_CHARS) : start_idx], "before"
            )
            context_after = self._tidy_preview_context(
                full_text[start_idx + length : start_idx + length + PREVIEW_CONTEXT_CHARS], "after"
            )

        # F4/F5: when the resolved edit is a whole-paragraph heading replacement
        # (PARAGRAPH_REPLACE), target_text/new_text still carry the markdown '#'
        # heading prefix from the projection. That prefix is not literal document
        # text, so surfacing it inside {--...--}/{++...++} markup misrepresents
        # the change as touching '#' characters. Strip a leading run of '#' (plus
        # the following whitespace) from both sides of the rendered preview.
        display_target = target_text
        display_new = new_text
        if getattr(edit, "_internal_op", None) == EditOperationType.PARAGRAPH_REPLACE:
            display_target = re.sub(r"^#+\s*", "", target_text)
            display_new = re.sub(r"^#+\s*", "", new_text)
        # Bound the echoed edit values: previews flow into LLM context windows
        # and must not multiply an oversized new_text/target_text (QA C2).
        display_target = truncate_middle(display_target, PREVIEW_TEXT_CAP)
        display_new = truncate_middle(display_new, PREVIEW_TEXT_CAP)
        insertion = f"{{++{display_new}++}}" if display_new else ""
        critic_markup = f"{context_before}{{--{display_target}--}}{insertion}{context_after}"

        clean_text = critic_markup
        clean_text = re.sub(r"\{>>.*?<<\}", "", clean_text, flags=re.DOTALL)
        clean_text = re.sub(r"\{--.*?--\}", "", clean_text, flags=re.DOTALL)
        clean_text = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", clean_text, flags=re.DOTALL)

        return critic_markup, clean_text

    def _first_live_match(self, target_text: str, is_regex: bool = False) -> Tuple[int, int]:
        all_matches = self.mapper.find_all_match_indices(target_text, is_regex=is_regex)
        if len(all_matches) <= 1:
            return self.mapper.find_match_index(target_text, is_regex=is_regex)
        for start, length in all_matches:
            real_spans = [
                s for s in self.mapper.spans if s.run is not None and s.end > start and s.start < start + length
            ]
            if not real_spans:
                # Virtual-only range (meta bubble, marker): not document text
                # (ADEU-QA-002 C) — never a live match.
                continue
            if any(not s.del_id for s in real_spans):
                return start, length
        return self.mapper.find_match_index(target_text, is_regex=is_regex)

    _PAIR_WALK_SKIP_TAGS = (
        "w:commentRangeStart",
        "w:commentRangeEnd",
        "w:commentReference",
        "w:rPr",
        "w:pPr",
    )

    @staticmethod
    def _paragraph_mark_revision(p_el):
        """
        The pending <w:ins>/<w:del> revision mark on this paragraph's own
        paragraph mark (pPr/rPr), or None. A pending mark means the paragraph
        BOUNDARY itself is part of an unresolved revision, so revision
        elements on either side of it are contiguous in one of the two
        document states (original or accepted).
        """
        if p_el is None or p_el.tag != qn("w:p"):
            return None
        pPr = p_el.find(qn("w:pPr"))
        rPr = pPr.find(qn("w:rPr")) if pPr is not None else None
        if rPr is None:
            return None
        for tag in ("w:ins", "w:del"):
            mark = rPr.find(qn(tag))
            if mark is not None:
                return mark
        return None

    def _get_paired_nodes(self, node):
        """
        Finds all w:ins/w:del nodes that form a single logical Modification
        block with `node`: contiguous same-author siblings, extended ACROSS
        paragraph boundaries whose own paragraph mark is a pending same-author
        revision (F1, QA 2026-07-23). A multi-paragraph replacement stores its
        deletion in the source paragraph and spreads its insertion (one shared
        id, including tracked paragraph marks) over following paragraphs — the
        pending marks make those elements one contiguous revision even though
        they are not XML siblings. Ordinary paragraph boundaries (no tracked
        mark) never group, so contiguous pairing behavior is otherwise
        unchanged.
        """
        pairs = set()
        author = node.get(qn("w:author"))
        skip_tags = tuple(qn(t) for t in self._PAIR_WALK_SKIP_TAGS)

        def _paragraph_of(el):
            cur = el
            while cur is not None and cur.tag != qn("w:p"):
                cur = cur.getparent()
            return cur

        def _sibling_paragraph(p_el, forward: bool):
            sib = p_el.getnext() if forward else p_el.getprevious()
            while sib is not None and sib.tag != qn("w:p"):
                sib = sib.getnext() if forward else sib.getprevious()
            return sib

        def _crossable_mark(p_el):
            """The boundary's pending revision mark when it belongs to the
            same author, else None."""
            mark = self._paragraph_mark_revision(p_el)
            if mark is not None and mark.get(qn("w:author")) == author:
                return mark
            return None

        # Look forward
        current_p = _paragraph_of(node)
        nxt = node.getnext()
        while True:
            if nxt is None:
                # End of paragraph: cross into the next paragraph only when
                # the boundary (this paragraph's own mark) is a pending
                # same-author revision.
                mark = _crossable_mark(current_p) if current_p is not None else None
                next_p = _sibling_paragraph(current_p, forward=True) if mark is not None else None
                if next_p is None:
                    break
                pairs.add(mark)
                current_p = next_p
                nxt = next_p[0] if len(next_p) else None
                continue
            if nxt.tag in skip_tags:
                nxt = nxt.getnext()
                continue
            if nxt.tag in (qn("w:ins"), qn("w:del")) and nxt.get(qn("w:author")) == author:
                pairs.add(nxt)
                nxt = nxt.getnext()
                continue
            break

        # Look backward
        current_p = _paragraph_of(node)
        prev = node.getprevious()
        while True:
            if prev is None:
                # Start of paragraph: cross into the previous paragraph only
                # when the boundary (the PREVIOUS paragraph's own mark) is a
                # pending same-author revision.
                prev_p = _sibling_paragraph(current_p, forward=False) if current_p is not None else None
                mark = _crossable_mark(prev_p) if prev_p is not None else None
                if mark is None or prev_p is None:
                    break
                pairs.add(mark)
                current_p = prev_p
                prev = prev_p[-1] if len(prev_p) else None
                continue
            if prev.tag in skip_tags:
                prev = prev.getprevious()
                continue
            if prev.tag in (qn("w:ins"), qn("w:del")) and prev.get(qn("w:author")) == author:
                pairs.add(prev)
                prev = prev.getprevious()
                continue
            break

        return list(pairs)

    def _scan_existing_ids(self) -> int:
        """
        Scans the document body for existing w:id attributes in w:ins and w:del
        to ensure new IDs do not collide.
        """
        max_id = 0
        for tag in ["w:ins", "w:del"]:
            elements = self.doc.element.xpath(f"//{tag}")
            for el in elements:
                try:
                    val = int(el.get(qn("w:id")))
                    if val > max_id:
                        max_id = val
                except (ValueError, TypeError):
                    pass
        return max_id

    def _get_next_id(self):
        self.current_id += 1
        return str(self.current_id)

    def _create_track_change_tag(self, tag_name: str, author: str = "", reuse_id: Optional[str] = None):
        tag = create_element(tag_name)
        wid = reuse_id if reuse_id is not None else self._get_next_id()
        create_attribute(tag, "w:id", wid)
        create_attribute(tag, "w:author", author or self.author)
        create_attribute(tag, "w:date", self.timestamp)
        create_attribute(tag, "w16du:dateUtc", self.timestamp)
        return tag

    def _set_text_content(self, element, text: str):
        element.text = text
        if text.strip() != text:
            create_attribute(element, "xml:space", "preserve")

    def _parse_markdown_style(self, text: str) -> tuple[str, str | None]:
        """
        Detects if text starts with markdown header (e.g. '## Title') or list markers (e.g. '* ', '1. ').
        Returns (clean_text, style_name).
        """
        stripped_text = text.lstrip()

        # Headers
        if stripped_text.startswith("#"):
            level = 0
            while stripped_text.startswith("#"):
                level += 1
                stripped_text = stripped_text[1:]

            if stripped_text.startswith(" "):
                return stripped_text.strip(), f"Heading {level}"

        # Bullet Lists
        if stripped_text.startswith("* ") or stripped_text.startswith("- "):
            return stripped_text[2:].strip(), "List Paragraph"

        # Numbered lists: the projection emits ordered items with a CONSTANT
        # "1. " marker (Markdown renumbers), so only that exact shape converts
        # back into a list style. Any other leading number ("2024. Year in
        # review", "3. Clause text") is literal document text. Continuation
        # items inside an existing list anchor keep full "\d+." handling via
        # the list-anchored insertion path.
        match = re.match(r"^1\.\s+", stripped_text)
        if match:
            return stripped_text[match.end() :].strip(), "List Number"

        return text, None

    def _edit_declares_emphasis(self, edit: "ModifyText") -> bool:
        """
        True when this edit's target or replacement text carries explicit
        bold/italic markers, making the markers AUTHORITATIVE for the inserted
        runs' formatting. Replacing `**X**` with `_X_` must yield italic-only
        text, and replacing `**X**` with `X` must yield plain text — inheriting
        the replaced span's run properties on top of (or instead of) the
        requested markers silently produces the wrong document while the
        report claims success (QA 2026-07-19 F-02). Plain-text edits (no
        markers on either side) keep inheriting the context style so partial
        replacements inside a styled span never lose formatting.

        Detection reuses _parse_inline_markdown so suppression triggers exactly
        when the style parser will emit marker-derived formatting.
        """
        for text in (edit.target_text, edit.new_text):
            if not text or ("**" not in text and "_" not in text):
                continue
            if any(props for _seg, props in self._parse_inline_markdown(text)):
                return True
        return False

    def _parse_inline_markdown(
        self, text: str, base_style: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Recursively parses bold (**) and italic (_) markdown.
        """
        if base_style is None:
            base_style = {}

        if not text:
            return []

        token_pattern = re.compile(r"(\*\*.*?\*\*)|(_.*?_)")

        match = token_pattern.search(text)

        if not match:
            return [(text, base_style)]

        start, end = match.span()

        if match.group(1):
            tag_type = "bold"
            inner_raw = match.group(1)
        else:
            tag_type = "italic"
            inner_raw = match.group(2)

        pre_text = text[:start]
        post_text = text[end:]

        results = []

        if pre_text:
            results.append((pre_text, base_style))

        new_style = base_style.copy()
        if tag_type == "bold":
            inner_content = inner_raw[2:-2]
            new_style["bold"] = True
        else:
            inner_content = inner_raw[1:-1]
            new_style["italic"] = True

        results.extend(self._parse_inline_markdown(inner_content, new_style))
        results.extend(self._parse_inline_markdown(post_text, base_style))

        return results

    def _is_native_heading_paragraph(self, paragraph) -> bool:
        """
        Does `paragraph` render with "#" markers in the text projection?

        Style NAMES are not enough: real templates declare their heading-ness
        as <w:outlineLvl> inside styles.xml under a house name ("LegalNum2L1"),
        which Word honours and a startswith("Heading") test does not.
        is_native_heading resolves the style chain, and is the very function
        the mapper projects with, so the scrub below cannot drift away from
        what the agent reads.
        """
        try:
            from adeu.utils.docx import _get_style_cache, is_native_heading

            part = getattr(paragraph, "part", None) or getattr(self.doc, "part", None)
            style_cache, default_pstyle = _get_style_cache(part)
            return is_native_heading(paragraph, style_cache, default_pstyle)
        except Exception:
            return False

    def _clone_pPr_scrubbing_headings(self, source_paragraph):
        """
        Deep-copies `source_paragraph`'s <w:pPr> for a paragraph that CONTINUES
        it, dropping the heading-ness: an inserted body paragraph must not
        inherit the heading style (nor, with it, the section's automatic
        numbering) of the heading it follows.
        """
        pPr_clone = deepcopy(source_paragraph.pPr)
        pStyle_el = pPr_clone.find(qn("w:pStyle"))
        if pStyle_el is not None:
            style_val = pStyle_el.get(qn("w:val"))
            is_heading = style_val and (
                style_val.startswith("Heading")
                or style_val == "Title"
                or style_val.replace(" ", "").startswith("Heading")
                or self._is_native_heading_paragraph(source_paragraph)
            )
            if is_heading:
                pPr_clone.remove(pStyle_el)
        outlineLvl_el = pPr_clone.find(qn("w:outlineLvl"))
        if outlineLvl_el is not None:
            pPr_clone.remove(outlineLvl_el)
        return pPr_clone

    def track_insert(
        self,
        text: str,
        anchor_run: Optional[Run] = None,
        anchor_paragraph: Optional[Paragraph] = None,
        comment: Optional[str] = None,
        suppress_inherited: bool = False,
        insert_before: bool = False,
        reuse_id: Optional[str] = None,
        positional_anchor_run: Optional[Run] = None,
    ) -> Tuple[Optional[Any], Optional[Any]]:
        """
        Inserts text. If text contains newlines, splits into multiple paragraphs.

        If `reuse_id` is provided, every <w:ins> element minted by this call
        (the inline insert, per-paragraph block inserts, and paragraph-break
        tracking markers in pPr/rPr) shares that w:id. This collapses multi-
        paragraph and multi-run insertions into a single logical revision
        from the agent's point of view.

        `anchor_run` supplies run STYLING and may be the run after the
        insertion point (_determine_style_source). `positional_anchor_run`
        names the run the insertion physically follows; suffix relocation for
        paragraph-splitting insertions keys on it (falling back to
        anchor_run when the two coincide).
        """
        lines = re.split(r"[\r\n]+", text)
        if not lines:
            return None, None

        # Resolve the current paragraph robustly
        current_p = None
        if anchor_paragraph is not None:
            current_p = anchor_paragraph._element
        elif anchor_run is not None:
            current_p = anchor_run._element.getparent()
            while current_p is not None and current_p.tag != qn("w:p"):
                current_p = current_p.getparent()

            if current_p is None and hasattr(anchor_run, "_parent"):
                p_obj = anchor_run._parent
                if hasattr(p_obj, "_element") and p_obj._element.tag == qn("w:p"):
                    current_p = p_obj._element

        # 0. Check if FIRST line implies a block element (Header)
        first_clean, first_style = self._parse_markdown_style(lines[0])

        # Block conversion additionally requires a REAL line break in the source
        # text. Without one the text is by construction a fragment spliced into
        # an existing paragraph, and a leading "- "/"* "/"# " is literal
        # content, not a block marker. Word-diffing makes this routine: modify
        # "Product" -> "Product - Draft" trims the common prefix and hands us
        # the fragment " - Draft", which _parse_markdown_style reads as a
        # bullet. Left ungated, that silently split the paragraph, minted a
        # numbered ListParagraph, ate the "- " as a fabricated marker, and still
        # reported status "applied". Note the em-dash spelling was never
        # affected, so the corruption tracked the punctuation an author chose.
        has_line_break = re.search(r"[\r\n]", text) is not None

        if first_style and has_line_break:
            if current_p is None:
                return None, None

            body = current_p.getparent()
            if body is None:
                return None, None

            try:
                p_index = body.index(current_p)
            except ValueError:
                return None, None

            created_nodes = []

            for i, line_text in enumerate(lines):
                c_text, s_name = self._parse_markdown_style(line_text)
                if not c_text and not s_name:
                    continue

                new_p = create_element("w:p")
                if s_name:
                    self._set_paragraph_style(new_p, s_name)
                elif current_p.pPr is not None:
                    pPr_clone = self._clone_pPr_scrubbing_headings(current_p)
                    new_p.append(pPr_clone)

                # Track the paragraph break itself as an insertion
                pPr = new_p.find(qn("w:pPr"))
                if pPr is None:
                    pPr = create_element("w:pPr")
                    new_p.insert(0, pPr)
                rPr = pPr.find(qn("w:rPr"))
                if rPr is None:
                    rPr = create_element("w:rPr")
                    pPr.append(rPr)
                ins_mark = self._create_track_change_tag("w:ins", reuse_id=reuse_id)
                rPr.append(ins_mark)

                new_ins = self._create_track_change_tag("w:ins", reuse_id=reuse_id)

                segments = self._parse_inline_markdown(c_text)

                for seg_text, seg_props in segments:
                    new_run = create_element("w:r")
                    if anchor_run and anchor_run._element.rPr is not None:
                        new_run.append(deepcopy(anchor_run._element.rPr))

                    self._apply_run_props(new_run, seg_props, suppress_inherited=suppress_inherited)

                    t = create_element("w:t")
                    self._set_text_content(t, seg_text)
                    new_run.append(t)
                    new_ins.append(new_run)

                new_p.append(new_ins)
                # Bug 1 fix: if the caller explicitly requested insert_before
                # (because the anchor is at the start of the paragraph), the
                # new heading-styled paragraphs go BEFORE the anchor.
                if insert_before:
                    body.insert(p_index + i, new_p)
                else:
                    body.insert(p_index + 1 + i, new_p)
                created_nodes.append((new_p, new_ins))

            if comment and created_nodes:
                start_p, start_ins = created_nodes[0]
                end_p, end_ins = created_nodes[-1]
                if start_p == end_p:
                    self._attach_comment(start_p, start_ins, start_ins, comment)
                else:
                    self._attach_comment_spanning(start_p, start_ins, end_p, end_ins, comment)

            return None, (created_nodes[-1][0] if created_nodes else None)

        # 1. Inline Logic
        first_line = lines[0]

        # BUG-23-3b: text that ENDS with a paragraph break inserted before an
        # anchor (e.g. final_new='Summary\n\n' inserted at the start of the
        # 'Conclusion' paragraph because 'Conclusion' was kept as the common
        # suffix) must become its OWN new paragraph ahead of the anchor, with a
        # tracked paragraph break between them. The default inline path can't
        # create that break when the anchor run is absent (anchor resolves to
        # the paragraph, not a run), so handle it explicitly here.
        if insert_before and current_p is not None and len(lines) >= 2 and lines[-1] == "":
            body = current_p.getparent()
            p_index = None
            if body is not None:
                try:
                    p_index = body.index(current_p)
                except ValueError:
                    p_index = None
            if body is not None and p_index is not None:
                content_lines = [ln for ln in lines if ln != ""]
                created_nodes = []
                for offset, line_text in enumerate(content_lines):
                    new_p = create_element("w:p")
                    if current_p.pPr is not None:
                        new_p.append(deepcopy(current_p.pPr))
                    pPr = new_p.find(qn("w:pPr"))
                    if pPr is None:
                        pPr = create_element("w:pPr")
                        new_p.insert(0, pPr)
                    rPr = pPr.find(qn("w:rPr"))
                    if rPr is None:
                        rPr = create_element("w:rPr")
                        pPr.append(rPr)
                    rPr.append(self._create_track_change_tag("w:ins", reuse_id=reuse_id))

                    new_ins = self._create_track_change_tag("w:ins", reuse_id=reuse_id)
                    for seg_text, seg_props in self._parse_inline_markdown(line_text):
                        new_run = create_element("w:r")
                        if anchor_run and anchor_run._element.rPr is not None:
                            new_run.append(deepcopy(anchor_run._element.rPr))
                        self._apply_run_props(new_run, seg_props, suppress_inherited=suppress_inherited)
                        t = create_element("w:t")
                        self._set_text_content(t, seg_text)
                        new_run.append(t)
                        new_ins.append(new_run)
                    new_p.append(new_ins)
                    body.insert(p_index + offset, new_p)
                    created_nodes.append((new_p, new_ins))

                if comment and created_nodes:
                    start_p, start_ins = created_nodes[0]
                    end_p, end_ins = created_nodes[-1]
                    if start_p == end_p:
                        self._attach_comment(start_p, start_ins, start_ins, comment)
                    else:
                        self._attach_comment_spanning(start_p, start_ins, end_p, end_ins, comment)

                if created_nodes:
                    return None, created_nodes[-1][0]

        ins_elem = self._track_insert_inline(
            first_line,
            anchor_run,
            suppress_inherited=suppress_inherited,
            reuse_id=reuse_id,
        )

        remaining_lines = lines[1:]

        # Bug 1B: We need to know whether there are stranded suffix runs in
        # current_p (runs after our anchor) BEFORE deciding the trailing-pop
        # policy. If there are, and new_text ends with a paragraph break,
        # we keep the trailing empty line so the loop creates a fresh
        # destination paragraph for the suffix to land in.
        positional_anchor = None
        suffix_nodes: list = []
        suffix_includes_anchor = False
        if current_p is not None:
            # ins_elem is attached by the CALLER after this method returns, so
            # it usually has no parent yet and cannot locate the insertion
            # point. The positional anchor run IS attached, and the insertion
            # lands immediately after it — its following siblings are the
            # suffix that relocates into the last new paragraph when the text
            # carries paragraph breaks.
            pos_run = positional_anchor_run or anchor_run
            if ins_elem is not None and ins_elem.getparent() is not None:
                positional_anchor = ins_elem
            elif pos_run is not None and pos_run._element.getparent() is not None:
                positional_anchor = pos_run._element
                # insert_before: the insertion will be attached BEFORE this
                # run, so the run itself belongs to the relocating suffix.
                # Without this, a paragraph-splitting insertion at paragraph
                # START leaves the host text glued to the FIRST inserted line
                # ("00." + insert "0.\n\n0 " read "0.00.\n\n0 " instead of
                # "0.\n\n0 00.") — hunt-profile counterexample, 2026-07-19.
                suffix_includes_anchor = insert_before
            while positional_anchor is not None and positional_anchor.getparent() is not current_p:
                positional_anchor = positional_anchor.getparent()
                if positional_anchor is current_p:
                    positional_anchor = None
                    break

            relocatable_tags = {qn("w:r"), qn("w:ins"), qn("w:del")}
            if positional_anchor is not None:
                nxt = positional_anchor if suffix_includes_anchor else positional_anchor.getnext()
                while nxt is not None:
                    if nxt.tag in relocatable_tags:
                        suffix_nodes.append(nxt)
                    nxt = nxt.getnext()
            elif insert_before:
                # No attached anchor run at all (paragraph-anchored insertion
                # at paragraph START): everything in the host paragraph
                # follows the insertion point, so it all relocates.
                suffix_nodes.extend(child for child in current_p if child.tag in relocatable_tags)

        # Decide whether to keep the trailing empty in remaining_lines.
        # Keep it when both conditions hold: the new_text ends with a
        # paragraph break (signalled by the trailing empty) AND we have
        # suffix runs to relocate. Otherwise the trailing empty is just
        # noise from a "...\n\n" terminator with no continuation.
        if remaining_lines and remaining_lines[-1] == "":
            if not suffix_nodes:
                remaining_lines.pop()

        last_p = None
        if remaining_lines:
            if current_p is None:
                return ins_elem, None

            parent_body = current_p.getparent()
            if parent_body is None:
                return ins_elem

            try:
                p_index = parent_body.index(current_p)
            except ValueError:
                return ins_elem

            has_num_pr = False
            if current_p.pPr is not None and current_p.pPr.find(qn("w:numPr")) is not None:
                has_num_pr = True

            for i, line_text in enumerate(remaining_lines):
                list_level = None
                if has_num_pr:
                    match = re.match(r"^([ \t]*)(?:\*|-|\d+\.)\s+", line_text)
                    if match:
                        prefix = match.group(0)
                        indent = match.group(1)
                        spaces = len(indent.replace("\t", "    "))
                        list_level = spaces // 4
                        line_text = line_text[len(prefix) :]

                clean_text, style_name = self._parse_markdown_style(line_text)
                new_p = create_element("w:p")
                if style_name:
                    self._set_paragraph_style(new_p, style_name)
                elif current_p.pPr is not None:
                    pPr_clone = self._clone_pPr_scrubbing_headings(current_p)
                    if list_level is not None:
                        numPr = pPr_clone.find(qn("w:numPr"))
                        if numPr is not None:
                            ilvl_el = numPr.find(qn("w:ilvl"))
                            if ilvl_el is not None:
                                ilvl_el.set(qn("w:val"), str(list_level))
                            else:
                                ilvl_el = create_element("w:ilvl")
                                ilvl_el.set(qn("w:val"), str(list_level))
                                numPr.append(ilvl_el)
                    new_p.append(pPr_clone)

                # Track the paragraph break itself as an insertion
                pPr = new_p.find(qn("w:pPr"))
                if pPr is None:
                    pPr = create_element("w:pPr")
                    new_p.insert(0, pPr)
                rPr = pPr.find(qn("w:rPr"))
                if rPr is None:
                    rPr = create_element("w:rPr")
                    pPr.append(rPr)
                ins_mark = self._create_track_change_tag("w:ins", reuse_id=reuse_id)
                rPr.append(ins_mark)

                new_ins = self._create_track_change_tag("w:ins", reuse_id=reuse_id)

                segments = self._parse_inline_markdown(clean_text)
                for seg_text, seg_props in segments:
                    new_run = create_element("w:r")
                    if anchor_run and anchor_run._element.rPr is not None:
                        new_run.append(deepcopy(anchor_run._element.rPr))

                    self._apply_run_props(new_run, seg_props, suppress_inherited=suppress_inherited)

                    t = create_element("w:t")
                    self._set_text_content(t, seg_text)
                    new_run.append(t)
                    new_ins.append(new_run)

                new_p.append(new_ins)
                parent_body.insert(p_index + 1 + i, new_p)
                last_p = new_p

            # Now relocate the suffix nodes (already gathered above) into
            # last_p. The destination is correct whether last_p is a
            # content-bearing line ("...New\n\n" + suffix → suffix joins
            # the empty trailing paragraph) or a normal line ("...New" +
            # suffix → suffix appends to the last content line).
            if last_p is not None and suffix_nodes:
                for node in suffix_nodes:
                    current_p.remove(node)
                    last_p.append(node)

        return ins_elem, last_p

    def _apply_paragraph_replace(self, edit: ModifyText) -> bool:
        """
        Implements PARAGRAPH_REPLACE: deletes an entire source paragraph
        (content + paragraph-break marker) and inserts a fresh styled
        paragraph after it. After accept_all_revisions, only the new
        paragraph remains.
        """
        target_para = getattr(edit, "_target_paragraph", None)
        if target_para is None:
            return False
        p_el = target_para._element

        # Mint shared revision IDs so the agent sees this as one logical
        # change (mirrors the reuse_id pattern used by track_insert). A
        # reserved id (F20 ascending pre-assignment) takes precedence.
        shared_id = edit._reserved_del_id or edit._reserved_ins_id or self._get_next_id()
        del_id = shared_id
        ins_id = shared_id

        # 1. Track-delete every content run in the source paragraph.
        runs_to_delete = []
        for child in list(p_el):
            if child.tag == qn("w:r"):
                runs_to_delete.append(Run(child, target_para))
            elif child.tag == qn("w:ins"):
                # Already-inserted content inside this paragraph: take
                # its child runs verbatim (we'll delete them too).
                for grand in list(child):
                    if grand.tag == qn("w:r"):
                        runs_to_delete.append(Run(grand, target_para))

        first_del_element = None
        for r in runs_to_delete:
            del_elem = self.track_delete_run(r, reuse_id=del_id)
            if first_del_element is None:
                first_del_element = del_elem

        # 2. Track-delete the paragraph break itself by stamping
        # pPr/rPr/<w:del>. accept_all_revisions removes any <w:p>
        # carrying this marker.
        pPr = p_el.find(qn("w:pPr"))
        if pPr is None:
            pPr = create_element("w:pPr")
            p_el.insert(0, pPr)
        rPr = pPr.find(qn("w:rPr"))
        if rPr is None:
            rPr = create_element("w:rPr")
            pPr.append(rPr)
        # Avoid stacking duplicate markers.
        if rPr.find(qn("w:del")) is None:
            del_break = self._create_track_change_tag("w:del", reuse_id=del_id)
            rPr.append(del_break)

        # 3. Build the new paragraph and insert it after the original.
        new_text = edit.new_text or ""
        new_clean, new_style_name = self._parse_markdown_style(new_text)

        body = p_el.getparent()
        if body is None:
            return False
        try:
            p_index = body.index(p_el)
        except ValueError:
            return False

        new_p = create_element("w:p")
        if new_style_name:
            self._set_paragraph_style(new_p, new_style_name)
        else:
            # Carry over the original paragraph's style if no marker was
            # given (rare but possible if new_text is plain text replacing
            # a heading).
            if pPr is not None:
                new_p.append(deepcopy(pPr))
                # Strip any tracked-change markers we just stamped.
                new_pPr = new_p.find(qn("w:pPr"))
                new_rPr = new_pPr.find(qn("w:rPr")) if new_pPr is not None else None
                if new_rPr is not None:
                    for d in new_rPr.findall(qn("w:del")):
                        new_rPr.remove(d)

        # Mark the new paragraph break itself as tracked-inserted so the
        # paragraph as a structural unit is part of the revision.
        new_pPr = new_p.find(qn("w:pPr"))
        if new_pPr is None:
            new_pPr = create_element("w:pPr")
            new_p.insert(0, new_pPr)
        new_rPr = new_pPr.find(qn("w:rPr"))
        if new_rPr is None:
            new_rPr = create_element("w:rPr")
            new_pPr.append(new_rPr)
        new_rPr.append(self._create_track_change_tag("w:ins", reuse_id=ins_id))

        # Inline content goes inside a single <w:ins>.
        new_ins = self._create_track_change_tag("w:ins", reuse_id=ins_id)
        for seg_text, seg_props in self._parse_inline_markdown(new_clean):
            new_run = create_element("w:r")
            self._apply_run_props(new_run, seg_props, suppress_inherited=False)
            t = create_element("w:t")
            self._set_text_content(t, seg_text)
            new_run.append(t)
            new_ins.append(new_run)
        new_p.append(new_ins)

        body.insert(p_index + 1, new_p)

        # 4. Attach the comment if any, spanning the source paragraph's
        # first deletion through the new paragraph's insertion.
        if edit.comment:
            if first_del_element is not None:
                self._attach_comment_spanning(p_el, first_del_element, new_p, new_ins, edit.comment)
            else:
                # Source paragraph was empty (no content runs). Anchor on
                # the new paragraph alone.
                self._attach_comment(new_p, new_ins, new_ins, edit.comment)

        self._record_used_revision_ids(edit, shared_id)
        return True

    def _apply_run_props(self, run_element, props: Dict[str, Any], suppress_inherited: bool = False) -> None:
        """
        Applies Bold/Italic properties to a run.
        Uses python-docx native Run object to ensure XML schema ordering is correct.
        """
        if not props:
            if not suppress_inherited:
                return
            props = {}

        # Wrap the OxmlElement in a Run to let python-docx handle exact schema ordering
        run_obj = Run(run_element, None)  # type: ignore

        # Handle Bold
        if props.get("bold"):
            run_obj.bold = True
            rPr = run_element.find(qn("w:rPr"))
            if rPr is not None:
                b_elem = rPr.find(qn("w:b"))
                if b_elem is not None:
                    b_elem.set(qn("w:val"), "1")
        elif suppress_inherited:
            rPr = run_element.find(qn("w:rPr"))
            if rPr is not None:
                for b in rPr.findall(qn("w:b")):
                    rPr.remove(b)
                # Remove Complex Script bold (Bug #12)
                for bCs in rPr.findall(qn("w:bCs")):
                    rPr.remove(bCs)

        # Handle Italic
        if props.get("italic"):
            run_obj.italic = True
            rPr = run_element.find(qn("w:rPr"))
            if rPr is not None:
                i_elem = rPr.find(qn("w:i"))
                if i_elem is not None:
                    i_elem.set(qn("w:val"), "1")
        elif suppress_inherited:
            rPr = run_element.find(qn("w:rPr"))
            if rPr is not None:
                for i in rPr.findall(qn("w:i")):
                    rPr.remove(i)
                # Remove Complex Script italic (Bug #12)
                for iCs in rPr.findall(qn("w:iCs")):
                    rPr.remove(iCs)

    def _set_paragraph_style(self, p_element, style_name: str):
        existing_pPr = p_element.find(qn("w:pPr"))
        if existing_pPr is not None:
            p_element.remove(existing_pPr)
        pPr = create_element("w:pPr")
        pStyle = create_element("w:pStyle")

        try:
            style_id = self.doc.styles[style_name].style_id
        except (KeyError, ValueError):
            style_id = style_name.replace(" ", "")

        create_attribute(pStyle, "w:val", style_id)
        pPr.append(pStyle)

        # F5 (QA 2026-07-23): a "- "/"* " markdown bullet resolves to the
        # "List Paragraph" style, but the style alone renders as indented text
        # with NO bullet (half-applied) — a real bullet needs w:numPr pointing
        # at a bullet numbering definition, which is also what makes the clean
        # re-read project "* item" again (get_paragraph_prefix resolves numPr).
        if style_name == "List Paragraph":
            bullet_num_id = self._ensure_bullet_num_id()
            if bullet_num_id is not None:
                numPr = create_element("w:numPr")
                ilvl = create_element("w:ilvl")
                create_attribute(ilvl, "w:val", "0")
                numPr.append(ilvl)
                numId = create_element("w:numId")
                create_attribute(numId, "w:val", bullet_num_id)
                numPr.append(numId)
                pPr.append(numPr)

        p_element.insert(0, pPr)

    # Minimal single-level bullet definition, injected when the document has
    # no numbering part (or none of its definitions is a bullet). The private
    # use glyph U+F0B7 with the Symbol font is Word's canonical round bullet.
    _BULLET_LVL_XML = (
        '<w:lvl {ns} w:ilvl="0">'
        '<w:start w:val="1"/>'
        '<w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val=""/>'
        '<w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>'
        '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>'
        "</w:lvl>"
    )

    def _ensure_bullet_num_id(self) -> Optional[str]:
        """
        Returns the w:numId of a bullet numbering definition, creating one if
        needed (F5, QA 2026-07-23). Resolution order:

          1. Reuse: the first w:num in word/numbering.xml whose abstractNum
             has numFmt="bullet" at ilvl 0.
          2. Extend: append a minimal single-level bullet abstractNum + num to
             the existing numbering part.
          3. Create: mint word/numbering.xml (content-type override and the
             document-part relationship are handled by python-docx's package
             writer once the part is registered), following the comments-part
             creation pattern (redline/comments.py).

        The resolved id is cached per engine instance.
        """
        if getattr(self, "_bullet_num_id", None):
            return self._bullet_num_id

        from docx.oxml.ns import nsdecls

        package = self.doc.part.package
        numbering_part = None
        for p in package.parts:
            if str(p.partname).endswith("/numbering.xml"):
                numbering_part = p
                break

        ns = nsdecls("w")

        if numbering_part is None:
            # 3. Create the numbering part with one bullet definition.
            from docx.opc.constants import CONTENT_TYPE as CT
            from docx.opc.constants import RELATIONSHIP_TYPE as RT
            from docx.opc.packuri import PackURI
            from docx.opc.part import XmlPart

            xml_bytes = (
                f"<w:numbering {ns}>"
                f'<w:abstractNum w:abstractNumId="0">'
                f'<w:multiLevelType w:val="singleLevel"/>'
                f"{self._BULLET_LVL_XML.format(ns='')}"
                f"</w:abstractNum>"
                f'<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
                f"</w:numbering>"
            ).encode("utf-8")
            logger.info("Creating new numbering part for markdown bullet")
            new_part = XmlPart(PackURI("/word/numbering.xml"), CT.WML_NUMBERING, parse_xml(xml_bytes), package)
            package.parts.append(new_part)
            self.doc.part.relate_to(new_part, RT.NUMBERING)
            if hasattr(package, "_adeu_numbering_cache"):
                del package._adeu_numbering_cache
            self._bullet_num_id = "1"
            return self._bullet_num_id

        # Bind an editable root (Proxy Class OPC Binding, AI_CONTEXT §8).
        if not hasattr(numbering_part, "_adeu_element"):
            if hasattr(numbering_part, "_element"):
                numbering_part._adeu_element = numbering_part._element
            else:
                numbering_part._adeu_element = parse_xml(numbering_part.blob)
        root = numbering_part._adeu_element

        # 1. Reuse the first numId whose abstract definition is a bullet at
        # level 0 (python-docx's default template ships several).
        bullet_abstract_ids = set()
        for abstract in root.findall(qn("w:abstractNum")):
            for lvl in abstract.findall(qn("w:lvl")):
                if lvl.get(qn("w:ilvl")) == "0":
                    fmt = lvl.find(qn("w:numFmt"))
                    if fmt is not None and fmt.get(qn("w:val")) == "bullet":
                        bullet_abstract_ids.add(abstract.get(qn("w:abstractNumId")))
                    break
        for num in root.findall(qn("w:num")):
            a_ref = num.find(qn("w:abstractNumId"))
            if a_ref is not None and a_ref.get(qn("w:val")) in bullet_abstract_ids:
                num_id = num.get(qn("w:numId"))
                if num_id:
                    self._bullet_num_id = num_id
                    return self._bullet_num_id

        # 2. No bullet definition: append a minimal one to the existing part.
        def _max_attr(tag: str, attr: str) -> int:
            vals = [el.get(qn(attr)) for el in root.findall(qn(tag))]
            return max((int(v) for v in vals if v and v.lstrip("-").isdigit()), default=0)

        new_abstract_id = str(_max_attr("w:abstractNum", "w:abstractNumId") + 1)
        new_num_id = str(_max_attr("w:num", "w:numId") + 1)

        abstract_el = parse_xml(
            f'<w:abstractNum {ns} w:abstractNumId="{new_abstract_id}">'
            f'<w:multiLevelType w:val="singleLevel"/>'
            f"{self._BULLET_LVL_XML.format(ns='')}"
            f"</w:abstractNum>".encode("utf-8")
        )
        num_el = parse_xml(
            f'<w:num {ns} w:numId="{new_num_id}"><w:abstractNumId w:val="{new_abstract_id}"/></w:num>'.encode("utf-8")
        )

        # Schema order: every w:abstractNum precedes the first w:num.
        first_num = root.find(qn("w:num"))
        if first_num is not None:
            first_num.addprevious(abstract_el)
        else:
            root.append(abstract_el)
        root.append(num_el)

        # Re-bind so python-docx serializes the mutated root (XmlPart parts
        # serialize _element; generic parts are re-blobbed by save_to_stream).
        if hasattr(numbering_part, "_element"):
            numbering_part._element = root
        if hasattr(package, "_adeu_numbering_cache"):
            del package._adeu_numbering_cache
        logger.info("Added bullet numbering definition", num_id=new_num_id)
        self._bullet_num_id = new_num_id
        return self._bullet_num_id

    def _track_insert_inline(
        self,
        text: str,
        anchor_run: Optional[Run] = None,
        suppress_inherited: bool = False,
        reuse_id: Optional[str] = None,
    ):
        ins = self._create_track_change_tag("w:ins", reuse_id=reuse_id)

        segments = self._parse_inline_markdown(text)

        for seg_text, seg_props in segments:
            run = create_element("w:r")

            if anchor_run and anchor_run._element.rPr is not None:
                rPr_clone = deepcopy(anchor_run._element.rPr)
                # Prevent hidden/struck text bugs by stripping vanish and strike from deepcopies.
                # BUG-23-2: italic emphasis from the anchor run is also stripped — an inserted
                # replacement run must not silently inherit the surrounding italic styling (there
                # is no agent-facing override mechanism for it). Bold is intentionally preserved
                # because it usually carries structural meaning (headings, defined terms) that the
                # reviewer expects the replacement to keep.
                for tag in ["w:vanish", "w:strike", "w:dstrike", "w:i", "w:iCs"]:
                    for el in rPr_clone.findall(qn(tag)):
                        rPr_clone.remove(el)
                run.append(rPr_clone)
            self._apply_run_props(run, seg_props, suppress_inherited=suppress_inherited)

            t = create_element("w:t")
            self._set_text_content(t, seg_text)
            run.append(t)
            ins.append(run)

        if len(ins) == 0:
            return None

        return ins

    def _insert_and_split_ins(self, parent_ins, split_index: int, new_elem):
        """
        Splits a w:ins element to insert a new element (like w:del or another w:ins)
        without creating invalid nested w:ins tags.
        """
        grandparent = parent_ins.getparent()
        if grandparent is None:
            return

        parent_index = grandparent.index(parent_ins)

        left_ins = create_element("w:ins")
        for attr, val in parent_ins.attrib.items():
            left_ins.set(attr, val)

        right_ins = create_element("w:ins")
        for attr, val in parent_ins.attrib.items():
            right_ins.set(attr, val)

        # Snapshot children to safely extract them across loops
        children = list(parent_ins)
        for child in children[:split_index]:
            left_ins.append(child)
        for child in children[split_index:]:
            right_ins.append(child)

        insert_idx = parent_index
        if len(left_ins) > 0:
            grandparent.insert(insert_idx, left_ins)
            insert_idx += 1

        if new_elem is not None:
            grandparent.insert(insert_idx, new_elem)
            insert_idx += 1

        if len(right_ins) > 0:
            grandparent.insert(insert_idx, right_ins)

        grandparent.remove(parent_ins)

    def track_delete_run(self, run: Run, reuse_id: Optional[str] = None):
        del_tag = self._create_track_change_tag("w:del", reuse_id=reuse_id)

        # Clone the run to preserve special content (w:drawing, w:commentReference)
        new_run = deepcopy(run._r)

        # Convert w:t to w:delText
        for t in new_run.findall(qn("w:t")):
            t.tag = qn("w:delText")

        del_tag.append(new_run)

        parent = run._r.getparent()
        if parent is None:
            return None

        # Replace the run with <w:del> in place. When the run lives inside
        # another author's <w:ins>, this leaves the <w:del> NESTED inside that
        # <w:ins> (<w:ins author="A"><w:del author="B">…</w:del></w:ins>) — the
        # canonical OOXML representation of "B deletes A's still-pending
        # insertion." This preserves A's authorship and makes reject-all revert
        # the contingent text to nothing (rejecting A's <w:ins> removes the
        # nested <w:del> with it) rather than promoting it to committed text.
        # The replacement-insertion side (in _apply_single_edit_indexed) splits
        # the enclosing <w:ins> so the new <w:ins> is a sibling, never <w:ins>
        # nested in <w:ins>.
        parent.replace(run._r, del_tag)
        return del_tag

    @staticmethod
    def _paragraph_child_ancestor(element, paragraph):
        """
        Return the ancestor of ``element`` that is a direct child of
        ``paragraph`` (or ``element`` itself if it already is). Comment range
        markers must be siblings of a paragraph-level child, so an element that
        lives inside a <w:ins>/<w:del> wrapper has to be lifted to that wrapper.
        """
        cur = element
        while cur.getparent() is not None and cur.getparent() is not paragraph:
            cur = cur.getparent()
        return cur

    @staticmethod
    def _is_inside_pPr(element) -> bool:
        """
        Check if the given element is inside a w:pPr tag.
        """
        cur = element
        while cur is not None:
            if cur.tag == qn("w:pPr"):
                return True
            cur = cur.getparent()
        return False

    # XML root tags of stories that can host comment anchors. Word (and
    # LibreOffice, which refuses to LOAD such files) only supports comment
    # ranges in the main document story — never in headers, footers,
    # footnotes or endnotes (QA 2026-07-18 H4/C1).
    _MAIN_STORY_ROOT = qn("w:document")

    def _comment_anchor_in_main_story(self, element) -> bool:
        root = element
        while root.getparent() is not None:
            root = root.getparent()
        return root.tag == self._MAIN_STORY_ROOT

    def _skip_comment_outside_main_story(self, element, text: str) -> bool:
        """
        When the anchor lives outside the main document story, records a
        user-visible warning and returns True (caller must skip the comment).
        The tracked change itself still applies — only the bubble is dropped.
        """
        if self._comment_anchor_in_main_story(element):
            return False
        root = element
        while root.getparent() is not None:
            root = root.getparent()
        story = {
            qn("w:ftr"): "footer",
            qn("w:hdr"): "header",
            qn("w:footnotes"): "footnote",
            qn("w:endnotes"): "endnote",
        }.get(root.tag, "non-body")
        msg = (
            f'- Warning: the comment "{text[:60]}" was NOT attached: Word does not support '
            f"comments inside a {story} part, and writing one produces a document other "
            "applications cannot open. The tracked change itself was applied."
        )
        self.skipped_details.append(msg)
        logger.warning("Comment anchor outside main story; comment dropped", story=story, text=text[:60])
        return True

    def _attach_comment(self, parent_element, start_element, end_element, text: str):
        if not text:
            return
        # The anchor context can be gone by the time we get here (e.g. the
        # enclosing <w:ins> was split and detached from the tree). Resolve the
        # anchor indexes BEFORE minting the comment so a failed anchor skips
        # cleanly instead of crashing on parent_element.index(None-parent) and
        # leaving an orphaned entry in comments.xml (QA 2026-07-17 F8).
        if parent_element is None or start_element is None or end_element is None:
            logger.warning("Comment anchor context missing; skipping comment attachment", text=text[:60])
            return
        if self._skip_comment_outside_main_story(parent_element, text):
            return

        # Ensure the anchor elements are actual direct children of parent_element
        start_element = self._paragraph_child_ancestor(start_element, parent_element)
        end_element = self._paragraph_child_ancestor(end_element, parent_element)

        try:
            start_index = parent_element.index(start_element)
            end_index = parent_element.index(end_element)
        except ValueError:
            logger.warning("Comment anchor elements are not children of the parent; skipping", text=text[:60])
            return

        comment_id = self.comments_manager.add_comment(self.author, text)
        range_start = create_element("w:commentRangeStart")
        create_attribute(range_start, "w:id", comment_id)
        range_end = create_element("w:commentRangeEnd")
        create_attribute(range_end, "w:id", comment_id)

        ref_run = create_element("w:r")
        rPr = create_element("w:rPr")
        rStyle = create_element("w:rStyle")
        create_attribute(rStyle, "w:val", "CommentReference")
        rPr.append(rStyle)
        ref_run.append(rPr)

        ref = create_element("w:commentReference")
        create_attribute(ref, "w:id", comment_id)
        ref_run.append(ref)

        parent_element.insert(start_index, range_start)
        end_index = parent_element.index(end_element)
        parent_element.insert(end_index + 1, range_end)
        parent_element.insert(end_index + 2, ref_run)

    def _attach_comment_spanning(self, start_p, start_el, end_p, end_el, text: str):
        if not text:
            return
        if start_p is None or end_p is None:
            logger.warning("Comment anchor context missing; skipping comment attachment", text=text[:60])
            return
        if self._skip_comment_outside_main_story(start_p, text) or self._skip_comment_outside_main_story(end_p, text):
            return

        # Ensure the anchor elements are actual direct children of their respective paragraphs
        start_el = self._paragraph_child_ancestor(start_el, start_p)
        end_el = self._paragraph_child_ancestor(end_el, end_p)

        comment_id = self.comments_manager.add_comment(self.author, text)

        range_start = create_element("w:commentRangeStart")
        create_attribute(range_start, "w:id", comment_id)

        range_end = create_element("w:commentRangeEnd")
        create_attribute(range_end, "w:id", comment_id)

        ref_run = create_element("w:r")
        rPr = create_element("w:rPr")
        rStyle = create_element("w:rStyle")
        create_attribute(rStyle, "w:val", "CommentReference")
        rPr.append(rStyle)
        ref_run.append(rPr)

        ref = create_element("w:commentReference")
        create_attribute(ref, "w:id", comment_id)
        ref_run.append(ref)

        try:
            idx_start = start_p.index(start_el)
            start_p.insert(idx_start, range_start)
        except ValueError:
            pass

        try:
            idx_end = end_p.index(end_el)
            end_p.insert(idx_end + 1, range_end)
            end_p.insert(idx_end + 2, ref_run)
        except ValueError:
            pass

    def _control_gate_context(self, mapper: Any, start: int, length: int):
        """(intersecting, at_start, at_end) control stacks for a changed range."""
        intersecting = mapper.controls_intersecting(start, length) if hasattr(mapper, "controls_intersecting") else []
        at_start = mapper.controls_at(start) if hasattr(mapper, "controls_at") else []
        at_end = mapper.controls_at(max(start + length - 1, start)) if hasattr(mapper, "controls_at") else []
        return intersecting, at_start, at_end

    def _deletes_entire_control(self, mapper: Any, info: Any, start: int, length: int, final_new: str) -> bool:
        """Would this edit dissolve ``info``'s wrapper rather than empty it?

        G2 protects a control's EXISTENCE, not its text: emptying a
        delete-locked control is allowed and leaves the wrapper with an empty
        pair (A3.3). Only a deletion that also consumes text outside the
        control would have to hoist the wrapper away, so the test is "covers
        all of the content AND reaches past it".
        """
        if final_new.strip():
            return False
        rng = next((r for r in getattr(mapper, "control_ranges", []) if r[2] is info), None)
        if rng is None:
            return False
        c_start, c_end, _ = rng
        covers_all = start <= c_start and start + length >= c_end
        reaches_outside = start < c_start or start + length > c_end
        return covers_all and reaches_outside

    def _apply_gate_refusal(self, mapper: Any, start: int, length: int, edit: Any = None) -> Optional[str]:
        """The apply-path subset of the gate matrix; a reason string, or None.

        Only the document-property gates run here — content locks, group
        regions, data binding and protection. Deliberately not the whole
        matrix: G8 and G11 depend on the target STRING, and G14/G15 on the
        edit's shape, none of which a positionally-pinned edit has resolved
        in a form this layer can trust. Those stay validate-only, exactly as
        the paragraph-merge refusal does.
        """
        overrides = self.gate_overrides
        if self.protection.active and not overrides.ignore_document_protection and self.protection.edit == "readOnly":
            return "document is read-only protected"
        controls = mapper.controls_intersecting(start, length) if hasattr(mapper, "controls_intersecting") else []
        if not controls:
            return None
        # G13 refuses TEXT edits to bound content and points the caller at
        # set_field. A fill desugars into pinned ModifyText sub-edits, so
        # without this exemption the backstop would refuse the very operation
        # the error recommends — and the recommendation would be a dead end.
        # set_field is safe here precisely because it dual-writes the store,
        # which is the whole reason the text path is not.
        from_set_field = isinstance(getattr(edit, "_parent_edit_ref", None), SetField)
        info = next((i for i in controls if getattr(i, "bound", False)), None)
        if info is not None and not from_set_field:
            return f"{describe_control(info)} is data-bound"
        if overrides.ignore_control_locks:
            return None
        info = next((i for i in controls if getattr(i, "content_locked", False)), None)
        if info is not None:
            return f"{describe_control(info)} is content-locked"
        return None

    def _check_control_gates(
        self,
        edit_number: int,
        edit: Any,
        mapper: Any,
        start: int,
        length: int,
        *,
        final_target: str = "",
        final_new: str = "",
    ) -> Optional[str]:
        """Run the CC-4 gate matrix over one resolved edit; first failure wins.

        Order is deliberate, most-fundamental first: document protection binds
        regardless of where the edit lands, so it is checked before anything
        about the control; then the two category errors that no override can
        reasonably bypass (bound content, placeholder ghosts), because telling
        the caller "this text is not what you think it is" is more useful than
        telling them a lock stopped them; then the lock gates; then structure.
        """
        overrides = self.gate_overrides
        infos = list(getattr(mapper, "_sdt_infos", {}).values())
        intersecting, at_start, at_end = self._control_gate_context(mapper, start, length)

        is_comment_only = bool(getattr(edit, "comment", None)) and (edit.new_text or "") == (edit.target_text or "")

        err = check_protection_blocks_edit(
            edit_number,
            self.protection,
            controls=intersecting,
            is_comment_only=is_comment_only,
            overrides=overrides,
        )
        if err:
            return err
        # Comment-only edits mutate no text, so the tracking guarantee that
        # G5's untracked-write gate defends is not at stake for them.
        if not is_comment_only:
            err = check_untracked_write(edit_number, self.protection, overrides)
            if err:
                return err

        # G8 works off the target string, not the range: an empty control has
        # no content spans to intersect (see gates.check_placeholder_target).
        err = check_placeholder_target(edit_number, edit.target_text or "", infos)
        if err:
            return err

        if not intersecting:
            return None

        err = check_bound_control(edit_number, intersecting)
        if err:
            return err
        err = check_checkbox_edit(edit_number, intersecting, edit.target_text or "", edit.new_text or "")
        if err:
            return err
        err = check_content_lock(edit_number, intersecting, overrides)
        if err:
            return err
        err = check_group_region(edit_number, intersecting, overrides)
        if err:
            return err
        for info in intersecting:
            if self._deletes_entire_control(mapper, info, start, length, final_new):
                err = check_delete_lock(
                    edit_number,
                    [info],
                    deletes_entire_control=True,
                    overrides=overrides,
                )
                if err:
                    return err

        # G15: a merge is what makes a wall crossing structural rather than
        # segmentable. Without a paragraph break being consumed, a crossing is
        # G14's business (auto-segment), not a refusal.
        if "\n\n" in final_target and "\n\n" not in final_new:
            crossed = crossed_control_walls(intersecting, at_start, at_end)
            err = check_block_merge_across_control(edit_number, crossed)
            if err:
                return err

        # G14: the edit is valid on both sides of a wall it crosses, so it
        # applies — the word-level sub-edit splitter already lands each half
        # on its own side. What was missing is the disclosure: an agent that
        # asked to change text "in CC:3" and silently got a change half
        # outside it has been told something untrue by omission.
        crossed = crossed_control_walls(intersecting, at_start, at_end)
        if crossed:
            try:
                edit._warning = segmentation_note(crossed)
            except (AttributeError, ValueError):
                # Report notes are advisory; never fail an otherwise-valid
                # edit because a model object would not take the attribute.
                pass

        # Record what an override let through, for the report disclosure.
        if overrides.ignore_control_locks:
            for info in intersecting:
                if getattr(info, "content_locked", False) or getattr(info, "cls", None) == "group":
                    if not any(x is info for x in self._overridden_controls):
                        self._overridden_controls.append(info)
        return None

    def validate_edits(
        self,
        edits: List[Union[ModifyText, InsertTableRow, DeleteTableRow, SetField]],
        index_offset: int = 0,
    ) -> List[str]:
        """
        Validates edits against the document's CURRENT state.
        Returns a list of error strings. If the list is empty, the edits are
        safe to apply against the state the engine holds right now.

        Batches apply sequentially, so the batch loop calls this one edit at a
        time between applies; `index_offset` keeps the 1-based "Edit N Failed"
        labels aligned with the edit's position in the full batch.
        """
        errors = []

        # Ensure base mapper is ready, but DO NOT rebuild it if it already exists!
        # This saves ~15s of redundant O(N) DOM traversal on large files.
        if not self.mapper.full_text:
            self.mapper._build_map()

        # Category A: document-context-free string-shape validation.
        # Delegated to module-level helper so the Live Word path can call the
        # same checks. See validate_edit_strings docstring for what is checked.
        errors.extend(validate_edit_strings(edits, index_offset=index_offset))

        for i, edit in enumerate(edits, start=index_offset):
            # Caller-pinned indexes (e.g. generate_edits_from_text output)
            # resolve by position, not content: ambiguity / not-found checks
            # are meaningless for them and false-positive whenever the target
            # coincidentally matches unrelated text (a comment timestamp, an
            # earlier redline). The string-shape checks above still apply.
            if isinstance(edit, SetField):
                from adeu.fields import FieldResolutionError

                # Resolve the field NOW, before anything is written. An
                # unresolvable or ambiguous target is the recoverable half of
                # A4.2, and the batch contract promises those fail the whole
                # run without touching the document.
                try:
                    self._resolve_set_field_targets(edit)
                except FieldResolutionError as fe:
                    errors.append(f"- Edit {i + 1} Failed: {fe}")
                continue
            if (
                getattr(edit, "_match_start_index", None) is not None
                or getattr(edit, "_resolved_start_idx", None) is not None
            ):
                continue
            if not edit.target_text:
                # A text-anchored edit with no anchor can never resolve;
                # reject it up front so the transactional contract applies.
                errors.append(
                    f"- Edit {i + 1} Failed: target_text is empty. Pure insertions are expressed as a "
                    "replacement: put the text immediately around the insertion point in target_text "
                    "and repeat it (plus the new text) in new_text."
                )
                continue
            is_regex = getattr(edit, "regex", False)
            match_mode = getattr(edit, "match_mode", "strict")

            if is_regex:
                # An unparsable pattern must be diagnosed as a regex problem.
                # Without this check it falls through the matcher's silent
                # re.error guard and surfaces as "target text not found",
                # sending the user hunting for a typo in the document instead
                # of in the pattern (QA 2026-07-19 F-13).
                try:
                    re.compile(edit.target_text)
                except re.error as regex_err:
                    errors.append(
                        f"- Edit {i + 1} Failed: target_text is not a valid regular expression "
                        f'({regex_err}). Fix the pattern, or set "regex": false to match the '
                        "text literally."
                    )
                    continue

            # Matches covering ONLY virtual projection text (meta bubbles,
            # timestamps, style markers) are phantoms: they can neither be
            # edited nor legitimately ambiguate a real match — a target of
            # "4" was rejected as "appears 8 times" because comment-bubble
            # timestamps matched (QA 2026-07-19 ADEU-QA-002 C).
            matches = self.mapper.drop_virtual_only_matches(
                self.mapper.find_all_match_indices(edit.target_text, is_regex=is_regex)
            )
            active_text = self.mapper.full_text
            target_mapper = self.mapper

            # BUG-23-5: a copy of the target that lives entirely inside a
            # tracked deletion (<w:del>) is not a live, editable occurrence.
            # Dropped BEFORE the clean/original fallbacks so a deleted-only
            # target flows into the inside-a-deletion diagnostic below
            # instead of resolving against deleted text. (Historically this
            # filter ran only to disambiguate multi-match targets; a single
            # deleted-only match usually failed to resolve anyway because
            # the mapper fragmented styled deletions into separate
            # {--...--} blocks — with the projection twins aligned, the
            # coalesced block matches, and this filter is what enforces the
            # semantics. The apply-time resolver has always filtered these:
            # see _pre_resolve_heuristic_edit.)
            if matches:
                live_matches = []
                for start, length in matches:
                    real_spans = [
                        s for s in self.mapper.spans if s.run is not None and s.end > start and s.start < start + length
                    ]
                    if not real_spans or any(not s.del_id for s in real_spans):
                        live_matches.append((start, length))
                matches = live_matches

            # Fallback to Clean View if not found in Raw View (matches heuristic logic)
            if len(matches) == 0:
                if not self.clean_mapper:
                    self.clean_mapper = DocumentMapper(self.doc, clean_view=True)
                matches = self.clean_mapper.drop_virtual_only_matches(
                    self.clean_mapper.find_all_match_indices(edit.target_text, is_regex=is_regex)
                )
                if len(matches) > 0:
                    active_text = self.clean_mapper.full_text
                    target_mapper = self.clean_mapper

            is_deleted_text = False
            deleted_authors = set()

            # Check original view if still not found
            if len(matches) == 0:
                if not self.original_mapper:
                    self.original_mapper = DocumentMapper(self.doc, original_view=True)
                orig_matches = self.original_mapper.drop_virtual_only_matches(
                    self.original_mapper.find_all_match_indices(edit.target_text, is_regex=is_regex)
                )
                if len(orig_matches) > 0:
                    is_deleted_text = True
                    for start, length in orig_matches:
                        spans = [s for s in self.original_mapper.spans if s.end > start and s.start < start + length]
                        for s in spans:
                            if s.run is not None:
                                del_nodes = s.run._element.xpath("ancestor-or-self::w:del")
                                if del_nodes:
                                    auth = del_nodes[0].get(qn("w:author"))
                                    if auth:
                                        deleted_authors.add(auth)

            # The structural appendix is not part of the mapper's
            # projection, so all matches are valid document body matches.
            # (Deleted-only raw matches were already dropped above, before
            # the clean/original fallbacks ran.)
            valid_matches = matches

            if len(valid_matches) == 0:
                if is_deleted_text:
                    author_phrase = (
                        f"by {', '.join(sorted(deleted_authors))}" if deleted_authors else "by an existing revision"
                    )
                    errors.append(
                        f"- Edit {i + 1} Failed: Target text matches text inside a tracked deletion {author_phrase}. "
                        "Reject/accept that change first or target the active replacement text instead."
                    )
                else:
                    errors.append(
                        f"- Edit {i + 1} Failed: Target text not found in document:\n"
                        f'  "{truncate_middle(edit.target_text, REPORT_ECHO_CAP)}"'
                    )
            elif len(valid_matches) > 1 and match_mode == "strict":
                # valid_matches is a list of (start, length); the formatter
                # expects (start, end).
                positions = [(start, start + length) for start, length in valid_matches]
                errors.append(
                    format_ambiguity_error(
                        edit_index=i + 1,
                        target_text=edit.target_text,
                        haystack=active_text,
                        match_positions=positions,
                        terse=self.terse_errors,
                    )
                )

            if isinstance(edit, ModifyText) and len(valid_matches) == 1:
                start, length = valid_matches[0]
                actual_doc_text = active_text[start : start + length]
                effective_new_text = edit.new_text or ""
                prefix_len, suffix_len = trim_common_context(actual_doc_text, effective_new_text)
                t_end = len(actual_doc_text) - suffix_len
                final_target = actual_doc_text[prefix_len:t_end]
                final_new = effective_new_text[prefix_len : len(effective_new_text) - suffix_len]

                # QA 2026-07-18 C1: the projection flattens headers, body,
                # footers and notes into one string, but a text edit whose
                # matched span covers real text from two different OPC parts
                # cannot be applied without putting content in the wrong part
                # — including the insertion shape, whose anchor point at the
                # part gap is inherently ambiguous. Refuse the RAW match
                # range outright and ask for a single-part anchor.
                multi_part_doc = len([r for r in target_mapper.part_ranges if r[1] > r[0]]) > 1
                raw_span_parts = (
                    sorted(
                        {
                            s.part_index
                            for s in target_mapper.spans
                            if s.run is not None and s.end > start and s.start < start + length
                        }
                    )
                    if multi_part_doc
                    else []
                )
                if len(raw_span_parts) > 1:
                    kinds = " → ".join(target_mapper.part_kind_of(pi) or "?" for pi in raw_span_parts)
                    errors.append(
                        f"- Edit {i + 1} Failed: target_text spans a structural document-part "
                        f"boundary ({kinds}). Headers, body, footers and footnotes are separate "
                        "Word parts — an edit cannot cross between them. Anchor the edit on text "
                        "within a single part (split it into one edit per part if both sides "
                        "must change)."
                    )

                # QA 2026-07-18 M5: image markers are read-only projections.
                # Only the CHANGED span matters — markers sitting untouched in
                # the shared context are fine.
                eff_start = start + prefix_len
                eff_end = start + length - suffix_len

                # CC-4 content-control gates (spec-gates §2). Same shape as
                # the part-boundary refusal above, for the same reason: a
                # control wall is a place where an edit that looks fine in the
                # flattened projection cannot be applied to the XML.
                #
                # The CHANGED range (eff_*), not the raw match, so shared
                # context reaching into a locked control does not by itself
                # refuse the edit — the caller is not modifying it. This is
                # the image-marker gate's rule, not the part gate's: the part
                # gate uses the raw range because the insertion ANCHOR is
                # ambiguous at a part gap, which has no analogue here.
                gate_error = self._check_control_gates(
                    i + 1,
                    edit,
                    target_mapper,
                    eff_start,
                    max(eff_end - eff_start, 0),
                    final_target=final_target,
                    final_new=final_new,
                )
                if gate_error:
                    errors.append(gate_error)
                if eff_end > eff_start:
                    overlapping = [
                        s
                        for s in target_mapper.spans
                        if s.end > eff_start and s.start < eff_end and (s.run is not None or s.text.strip())
                    ]
                    if any(getattr(s, "is_image_marker", False) for s in overlapping):
                        errors.append(
                            f"- Edit {i + 1} Failed: the target overlaps a read-only image marker "
                            "(![alt](docx-image:N)). Images cannot be edited or removed via text "
                            "replacement — target the text around the image instead."
                        )

                # QA 2026-07-18 H4: comments can only be anchored in the main
                # document story. A comment-only edit (target == new) whose
                # match lives in a header/footer/footnote has no effect Word
                # or LibreOffice could render — refuse it clearly.
                if (
                    edit.comment
                    and (edit.new_text or "") == (edit.target_text or "")
                    and hasattr(target_mapper, "part_kind_at")
                ):
                    kind_here = target_mapper.part_kind_at(start)
                    if kind_here not in (None, "body"):
                        errors.append(
                            f"- Edit {i + 1} Failed: comments cannot be anchored inside a {kind_here} "
                            "part — Word only supports comments in the main document body. Comment on "
                            "the related body text instead."
                        )

                # QA 2026-07-18 C2: a replacement may not smuggle new
                # pipe-delimited row lines into a table cell. Rows are
                # structural; adding one requires the insert_row operation.
                if self._introduces_table_row_text(target_mapper, start, length, final_target, final_new):
                    errors.append(
                        f"- Edit {i + 1} Failed: new_text introduces a pipe-delimited row line inside "
                        "a table. Text replacement cannot create table rows — use the structured "
                        '\'insert_row\' operation instead (e.g. {"type": "insert_row", '
                        '"target_text": "<anchor row text>", "cells": ["...", "..."]}).'
                    )

                if "\n\n" in final_target:
                    # A *balanced* multi-paragraph modification (the target and the
                    # replacement contain the same number of paragraph breaks) is
                    # safe: apply_edits splits it into one sub-edit per paragraph
                    # segment, leaving the structural \n\n breaks untouched. Only
                    # reject when the paragraph structure would actually change
                    # (a merge or split), which cannot be expressed as a
                    # per-paragraph text replacement. See _resolve_single_match.
                    balanced = actual_doc_text.count("\n\n") == effective_new_text.count("\n\n")
                    if not balanced:
                        if "\n\n" in final_new:
                            parts = actual_doc_text.split("\n\n")
                            if len(parts) >= 2 and parts[0].strip() and parts[-1].strip():
                                errors.append(
                                    f"- Edit {i + 1} Failed: target_text spans a paragraph boundary "
                                    "with body text on both sides. The paragraph break is a structural "
                                    "element, not literal text, so it cannot be replaced as "
                                    "a single span without corrupting the document. "
                                    "Split this into one edit per paragraph."
                                )
                        else:
                            parts = final_target.split("\n\n")
                            if len(parts) >= 2 and parts[0].strip() and parts[-1].strip():
                                errors.append(
                                    f"- Edit {i + 1} Failed: target_text spans a paragraph boundary "
                                    "with body text on both sides. "
                                    "The paragraph break is a structural element, not literal text, "
                                    "so it cannot be replaced as "
                                    "a single span without corrupting the document. Split this into "
                                    "one edit per paragraph."
                                )

            for start, length in valid_matches:
                spans = [s for s in target_mapper.spans if s.end > start and s.start < start + length]
                # Foreign insertions overlapping the target, keyed by author.
                ins_authors_to_ids: dict[str, set[str]] = {}
                # Foreign comments overlapping the target, keyed by author.
                comment_authors_to_ids: dict[str, set[str]] = {}
                # Does any real (run-backed) text in the target lie OUTSIDE a
                # foreign insertion? If so the target only partially overlaps the
                # insertion and replacing it as one span would split the <w:ins>
                # boundary — that case must still be refused.
                has_non_foreign_real_text = False
                for s in spans:
                    if s.run is None:
                        continue
                    is_foreign_ins = False
                    if s.ins_id:
                        ins_nodes = self.doc.element.xpath(f"//w:ins[@w:id='{s.ins_id}']")
                        if ins_nodes:
                            auth = ins_nodes[0].get(qn("w:author"))
                            if auth and auth != self.author:
                                ins_authors_to_ids.setdefault(auth, set()).add(s.ins_id)
                                is_foreign_ins = True
                    if not is_foreign_ins:
                        has_non_foreign_real_text = True
                # Foreign comments anywhere in the target range (check every span,
                # not just the last one).
                for s in spans:
                    if s.comment_ids:
                        for cid in s.comment_ids:
                            c_data = self.mapper.comments_map.get(cid)
                            if c_data and c_data.get("author") and c_data.get("author") != self.author:
                                comment_authors_to_ids.setdefault(c_data["author"], set()).add(f"Com:{cid}")

                if ins_authors_to_ids:
                    # A single-occurrence (strict/first) modification whose target
                    # lies ENTIRELY inside foreign-authored insertion(s) is
                    # allowed: track_delete_run splits the enclosing <w:ins> and
                    # nests the change, producing valid tracked-change XML. Refuse
                    # the remaining cases — match_mode "all" fan-outs and partial
                    # overlaps that straddle the insertion boundary.
                    fully_within_foreign_ins = not has_non_foreign_real_text
                    if not (match_mode in ("strict", "first") and fully_within_foreign_ins):
                        # Keep the hint bounded: naming every author and every id
                        # makes the refusal grow without limit, blowing the message
                        # token budget. One author with up to two ids is enough to
                        # act on; the rest are summarised as a count.
                        sorted_authors = sorted(ins_authors_to_ids.keys())
                        named_author = sorted_authors[0]
                        sorted_ids = sorted(
                            ins_authors_to_ids[named_author], key=lambda x: int(x) if x.isdigit() else 0
                        )
                        first_target_id = f"Chg:{sorted_ids[0]}" if sorted_ids else None
                        id_hints = ", ".join(f"Chg:{cid}" for cid in sorted_ids[:2])
                        hint_suffix = f" (e.g. {id_hints})" if id_hints else ""
                        if len(sorted_authors) > 1:
                            hint_suffix += f" (+{len(sorted_authors) - 1} more)"
                        accept_json = (
                            f'{{"type": "accept", "target_id": "{first_target_id}"}}' if first_target_id else ""
                        )
                        # Narrowing to one occurrence only helps when the target
                        # sits wholly inside the insertion: on a straddle,
                        # strict/first are refused here too, so offering them
                        # would send the caller round the same loop.
                        if match_mode == "all" and fully_within_foreign_ins:
                            advice = 'or use match_mode="strict" or "first", or scope your edit outside of it.'
                        else:
                            advice = "or scope your edit outside of it."
                        head = f"- Edit {i + 1} Failed: Modification targets an active insertion from another author ("
                        tail = f"{hint_suffix}). Accept first with {accept_json} {advice}"
                        # Author names are arbitrary strings (firm and department
                        # names, not just people), so the name only gets what is
                        # left of the refusal budget. That keeps the message
                        # bounded whatever the document says.
                        author_budget = GUARD_MESSAGE_CAP - len(head) - len(tail)
                        msg = head + clamp_text(named_author, author_budget) + tail
                        # w:id values are document-supplied too, and a long one
                        # blows the budget through head+tail alone (leaving no
                        # author budget to give back), so clamp the whole message.
                        errors.append(clamp_text(msg, GUARD_MESSAGE_CAP))
                        continue

                # Foreign comment ranges do NOT block deliberate single-occurrence
                # edits: amending body text under a colleague's comment is a
                # normal review workflow, and the comment anchor survives the
                # tracked change. Only blind match_mode="all" fan-outs are
                # refused, so a bulk replacement cannot silently sweep through
                # another author's annotations (transactional rollback).
                if comment_authors_to_ids and match_mode == "all":
                    author_hints = []
                    for auth in sorted(comment_authors_to_ids.keys()):
                        sorted_ids = sorted(
                            comment_authors_to_ids[auth],
                            key=lambda x: int(x.split(":")[-1]) if x.split(":")[-1].isdigit() else 0,
                        )
                        id_hints = ", ".join(sorted_ids)
                        author_hints.append(f"{auth} (e.g. {id_hints})" if id_hints else auth)
                    errors.append(
                        f'- Edit {i + 1} Failed: match_mode="all" would sweep through a comment range from '
                        f"another author ({', '.join(author_hints)}). Target the commented text deliberately "
                        f'with match_mode "strict" or "first", or scope your edit outside of it.'
                    )

            # Structural table edits: verify the anchor really is a table row,
            # and that insert_row does not provide more cells than the row has
            # columns — extra cells must never be silently discarded (QA M3).
            if isinstance(edit, (InsertTableRow, DeleteTableRow)) and valid_matches:
                start, length = valid_matches[0]
                n_cols = self._column_count_at(target_mapper, start, length)
                if n_cols is None:
                    op_name = "insert_row" if isinstance(edit, InsertTableRow) else "delete_row"
                    errors.append(
                        f"- Edit {i + 1} Failed: {op_name} target text was found, but it is not inside "
                        "a table row. Anchor the operation on text that appears within the table."
                    )
                elif isinstance(edit, InsertTableRow) and len(edit.cells) > n_cols:
                    errors.append(
                        f"- Edit {i + 1} Failed: insert_row provides {len(edit.cells)} cells but the "
                        f"target table has {n_cols} column(s). The extra cell(s) would be dropped. "
                        f"Provide at most {n_cols} cells — rows given fewer cells are padded with "
                        "empty ones."
                    )

        return errors

    @staticmethod
    def _column_count_at(mapper: DocumentMapper, start: int, length: int) -> Optional[int]:
        """
        Number of columns (w:tc elements) in the table row containing the text
        at [start, start+length) in `mapper`, or None if that text is not
        inside a table row.
        """
        for s in mapper.spans:
            if s.end <= start or s.start >= start + length:
                continue
            curr = None
            if s.run is not None:
                curr = s.run._element
            elif s.paragraph is not None:
                curr = s.paragraph._element

            while curr is not None:
                if curr.tag == qn("w:tr"):
                    return len(curr.findall(qn("w:tc")))
                curr = curr.getparent()
        return None

    @classmethod
    def _introduces_table_row_text(
        cls,
        mapper: DocumentMapper,
        start: int,
        length: int,
        final_target: str,
        final_new: str,
    ) -> bool:
        """
        True when a replacement anchored in a table would ADD line-separated
        pipe-delimited content — the text shape of a table row. Writing that
        into a cell renders a fake row inside one cell while the real grid
        stays unchanged (QA 2026-07-18 C2); such edits must use insert_row.
        """
        if "\n" not in final_new or " | " not in final_new:
            return False
        new_pipe_lines = sum(1 for line in final_new.split("\n") if " | " in line)
        old_pipe_lines = sum(1 for line in final_target.split("\n") if " | " in line)
        if new_pipe_lines <= old_pipe_lines:
            return False
        return cls._column_count_at(mapper, start, max(length, 1)) is not None

    def _refresh_after_sequential_edit(self) -> None:
        """
        Rebuilds every text projection after a batch edit mutated the DOM, so
        the NEXT edit in the sequential batch validates and resolves against
        the document state this one produced (chaining). Mirrors the Node
        engine, which re-creates its mapper after each applied edit.
        """
        self.mapper = DocumentMapper(self.doc)
        # Offsets into mapper.full_text; rebuilt whenever the mapper is.
        self._cc_anchor_pairs = None
        self.clean_mapper = None
        self.original_mapper = None

    def _restore_from_snapshot(self, snapshot: Optional[BytesIO]) -> None:
        """
        Rolls the engine back to a pre-batch snapshot (as produced by
        save_to_stream). Used for transactional rejection: when anything in a
        batch fails, every edit AND every review action the batch already
        applied is undone before the BatchValidationError propagates.
        """
        if snapshot is None:
            return
        self.__init__(  # type: ignore[misc]
            snapshot,
            author=self.author,
            id_discovery_hint=self.id_discovery_hint,
            terse_errors=self.terse_errors,
        )

    def _batch_fingerprint(self) -> str:
        """
        Everything a batch can change, cheaply. Compared before the batch and
        after a rollback to VERIFY the rollback rather than assume it (see
        `rollback_verified`).

        Every way a batch mutates a document lands here: an applied edit mints
        new w:ins/w:del ids, accept/reject retires them, reply (and an edit's
        `comment`) adds a comment id. Count AND ids, because a document may
        reuse one w:id across several elements.

        Read from the TREE, never from the mapper: the mapper is rebuilt by the
        rollback but not by every operation that precedes a batch (accept_all
        leaves it stale by design), so a mapper-derived value would compare a
        stale "before" against a fresh "after" and report a clean rollback as a
        leak.
        """
        revisions = [
            n
            for tag in ("w:ins", "w:del") + self._FORMAT_CHANGE_TAGS
            for n in self.doc.element.findall(f".//{qn(tag)}")
        ]
        return "|".join(
            (
                str(len(revisions)),
                ",".join(self._existing_change_ids()),
                ",".join(self._existing_comment_ids()),
            )
        )

    def _verify_rollback(self, pre_batch_fingerprint: Optional[str]) -> None:
        """
        Did the rollback actually roll back? A rejected batch is a promise that
        the document is untouched; this is the check that the promise held, and
        the ONLY thing a caching caller can safely key document reuse off.

        Runs AFTER `_restore_from_snapshot`, which re-runs `__init__` and so
        resets the flag along with everything else.

        None = not fingerprinted (an edit-only batch): the edit rollback path
        is unchanged and separately pinned, and fingerprinting every batch
        would put a whole-document revision walk on the hot path for it.
        """
        if pre_batch_fingerprint is None:
            return
        try:
            self.rollback_verified = self._batch_fingerprint() == pre_batch_fingerprint
        except Exception:
            self.rollback_verified = False

    @staticmethod
    def _report_new_text(edit: Any) -> str:
        """
        The "new text" a batch report should show for an edit. InsertTableRow
        has no new_text field — surface its cell contents rather than a
        misleading empty string (QA M4).
        """
        if isinstance(edit, InsertTableRow):
            return " | ".join(edit.cells)
        return getattr(edit, "new_text", "") or ""

    @staticmethod
    def _flag_surviving_js_backreference(edit: Any, substituted_text: str) -> None:
        """
        Non-fatal guardrail (QA 2026-07-23 customer C2): Python's `re` engine
        does not expand JavaScript-style $N backreferences, so `new_text`
        containing "$1" is written into the document as the literal text
        "$1". That is spec-sanctioned platform behavior (spec §6), but doing
        it SILENTLY corrupted a payment clause in QA. When a $N token from
        new_text survives substitution verbatim AND the pattern actually has
        that capture group (i.e. a backreference was plausibly intended),
        stash a warning for the edit report. Never a hard reject: "$1,000"
        style literals are everyday legal text.
        """
        m = re.search(r"\$(\d+)", getattr(edit, "new_text", None) or "")
        if not m or m.group(0) not in substituted_text:
            return
        try:
            group_count = re.compile(edit.target_text).groups
        except re.error:
            return
        if not (0 < int(m.group(1)) <= group_count):
            return
        edit._warning = (
            f"new_text contains '{m.group(0)}', which Python's re engine does not expand — "
            f"the literal text '{m.group(0)}' was written into the document. For a "
            "capture-group backreference use \\1 or \\g<1> ($N is JavaScript syntax). "
            "If you meant a literal dollar amount, ignore this warning."
        )

    def _build_edit_report(self, edit: Any) -> dict:
        """Builds the per-edit result dict after apply_edits ran on the edit."""
        success = getattr(edit, "_applied_status", False)
        edit_error_msg = getattr(edit, "_error_msg", None)
        critic_markup = None
        clean_text = None
        # Punctuation-anchor warning is failure-context only: on success
        # the redline preview below already reports the change cleanly.
        # Resolution advisories (edit._warning, e.g. the surviving-$N
        # backreference guardrail) surface in BOTH outcomes.
        warning = getattr(edit, "_warning", None)
        if success:
            critic_markup, clean_text = self._build_edit_context_previews(edit)
        else:
            warning = warning or self._check_punctuation_warning(getattr(edit, "target_text", ""))
        return {
            "status": "applied" if success else "failed",
            "type": getattr(edit, "type", "modify"),
            # Echoes of caller-supplied values are bounded so an oversized edit
            # cannot balloon the report/JSON output (QA C2).
            "target_text": truncate_middle(getattr(edit, "target_text", ""), REPORT_ECHO_CAP),
            "new_text": truncate_middle(self._report_new_text(edit), REPORT_ECHO_CAP),
            # Every per-edit report carries the edit's comment — the report
            # is where an agent verifies the comment it wrote
            # (F7, QA 2026-07-23).
            "comment": getattr(edit, "comment", None),
            "warning": warning,
            "error": edit_error_msg,
            "critic_markup": critic_markup,
            "clean_text": clean_text,
            "pages": getattr(edit, "_pages", []),
            "heading_path": getattr(edit, "_heading_path", ""),
            "field": getattr(edit, "_field", ""),
            "occurrences_modified": getattr(edit, "_occurrences_modified", 0),
            "match_mode": getattr(edit, "match_mode", "strict"),
        }

    def process_batch(
        self,
        changes: List[DocumentChange],
        original_indices: Optional[List[int]] = None,
        partial: bool = False,
    ) -> dict:
        """
        Processes a unified batch of actions and edits safely.
        """
        return self._process_batch_internal(changes, original_indices=original_indices, partial=partial)

    def _field_label_at(self, offset: int) -> str:
        """``CC:<N> "<alias>" (tag: <tag>)`` for the control containing ``offset``.

        Audit-trail symmetry with ``heading_path`` (spec-fields-ledger §6): a
        reviewer reading the report needs to know an edit landed inside a
        content control, because that is what decides whether Word will let a
        human keep it.

        Resolves the INNERMOST containing control — an edit inside CC:9 reports
        CC:9, not the group CC:8 that wraps it, which is the more specific and
        more actionable answer.
        """
        pairs = self._cc_anchor_pairs
        if pairs is None:
            pairs = []
            text = self.mapper.full_text
            opens: dict[int, tuple[int, int]] = {}
            for m in _CC_ANCHOR_SCAN_RE.finditer(text):
                ordinal = int(m.group(2))
                if m.group(1):
                    if ordinal in opens:
                        _open_start, open_end = opens.pop(ordinal)
                        pairs.append((open_end, m.start(), ordinal))
                else:
                    opens[ordinal] = (m.start(), m.end())
            self._cc_anchor_pairs = pairs

        best: tuple[int, int, int] | None = None
        for start, end, ordinal in pairs:
            if start <= offset <= end and (best is None or (end - start) < (best[1] - best[0])):
                best = (start, end, ordinal)
        if best is None:
            return ""

        ordinal = best[2]
        info = next(
            (i for i in getattr(self.mapper, "_sdt_infos", {}).values() if i.ordinal == ordinal),
            None,
        )
        label = f"CC:{ordinal}"
        if info is not None and info.alias:
            label += f' "{info.alias}"'
        if info is not None and info.tag:
            label += f" (tag: {info.tag})"
        return label

    def _get_heading_path_and_page(self, start_idx: int, text: str, page_offsets: List[int]) -> Tuple[str, int]:
        page = 1
        for i, off in enumerate(page_offsets):
            if start_idx >= off:
                page = i + 1
            else:
                break

        lines = text[:start_idx].split("\n")
        path: List[str] = []
        current_level = 999
        for line in reversed(lines):
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                level = len(m.group(1))
                if level < current_level:
                    clean_heading = re.sub(r"\*\*|__|[*_]", "", m.group(2))
                    clean_heading = re.sub(r"\{#[^}]+\}", "", clean_heading).strip()
                    if len(clean_heading) > 80:
                        clean_heading = clean_heading[:80] + "..."
                    path.insert(0, clean_heading)
                    current_level = level
                    if level == 1:
                        break
        return " > ".join(path) if path else "", page

    def get_pending_revision_authors(self) -> set[str]:
        """Collects author names from all pending revisions and comments.

        The ``w:author`` attribute *is* the signal: every tracked-change and
        comment marker carries it (``w:ins``, ``w:del``, ``w:moveTo``,
        ``w:pPrChange``, ``w:tblPrChange``, ``w:cellIns``, ``w:cellDel``,
        ``w:comment``, ...). Matching on a list of tag names instead silently
        drops the table/row/cell revision markers, so we collect the attribute
        wherever it appears — in the live body and in every other XML part.

        The walk uses ``iter()`` rather than an XPath predicate on purpose:
        roots of parts without a registered oxml element class (``w:footnotes``,
        ``w:endnotes``, ...) are plain lxml elements that bind no ``w`` prefix,
        so ``//*[@w:author]`` raises ``XPathEvalError`` on them, while
        ``BaseOxmlElement.xpath()`` accepts no ``namespaces`` argument.

        Word's persona registry is skipped: its ``w15:person`` entries survive
        accepting every revision, so its ``w:author`` attributes are metadata
        rather than pending revisions. It is matched on the
        ``application/vnd.ms-word.people+xml`` content type, because its part
        name is not fixed (``word/people.xml``, ``word/people1.xml``, ...).
        """
        authors: set[str] = set()
        author_attr = qn("w:author")

        def collect(root: Any) -> None:
            for el in root.iter(etree.Element):
                author = el.get(author_attr)
                if author:
                    authors.add(author)

        collect(self.doc.element)

        try:
            comments_data = self.comments_manager.extract_comments_data()
            for c_info in comments_data.values():
                c_author = c_info.get("author")
                if c_author and c_author != "Unknown":
                    authors.add(c_author)
        except Exception:
            pass

        try:
            package = getattr(self.doc.part, "package", None)
            parts = list(getattr(package, "parts", None) or ())
        except Exception:
            parts = []

        for part in parts:
            # The main part is already covered by the live body scan above,
            # which also sees edits not yet serialized to its blob.
            if part is self.doc.part or not str(part.content_type).endswith("+xml"):
                continue
            # Persona registry: identified by its content type, since Word does
            # not fix its part name (`people.xml`, `people1.xml`, ...).
            partname = str(part.partname).lower()
            if str(part.content_type) == "application/vnd.ms-word.people+xml" or "people" in partname:
                continue
            try:
                root = parse_xml(part.blob)
            except Exception:
                # Unparsable payload: nothing to scan. Scanning itself is left
                # outside the guard so real scan failures are never swallowed.
                continue
            collect(root)

        return authors

    def _process_batch_internal(
        self,
        changes: List[DocumentChange],
        original_indices: Optional[List[int]] = None,
        partial: bool = False,
    ) -> dict:
        """
        Internal execution engine for batches of edits and actions.
        """
        pending_authors = self.get_pending_revision_authors()
        author_impersonation_warning = None
        if self.author and self.author in pending_authors:
            author_impersonation_warning = (
                f"[!] Warning: acting author '{self.author}' matches an author with pending revisions in this document."
            )

        self.skipped_details = []
        self._overridden_controls = []
        failed_list: List[Tuple[int, str]] = []

        actions_with_idx = [
            (original_indices[i] if original_indices else i, c)
            for i, c in enumerate(changes)
            if isinstance(c, (AcceptChange, RejectChange, ReplyComment))
        ]
        edits_with_idx = [
            (original_indices[i] if original_indices else i, c)
            for i, c in enumerate(changes)
            if isinstance(c, (ModifyText, InsertTableRow, DeleteTableRow, SetField))
        ]

        actions = [c for _, c in actions_with_idx]
        action_indices = [i for i, _ in actions_with_idx]

        applied_actions, skipped_actions, already_resolved_actions = 0, 0, 0

        # ONE transaction for the WHOLE batch. The snapshot has to predate the
        # review actions, not just the edits: they mutate the document too, and
        # a rejection promises the caller that "it was rolled back and nothing
        # was saved". Taken after apply_review_actions, the snapshot CONTAINED
        # every accept/reject/reply of a batch a later edit went on to reject,
        # so the rollback could not undo them; a reused engine (or, on the Node
        # twin, a cached DOM) then carried each rejected attempt's reply into
        # the next one (BUG 2026-08-12).
        #
        # LAZY SNAPSHOT (docs/Performance.md §5.2): the snapshot must equal the
        # engine's CURRENT state. Until something mutates the tree — a previous
        # batch on this engine instance — the pristine load-time bytes ARE that
        # state and the full save_to_stream serialize+re-zip is skipped.
        # Hoisting the capture above the actions makes that fast path MORE
        # reachable, since the actions no longer dirty the tree first. What it
        # does cost is a snapshot for an ACTION-ONLY batch, which used to take
        # none — on a fresh engine that is a BytesIO over bytes already held.
        # `partial` is the explicit opt-out of transactional rejection, so it
        # keeps nothing to roll back to.
        pre_batch_snapshot: Optional[BytesIO] = None
        pre_batch_fingerprint: Optional[str] = None
        # Revision-id watermark: every id above this was minted by THIS batch,
        # which is how the stranded-anchor advisory tells the deletions it
        # caused from the ones the document arrived with.
        pre_batch_revision_id = self.current_id
        self.rollback_verified = True
        if not partial and (actions or edits_with_idx):
            if self._mutated_since_load:
                pre_batch_snapshot = self.save_to_stream()
            else:
                pre_batch_snapshot = BytesIO(self._pristine_bytes)
            if actions:
                pre_batch_fingerprint = self._batch_fingerprint()

        if actions:
            # G7/G4 (spec-gates §2): protection gates review actions BEFORE
            # the id-existence check, because "that id does not exist" would
            # be a misleading answer to "why did my Accept fail" in a document
            # where no Accept can succeed at all.
            protection_errors = [
                err
                for err in (
                    check_protection_blocks_review(
                        idx + 1,
                        getattr(act, "type", ""),
                        self.protection,
                        self.gate_overrides,
                    )
                    for idx, act in zip(action_indices, actions, strict=True)
                )
                if err
            ]
            if protection_errors:
                failed_list.extend(_extract_failed_indices(protection_errors))
                raise BatchValidationError(protection_errors, failed=failed_list)
            action_shape_errors = validate_review_action_batch(actions, indices=action_indices)
            if action_shape_errors:
                failed_list.extend(_extract_failed_indices(action_shape_errors))
                raise BatchValidationError(action_shape_errors, failed=failed_list)
            # Document-aware pairing check BEFORE any action mutates the DOM:
            # accept + reject across one replacement's del+ins pair is a
            # contradiction, not two independent operations (ADEU-QA-004).
            pairing_errors = self.validate_action_pairing(actions, indices=action_indices)
            if pairing_errors:
                failed_list.extend(_extract_failed_indices(pairing_errors))
                raise BatchValidationError(pairing_errors, failed=failed_list)
            applied_actions, skipped_actions, already_resolved_actions = self.apply_review_actions(
                actions, indices=action_indices
            )
            if skipped_actions > 0:
                skipped_fails = _extract_failed_indices(self.skipped_details)
                failed_list.extend(skipped_fails)
                if not partial:
                    # An action can fail at APPLY time (a reply whose parent
                    # cannot be threaded, a w:id shared across authors) — after
                    # validation passed and after earlier actions in the batch
                    # already applied. Capture the details first: the restore
                    # re-initializes the engine, which clears skipped_details.
                    details = list(self.skipped_details)
                    self._restore_from_snapshot(pre_batch_snapshot)
                    self._verify_rollback(pre_batch_fingerprint)
                    raise BatchValidationError(details, failed=failed_list)
            if edits_with_idx:
                self.clean_mapper = None
                self.original_mapper = None

        body_text, _ = split_structural_appendix(self.mapper.full_text)
        pag_res = paginate(body_text, "")
        page_offsets = pag_res.body_page_offsets

        edits_reports = []
        applied_edits, skipped_edits = 0, 0

        if edits_with_idx:
            # Batches apply SEQUENTIALLY: each edit is validated and applied
            # against the document state produced by the edits before it, so a
            # later edit may target text an earlier edit introduced (chaining).
            # Validation failures keep the batch transactional: the run
            # restores the pre-batch snapshot — taken above, BEFORE this
            # batch's actions applied — and rejects everything, with the
            # per-edit reports carried inside the BatchValidationError details.
            cloned_edits = [(orig_idx, deepcopy(e)) for orig_idx, e in edits_with_idx]

            def _pinned_idx(e: Any) -> Optional[int]:
                if e._resolved_start_idx is not None:
                    return e._resolved_start_idx
                return e._match_start_index

            # Caller-pinned indexes (e.g. generate_edits_from_text output) are
            # coordinates in the INITIAL document state. Apply them first in
            # one descending sweep — positions below an applied edit never
            # move — then let text-anchored edits re-resolve sequentially
            # against the mutated text. Mirrors the Node engine's ordering.
            pinned = [(k, orig_idx, e) for k, (orig_idx, e) in enumerate(cloned_edits) if _pinned_idx(e) is not None]
            unpinned = [(k, orig_idx, e) for k, (orig_idx, e) in enumerate(cloned_edits) if _pinned_idx(e) is None]

            reports_by_input: List[Optional[dict]] = [None] * len(cloned_edits)
            validation_errors: List[str] = []

            # Caller-pinned edits resolve by position, so the document-context
            # checks (not-found / ambiguity) don't apply to them — but the
            # string-shape checks do, exactly as the validate_edits docstring
            # promises. Without this, the text-diff path writes raw CriticMarkup
            # (including reviewer names and change IDs) into document bodies as
            # prose (QA 2026-07-17 F8).
            pinned_ok: List[Tuple[int, int, Any]] = []
            for k, orig_idx, e in pinned:
                shape_errors = validate_edit_strings([e], index_offset=orig_idx)
                if shape_errors:
                    validation_errors.extend(shape_errors)
                    skipped_edits += 1
                    err_msg = "\n".join(shape_errors)
                    failed_list.append((orig_idx, err_msg))
                    reports_by_input[k] = {
                        "status": "failed",
                        "type": getattr(e, "type", "modify"),
                        "target_text": truncate_middle(getattr(e, "target_text", ""), REPORT_ECHO_CAP),
                        "new_text": truncate_middle(self._report_new_text(e), REPORT_ECHO_CAP),
                        "comment": getattr(e, "comment", None),
                        "warning": None,
                        "error": err_msg,
                        "critic_markup": None,
                        "clean_text": None,
                    }
                else:
                    pinned_ok.append((k, orig_idx, e))

            if pinned_ok:
                p_applied, p_skipped = self.apply_edits([e for _, _, e in pinned_ok], page_offsets=page_offsets)
                applied_edits += p_applied
                skipped_edits += p_skipped
                # Refresh projections BEFORE building reports so previews can
                # slice the actual post-apply document state (F6).
                if p_applied > 0:
                    self._refresh_after_sequential_edit()
                for k, orig_idx, e in pinned_ok:
                    rep = self._build_edit_report(e)
                    reports_by_input[k] = rep
                    if rep.get("status") == "failed":
                        failed_list.append((orig_idx, rep.get("error") or "Failed to apply edit"))

            for k, orig_idx, edit in unpinned:
                try:
                    single_errors = self.validate_edits([edit], index_offset=orig_idx)
                except RegexTimeoutError as e:
                    # A pathological user pattern must fail as a clean per-edit
                    # validation error, never a hang or traceback (QA F5).
                    single_errors = [f"- Edit {orig_idx + 1} Failed: {e}"]
                if single_errors:
                    err_text = "\n".join(single_errors)
                    failed_list.append((orig_idx, err_text))
                    if applied_edits > 0 and not partial:
                        hint = (
                            f"\n  Note: {applied_edits} earlier edit(s) in this batch validated "
                            "against the intermediate document state; because this batch failed, it "
                            "was rolled back and nothing was saved. Batches apply sequentially — "
                            "each edit must target the document text as it reads AFTER the preceding "
                            "edits (e.g. target the replacement text an earlier edit introduced, not "
                            "the original wording)."
                        )
                        single_errors = [err + hint for err in single_errors]
                    validation_errors.extend(single_errors)
                    skipped_edits += 1
                    # Punctuation-anchor warning is failure-context only; on
                    # success the redline preview reports the change cleanly.
                    warning = self._check_punctuation_warning(getattr(edit, "target_text", ""))
                    reports_by_input[k] = {
                        "status": "failed",
                        "type": getattr(edit, "type", "modify"),
                        "target_text": truncate_middle(getattr(edit, "target_text", ""), REPORT_ECHO_CAP),
                        "new_text": truncate_middle(self._report_new_text(edit), REPORT_ECHO_CAP),
                        "comment": getattr(edit, "comment", None),
                        "warning": warning,
                        "error": "\n".join(single_errors),
                        "critic_markup": None,
                        "clean_text": None,
                    }
                    continue

                e_applied, e_skipped = self.apply_edits([edit], page_offsets=page_offsets)
                applied_edits += e_applied
                skipped_edits += e_skipped
                # Refresh projections BEFORE building the report so the
                # preview slices the actual post-apply document state (F6).
                if e_applied > 0:
                    self._refresh_after_sequential_edit()
                rep = self._build_edit_report(edit)
                reports_by_input[k] = rep
                if rep.get("status") == "failed":
                    failed_list.append((orig_idx, rep.get("error") or "Failed to apply edit"))

            if not partial and validation_errors:
                # Transactional rejection: undo everything this batch already
                # applied — its edits AND its review actions — before raising.
                self._restore_from_snapshot(pre_batch_snapshot)
                self._verify_rollback(pre_batch_fingerprint)
                raise BatchValidationError(
                    validation_errors,
                    failed=failed_list,
                )

            edits_reports = [r for r in reports_by_input if r is not None]

        # Cross-edit advisory: individually legal deletions can still add up to
        # a sentence that reads as gibberish once accepted. Runs only after the
        # batch has committed, and only ever appends to skipped_details.
        if applied_edits > 0:
            try:
                self._warn_stranded_comment_anchors(pre_batch_revision_id)
            except Exception:
                # An advisory must never be able to fail a committed batch.
                logger.debug("stranded_comment_anchor_check_failed", exc_info=True)

        from adeu import __version__

        failed_objs = [{"index": idx, "reason": reason, "error": reason} for idx, reason in failed_list]
        status_str = "partial" if (partial and failed_list) else "ok"

        return {
            "status": status_str,
            "failed": failed_objs,
            "actions_applied": applied_actions,
            "actions_skipped": skipped_actions,
            # Actions whose target was already resolved by an earlier action
            # of this batch (via its replacement pair): consistent no-ops,
            # never counted as applied — every reported "applied" action
            # causes an observable state transition (ADEU-QA-004).
            "actions_already_resolved": already_resolved_actions,
            "edits_applied": applied_edits,
            "edits_skipped": skipped_edits,
            # edits_applied counts change OBJECTS; this is the total number of
            # document occurrences they modified (match_mode="all" fan-out),
            # so automation never has to guess which of the two a count means
            # (QA 2026-07-19 F-21).
            "occurrences_modified": sum((r.get("occurrences_modified") or 0) for r in edits_reports),
            "skipped_details": self.skipped_details,
            "edits": edits_reports,
            "author_impersonation_warning": author_impersonation_warning,
            # spec-gates §5: an override that was actually exercised is
            # disclosed in the report header. Silence here would let a batch
            # bypass a safety rail with no trace for the human reviewing it.
            "overrides_note": overrides_note(self.gate_overrides, self._overridden_controls),
            "engine": "python",
            "version": __version__,
        }

    @staticmethod
    def _record_used_revision_ids(edit: Any, *ids: Optional[str]) -> None:
        """
        Remembers the revision ids an applied edit wrote into the document —
        on the edit itself and on its parent (fan-out sub-edits report through
        the parent). The post-apply preview builder locates the edit's spans
        by these ids (F6, QA 2026-07-23).
        """
        real_ids = [i for i in ids if i]
        if not real_ids:
            return
        for target in (edit, getattr(edit, "_parent_edit_ref", None)):
            if target is None:
                continue
            used = getattr(target, "_used_revision_ids", None)
            if used is not None:
                used.extend(real_ids)

    @staticmethod
    def _derive_internal_op(edit: ModifyText) -> str:
        """The operation `_apply_single_edit_indexed` will run for this edit."""
        op = edit._internal_op
        if op is None:
            if not edit.target_text and edit.new_text:
                op = EditOperationType.INSERTION
            elif edit.target_text and not edit.new_text:
                op = EditOperationType.DELETION
            else:
                op = EditOperationType.MODIFICATION
        return op

    def _reserve_revision_ids(self, resolved_edits: List[Tuple[Any, Any]]) -> None:
        """
        Assigns each resolved sub-edit its revision id(s) in ASCENDING
        document order (first occurrence gets the lowest ids; within one
        occurrence the del id precedes the ins id), before the descending
        apply sweep mutates anything (F20, QA 2026-07-23). Ids reserved for
        sub-edits that later fail or are skipped stay unused — gaps are fine,
        reverse-reading ids are not.
        """
        for edit, _orig_new in sorted(resolved_edits, key=lambda x: x[0]._resolved_start_idx or 0):
            if isinstance(edit, InsertTableRow):
                edit._reserved_ins_id = self._get_next_id()
                continue
            if isinstance(edit, DeleteTableRow):
                edit._reserved_del_id = self._get_next_id()
                continue
            op = self._derive_internal_op(edit)
            if op == EditOperationType.PARAGRAPH_REPLACE:
                # One shared id for both sides (mirrors _apply_paragraph_replace).
                shared_id = self._get_next_id()
                edit._reserved_del_id = shared_id
                edit._reserved_ins_id = shared_id
            elif op == EditOperationType.DELETION:
                edit._reserved_del_id = self._get_next_id()
            elif op == EditOperationType.INSERTION:
                edit._reserved_ins_id = self._get_next_id()
            elif op == EditOperationType.MODIFICATION:
                edit._reserved_del_id = self._get_next_id()
                edit._reserved_ins_id = self._get_next_id()
            # COMMENT_ONLY / URL_RETARGET consume no revision ids.

    # ------------------------------------------------------------------
    # set_field (CC-5, spec-set-field.md)
    # ------------------------------------------------------------------

    def _field_entries(self) -> List[Any]:
        """The ledger rows for the CURRENT document state.

        Deliberately re-collected whenever the projection has been rebuilt: a
        `set_field` earlier in the batch may have filled, cleared or unwrapped
        a control, and resolving a later one against a stale ledger would
        target an offset that no longer means what it did.
        """
        from adeu.fields import collect_fields

        cached: Optional[Tuple[str, List[Any]]] = self._field_entries_cache
        if cached is not None and cached[0] is self.mapper.full_text:
            return cached[1]
        entries = collect_fields(self.doc, self.mapper.full_text, None)
        self._field_entries_cache = (self.mapper.full_text, entries)
        return entries

    def _resolve_set_field_targets(self, edit: "SetField") -> List[Any]:
        """The controls this `set_field` names, or a FieldResolutionError."""
        from adeu.fields import resolve_field

        return resolve_field(self._field_entries(), edit.field, edit.match_mode)

    def _sdt_info_for_ordinal(self, ordinal: int) -> Any:
        return next(
            (i for i in getattr(self.mapper, "_sdt_infos", {}).values() if i.ordinal == ordinal),
            None,
        )

    def _cc_content_range(self, ordinal: int) -> Optional[Tuple[int, int]]:
        """The projection offsets BETWEEN this control's anchor pair.

        `None` when the control does not anchor (spec §1 leaves groups,
        repeating sections and nested-rich-text ledger-only), which is the
        signal that it has no single editable content span.
        """
        self._field_label_at(0)  # builds _cc_anchor_pairs if cold
        for start, end, ord_ in self._cc_anchor_pairs or []:
            if ord_ == ordinal:
                return (start, end)
        return None

    def _resolve_set_field(
        self,
        edit: "SetField",
        resolved_edits: List[Tuple[Union[ModifyText, InsertTableRow, DeleteTableRow], Any]],
    ) -> None:
        """Desugar one `set_field` into pinned `ModifyText` sub-edits.

        This is the whole design of CC-5 in one method. `set_field` writes
        nothing itself: it performs the untracked teardown Word performs
        (placeholder state, §4.1-4.2), then hands the actual content change to
        the ordinary edit pipeline as a position-pinned `ModifyText`. That is
        what makes A4.12 true by construction — the gates, atomicity, author
        resolution and reporting all see a normal edit, so `set_field` cannot
        acquire a special pass through any of them by accident.
        """
        from adeu.fields import FieldResolutionError
        from adeu.utils.field_write import (
            clear_placeholder,
            find_bound_store,
            option_is_listed,
            parse_iso_date,
            refuse_class,
            refuse_value,
            render_date,
            resolve_option,
            sdt_content,
            set_dropdown_last_value,
            set_full_date,
            write_bound_value,
        )

        try:
            hits = self._resolve_set_field_targets(edit)
        except FieldResolutionError as fe:
            edit._applied_status = False
            edit._error_msg = str(fe)
            self.skipped_details.append(f"- {fe}")
            return

        # Phase 0: refuse before touching anything. Class first (A4.11), then
        # the structure rules (A4.7). Both are checked for EVERY target before
        # any is written, so a match_mode="all" fan-out cannot half-apply and
        # leave the document in a state no single call could have produced.
        for entry in hits:
            info = self._sdt_info_for_ordinal(entry.ordinal)
            cls = info.cls if info is not None else entry.cls_word
            msg = refuse_class(cls, entry.ordinal)
            if msg is None and info is not None:
                msg = refuse_value(info, entry.ordinal, edit.value)
            if msg is not None:
                edit._applied_status = False
                edit._error_msg = msg
                self.skipped_details.append(f"- {msg}")
                return

        # Phase 1: the untracked teardown, for every target, before any
        # offsets are read. Clearing a placeholder deletes the ghost text from
        # the projection, so ranges computed before it would be stale by
        # exactly the length of the prompt.
        touched = False
        for entry in hits:
            info = self._sdt_info_for_ordinal(entry.ordinal)
            if info is not None and info.showing_placeholder:
                if clear_placeholder(info):
                    touched = True
        if touched:
            self._mutated_since_load = True
            self._invalidate_projection_caches()

        # Phase 1b: per-class value translation. The caller's string is not
        # always what gets written: a dropdown's `w:value` resolves to its
        # display text, and a date renders through the control's own format.
        # Computed per target because two controls sharing a tag may declare
        # different formats or option lists.
        effective: dict = {}
        notes: dict = {}
        for entry in hits:
            info = self._sdt_info_for_ordinal(entry.ordinal)
            if info is None:
                continue
            if info.cls in ("dropdown", "combobox"):
                display, err = resolve_option(info, edit.value)
                if err is not None:
                    msg = f"CC:{entry.ordinal}: {err}"
                    edit._applied_status = False
                    edit._error_msg = msg
                    self.skipped_details.append(f"- {msg}")
                    return
                effective[entry.ordinal] = display
                if info.cls == "combobox" and not option_is_listed(info, display):
                    notes[entry.ordinal] = f"'{display}' is not in the option list"
            elif info.cls == "date":
                parts = parse_iso_date(edit.value)
                if parts is None:
                    msg = (
                        f"CC:{entry.ordinal} is a date control; '{edit.value}' is not a date. "
                        "Use the canonical YYYY-MM-DD form (e.g. 2026-03-01)."
                    )
                    edit._applied_status = False
                    edit._error_msg = msg
                    self.skipped_details.append(f"- {msg}")
                    return
                text, unsupported = render_date(parts, info.date_format)
                effective[entry.ordinal] = text
                if unsupported:
                    notes[entry.ordinal] = (
                        f"the control's date format '{info.date_format}' is not supported in v1; "
                        f"wrote the canonical {text}"
                    )

        # Phase 2: checkboxes are written directly; everything else desugars.
        def _cls_of(e: Any) -> str:
            info = self._sdt_info_for_ordinal(e.ordinal)
            return info.cls if info is not None else e.cls_word

        direct = [e for e in hits if _cls_of(e) == "checkbox"]
        if direct:
            ok = True
            for entry in direct:
                info = self._sdt_info_for_ordinal(entry.ordinal)
                if info is None or not self._apply_checkbox_set_field(edit, entry, info):
                    ok = False
            if ok:
                self._invalidate_projection_caches()
            return

        # Phase 2b: one pinned sub-edit per target.
        for entry in hits:
            span = self._cc_content_range(entry.ordinal)
            if span is None:
                msg = (
                    f"CC:{entry.ordinal} is a {entry.cls_word} and is not a value-bearing field. "
                    "set_field fills text, rich-text, dropdown, combobox, date and checkbox controls."
                )
                edit._applied_status = False
                edit._error_msg = msg
                self.skipped_details.append(f"- {msg}")
                return

            start, end = span
            current = self.mapper.full_text[start:end]
            value = effective.get(entry.ordinal, edit.value)
            sub = ModifyText(
                type="modify",
                target_text=current,
                new_text=value,
                comment=edit.comment,
            )
            # Always atomic, comment or not (spec §3): a fill is one logical
            # act, and word-splitting it would scatter a single field update
            # across several review entries.
            sub._internal_op = (
                EditOperationType.INSERTION
                if not current
                else (EditOperationType.DELETION if not edit.value else EditOperationType.MODIFICATION)
            )
            sub._resolved_start_idx = start
            sub._active_mapper_ref = self.mapper
            sub._parent_edit_ref = edit
            if not current:
                # Nothing left inside the control to anchor to; name the host.
                info = self._sdt_info_for_ordinal(entry.ordinal)
                if info is not None:
                    sub._insert_host_el = sdt_content(info.element)
            if edit._resolved_start_idx is None:
                edit._resolved_start_idx = start
                edit._resolved_proxy_edit = sub
            # The attribute syncs ride along with the content change and take
            # no revision of their own (spec §5, the URL_RETARGET class): the
            # visible text carries the redline, and a second revision for the
            # attribute would show a reviewer two changes for one act.
            info = self._sdt_info_for_ordinal(entry.ordinal)
            if info is not None:
                if info.cls in ("dropdown", "combobox"):
                    set_dropdown_last_value(info, value)
                elif info.cls == "date":
                    parts = parse_iso_date(edit.value)
                    if parts is not None:
                        set_full_date(info, parts)
            # A bound control dual-writes: the tracked content change above,
            # and the CustomXML node it mirrors. The store WINS ON OPEN
            # (CC-6(e)), so content-only writing to a bound control is data
            # loss with extra steps - Word silently rewrites the content back
            # from the store, discarding the edit with no revision to show
            # for it. A store that cannot be resolved downgrades to
            # content-only plus a warning, because dangling bindings exist in
            # the wild and refusing the edit would be worse than disclosing.
            if info is not None and info.bound:
                store = find_bound_store(self.doc, info.store_item_id)
                wrote = store is not None and write_bound_value(store, info.binding_xpath, value)
                if wrote:
                    notes[entry.ordinal] = f"bound store {info.binding_xpath} updated to match" + (
                        f"; {notes[entry.ordinal]}" if entry.ordinal in notes else ""
                    )
                else:
                    notes[entry.ordinal] = (
                        f"WARNING: this field is bound to {info.binding_xpath} but the data store "
                        "could not be resolved, so only the visible text was updated. If the store "
                        "is restored later, Word will overwrite this edit from it."
                        + (f"; {notes[entry.ordinal]}" if entry.ordinal in notes else "")
                    )

            note = notes.get(entry.ordinal)
            if note:
                existing = edit._warning
                edit._warning = f"{existing}; {note}" if existing else note
            # A `w:temporary` control does not survive being edited: Word
            # unwraps it on ANY content change, tracked or not (CC-6(c)). The
            # unwrap is one-way - the revision outlives the wrapper, so
            # rejecting the fill restores the old text but not the control -
            # and matching Word here is what keeps a round trip stable.
            if info is not None and info.temporary:
                sub._unwrap_sdt_after = info.element
                existing_note = notes.get(entry.ordinal)
                unwrap_note = "this control was temporary and has been unwrapped, as Word does on any edit"
                notes[entry.ordinal] = f"{existing_note}; {unwrap_note}" if existing_note else unwrap_note
                note = notes.get(entry.ordinal)
                if note:
                    edit._warning = note

            resolved_edits.append((sub, value))

    def _apply_checkbox_set_field(self, edit: "SetField", entry: Any, info: Any) -> bool:
        """The checkbox fill (A4.6), which cannot desugar into a ModifyText.

        A checkbox has no anchor pair and no editable content span - it
        projects as virtual `[x]` text - so there is no offset for a pinned
        edit to target. It is written directly instead: the state attribute
        flips silently, and the glyph swap carries the redline.

        `w:ins` goes BEFORE `w:del`, which is Word's own order (CC-6(b)) and
        is visible rather than cosmetic: the projection reads document order,
        so the reverse would render `{--Y--}{++N++}` where Word renders
        `{++N++}{--Y--}`.
        """
        from copy import deepcopy

        from adeu.utils.field_write import (
            checkbox_glyph,
            glyph_run,
            parse_checkbox_value,
            set_checkbox_checked,
        )

        checked = parse_checkbox_value(edit.value)
        if checked is None:
            msg = (
                f"CC:{entry.ordinal} is a checkbox; '{edit.value}' is neither checked nor unchecked. "
                "Use true/false (also accepted: x, [x], 1, 0, yes, no)."
            )
            edit._applied_status = False
            edit._error_msg = msg
            self.skipped_details.append(f"- {msg}")
            return False

        old_run = glyph_run(info)
        char, font = checkbox_glyph(info, checked)

        new_run = deepcopy(old_run) if old_run is not None else create_element("w:r")
        assert new_run is not None
        for t in new_run.findall(qn("w:t")):
            new_run.remove(t)
        t_el = create_element("w:t")
        t_el.text = char
        new_run.append(t_el)
        if font:
            rpr = new_run.find(qn("w:rPr"))
            if rpr is None:
                rpr = create_element("w:rPr")
                new_run.insert(0, rpr)
            for existing in rpr.findall(qn("w:rFonts")):
                rpr.remove(existing)
            fonts = create_element("w:rFonts")
            for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                fonts.set(qn(attr), font)
            rpr.insert(0, fonts)

        ins = self._create_track_change_tag("w:ins", reuse_id=self._get_next_id())
        ins.append(new_run)

        parent = old_run.getparent() if old_run is not None else None
        if parent is None:
            from adeu.utils.field_write import sdt_content

            parent = sdt_content(info.element)
            if parent is None:
                return False
            parent.append(ins)
        else:
            # `parent` is non-None only when old_run is, so the old glyph is
            # always available to wrap as the deletion half of the pair.
            assert old_run is not None
            index = list(parent).index(old_run)
            parent.insert(index, ins)
            del_tag = self._create_track_change_tag("w:del", reuse_id=self._get_next_id())
            del_run = deepcopy(old_run)
            for t in del_run.findall(qn("w:t")):
                t.tag = qn("w:delText")
            del_tag.append(del_run)
            parent.replace(old_run, del_tag)

        set_checkbox_checked(info, checked)
        self._mutated_since_load = True
        edit._applied_status = True
        edit._occurrences_modified = (edit._occurrences_modified or 0) + 1
        return True

    def _empty_control_fill_host(self, mapper: Any, offset: int) -> Optional[Any]:
        """`w:sdtContent` when `offset` is the content position of an EMPTY control.

        This is the "empty-pair insertion" surface (A4.10): the sanctioned way
        to fill a field with a text-first edit is to type between its anchors,
        which produces an insertion at the exact offset where the pair's open
        and close tokens meet. Offsets alone cannot express "inside" there -
        the control contains no run to anchor to - so without this the value
        lands NEXT TO the field, and the document reads
        `Acme Ltd.{#cc:2}{#/cc:2}` with the control still empty.

        Shared with `set_field` deliberately: A4.10 requires the two routes to
        produce identical XML, and the only way to guarantee that is for them
        to run the same code rather than to agree by inspection.
        """
        from adeu.utils.field_write import clear_placeholder, sdt_content

        text = getattr(mapper, "full_text", "") or ""
        if not text:
            return None
        opens: dict = {}
        for m in _CC_ANCHOR_SCAN_RE.finditer(text):
            ordinal = int(m.group(2))
            if m.group(1):
                if ordinal in opens:
                    _s, open_end = opens.pop(ordinal)
                    if open_end == m.start() == offset:
                        info = self._sdt_info_for_ordinal(ordinal)
                        if info is None:
                            return None
                        # Same untracked teardown Word performs, so a
                        # text-first fill of a placeholder control does not
                        # leave the ghost styling behind (CC-6(a)).
                        if info.showing_placeholder and clear_placeholder(info):
                            self._mutated_since_load = True
                            self._cc_anchor_pairs = None
                            self._field_entries_cache = None
                        return sdt_content(info.element)
            else:
                opens[ordinal] = (m.start(), m.end())
        return None

    def _invalidate_projection_caches(self) -> None:
        """Drop everything keyed on the projection after an untracked write."""
        self.mapper._build_map()
        self._cc_anchor_pairs = None
        self._field_entries_cache = None

    def apply_edits(
        self,
        edits: List[Union[ModifyText, InsertTableRow, DeleteTableRow, SetField]],
        page_offsets: Optional[List[int]] = None,
        index_offset: int = 0,
    ) -> tuple[int, int]:
        if page_offsets is None:
            from adeu.pagination import paginate, split_structural_appendix

            body_text, _ = split_structural_appendix(self.mapper.full_text)
            page_offsets = paginate(body_text, "").body_page_offsets

        # Conservative mutation marker for the lazy pre-batch snapshot: an
        # application ATTEMPT can write to the tree even when the edit is
        # ultimately counted skipped (partial sub-edit failures roll back via
        # the batch snapshot, not per-edit). Flag before the first write.
        if edits:
            self._mutated_since_load = True

        applied = 0
        skipped = 0

        for edit in edits:
            edit._applied_status = False
            edit._error_msg = None
            edit._any_sub_failure = False

        # SetField is absent from this union on purpose: it resolves INTO
        # pinned ModifyText sub-edits and never reaches the apply layer
        # itself, which is what lets the gates and the reporting treat a fill
        # as the ordinary edit it becomes.
        resolved_edits: List[Tuple[Union[ModifyText, InsertTableRow, DeleteTableRow], Any]] = []

        # Pre-resolve phase: locate all edits against initial clean state
        for idx, edit in enumerate(edits):
            edit_idx = index_offset + idx
            if isinstance(edit, SetField):
                # Before the pinned branch: `set_field` addresses its target
                # by id, so a caller-supplied offset would be meaningless -
                # and taking the pinned path would hand the apply layer a
                # change with no target_text at all.
                self._resolve_set_field(edit, resolved_edits)
                if edit._error_msg:
                    skipped += 1
            elif edit._resolved_start_idx is not None or edit._match_start_index is not None:
                if edit._resolved_start_idx is None:
                    edit._resolved_start_idx = edit._match_start_index
                # CC-14: caller-pinned edits skip resolution entirely and go
                # straight to the apply layer, so the shared-trailing-mark
                # normalisation the resolution path performs has to happen here
                # too. make_edits_self_contained widens a target to make it
                # unique and routinely produces this shape WITH a pinned index
                # (129 of 4,000 randomised paragraph edits); such a batch
                # applied in-process — no JSON round trip to drop the index —
                # silently lost a paragraph break. Structural ops carry an
                # explicit _internal_op and are left alone.
                if (
                    isinstance(edit, ModifyText)
                    and getattr(edit, "_internal_op", None) is None
                    and edit.target_text
                    and edit.new_text
                ):
                    edit.target_text, edit.new_text = _trim_shared_trailing_paragraph_mark(
                        edit.target_text, edit.new_text
                    )
                # Caller-pinned indices (diff output) are CLEAN-view character
                # offsets; the raw-view mapper fallback would mis-anchor them
                # on documents whose views differ (AP-05).
                if edit._active_mapper_ref is None:
                    if not self.clean_mapper:
                        self.clean_mapper = DocumentMapper(self.doc, clean_view=True)
                    edit._active_mapper_ref = self.clean_mapper
                # A pure insertion landing exactly between an empty control's
                # anchors is a field fill expressed as text (A4.10).
                if (
                    isinstance(edit, ModifyText)
                    and not edit.target_text
                    and edit.new_text
                    and edit._insert_host_el is None
                ):
                    host = self._empty_control_fill_host(edit._active_mapper_ref, edit._resolved_start_idx or 0)
                    if host is not None:
                        edit._insert_host_el = host
                resolved_edits.append((edit, getattr(edit, "new_text", None)))
            elif isinstance(edit, (InsertTableRow, DeleteTableRow)):
                # Simplified resolution for structural edits
                matches = self.mapper.drop_virtual_only_matches(self.mapper.find_all_match_indices(edit.target_text))
                resolved_mapper = self.mapper
                if not matches:
                    # Try clean view
                    if not self.clean_mapper:
                        self.clean_mapper = DocumentMapper(self.doc, clean_view=True)
                    matches = self.clean_mapper.drop_virtual_only_matches(
                        self.clean_mapper.find_all_match_indices(edit.target_text)
                    )
                    resolved_mapper = self.clean_mapper

                if matches:
                    match_mode = getattr(edit, "match_mode", "strict")

                    unique_matches = []
                    seen_trs = set()

                    for m_start, m_len in matches:
                        anchor_run, anchor_paragraph = resolved_mapper.get_insertion_anchor(m_start, rebuild_map=False)
                        target_element = None
                        if anchor_run:
                            target_element = anchor_run._element
                        elif anchor_paragraph:
                            target_element = anchor_paragraph._element

                        tr = None
                        curr = target_element
                        while curr is not None:
                            if curr.tag == qn("w:tr"):
                                tr = curr
                                break
                            curr = curr.getparent()

                        if tr is not None and tr not in seen_trs:
                            seen_trs.add(tr)
                            unique_matches.append((m_start, m_len))

                    if unique_matches:
                        matches_to_apply = unique_matches
                        if match_mode in ("strict", "first"):
                            matches_to_apply = unique_matches[:1]

                        if match_mode == "all" or len(matches_to_apply) > 1:
                            for m_start, _m_len in matches_to_apply:
                                sub_edit = deepcopy(edit)
                                sub_edit._resolved_start_idx = m_start
                                sub_edit._active_mapper_ref = resolved_mapper
                                sub_edit._parent_edit_ref = edit
                                resolved_edits.append((sub_edit, None))
                        else:
                            edit._resolved_start_idx = matches_to_apply[0][0]
                            edit._active_mapper_ref = resolved_mapper
                            resolved_edits.append((edit, None))
                    else:
                        skipped += 1
                        edit._applied_status = False
                        target_snippet = edit.target_text.strip()[:40]
                        msg = f"- Failed to locate row target: '{target_snippet}...'"
                        self.skipped_details.append(msg)
                        edit._error_msg = msg
                else:
                    skipped += 1
                    target_snippet = edit.target_text.strip()[:40]
                    self.skipped_details.append(f"- Failed to apply structural edit targeting: '{target_snippet}...'")
            else:
                try:
                    resolved = self._pre_resolve_heuristic_edit(edit, index_offset=edit_idx)
                except RegexTimeoutError as e:
                    # Direct apply_edits callers bypass validate_edits; the
                    # time budget must still fail cleanly here (QA F5).
                    skipped += 1
                    edit._applied_status = False
                    msg = f"- Failed to apply edit targeting: '{(edit.target_text or '')[:40]}...' ({e})"
                    self.skipped_details.append(msg)
                    edit._error_msg = msg
                    continue
                if resolved:
                    if isinstance(resolved, list):
                        for r in resolved:
                            r._resolved_start_idx = r._match_start_index
                            r._parent_edit_ref = edit
                            if edit._resolved_start_idx is None:
                                edit._resolved_start_idx = r._resolved_start_idx
                            if getattr(edit, "_resolved_proxy_edit", None) is None:
                                edit._resolved_proxy_edit = r
                            resolved_edits.append((r, r.new_text))
                    else:
                        resolved._resolved_start_idx = resolved._match_start_index
                        resolved._parent_edit_ref = edit
                        edit._resolved_start_idx = resolved._resolved_start_idx
                        edit._resolved_proxy_edit = resolved
                        resolved_edits.append((resolved, edit.new_text))
                else:
                    skipped += 1
                    edit._applied_status = False

                    # N2 Fix: Safe display text fallback for heuristic failures
                    display_text = edit.target_text or "insertion"
                    if not display_text.strip() and hasattr(edit, "_original_target_text"):
                        display_text = edit._original_target_text or "insertion"

                    target_snippet = display_text.strip()[:40]
                    if not target_snippet:
                        target_snippet = "insertion"

                    msg = f"- Failed to apply edit targeting: '{target_snippet}...'"
                    if getattr(edit, "_is_table_edit", False) or " | " in (edit.target_text or ""):
                        msg += (
                            ". (Note: Structural table changes like adding/removing rows or columns "
                            "are not supported via text replace)."
                        )
                    self.skipped_details.append(msg)
                    edit._error_msg = msg

        # Reserve revision ids in ASCENDING document order BEFORE the
        # descending apply sweep: ids minted lazily during the bottom-up sweep
        # numbered a match_mode="all" fan-out in reverse (Chg:5/6, 3/4, 1/2
        # for the 1st/2nd/3rd occurrence), making ids read as if the last
        # occurrence were edited first (F20, QA 2026-07-23). Sequential
        # separate edits already ascend and are unaffected — each apply_edits
        # call reserves after the previous call finished minting.
        self._reserve_revision_ids(resolved_edits)

        # Process all edits backwards in a single O(N) sweep to avoid index drift and map rebuilds
        resolved_edits.sort(key=lambda x: x[0]._resolved_start_idx or 0, reverse=True)

        # Snapshot preview context now, while every resolved offset still refers
        # to the untouched document. The sweep below mutates the DOM and rebuilds
        # the map, shifting offsets and injecting tracked-change markup —
        # slicing full_text at report time garbles previews (QA H1).
        for res_edit, _ in resolved_edits:
            self._capture_preview_context(res_edit)
            parent = getattr(res_edit, "_parent_edit_ref", None)
            if parent is not None:
                self._capture_parent_preview_context(parent)

        occupied_ranges: List[Tuple[int, int]] = []
        # Sub-edits split from one balanced multi-paragraph modification share a
        # _split_group_id; count the group as a single applied edit (and a single
        # occurrence), even though it touches several paragraphs.
        counted_split_groups: set = set()

        for edit, orig_new in resolved_edits:
            start = edit._resolved_start_idx or 0
            # An insert_row does not consume its anchor text — it adds an
            # adjacent row. Give it a zero-width range so several inserts
            # sharing one anchor (consecutive new rows) never flag each other
            # as overlapping.
            if isinstance(edit, InsertTableRow):
                end = start
            else:
                end = start + (len(edit.target_text) if edit.target_text else 0)

            if any(start < occ_end and end > occ_start for occ_start, occ_end in occupied_ranges):
                logger.warning(f"Skipping overlapping edit at index {start}")
                skipped += 1

                display_text = edit.target_text or "insertion"
                if not display_text.strip() and hasattr(edit, "_original_target_text"):
                    display_text = edit._original_target_text or "insertion"
                target_snippet = display_text.strip()[:40]

                msg = f"- Skipped overlapping edit targeting: '{target_snippet}...'"
                if getattr(edit, "_is_table_edit", False):
                    msg += ". (Note: Overlapping cell edits in tables must be processed in separate batches)."
                self.skipped_details.append(msg)
                edit._applied_status = False
                edit._error_msg = msg
                edit._any_sub_failure = True
                parent = getattr(edit, "_parent_edit_ref", None)
                if parent is not None:
                    parent._any_sub_failure = True
                    parent._applied_status = False
                    parent._error_msg = msg
                continue

            success = False
            if isinstance(edit, InsertTableRow):
                success = self._apply_insert_row(edit)
            elif isinstance(edit, DeleteTableRow):
                success = self._apply_delete_row(edit)
            else:
                # Never rebuild the map inside the sweep: sub-edits apply in
                # strictly descending offset order, and every DOM mutation
                # (run splits, w:del wraps, w:ins insertions, bottom-up
                # paragraph merges) happens at or above the current offset, so
                # spans below it stay valid in the stale map. Rebuilding here
                # made regex + match_mode="all" O(occurrences × document):
                # 500 matches took 78s instead of ~2s (QA 2026-07-19 F-06).
                success = self._apply_single_edit_indexed(edit, original_new_text=orig_new, rebuild_map=False)

            if success and edit._unwrap_sdt_after is not None:
                # After the content change, never before: the edit resolves
                # against offsets inside the control, and dissolving the
                # wrapper first would move them.
                from adeu.utils.content_controls import SdtInfo as _SdtInfo
                from adeu.utils.field_write import unwrap_sdt

                unwrap_sdt(_SdtInfo(element=edit._unwrap_sdt_after, cls="text"))

            if success:
                # A balanced multi-paragraph split fans one logical edit into
                # several paragraph sub-edits sharing a _split_group_id; count it
                # once. Edits with no group id (the common case) always count.
                group_id = getattr(edit, "_split_group_id", None)
                first_in_group = group_id is None or group_id not in counted_split_groups
                if first_in_group and group_id is not None:
                    counted_split_groups.add(group_id)
                if first_in_group:
                    applied += 1
                occupied_ranges.append((start, end))
                edit._applied_status = True
                parent = getattr(edit, "_parent_edit_ref", None)
                if parent is not None:
                    parent._applied_status = True
                    if first_in_group:
                        parent._occurrences_modified = getattr(parent, "_occurrences_modified", 0) + 1
                    path, page = self._get_heading_path_and_page(start, self.mapper.full_text, page_offsets)
                    pages = getattr(parent, "_pages", [])
                    if page not in pages:
                        pages.insert(0, page)
                    parent._pages = pages
                    parent._heading_path = path
                    parent._field = self._field_label_at(start)
                else:
                    if first_in_group:
                        edit._occurrences_modified = getattr(edit, "_occurrences_modified", 0) + 1
                    path, page = self._get_heading_path_and_page(start, self.mapper.full_text, page_offsets)
                    pages = getattr(edit, "_pages", [])
                    if page not in pages:
                        pages.insert(0, page)
                    edit._pages = pages
                    edit._heading_path = path
                    edit._field = self._field_label_at(start)
            else:
                skipped += 1

                display_text = edit.target_text or "insertion"
                if not display_text.strip() and hasattr(edit, "_original_target_text"):
                    display_text = edit._original_target_text or "insertion"
                target_snippet = display_text.strip()[:40]
                if not target_snippet:
                    target_snippet = "insertion"

                msg = f"- Failed to apply edit targeting: '{target_snippet}...'"
                if getattr(edit, "_is_table_edit", False):
                    msg += (
                        ". (Note: Structural table changes or overlapping cell"
                        + " edits are not supported via text replace)."
                    )
                self.skipped_details.append(msg)
                edit._applied_status = False
                edit._error_msg = msg
                edit._any_sub_failure = True
                parent = getattr(edit, "_parent_edit_ref", None)
                if parent is not None:
                    parent._any_sub_failure = True
                    if not getattr(parent, "_applied_status", False):
                        parent._applied_status = False
                        parent._error_msg = msg

        # Return LOGICAL edit counts over the caller's input list: one
        # match_mode="all" edit over N occurrences is one applied edit (its
        # occurrence count lives in _occurrences_modified / the report),
        # never N (QA 2026-07-19 F-21). An edit with any failed or skipped
        # sub-edit counts as skipped so the all-or-nothing batch contract is
        # unchanged, even when its other occurrences applied.
        applied = 0
        skipped = 0
        for input_edit in edits:
            if getattr(input_edit, "_applied_status", False) and not getattr(input_edit, "_any_sub_failure", False):
                applied += 1
            else:
                skipped += 1
        return applied, skipped

    def _apply_insert_row(self, edit: InsertTableRow) -> bool:
        start_idx = edit._resolved_start_idx if edit._resolved_start_idx is not None else edit._match_start_index
        if start_idx is None:
            return False

        # The offset must be looked up in the coordinate space it was
        # resolved in: a clean-view offset applied to the raw mapper points
        # at earlier text once tracked changes exist.
        active_mapper = edit._active_mapper_ref or self.mapper

        target_spans = [
            s for s in active_mapper.spans if s.end > start_idx and s.start < start_idx + len(edit.target_text)
        ]
        row_el = None
        if target_spans:
            # 1. Prefer real runs
            for s in target_spans:
                if s.run is not None:
                    curr = s.run._element
                    while curr is not None:
                        if curr.tag == qn("w:tr"):
                            row_el = curr
                            break
                        curr = curr.getparent()
                    if row_el is not None:
                        break

            # 2. Fall back to paragraphs (handles virtual empty-cell anchors)
            if row_el is None:
                for s in target_spans:
                    if s.paragraph is not None:
                        curr = s.paragraph._element
                        while curr is not None:
                            if curr.tag == qn("w:tr"):
                                row_el = curr
                                break
                            curr = curr.getparent()
                        if row_el is not None:
                            break

        if row_el is None:
            return False

        table_el = row_el.getparent()
        if table_el.tag != qn("w:tbl"):
            return False

        from docx.table import Table, _Row

        table = Table(table_el, table_el.getparent())

        # Create a new row by cloning the current row (to preserve formatting/cells)
        new_row_el = deepcopy(row_el)

        # Clear text from all cells in the new row
        for tc in new_row_el.xpath(".//w:tc"):
            # Clear existing paragraphs except one empty one
            for p in tc.xpath("./w:p"):
                tc.remove(p)
            tc.append(create_element("w:p"))

        # Set new cell text
        new_row = _Row(new_row_el, table)
        for i, cell_text in enumerate(edit.cells):
            if i < len(new_row.cells):
                new_row.cells[i].text = cell_text

        # Inject tracked change info (reserved id: F20 ascending pre-assignment)
        trPr = new_row_el.get_or_add_trPr()
        ins = self._create_track_change_tag("w:ins", reuse_id=edit._reserved_ins_id)
        trPr.append(ins)

        # Insert into DOM
        if edit.position == "above":
            row_el.addprevious(new_row_el)
        else:
            row_el.addnext(new_row_el)

        return True

    def _apply_delete_row(self, edit: DeleteTableRow) -> bool:
        start_idx = edit._resolved_start_idx if edit._resolved_start_idx is not None else edit._match_start_index
        if start_idx is None:
            return False

        # Same coordinate-space rule as _apply_insert_row.
        active_mapper = edit._active_mapper_ref or self.mapper

        target_spans = [
            s for s in active_mapper.spans if s.end > start_idx and s.start < start_idx + len(edit.target_text)
        ]
        row_el = None
        if target_spans:
            # 1. Prefer real runs
            for s in target_spans:
                if s.run is not None:
                    curr = s.run._element
                    while curr is not None:
                        if curr.tag == qn("w:tr"):
                            row_el = curr
                            break
                        curr = curr.getparent()
                    if row_el is not None:
                        break

            # 2. Fall back to paragraphs
            if row_el is None:
                for s in target_spans:
                    if s.paragraph is not None:
                        curr = s.paragraph._element
                        while curr is not None:
                            if curr.tag == qn("w:tr"):
                                row_el = curr
                                break
                            curr = curr.getparent()
                        if row_el is not None:
                            break

        if row_el is None:
            return False

        # Instead of removing, we mark as deleted (reserved id: F20
        # ascending pre-assignment)
        trPr = row_el.get_or_add_trPr()
        del_el = self._create_track_change_tag("w:del", reuse_id=edit._reserved_del_id)
        trPr.append(del_el)

        return True

    def _is_row_fully_deleted(self, row_el, start_idx: int, length: int, active_mapper) -> bool:
        # Find all active runs currently under row_el
        active_runs = []
        for r_el in row_el.findall(".//" + qn("w:r")):
            parent = r_el.getparent()
            is_deleted = False
            while parent is not None and parent != row_el:
                if parent.tag == qn("w:del"):
                    is_deleted = True
                    break
                parent = parent.getparent()
            if not is_deleted:
                active_runs.append(r_el)

        # If there are still active runs, the row is not fully deleted
        if active_runs:
            return False

        # Since row_el was collected in seen_rows, we know it was targeted.
        return True

    def _mark_fully_deleted_rows_in_range(
        self, del_elems, virtual_spans, start_idx: int, length: int, active_mapper, del_id: Optional[str]
    ) -> None:
        seen_rows = set()
        for del_elem in del_elems:
            curr = del_elem
            row_el = None
            while curr is not None:
                if curr.tag == qn("w:tr"):
                    row_el = curr
                    break
                curr = curr.getparent()
            if row_el is not None and row_el not in seen_rows:
                seen_rows.add(row_el)

        for span in virtual_spans:
            if span.paragraph:
                curr = span.paragraph._element
                row_el = None
                while curr is not None:
                    if curr.tag == qn("w:tr"):
                        row_el = curr
                        break
                    curr = curr.getparent()
                if row_el is not None and row_el not in seen_rows:
                    seen_rows.add(row_el)

        for row_el in seen_rows:
            if self._is_row_fully_deleted(row_el, start_idx, length, active_mapper):
                trPr = row_el.get_or_add_trPr()
                if trPr.find(qn("w:del")) is None:
                    del_el = self._create_track_change_tag("w:del", reuse_id=del_id)
                    trPr.append(del_el)

    def _maybe_paragraph_replace(
        self,
        edit: ModifyText,
        start_idx: int,
        match_len: int,
        active_mapper: DocumentMapper,
    ) -> Optional[ModifyText]:
        """
        If the edit's target spans exactly one full paragraph (its heading
        prefix included), and new_text is a single paragraph involving a
        heading style change, returns a synthesized ModifyText tagged with
        the PARAGRAPH_REPLACE internal op. Otherwise returns None.

        See _pre_resolve_heuristic_edit for context on why this fast path
        exists.
        """
        new_text = edit.new_text or ""
        if not new_text:
            return None

        # new_text must be a single paragraph — '\n\n' would mean
        # multi-paragraph and is out of scope for this fast path.
        if "\n\n" in new_text:
            return None

        # Identify the paragraph whose full projected span equals the
        # matched range. We look for a paragraph p such that:
        #   - the leftmost span belonging to p starts at start_idx
        #   - the rightmost span belonging to p ends at start_idx + match_len
        end_idx = start_idx + match_len
        target_para = None

        # Spans for each paragraph: collect min start / max end.
        # We only consider spans that are tagged with a paragraph (real or
        # virtual prefix), and we require coverage by both endpoints.

        # Per paragraph: [lo, hi] over every span, plus real_lo over spans
        # backed by an actual run. They differ when the projection prepends a
        # virtual marker — a Heading 1 paragraph "2. Confidentiality" projects
        # as "# 2. Confidentiality", so an edit targeting the BARE heading text
        # starts at real_lo, not lo. That is still a whole-paragraph edit.
        # Requiring lo alone rejected it, and the edit fell through to the
        # inline path, which wrote the replacement marker into the document as
        # literal text ("{++## ++}2. Confidentiality", style left untouched).
        # The TypeScript engine accepted it, so this was also a parity gap.
        bounds: Dict[Any, List[Optional[int]]] = defaultdict(_empty_bounds)
        real_lows: Dict[Any, int] = {}
        for s in active_mapper.spans:
            if s.paragraph is None:
                continue
            # Skip the inter-paragraph "\n\n" virtual separator
            # (run is None and text == "\n\n" means
            # the separator was attached to s.paragraph as the trailing
            # newline; we exclude it from the boundary calculation).
            if s.run is None and s.text == "\n\n":
                continue
            lo, hi = bounds[s.paragraph]
            if lo is None or s.start < lo:
                lo = s.start
            if hi is None or s.end > hi:
                hi = s.end
            bounds[s.paragraph] = [lo, hi]
            if s.run is not None:
                prev_real = real_lows.get(s.paragraph)
                if prev_real is None or s.start < prev_real:
                    real_lows[s.paragraph] = s.start

        for p, (lo, hi) in bounds.items():
            if hi != end_idx:
                continue
            if lo == start_idx or real_lows.get(p) == start_idx:
                target_para = p

                break

        if target_para is None:
            return None

        # At least one side must carry a block marker. If the source paragraph
        # is unstyled and new_text names no block style, the existing
        # inline-edit path handles it correctly — don't intercept.
        #
        # This gate used to admit HEADINGS ONLY, which broke dual-engine
        # parity: for a whole-paragraph replace like modify("Alpha", "- Beta")
        # the TypeScript engine consumed the "- " and restyled the paragraph to
        # a bullet, while Python fell through to the inline path and inserted a
        # literal "- Beta" with no style. The marker leaked into the document as
        # text. "* ", "- " and "1. " are block markers exactly as "# " is, so
        # every style _parse_markdown_style recognises is admitted here.
        from adeu.utils.docx import is_heading_paragraph

        source_is_heading = is_heading_paragraph(target_para)

        # Detect whether new_text starts with any block marker (heading,
        # bullet or numbered), not just a heading one.
        new_clean, new_style = self._parse_markdown_style(new_text)

        if not source_is_heading and new_style is None:
            return None

        # Synthesize a proxy edit pointing at the original paragraph.
        proxy_edit = ModifyText(
            type="modify",
            target_text=edit.target_text,
            new_text=edit.new_text,
            comment=edit.comment,
        )
        proxy_edit._match_start_index = start_idx
        proxy_edit._internal_op = EditOperationType.PARAGRAPH_REPLACE
        proxy_edit._resolved_start_idx = start_idx
        proxy_edit._active_mapper_ref = active_mapper
        # Stash the resolved paragraph for the apply step.
        proxy_edit._target_paragraph = target_para  # type: ignore[attr-defined]
        return proxy_edit

    @staticmethod
    def _restore_matched_typography(actual_doc_text: str, caller_target: str, new_text: str) -> str:
        """
        Undoes the typographic drift a forgiving MATCH introduces into a
        literal WRITE (BUG_comment_threading_anchoring_and_typography.md B4).

        `DocumentMapper` matches a target with straight quotes against a
        document with curly ones — deliberately, because that is how LLMs
        normalize typography. The apply path then word-diffs the document's
        real slice against the caller's `new_text`, so each forgiven character
        became a genuine `w:del`/`w:ins` pair in text the caller never
        targeted. Restore the document's characters wherever the caller changed
        nothing.

        The guard is the asymmetry itself: only when the document slice carries
        smart typography that the caller's own target does NOT is the match
        known to have been forgiving. A caller quoting the document's real
        characters (`“Confidential”` → `"Confidential"`) is asking for the
        change and still gets it.
        """
        if not new_text or not actual_doc_text:
            return new_text
        if not has_smart_quotes(actual_doc_text):
            return new_text
        if has_smart_quotes(caller_target or ""):
            return new_text
        return restore_document_typography(actual_doc_text, new_text)

    def _pre_resolve_heuristic_edit(
        self, edit: ModifyText, index_offset: int = 0
    ) -> Union[ModifyText, List[ModifyText], None]:
        if not edit.target_text:
            return None

        is_regex = getattr(edit, "regex", False)
        match_mode = getattr(edit, "match_mode", "strict")

        matches = self.mapper.drop_virtual_only_matches(
            self.mapper.find_all_match_indices(edit.target_text, is_regex=is_regex)
        )
        active_mapper = self.mapper

        if not matches:
            if not self.clean_mapper:
                self.clean_mapper = DocumentMapper(self.doc, clean_view=True)
            matches = self.clean_mapper.drop_virtual_only_matches(
                self.clean_mapper.find_all_match_indices(edit.target_text, is_regex=is_regex)
            )
            if matches:
                active_mapper = self.clean_mapper
            else:
                return None

        live_matches = []
        for s, match_len in matches:
            real_spans = [
                span
                for span in active_mapper.spans
                if span.run is not None and span.end > s and span.start < s + match_len
            ]
            # Virtual-only matches were already dropped above; here we only
            # skip matches buried entirely inside tracked deletions.
            if not real_spans or any(not span.del_id for span in real_spans):
                live_matches.append((s, match_len))

        if not live_matches:
            return None

        if match_mode in ("strict", "first"):
            live_matches = live_matches[:1]

        all_sub_edits = []

        for start_idx, match_len in live_matches:
            actual_doc_text = active_mapper.full_text[start_idx : start_idx + match_len]
            current_effective_new_text = edit.new_text or ""

            if re.match(r"^\{#cell:[^}]+\}$", actual_doc_text.strip()):
                ins_text = current_effective_new_text
                ins_text = ins_text.replace(actual_doc_text.strip(), "")
                # A NON-empty cell: the anchor sits after the existing cell
                # text, so the insertion lands at the END of the cell — and a
                # new_text that echoes the existing content ("By: /s/ Signer"
                # for a cell already reading "By: ") must not duplicate it
                # (QA round 3, finding 1.3).
                anchor_span = next(
                    (s for s in active_mapper.spans if s.start <= start_idx < s.end and s.paragraph is not None),
                    None,
                )
                if anchor_span is not None and anchor_span.paragraph is not None and ins_text:
                    anchor_p_el = anchor_span.paragraph._element
                    existing_cell_text = "".join(
                        s.text
                        for s in active_mapper.spans
                        if s.end <= start_idx
                        and s.run is not None
                        and s.paragraph is not None
                        and s.paragraph._element is anchor_p_el
                    )
                    if existing_cell_text:
                        for candidate in (existing_cell_text, existing_cell_text.rstrip()):
                            if candidate and ins_text.startswith(candidate):
                                ins_text = ins_text[len(candidate) :].lstrip()
                                break
                        # Appending after a label ("Nimi" + "Testi") must not
                        # glue words together (TC 5.1).
                        if ins_text and not existing_cell_text[-1].isspace() and not ins_text[0].isspace():
                            ins_text = " " + ins_text
                if ins_text:
                    sub_mt = ModifyText(type="modify", target_text="", new_text=ins_text, comment=edit.comment)
                    sub_mt._match_start_index = start_idx
                    sub_mt._internal_op = "INSERTION"
                    sub_mt._active_mapper_ref = active_mapper
                    all_sub_edits.append(sub_mt)
                elif edit.comment:
                    sub_mt = ModifyText(type="modify", target_text="", new_text="", comment=edit.comment)
                    sub_mt._match_start_index = start_idx
                    sub_mt._internal_op = "COMMENT_ONLY"
                    sub_mt._active_mapper_ref = active_mapper
                    all_sub_edits.append(sub_mt)
                continue

            if is_regex and current_effective_new_text:
                try:
                    current_effective_new_text = re.sub(edit.target_text, current_effective_new_text, actual_doc_text)
                except re.error:
                    pass
                else:
                    self._flag_surviving_js_backreference(edit, current_effective_new_text)

            # The matcher forgave a typographic mismatch to find this
            # occurrence (an LLM writes "parties' Master", the document reads
            # "parties’ Master"), so the writer must forgive the same one:
            # otherwise the caller's straight quotes are written back verbatim
            # and every untargeted curly character becomes a real tracked
            # change on a provision nobody touched (B4). Keyed on the MATCH
            # being typography-forgiving — a caller who quotes the document's
            # own characters and asks for different ones still gets the change.
            current_effective_new_text = self._restore_matched_typography(
                actual_doc_text, edit.target_text, current_effective_new_text
            )

            # Stash the first occurrence's full match for the report preview,
            # so it can show the complete logical change rather than only the
            # first word-diff sub-edit (e.g. "{--two--}{++five++} (2) years"
            # for a "two (2) years" -> "five (5) years" edit).
            if edit._preview_span is None:
                edit._preview_span = (start_idx, match_len)
                edit._preview_matched_text = actual_doc_text
                edit._preview_new_text = current_effective_new_text
                edit._preview_mapper_ref = active_mapper

            para_replace = self._maybe_paragraph_replace(edit, start_idx, match_len, active_mapper)
            if para_replace is not None:
                if is_regex:
                    para_replace.new_text = current_effective_new_text
                all_sub_edits.append(para_replace)
                continue

            res = self._resolve_single_match(
                edit,
                start_idx,
                match_len,
                active_mapper,
                actual_doc_text,
                current_effective_new_text,
                index_offset=index_offset,
            )
            if isinstance(res, list):
                all_sub_edits.extend(res)
            elif res:
                all_sub_edits.append(res)

        if not all_sub_edits:
            return None

        if match_mode == "all" or len(all_sub_edits) > 1:
            return all_sub_edits
        return all_sub_edits[0]

    def _single_commented_sub_edit(
        self,
        target_str: str,
        new_str: str,
        base_offset: int,
        comment: str,
        is_table: bool,
        active_mapper,
    ) -> List[ModifyText]:
        """
        Build a single (unfragmented) sub-edit for a commented change.

        Shared prefix/suffix are still trimmed (word-boundary aware) so the
        redline stays minimal at the edges, but the changed middle is emitted
        as ONE tracked change rather than fanned out per word. The comment then
        anchors around the whole span. See _word_diff_sub_edits for why a
        commented change must not be split.
        """
        if target_str == new_str:
            # A pure comment anchor (no textual change) has nothing to trim to;
            # trimming identical strings would collapse the span to zero length
            # and the COMMENT_ONLY apply path would find no runs to attach to.
            # Keep the whole span as the anchor.
            final_target = target_str
            final_new = new_str
            start = base_offset
            op = "COMMENT_ONLY"
        else:
            prefix_len, suffix_len = trim_common_context(target_str, new_str)
            final_target = target_str[prefix_len : len(target_str) - suffix_len]
            final_new = new_str[prefix_len : len(new_str) - suffix_len]
            start = base_offset + prefix_len

            # CC-14: see _trim_shared_trailing_paragraph_mark. trim_common_context
            # is word-boundary aware and will not trim across "\n\n", so a
            # commented change like "A.\n\n" -> "Z.\n\nY.\n\n" arrives whole.
            final_target, final_new = _trim_shared_trailing_paragraph_mark(final_target, final_new)

            if not final_target and final_new:
                op = EditOperationType.INSERTION
            elif final_target and not final_new:
                op = EditOperationType.DELETION
            else:
                op = EditOperationType.MODIFICATION

        sub_edit = ModifyText(
            type="modify",
            target_text=final_target,
            new_text=final_new,
            comment=comment,
        )
        sub_edit._resolved_start_idx = start
        sub_edit._match_start_index = start
        sub_edit._active_mapper_ref = active_mapper
        sub_edit._internal_op = op
        if is_table:
            sub_edit._is_table_edit = True

        return [sub_edit]

    def _word_diff_sub_edits(
        self,
        target_str: str,
        new_str: str,
        base_offset: int,
        parent_comment: Optional[str] = None,
        is_table: bool = False,
        active_mapper=None,
    ) -> List[ModifyText]:
        # A modify that carries a comment must stay ONE contiguous tracked
        # change so its comment anchor wraps the whole logical edit. Word-level
        # fan-out would split it into several Chg pairs and attach the comment
        # to only one fragment; rejecting THAT fragment then silently destroys
        # the comment (and any reply thread) while the other fragments — and the
        # batch's "1 applied" report — give no hint the annotation is gone
        # (QA 2026-07-22 bug #1). Emit a single sub-edit over the minimal
        # word-boundary-trimmed changed span so a commented change is atomic:
        # rejecting it reverts the entire edit, with no orphaned "other half".
        if parent_comment is not None:
            return self._single_commented_sub_edit(
                target_str, new_str, base_offset, parent_comment, is_table, active_mapper
            )

        try:
            raw_sub_edits = generate_edits_from_text(target_str, new_str)
        except Exception as e:
            logger.warning("generate_edits_from_text failed, falling back to wholesale edit", error=str(e))
            raw_sub_edits = []

        # Hunks made purely of style markers are projection artifacts, never
        # user intent: they arise when a PLAIN target fuzzy-matched styled
        # document text ("Net 90 Days" against "**Net 90 Days**"), and the
        # resulting `**`-deletion sub-edits target virtual spans that can
        # never apply — the batch reports phantom skips while the formatting
        # silently stays (QA 2026-07-19 F-02 sibling). Edits that DO declare
        # markers never reach this word-diff path (they resolve as whole-span
        # markdown proxies), so dropping marker-only hunks here is always
        # correct.
        def _marker_only(text: str) -> bool:
            stripped = text.strip()
            return bool(stripped) and not stripped.strip("*_")

        raw_sub_edits = [
            e
            for e in raw_sub_edits
            if not (
                (not e.target_text or _marker_only(e.target_text))
                and (not e.new_text or _marker_only(e.new_text))
                and (e.target_text or e.new_text)
            )
        ]

        if not raw_sub_edits:
            fallback_edit = ModifyText(
                type="modify",
                target_text=target_str,
                new_text=new_str,
                comment=parent_comment,
            )
            fallback_edit._resolved_start_idx = base_offset
            fallback_edit._match_start_index = base_offset
            fallback_edit._active_mapper_ref = active_mapper
            if is_table:
                fallback_edit._is_table_edit = True
            if target_str == new_str:
                fallback_edit._internal_op = "COMMENT_ONLY"
            elif not target_str and new_str:
                fallback_edit._internal_op = EditOperationType.INSERTION
            elif target_str and not new_str:
                fallback_edit._internal_op = EditOperationType.DELETION
            elif target_str and new_str:
                fallback_edit._internal_op = EditOperationType.MODIFICATION
            else:
                fallback_edit._internal_op = "COMMENT_ONLY"
            return [fallback_edit]

        sub_edits = []
        comment_assigned = False
        for raw_edit in raw_sub_edits:
            sub_start = base_offset + (raw_edit._match_start_index or 0)
            should_attach_comment = (parent_comment is not None) and (not comment_assigned)
            if should_attach_comment:
                comment_assigned = True

            sub_edit = ModifyText(
                type="modify",
                target_text=raw_edit.target_text,
                new_text=raw_edit.new_text,
                comment=parent_comment if should_attach_comment else None,
            )
            sub_edit._resolved_start_idx = sub_start
            sub_edit._match_start_index = sub_start
            sub_edit._active_mapper_ref = active_mapper
            if is_table:
                sub_edit._is_table_edit = True

            t_val = raw_edit.target_text
            n_val = raw_edit.new_text
            if not t_val and n_val:
                sub_edit._internal_op = EditOperationType.INSERTION
            elif t_val and not n_val:
                sub_edit._internal_op = EditOperationType.DELETION
            elif t_val and n_val:
                sub_edit._internal_op = EditOperationType.MODIFICATION
            else:
                sub_edit._internal_op = "COMMENT_ONLY"

            sub_edits.append(sub_edit)

        return sub_edits

    def _resolve_single_match(
        self,
        edit,
        start_idx,
        match_len,
        active_mapper,
        actual_doc_text,
        effective_new_text,
        index_offset: int = 0,
    ):
        if "](" in actual_doc_text:
            t_links = list(re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", actual_doc_text))
            n_links = list(re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", effective_new_text))
            if len(t_links) == 1 and len(n_links) == 1:
                t_text, t_url = t_links[0].groups()
                n_text, n_url = n_links[0].groups()

                sub_edits = []
                if t_text != n_text:
                    t_idx = actual_doc_text.find(t_text)
                    txt_edit = ModifyText(
                        type="modify",
                        target_text=t_text,
                        new_text=n_text,
                        comment=edit.comment,
                    )
                    txt_edit._match_start_index = start_idx + t_idx
                    txt_edit._resolved_start_idx = start_idx + t_idx
                    txt_edit._internal_op = EditOperationType.MODIFICATION
                    txt_edit._active_mapper_ref = active_mapper
                    sub_edits.append(txt_edit)

                if t_url != n_url:
                    t_idx = actual_doc_text.find(t_url)
                    url_edit = ModifyText(type="modify", target_text=t_url, new_text=n_url, comment=None)
                    url_edit._resolved_start_idx = start_idx + t_idx
                    url_edit._match_start_index = start_idx + t_idx
                    url_edit._internal_op = "URL_RETARGET"
                    url_edit._active_mapper_ref = active_mapper
                    sub_edits.append(url_edit)

                if sub_edits:
                    return sub_edits if len(sub_edits) > 1 else sub_edits[0]

        # TABLE CELL SPLITTING LOGIC (R1, R2, R3, R4, N1 Fix)
        # Check if the target text actually overlaps a virtual table boundary (" | ")
        overlaps_virtual_pipe = False
        if active_mapper:
            overlaps_virtual_pipe = any(
                s.text == " | " and s.run is None and s.start < start_idx + match_len and s.end > start_idx
                for s in active_mapper.spans
            )

        if overlaps_virtual_pipe:
            actual_cells = actual_doc_text.split("|")
            new_cells = effective_new_text.split("|")

            if len(actual_cells) != len(new_cells):
                # Reject structural modifications to tables (adding/removing columns) via text replacement
                raise BatchValidationError(
                    [
                        f"Target text spans {len(actual_cells)} table cells, but replacement provides "
                        f"{len(new_cells)}. "
                        "To modify text without altering table structure (rows or columns), ensure the replacement "
                        "contains the exact same number of '|' separators "
                        "(e.g., replace with 'CellC | ' to empty the second cell)."
                    ]
                )

            if len(actual_cells) > 1:
                sub_edits = []

                # actual_doc_text IS the document slice at
                # [start_idx, start_idx + len): per-cell offsets are exact
                # arithmetic over that slice — never a search of
                # mapper.full_text, which cannot distinguish repeated cell
                # text and lands in the wrong cell when the matched range
                # starts inside a " | " separator.
                cell_start_in_target = 0

                # Determine which cell should receive the comment (first cell that actually changes, or cell 0)
                target_comment_idx = 0
                for idx, (a, n) in enumerate(zip(actual_cells, new_cells, strict=True)):
                    if a.strip() != n.strip():
                        target_comment_idx = idx
                        break

                for cell_idx, (a_cell, n_cell) in enumerate(zip(actual_cells, new_cells, strict=True)):
                    a_clean = a_cell.strip()
                    n_clean = n_cell.strip()
                    actual_start = start_idx + cell_start_in_target + (a_cell.find(a_clean) if a_clean else 0)

                    should_attach_comment = (edit.comment is not None) and (cell_idx == target_comment_idx)

                    if a_clean != n_clean or should_attach_comment:
                        cell_sub_edits = self._word_diff_sub_edits(
                            target_str=a_clean,
                            new_str=n_clean,
                            base_offset=actual_start,
                            parent_comment=edit.comment if should_attach_comment else None,
                            is_table=True,
                            active_mapper=active_mapper,
                        )
                        for se in cell_sub_edits:
                            se._original_target_text = edit.target_text
                            se._split_group_id = start_idx
                        sub_edits.extend(cell_sub_edits)

                    cell_start_in_target += len(a_cell) + 1  # +1 for the '|'

                return sub_edits
            # Exactly one "cell": the target merely brushes a separator (its
            # match range starts or ends inside " | ") without crossing into
            # another cell's text. That is an ordinary in-cell edit — fall
            # through to the standard resolution.

        if actual_doc_text == effective_new_text or edit.target_text == effective_new_text:
            proxy_edit = ModifyText(
                type="modify",
                target_text=actual_doc_text,
                new_text=actual_doc_text,
                comment=edit.comment,
            )
            proxy_edit._resolved_start_idx = start_idx
            proxy_edit._match_start_index = start_idx
            proxy_edit._internal_op = "COMMENT_ONLY"
            proxy_edit._active_mapper_ref = active_mapper
            return proxy_edit

        if effective_new_text.startswith(actual_doc_text):
            proxy_edit = ModifyText(
                type="modify",
                target_text="",
                new_text=effective_new_text[len(actual_doc_text) :],
                comment=edit.comment,
            )
            proxy_edit._resolved_start_idx = start_idx + match_len
            proxy_edit._match_start_index = start_idx + match_len
            proxy_edit._internal_op = EditOperationType.INSERTION
            proxy_edit._active_mapper_ref = active_mapper
            return proxy_edit

        if (
            effective_new_text.startswith(actual_doc_text.rstrip())
            and len(effective_new_text) > len(actual_doc_text.rstrip())
            # CC-14: ...but NOT when the replacement introduces a paragraph
            # break the target does not have. This branch preserves the
            # target's trailing whitespace by inserting BEFORE it, which is
            # right for a separator space inside one paragraph ("Section 1 " →
            # "Section 1 Revised" must not glue the next word on) and wrong the
            # moment a "\n\n" lands in the remainder: the preserved space is
            # then stranded at the START of the new paragraph. A paragraph
            # split at a space is exactly the shape the diff emits for
            # "0 0." → "0.\n\n0.", which applied cleanly and produced
            # "0.\n\n 0." — accepted, reported successful, silently wrong.
            # The F1 rule below already resolves this shape correctly, as one
            # atomic modification consuming the WHOLE matched span, so fall
            # through to it rather than duplicating the reasoning here.
            and not ("\n\n" in effective_new_text and "\n\n" not in actual_doc_text)
        ):
            # Smart Fallback: Handle trailing space omissions (e.g. LLM appended \n without the space).
            # It only applies when the new text genuinely EXTENDS the rstripped target: when the
            # remainder is empty the caller asked to DELETE the target's trailing whitespace (most
            # importantly a "\n\n" paragraph mark, the shape make_edits_self_contained emits for a
            # paragraph merge), and an insertion of "" would silently drop that deletion while the
            # batch still reported the edit as applied. Fall through to the trimming path instead,
            # which resolves it as the DELETION validate_edits already predicted.
            proxy_edit = ModifyText(
                type="modify",
                target_text="",
                new_text=effective_new_text[len(actual_doc_text.rstrip()) :],
                comment=edit.comment,
            )
            proxy_edit._resolved_start_idx = start_idx + len(actual_doc_text.rstrip())
            proxy_edit._match_start_index = start_idx + len(actual_doc_text.rstrip())
            proxy_edit._internal_op = EditOperationType.INSERTION
            proxy_edit._active_mapper_ref = active_mapper
            return proxy_edit

        prefix_len, suffix_len = trim_common_context(actual_doc_text, effective_new_text)

        t_end = len(actual_doc_text) - suffix_len
        n_end = len(effective_new_text) - suffix_len

        final_target = actual_doc_text[prefix_len:t_end]
        final_new = effective_new_text[prefix_len:n_end]
        effective_start_idx = start_idx + prefix_len
        # or more paragraph breaks and the replacement preserves the same
        # number of breaks. Apply it as one independent sub-edit per paragraph
        # segment so the structural \n\n breaks are left intact. Each sub-edit
        # shares a _split_group_id (the occurrence's start index) so the batch
        # report still counts it as a single applied edit. Unbalanced cases
        # (a genuine paragraph merge or split) fall through to the guard below.
        if "\n\n" in actual_doc_text and actual_doc_text.count("\n\n") == effective_new_text.count("\n\n"):
            target_segs = actual_doc_text.split("\n\n")
            new_segs = effective_new_text.split("\n\n")
            split_sub_edits: List[ModifyText] = []
            seg_offset = start_idx
            comment_assigned = False
            for t_seg, n_seg in zip(target_segs, new_segs, strict=True):
                if t_seg != n_seg:
                    seg_comment = edit.comment if (edit.comment and not comment_assigned) else None
                    seg_sub_edits = self._word_diff_sub_edits(
                        target_str=t_seg,
                        new_str=n_seg,
                        base_offset=seg_offset,
                        parent_comment=seg_comment,
                        is_table=False,
                        active_mapper=active_mapper,
                    )
                    if any(se.comment is not None for se in seg_sub_edits):
                        comment_assigned = True
                    for se in seg_sub_edits:
                        se._split_group_id = start_idx
                        split_sub_edits.append(se)
                # Advance past this segment plus its "\n\n" separator span.
                seg_offset += len(t_seg) + 2
            if split_sub_edits:
                return split_sub_edits

        # After trimming shared context, an edit whose target remainder is
        # EMPTY is a pure insertion with exactly one hunk. Resolve it
        # directly at the effective offset instead of word-diffing the full
        # strings: dmp's alignment can cross-match punctuation between the
        # shared context and the inserted text (pairing the period of
        # "two." with "marker.") and split the insertion apart.
        if not final_target and final_new:
            proxy_edit = ModifyText(
                type="modify",
                target_text="",
                new_text=final_new,
                comment=edit.comment,
            )
            proxy_edit._resolved_start_idx = effective_start_idx
            proxy_edit._match_start_index = effective_start_idx
            proxy_edit._internal_op = EditOperationType.INSERTION
            proxy_edit._active_mapper_ref = active_mapper
            return proxy_edit

        # F1 (QA 2026-07-23): a replacement whose new text spans MULTIPLE
        # paragraphs while its target sits inside ONE paragraph must not have
        # its common affixes trimmed away (nor be word-diffed, which trims the
        # same way): a shared trailing "." pairs the original sentence's final
        # period with the replacement's, stranding a "."-only container in the
        # source paragraph while the replacement's last sentence loses its
        # period. Emit ONE atomic modification covering the ENTIRE matched
        # text and carrying the ENTIRE new text, so the deletion consumes the
        # whole target and the insertion stays complete. (The pure-insertion
        # proxy above still wins when the trimmed target remainder is empty —
        # the v6-H2 paragraph-insertion shape.)
        if "\n\n" in effective_new_text and "\n\n" not in actual_doc_text and final_target:
            proxy_edit = ModifyText(
                type="modify",
                target_text=actual_doc_text,
                new_text=effective_new_text,
                comment=edit.comment,
            )
            proxy_edit._resolved_start_idx = start_idx
            proxy_edit._match_start_index = start_idx
            proxy_edit._internal_op = EditOperationType.MODIFICATION
            proxy_edit._active_mapper_ref = active_mapper
            return proxy_edit

        # BUG-23-4: Reject boundary-crossing plain-paragraph modifications with text on both sides
        # to prevent structural paragraph-break corruption.
        if "\n\n" in final_target:
            if "\n\n" in final_new:
                before, _, after = actual_doc_text.partition("\n\n")
                if before.strip() and after.strip():
                    raise BatchValidationError(
                        [
                            f"- Edit {index_offset + 1} Failed: target_text spans a paragraph "
                            "boundary with body text on both sides. "
                            "The paragraph break is a structural element, "
                            "not literal text, so it cannot be replaced as "
                            "a single span "
                            "without corrupting the document. "
                            "Split this into one edit per paragraph."
                        ]
                    )
            else:
                before, _, after = final_target.partition("\n\n")
                if before.strip() and after.strip():
                    raise BatchValidationError(
                        [
                            f"- Edit {index_offset + 1} Failed: target_text spans a paragraph "
                            "boundary with body text on both sides. "
                            "The paragraph break is a structural element, "
                            "not literal text, so it cannot be replaced as a single span "
                            "without corrupting the document. Split this into one edit per paragraph."
                        ]
                    )

        has_markdown = False
        if edit.new_text and ("**" in edit.new_text or "_" in edit.new_text):
            has_markdown = True
        if effective_new_text and ("**" in effective_new_text or "_" in effective_new_text):
            has_markdown = True
        if getattr(edit, "_has_markdown", False):
            has_markdown = True

        if has_markdown:
            if not final_target and final_new:
                effective_op = EditOperationType.INSERTION
            elif final_target and not final_new:
                effective_op = EditOperationType.DELETION
            elif final_target and final_new:
                effective_op = EditOperationType.MODIFICATION
            else:
                proxy_edit = ModifyText(
                    type="modify",
                    target_text=final_target,
                    new_text=final_new,
                    comment=edit.comment,
                )
                proxy_edit._match_start_index = effective_start_idx
                proxy_edit._internal_op = "COMMENT_ONLY"
                proxy_edit._active_mapper_ref = active_mapper
                proxy_edit._has_markdown = True
                return proxy_edit

            proxy_edit = ModifyText(
                type="modify",
                target_text=final_target,
                new_text=final_new,
                comment=edit.comment,
            )
            proxy_edit._resolved_start_idx = effective_start_idx
            proxy_edit._match_start_index = effective_start_idx
            proxy_edit._internal_op = effective_op
            proxy_edit._active_mapper_ref = active_mapper
            proxy_edit._has_markdown = True
            return proxy_edit

        sub_edits = self._word_diff_sub_edits(
            target_str=actual_doc_text,
            new_str=effective_new_text,
            base_offset=start_idx,
            parent_comment=edit.comment,
            is_table=False,
            active_mapper=active_mapper,
        )
        for se in sub_edits:
            se._split_group_id = start_idx
        return sub_edits

    def _apply_single_edit_indexed(
        self,
        edit: ModifyText,
        original_new_text: Optional[str] = None,
        rebuild_map: bool = True,
    ) -> bool:
        op = self._derive_internal_op(edit)
        active_mapper = edit._active_mapper_ref or self.mapper

        start_idx = edit._resolved_start_idx if edit._resolved_start_idx is not None else (edit._match_start_index or 0)
        target_text = edit.target_text
        length = len(target_text) if target_text else 0

        # Explicit bold/italic markers in the edit make the markers
        # authoritative: inserted runs must not additionally inherit the
        # replaced span's emphasis (QA 2026-07-19 F-02). The check keys on
        # THIS resolved edit's post-trim fields: when both sides carried the
        # SAME markers, trimming absorbed them into context (formatting
        # unchanged — keep inheriting), and a plain edit fuzzy-matched onto
        # styled document text never receives marker hunks at all (the
        # word-diff path drops marker-only artifacts).
        suppress_emphasis = self._edit_declares_emphasis(edit)

        logger.debug(f"Applying Edit at [{start_idx}:{start_idx + length}] Op={op}")

        # Whole-paragraph replacement: track-delete the entire source
        # paragraph (content + paragraph break) and emit a new tracked
        # paragraph with the new style.
        if op == EditOperationType.PARAGRAPH_REPLACE:
            return self._apply_paragraph_replace(edit)

        # Allocate logical-edit IDs up front: one id for the delete side and
        # one for the insert side per logical operation, reused across every
        # <w:ins>/<w:del> element this edit produces. A single ModifyText can
        # span multiple XML runs (e.g. a target containing a bold word, which
        # OOXML stores as a separate <w:r> element) or multiple paragraphs;
        # minting a fresh w:id per element would surface N [Chg:N] entries in
        # the projected bubble for what Word renders as a single review entry.
        # The mapper's _build_merged_meta_block deduplicates repeated IDs via
        # seen_sigs, collapsing the bubble without any projection-side change.
        del_id: Optional[str] = edit._reserved_del_id
        ins_id: Optional[str] = edit._reserved_ins_id
        if op in (EditOperationType.DELETION, EditOperationType.MODIFICATION) and del_id is None:
            del_id = self._get_next_id()
        if op in (EditOperationType.INSERTION, EditOperationType.MODIFICATION) and ins_id is None:
            ins_id = self._get_next_id()

        if op == "URL_RETARGET":
            target_spans = [s for s in active_mapper.spans if s.start <= start_idx < s.end]
            if target_spans and target_spans[0].hyperlink_id:
                # Resolve the relationship on the part that owns the hyperlink
                # (a footer link's rel lives on the footer part, not the main
                # document part). A missing id is a clean per-edit failure,
                # never a raw KeyError('rIdN') traceback (QA 2026-07-18 H3).
                owner = target_spans[0].paragraph
                part = owner.part if owner is not None and getattr(owner, "part", None) is not None else self.doc.part
                try:
                    rel = part.rels[target_spans[0].hyperlink_id]
                except KeyError:
                    logger.warning(
                        "Hyperlink relationship not found; skipping URL retarget",
                        r_id=target_spans[0].hyperlink_id,
                    )
                    return False
                rel._target = edit.new_text
                return True
            return False

        if op == "COMMENT_ONLY":
            target_runs = active_mapper.find_target_runs_by_index(start_idx, length, rebuild_map=rebuild_map)
            if not target_runs:
                return False
            if edit.comment:
                first_el = target_runs[0]._element
                last_el = target_runs[-1]._element
                start_p = first_el.getparent()
                while start_p is not None and start_p.tag != qn("w:p"):
                    start_p = start_p.getparent()
                end_p = last_el.getparent()
                while end_p is not None and end_p.tag != qn("w:p"):
                    end_p = end_p.getparent()

                # first_el / last_el may live inside a <w:ins> or <w:del> (e.g. a
                # comment-only edit anchored on another author's tracked
                # insertion). _attach_comment needs an element that is a DIRECT
                # child of the paragraph, so ascend to the child-of-paragraph
                # ancestor; the comment markers then wrap the whole ins/del.
                def _ascend_to_paragraph_child(el, p):
                    cur = el
                    while cur.getparent() is not None and cur.getparent() is not p:
                        cur = cur.getparent()
                    return cur

                if start_p is not None and end_p is not None:
                    first_anchor = _ascend_to_paragraph_child(first_el, start_p)
                    last_anchor = _ascend_to_paragraph_child(last_el, end_p)
                    if start_p == end_p:
                        self._attach_comment(start_p, first_anchor, last_anchor, edit.comment)
                    else:
                        self._attach_comment_spanning(start_p, first_anchor, end_p, last_anchor, edit.comment)
            return True

        if op == EditOperationType.INSERTION:
            final_new_text = edit.new_text or ""

            # A MACHINE-PINNED pure insertion (diff/text round-trip output:
            # authored with an empty target and no parent edit) positioned in
            # the separator gap between the body and a following part anchors
            # to the end of the BODY with forced new-paragraph semantics —
            # anchoring on the next part's first paragraph writes the new
            # final body paragraph into word/footer1.xml. Insertions DERIVED
            # from a target-anchored edit (parent ref set — e.g. prepending
            # "DRAFT " to "FOOTER MARKER") keep the user's chosen anchor:
            # their context names the part they meant.
            boundary_anchor: Optional[TextSpan] = None
            boundary = active_mapper.part_boundary_at(start_idx) if hasattr(active_mapper, "part_boundary_at") else None
            is_machine_pure_insertion = not edit.target_text and getattr(edit, "_parent_edit_ref", None) is None
            if boundary is not None and is_machine_pure_insertion:
                prev_i, next_i = boundary
                prev_kind = active_mapper.part_kind_of(prev_i)
                next_kind = active_mapper.part_kind_of(next_i)
                if prev_kind == "body" and next_kind != "body":
                    real_before = [s for s in active_mapper.spans if s.run is not None and s.part_index == prev_i]
                    if real_before:
                        boundary_anchor = real_before[-1]

            if boundary_anchor is not None:
                anchor_run, anchor_paragraph = boundary_anchor.run, boundary_anchor.paragraph
                if not final_new_text.startswith("\n"):
                    final_new_text = "\n\n" + final_new_text
            elif edit._insert_host_el is not None:
                # An empty content control names its host explicitly, because
                # position alone can no longer reach it: once the ghost run is
                # gone there is no run inside `w:sdtContent` to anchor to, and
                # the nearest run by offset lives OUTSIDE the control. The
                # insertion would then land next to the field instead of in
                # it - a filled-looking document whose control is still empty,
                # and whose value Word will not treat as the field's content.
                # Same shape as the OPC part boundary: a wall that offsets
                # cannot see, so the container is carried explicitly.
                anchor_run, anchor_paragraph = None, None
            else:
                anchor_run, anchor_paragraph = active_mapper.get_insertion_anchor(start_idx, rebuild_map=rebuild_map)
            if not anchor_run and not anchor_paragraph and edit._insert_host_el is None:
                return False

            insert_before = False
            if anchor_run is None and anchor_paragraph is not None:
                preceding = [s for s in active_mapper.spans if s.end == start_idx and s.paragraph == anchor_paragraph]
                if preceding and preceding[-1].text != "\n\n":
                    insert_before = True

            parent = None
            index = 0
            if edit._insert_host_el is not None:
                parent = edit._insert_host_el
                index = len(parent)
            elif anchor_run:
                parent = anchor_run._element.getparent()
                index = parent.index(anchor_run._element)
                # A tracked-deleted anchor (run inside <w:del>) cannot host
                # the new <w:ins> as a child — an insertion nested inside a
                # deletion is invalid revision XML. Lift the anchor to the
                # <w:del> wrapper so the insert lands beside the whole block.
                if parent.tag == qn("w:del"):
                    del_wrapper = parent
                    parent = del_wrapper.getparent()
                    if parent is not None:
                        index = parent.index(del_wrapper)
            elif anchor_paragraph:
                parent = anchor_paragraph._element
                for i, child in enumerate(parent):
                    if child.tag == qn("w:pPr"):
                        index = i + 1
                    else:
                        break

            if parent is None:
                return False

            # QA 2026-07-18 C2 (apply-level backstop, pinned edits bypass
            # validate_edits): refuse insertions that would write row-shaped
            # pipe text into a table cell.
            if self._introduces_table_row_text(active_mapper, start_idx, 1, "", final_new_text):
                return False

            if start_idx == 0:
                ins_elem, last_p = self.track_insert(
                    final_new_text,
                    anchor_run=anchor_run,
                    anchor_paragraph=anchor_paragraph,
                    comment=edit.comment,
                    suppress_inherited=suppress_emphasis,
                    insert_before=True,
                    reuse_id=ins_id,
                )
                if ins_elem is not None:
                    if parent.tag == qn("w:ins"):
                        self._insert_and_split_ins(parent, index, ins_elem)
                        actual_parent = parent.getparent()
                    else:
                        parent.insert(index, ins_elem)
                        actual_parent = parent

                    if edit.comment:
                        if last_p is not None:
                            last_ins_candidates = [
                                node for node in last_p.findall(f".//{qn('w:ins')}") if not self._is_inside_pPr(node)
                            ]
                            if last_ins_candidates:
                                last_ins = last_ins_candidates[-1]
                                self._attach_comment_spanning(actual_parent, ins_elem, last_p, last_ins, edit.comment)
                            else:
                                self._attach_comment(actual_parent, ins_elem, ins_elem, edit.comment)
                        else:
                            self._attach_comment(actual_parent, ins_elem, ins_elem, edit.comment)
            else:
                if anchor_run:
                    next_run = self._get_next_run(anchor_run)
                    style_run = self._determine_style_source(anchor_run, next_run, final_new_text)
                else:
                    style_run = None

                ins_elem, last_p = self.track_insert(
                    final_new_text,
                    anchor_run=style_run,
                    anchor_paragraph=anchor_paragraph,
                    comment=edit.comment,
                    suppress_inherited=suppress_emphasis,
                    insert_before=insert_before,
                    reuse_id=ins_id,
                    # style_run may be the run AFTER the insertion point (it
                    # carries formatting only); the insertion physically
                    # follows anchor_run — suffix relocation keys on it.
                    positional_anchor_run=anchor_run,
                )
                if ins_elem is not None:
                    if parent.tag == qn("w:ins"):
                        self._insert_and_split_ins(parent, index + 1, ins_elem)
                        actual_parent = parent.getparent()
                    else:
                        insert_idx = index + 1 if anchor_run else index
                        parent.insert(insert_idx, ins_elem)
                        actual_parent = parent

                    if edit.comment:
                        if last_p is not None:
                            last_ins_candidates = [
                                node for node in last_p.findall(f".//{qn('w:ins')}") if not self._is_inside_pPr(node)
                            ]
                            if last_ins_candidates:
                                last_ins = last_ins_candidates[-1]
                                self._attach_comment_spanning(actual_parent, ins_elem, last_p, last_ins, edit.comment)
                            else:
                                self._attach_comment(actual_parent, ins_elem, ins_elem, edit.comment)
                        else:
                            self._attach_comment(actual_parent, ins_elem, ins_elem, edit.comment)
                elif last_p is not None and edit.comment:
                    # Leading "\n\n" insertions (boundary re-anchors) create
                    # only new paragraphs — anchor the comment on the last one.
                    ins_list = [node for node in last_p.findall(f".//{qn('w:ins')}") if not self._is_inside_pPr(node)]
                    if ins_list:
                        self._attach_comment(last_p, ins_list[0], ins_list[-1], edit.comment)
            self._record_used_revision_ids(edit, ins_id)
            return True

        # QA 2026-07-18 C1 (apply-level backstop, pinned edits bypass
        # validate_edits): a modification/deletion may never mutate real text
        # from two different OPC parts in one span.
        if (
            op in (EditOperationType.DELETION, EditOperationType.MODIFICATION)
            and length
            and len([r for r in active_mapper.part_ranges if r[1] > r[0]]) > 1
        ):
            crossed_parts = {
                s.part_index
                for s in active_mapper.spans
                if s.run is not None and s.end > start_idx and s.start < start_idx + length
            }
            if len(crossed_parts) > 1:
                logger.warning(
                    "Refusing edit that spans OPC part boundary",
                    start=start_idx,
                    parts=sorted(crossed_parts),
                )
                return False

        # CC-4 (apply-level backstop, same reason as C1 above): pinned edits
        # skip validate_edits AND the resolver, so a diff-generated batch
        # reaches here with no gate having run. Only the gates whose answer
        # cannot change between validate and apply are repeated — locks,
        # binding and protection are properties of the document, not of the
        # match, so re-deriving them here is cheap and cannot disagree.
        if op in (EditOperationType.DELETION, EditOperationType.MODIFICATION) and length:
            blocked = self._apply_gate_refusal(active_mapper, start_idx, length, edit)
            if blocked:
                logger.warning(
                    "Refusing edit inside a gated content control",
                    start=start_idx,
                    reason=blocked,
                )
                if getattr(edit, "_error_msg", None) in (None, ""):
                    edit._error_msg = blocked
                return False

        target_runs = active_mapper.find_target_runs_by_index(start_idx, length, rebuild_map=rebuild_map)
        virtual_spans = []
        if op in (EditOperationType.DELETION, EditOperationType.MODIFICATION):
            virtual_spans = active_mapper.get_virtual_spans_in_range(start_idx, length)

        if not target_runs and not virtual_spans:
            affected_spans = [s for s in active_mapper.spans if s.end > start_idx and s.start < start_idx + length]
            if affected_spans and all(
                s.run is None and s.text != "\n\n" and not getattr(s, "is_image_marker", False) for s in affected_spans
            ):
                logger.debug(
                    f"Applied virtual no-op edit targeting purely virtual projection text: {repr(edit.target_text)}"
                )
                edit._applied_status = True
                return True
            return False

        affected_ps = set()
        for run in target_runs:
            if run._parent and hasattr(run._parent, "_element") and run._parent._element.tag == qn("w:p"):
                affected_ps.add(run._parent._element)

        if op == EditOperationType.DELETION:
            first_del_element = None
            last_del_element = None
            del_elems = []
            for run in target_runs:
                del_elem = self.track_delete_run(run, reuse_id=del_id)
                if del_elem is not None:
                    del_elems.append(del_elem)
                if first_del_element is None:
                    first_del_element = del_elem
                last_del_element = del_elem

            self._mark_fully_deleted_rows_in_range(del_elems, virtual_spans, start_idx, length, active_mapper, del_id)

            if edit.comment and first_del_element is not None and last_del_element is not None:
                # The deletions may be nested inside a foreign author's <w:ins>;
                # lift the comment anchors to their paragraph-level child.
                start_p = first_del_element.getparent()
                while start_p is not None and start_p.tag != qn("w:p"):
                    start_p = start_p.getparent()
                end_p = last_del_element.getparent()
                while end_p is not None and end_p.tag != qn("w:p"):
                    end_p = end_p.getparent()
                if start_p is not None:
                    first_del_element = self._paragraph_child_ancestor(first_del_element, start_p)
                if end_p is not None:
                    last_del_element = self._paragraph_child_ancestor(last_del_element, end_p)
                if start_p == end_p:
                    self._attach_comment(start_p, first_del_element, last_del_element, edit.comment)
                else:
                    self._attach_comment_spanning(
                        start_p,
                        first_del_element,
                        end_p,
                        last_del_element,
                        edit.comment,
                    )

        elif op == EditOperationType.MODIFICATION:
            first_del_element = None
            last_del_element = None
            del_elems = []
            for run in target_runs:
                del_elem = self.track_delete_run(run, reuse_id=del_id)
                if del_elem is not None:
                    del_elems.append(del_elem)
                if first_del_element is None:
                    first_del_element = del_elem
                last_del_element = del_elem

            if first_del_element is not None and last_del_element is not None and edit.new_text:
                parent = last_del_element.getparent()
                if parent is not None:
                    text_to_insert = edit.new_text
                    clean_text, style_name = self._parse_markdown_style(text_to_insert)
                    if style_name:
                        anchor_para = target_runs[-1]._parent
                        try:
                            current_style = getattr(anchor_para, "style", None)
                        except AttributeError:
                            current_style = None
                        if current_style and getattr(current_style, "name", "") == style_name:
                            text_to_insert = clean_text

                    del_r = last_del_element.find(qn("w:r"))
                    if del_r is None:
                        del_r = target_runs[-1]._element

                    ins_elem, last_p = self.track_insert(
                        text_to_insert,
                        anchor_run=Run(del_r, target_runs[-1]._parent),
                        comment=None,
                        suppress_inherited=suppress_emphasis,
                        reuse_id=ins_id,
                    )
                    if last_p is not None:
                        # The replacement re-created the content as NEW tracked
                        # paragraphs (heading-block insertion). If the deletion
                        # consumed everything visible in the source paragraph,
                        # the empty container must not survive an accept:
                        # track-delete its paragraph break too, mirroring
                        # _apply_paragraph_replace (QA round 3, finding 2.4).
                        src_p = first_del_element.getparent()
                        while src_p is not None and src_p.tag != qn("w:p"):
                            src_p = src_p.getparent()
                        if src_p is not None and not self._paragraph_has_visible_content(src_p):
                            src_pPr = src_p.find(qn("w:pPr"))
                            if src_pPr is None:
                                src_pPr = create_element("w:pPr")
                                src_p.insert(0, src_pPr)
                            src_rPr = src_pPr.find(qn("w:rPr"))
                            if src_rPr is None:
                                src_rPr = create_element("w:rPr")
                                src_pPr.append(src_rPr)
                            if src_rPr.find(qn("w:del")) is None:
                                src_rPr.append(self._create_track_change_tag("w:del", reuse_id=del_id))
                    if ins_elem is not None:
                        if parent.tag == qn("w:ins"):
                            # Revising another author's pending insertion: the
                            # <w:del> stays nested in their <w:ins>; splice our
                            # new <w:ins> in right after it by splitting their
                            # <w:ins> so we never produce <w:ins> within <w:ins>.
                            self._insert_and_split_ins(parent, parent.index(last_del_element) + 1, ins_elem)
                        else:
                            parent.insert(parent.index(last_del_element) + 1, ins_elem)

                    if edit.comment and first_del_element is not None:
                        # first_del_element / ins_elem may now sit inside a
                        # <w:ins> wrapper; lift anchors to their paragraph-level
                        # child so the comment range markers attach correctly.
                        start_p = first_del_element.getparent()
                        while start_p is not None and start_p.tag != qn("w:p"):
                            start_p = start_p.getparent()
                        first_anchor = (
                            self._paragraph_child_ancestor(first_del_element, start_p)
                            if start_p is not None
                            else first_del_element
                        )
                        if last_p is not None:
                            end_p = last_p
                            last_ins_candidates = [
                                node for node in last_p.findall(f".//{qn('w:ins')}") if not self._is_inside_pPr(node)
                            ]
                            if last_ins_candidates:
                                last_ins = last_ins_candidates[-1]
                                self._attach_comment_spanning(
                                    start_p,
                                    first_anchor,
                                    end_p,
                                    last_ins,
                                    edit.comment,
                                )
                        elif ins_elem is not None:
                            end_p = ins_elem.getparent()
                            while end_p is not None and end_p.tag != qn("w:p"):
                                end_p = end_p.getparent()
                            end_anchor = (
                                self._paragraph_child_ancestor(ins_elem, end_p) if end_p is not None else ins_elem
                            )
                            if start_p is not None and start_p == end_p:
                                self._attach_comment(start_p, first_anchor, end_anchor, edit.comment)
                            else:
                                self._attach_comment_spanning(
                                    start_p,
                                    first_anchor,
                                    end_p,
                                    end_anchor,
                                    edit.comment,
                                )
                        else:
                            self._attach_comment(
                                start_p,
                                first_anchor,
                                self._paragraph_child_ancestor(last_del_element, start_p)
                                if start_p is not None
                                else last_del_element,
                                edit.comment,
                            )

            # Row-deletion inference runs AFTER the replacement text is in the
            # tree. Evaluating it earlier saw every run in the row wrapped in
            # <w:del> — a whole-cell replacement then stamped w:trPr/w:del and
            # accept_all_revisions silently dropped the entire <w:tr>, taking
            # the inserted text with it (BUG_adeu_accept_all_table_row_loss).
            # A row only counts as deleted when no active run survives the
            # complete edit, e.g. a replacement spanning several rows whose new
            # text lands in the first one.
            self._mark_fully_deleted_rows_in_range(del_elems, virtual_spans, start_idx, length, active_mapper, del_id)

        # PHASE 2: OOXML Paragraph Merge Protocol
        if op in (EditOperationType.DELETION, EditOperationType.MODIFICATION):
            if op == EditOperationType.MODIFICATION and not target_runs and virtual_spans and edit.new_text:
                first_span = virtual_spans[0]
                if first_span.paragraph:
                    p1_el = first_span.paragraph._element
                    last_runs = p1_el.findall(f".//{qn('w:r')}")
                    anchor = Run(last_runs[-1], first_span.paragraph) if last_runs else None

                    ins_elem, _ = self.track_insert(
                        edit.new_text,
                        anchor_run=anchor,
                        comment=edit.comment,
                        reuse_id=ins_id,
                    )
                    if ins_elem is not None:
                        p1_el.append(ins_elem)

            for span in reversed(virtual_spans):
                if span.paragraph:
                    p1_element = span.paragraph._element
                    p2_element = p1_element.getnext()
                    while p2_element is not None and p2_element.tag != qn("w:p"):
                        p2_element = p2_element.getnext()

                    if p2_element is not None and p2_element.tag == qn("w:p"):
                        # Decide the merged container's properties BEFORE p2's
                        # children move in: when p1 keeps no visible content
                        # (a FULL paragraph deletion), the only surviving text
                        # is p2's — the merged paragraph must carry p2's
                        # properties (style, numbering). Keeping p1's restyled
                        # the following paragraph: deleting a heading turned
                        # the next body paragraph into a heading, deleting a
                        # plain paragraph before a list item stripped the
                        # item's numbering (QA 2026-07-19 ADEU-QA-002 B).
                        p1_fully_deleted = not self._paragraph_has_visible_content(p1_element)

                        # 1. Track pilcrow deletion in p1
                        pPr = p1_element.find(qn("w:pPr"))
                        if p1_fully_deleted:
                            p2_pPr = p2_element.find(qn("w:pPr"))
                            adopted = deepcopy(p2_pPr) if p2_pPr is not None else create_element("w:pPr")
                            # Section properties belong to p1's position in the
                            # document flow, never to p2's styling — carry them
                            # over so a section boundary is not destroyed.
                            if pPr is not None:
                                sect = pPr.find(qn("w:sectPr"))
                                if sect is not None and adopted.find(qn("w:sectPr")) is None:
                                    adopted.append(deepcopy(sect))
                                p1_element.remove(pPr)
                            p1_element.insert(0, adopted)
                            pPr = adopted
                        if pPr is None:
                            pPr = create_element("w:pPr")
                            p1_element.insert(0, pPr)
                        rPr = pPr.find(qn("w:rPr"))
                        if rPr is None:
                            rPr = create_element("w:rPr")
                            pPr.append(rPr)

                        if rPr.find(qn("w:del")) is None:
                            del_mark = self._create_track_change_tag("w:del")
                            rPr.append(del_mark)

                        # 2. Coalesce children from p2 to p1
                        for child in list(p2_element):
                            if child.tag != qn("w:pPr"):
                                p1_element.append(child)

                        # 3. Destroy orphan p2
                        parent = p2_element.getparent()
                        if parent is not None:
                            parent.remove(p2_element)

        for p_elem in affected_ps:
            has_visible = self._paragraph_has_visible_content(p_elem)

            if not has_visible:
                pPr = p_elem.find(qn("w:pPr"))
                if pPr is None:
                    pPr = create_element("w:pPr")
                    p_elem.insert(0, pPr)
                rPr = pPr.find(qn("w:rPr"))
                if rPr is None:
                    rPr = create_element("w:rPr")
                    pPr.append(rPr)
                if rPr.find(qn("w:del")) is None:
                    # The pilcrow deletion of a fully-emptied paragraph is part
                    # of the SAME logical change as the content deletion, so it
                    # shares del_id: accepting/rejecting the edit by its id
                    # then also resolves the paragraph mark (F1, QA 2026-07-23).
                    del_mark = self._create_track_change_tag("w:del", reuse_id=del_id)
                    rPr.append(del_mark)

        self._record_used_revision_ids(edit, del_id, ins_id)
        return True

    def _paragraph_has_visible_content(self, p_elem) -> bool:
        """
        True when the paragraph still carries visible content (w:t text,
        w:tab, w:br) that is NOT wrapped in a tracked deletion — i.e. the
        paragraph would render non-empty in the accepted document.
        """
        for tag in ["w:t", "w:tab", "w:br"]:
            for node in p_elem.findall(f".//{qn(tag)}"):
                is_deleted = False
                curr = node.getparent()
                while curr is not None and curr != p_elem.getparent():
                    if curr.tag == qn("w:del"):
                        is_deleted = True
                        break
                    curr = curr.getparent()
                if not is_deleted:
                    if tag == "w:t" and not node.text:
                        continue
                    return True
        return False

    def _is_last_paragraph_in_cell(self, p_elem) -> bool:
        """
        True when p_elem is the only <w:p> left inside its containing table
        cell — the floor for paragraph removal.

        ECMA-376 requires every <w:tc> to hold at least one block-level
        element, and Word treats a cell with none as a corrupt document. So
        accepting or rejecting a paragraph mark must never remove a cell's
        last paragraph; the marker is stripped instead, leaving the cell
        empty but valid (BUG_adeu_accept_all_table_row_loss).

        Paragraphs outside a table are unaffected: the body may legitimately
        end up with none.
        """
        cell = p_elem.getparent()
        while cell is not None and cell.tag != qn("w:tc"):
            cell = cell.getparent()
        if cell is None:
            return False
        return len(cell.findall(f".//{qn('w:p')}")) <= 1

    def _get_next_run(self, run: Run) -> Optional[Run]:
        curr = run._element
        while True:
            curr = curr.getnext()
            if curr is None:
                return None
            if curr.tag == qn("w:r"):
                return Run(curr, run._parent)

    def _determine_style_source(self, prev_run: Run, next_run: Optional[Run], insert_text: str) -> Run:
        if not next_run:
            return prev_run
        if insert_text and insert_text.endswith(" "):
            return next_run
        return prev_run

    def _inject_w16du_if_needed(self, part) -> None:
        """
        Lazily declare the w16du namespace on a part's root element, but ONLY
        when that part actually uses a w16du-qualified attribute (e.g. the
        w16du:dateUtc stamped on a tracked change). This preserves the
        invariant (report F9 / TC5) that an UNMODIFIED part — a header that
        was never edited — stays byte-for-byte untouched and never acquires
        the namespace, while still guaranteeing (VAL-CRIT-7 / VAL-OBS-1B)
        that a part which DID receive a tracked change carries the
        declaration at its root rather than an lxml-minted ns0 prefix.

        Operates on the live python-docx `_element` so header/footer parts
        (saved natively by Document.save) are covered; the main document
        part is skipped here — its tracked-change writes self-declare the
        prefix locally (lxml prefix registration at module scope), so a
        root-level injection pass over the 45 MB part is never needed.
        """
        if part == self.doc.part:
            return
        element = getattr(part, "_element", None)
        if element is None:
            return

        xml_bytes = etree.tostring(element, encoding="utf-8", pretty_print=False)
        xml_str = xml_bytes.decode("utf-8")

        # Only act if the part references the w16du namespace but hasn't
        # declared it (the common case: lxml serialized the attribute with a
        # generated ns0 prefix because the root lacked the declaration).
        uses_w16du = "w16du:" in xml_str or w16du_ns in xml_str
        already_declared = 'xmlns:w16du="' in xml_str or "xmlns:w16du='" in xml_str
        if not uses_w16du or already_declared:
            return

        w16du_ns_str = f'xmlns:w16du="{w16du_ns}"'
        xml_str = re.sub(r"(<w:[a-zA-Z0-9_]+ )", r"\1" + w16du_ns_str + " ", xml_str, count=1)
        new_root = parse_xml(xml_str.encode("utf-8"))
        # Collapse lxml's auto-generated ns0 prefix (emitted for the
        # w16du:dateUtc attributes that existed before the root declaration
        # was added) onto the canonical w16du prefix now declared at the root.
        etree.cleanup_namespaces(new_root, top_nsmap={"w16du": w16du_ns}, keep_ns_prefixes=["w16du"])
        part._element = new_root

    def save_to_stream(self) -> BytesIO:
        import lxml.etree as etree

        # Lazily declare w16du on any non-main part that picked up a tracked
        # change (and therefore a w16du:dateUtc attribute) during editing.
        for part in self.doc.part.package.parts:
            self._inject_w16du_if_needed(part)

        for part in self.doc.part.package.parts:
            if hasattr(part, "_adeu_element"):
                part._blob = etree.tostring(
                    part._adeu_element,
                    xml_declaration=True,
                    encoding="UTF-8",
                    standalone=True,
                )
        output = BytesIO()
        self.doc.save(output)
        output.seek(0)
        return output

    def _duplicate_revision_id_error(
        self, target_id: str, action_type: str, batch_idx: Optional[int] = None
    ) -> Optional[str]:
        """
        Refuses accept/reject on a w:id shared by revisions from DIFFERENT
        authors. Chg:N identifiers are the raw w:id values; uniqueness is
        assumed but not guaranteed for externally produced documents (merges,
        cross-document copy-paste), where one action would silently resolve
        several unrelated changes (QA 2026-07-17 F9). Same-author reuse is
        legitimate — this engine itself mints one id across every element of
        a single logical edit — so authorship is the discriminator.
        """
        nodes = [
            n
            for tag in ("w:ins", "w:del")
            for n in self.doc.element.findall(f".//{qn(tag)}")
            if n.get(qn("w:id")) == target_id
        ]
        authors = sorted({n.get(qn("w:author")) or "Unknown" for n in nodes})
        if len(authors) <= 1:
            return None
        prefix = f"- Action {batch_idx + 1} Failed: " if batch_idx is not None else "- Failed to apply action: "
        return (
            f"{prefix}{action_type} on Chg:{target_id} is ambiguous. The document "
            f"contains {len(nodes)} tracked-change elements sharing w:id={target_id} from different "
            f"authors ({', '.join(authors)}) — duplicate revision IDs produced outside this engine "
            "(e.g. by a document merge or copy-paste). Acting on this ID would resolve all of them "
            "at once. Resolve these changes individually in Word, or apply the intended outcome as "
            "an explicit text edit instead."
        )

    def _existing_change_ids(self) -> List[str]:
        """Distinct tracked-change ids (w:id on w:ins/w:del and format-change
        elements — all of them actionable) in the main story."""
        ids = {
            n.get(qn("w:id"))
            for tag in ("w:ins", "w:del") + self._FORMAT_CHANGE_TAGS
            for n in self.doc.element.findall(f".//{qn(tag)}")
            if n.get(qn("w:id"))
        }
        return sorted(ids, key=lambda x: (int(x) if x.isdigit() else 0, x))

    def _warn_stranded_comment_anchors(self, watermark: int) -> None:
        """
        Batch-level advisory for the "stranded comment anchor" shape (demo run
        2026-08-12, defect B).

        Editing around a foreign comment — deleting the words before its anchor
        and the words after it, while keeping the anchored phrase so the
        comment survives — is a legitimate move, and each of those deletions is
        legal on its own. Nothing cross-checked them against each other, so a
        batch could leave "...necessary for this litigationAttorney's Eyes
        Only;" behind and report two cleanly applied edits. The caller never
        learned the sentence it wrote is gibberish once accepted.

        So: warn, never reject. The condition is deliberately narrow, because a
        false positive here trains the caller to ignore the warning:
          - the anchored text must SURVIVE (text deleted along with its comment
            is the normal case and is already reported elsewhere),
          - there must be deleted text on BOTH sides of it in its own
            paragraph,
          - and at least one of those deletions must come from THIS batch, so a
            condition the caller inherited is not re-reported on every batch.

        `watermark` is the engine's revision-id counter as it stood before the
        batch: every id above it was minted by this batch (ids are monotonic,
        and a rejected batch reloads the document, resetting the counter).
        """
        try:
            body = self.doc.element.body
        except Exception:
            return
        starts = body.findall(".//" + qn("w:commentRangeStart"))
        if not starts:
            return

        authors: Optional[Dict[str, str]] = None
        stranded: List[tuple] = []

        for start in starts:
            cid = start.get(qn("w:id"))
            if not cid:
                continue

            # The anchor's own paragraph. A range that closes in a LATER
            # paragraph is a block-level annotation, not the single-sentence
            # shape this advisory is about.
            para = start.getparent()
            while para is not None and para.tag != qn("w:p"):
                para = para.getparent()
            if para is None:
                continue

            # ONE document-order pass, materialised: lxml hands out throwaway
            # proxies, so an element's identity is only stable while something
            # holds a reference to it. Keeping this list is what makes the
            # positional comparisons below meaningful.
            elements = list(para.iter())

            start_pos = end_pos = None
            for i, el in enumerate(elements):
                if el.get(qn("w:id")) != cid:
                    continue
                if start_pos is None and el.tag == qn("w:commentRangeStart"):
                    start_pos = i
                elif el.tag == qn("w:commentRangeEnd"):
                    end_pos = i
            # A range that closes in a LATER paragraph is a block-level
            # annotation, not the single-sentence shape this advisory is about.
            if start_pos is None or end_pos is None or end_pos < start_pos:
                continue

            def _inside_del(el, root) -> bool:
                node = el.getparent()
                while node is not None and node is not root:
                    if node.tag == qn("w:del"):
                        return True
                    node = node.getparent()
                return False

            # Text inside the range that is NOT itself deleted: what a reader
            # is left with after accepting everything.
            surviving = ""
            deleted_before = deleted_after = owned_by_this_batch = False
            for i, el in enumerate(elements):
                if el.tag == qn("w:t"):
                    if start_pos < i < end_pos and not _inside_del(el, para):
                        surviving += el.text or ""
                    continue
                if el.tag != qn("w:del"):
                    continue
                if i < start_pos:
                    side = "before"
                elif i > end_pos:
                    side = "after"
                else:
                    continue
                if not any((d.text or "").strip() for d in el.iter(qn("w:delText"))):
                    continue
                if side == "before":
                    deleted_before = True
                else:
                    deleted_after = True
                rid = el.get(qn("w:id")) or ""
                if rid.isdigit() and int(rid) > watermark:
                    owned_by_this_batch = True

            if not surviving.strip():
                continue

            if deleted_before and deleted_after and owned_by_this_batch:
                if authors is None:
                    authors = self._comment_authors()
                stranded.append((cid, surviving.strip()))

        for cid, text in stranded:
            who = (authors or {}).get(cid)
            label = f"comment Com:{cid} (by {who})" if who else f"comment Com:{cid}"
            self.skipped_details.append(
                f"- Warning: this batch deleted text on both sides of {label} but left its "
                f'anchored text "{truncate_middle(text, 60)}" in place, so once the changes are '
                "accepted that text stands alone in its sentence. If you kept it to preserve the "
                "comment's anchor, re-read the sentence; if you meant to remove the clause, extend "
                "one edit over the anchored text too. The edits themselves were applied."
            )

    def _existing_comment_ids(self) -> List[str]:
        """Comment ids present in the document, sorted for display."""
        try:
            ids = list(self.comments_manager.extract_comments_data().keys())
        except Exception:
            ids = []
        return sorted(ids, key=lambda x: (int(x) if x.isdigit() else 0, x))

    def _comment_authors(self) -> Dict[str, str]:
        """
        comment id -> author, for attributing a removal to a human. Callers that
        also need the id SET derive it from `.keys()` rather than calling
        `_existing_comment_ids` as well: each call re-parses the comments part.
        """
        try:
            return {
                cid: (data.get("author") or "Unknown")
                for cid, data in self.comments_manager.extract_comments_data().items()
            }
        except Exception:
            return {}

    @staticmethod
    def _describe_removed_comments(removed: Iterable[str], authors: Dict[str, str]) -> str:
        """
        Renders removed comments WITH their authors: an anonymous "removed
        comment Com:1" reads like the engine's own bookkeeping, which is exactly
        how the reported run rationalised destroying the reviewer's comment as
        success (B2). "comment Com:1 (by Sarah Chen)" cannot be misread.
        """
        ids = sorted(removed, key=lambda x: (int(x) if x.isdigit() else 0, x))
        rendered = ", ".join(f"Com:{cid} (by {authors[cid]})" if cid in authors else f"Com:{cid}" for cid in ids)
        noun = "comment" if len(ids) == 1 else "comments"
        return f"{noun} {rendered}"

    def _format_id_list(self, ids: List[str], prefix: str, limit: Optional[int] = None) -> str:
        if limit is None:
            limit = 8 if getattr(self, "terse_errors", False) else 20
        shown = ids[:limit]
        rendered = ", ".join(f"{prefix}{i}" for i in shown)
        if len(ids) > len(shown):
            rendered += f", … (+{len(ids) - len(shown)} more)"
        return rendered

    def _action_not_found_error(self, raw_id: str, target_id: str, act, batch_idx: Optional[int] = None) -> str:
        """
        Self-service diagnostic for accept/reject/reply on an id that resolved
        nothing. The other errors in this engine explain WHY and HOW to recover
        (ambiguous-match, major-deletions guard); this path used to emit only
        "Failed to apply action: reply on 99" with no reason and no way to find
        a valid id (QA 2026-07-22 bug #3). Names the expected id kind, lists the
        ids that actually exist, flags the common change/comment id mix-up, and
        points at the command that prints current ids.
        """
        change_ids = self._existing_change_ids()
        comment_ids = self._existing_comment_ids()
        has_prefix = raw_id.startswith("Chg:") or raw_id.startswith("Com:")
        find_hint = self.id_discovery_hint or (
            "Run `adeu extract <file> --mode changes` to list the current change (Chg:) and comment (Com:) ids."
        )
        prefix = f"- Action {batch_idx + 1} Failed: " if batch_idx is not None else "- Failed to apply action: "

        if isinstance(act, ReplyComment):
            # Echo the id the caller passed (normalizing a bare id to Com:N).
            echo = raw_id if has_prefix else f"Com:{target_id}"
            if target_id in change_ids:
                return (
                    f"{prefix}reply on {echo} — Chg:{target_id} is a tracked-change "
                    "id, not a comment. `reply` adds to an existing comment thread (Com:…); to comment "
                    "on a change instead, apply a modify with a `comment`. " + find_hint
                )
            avail = (
                f"Comment ids in this document: {self._format_id_list(comment_ids, 'Com:')}. "
                if comment_ids
                else "This document has no comments to reply to. "
            )
            return f"{prefix}reply on {echo} — no comment with that id exists. " + avail + find_hint

        # AcceptChange / RejectChange
        echo = raw_id if has_prefix else f"Chg:{target_id}"
        if target_id in comment_ids:
            return (
                f"{prefix}{act.type} on {echo} — Com:{target_id} is a comment id, "
                f"not a tracked change. accept/reject act on tracked changes (Chg:…); to respond to a "
                f"comment use `reply`. " + find_hint
            )
        avail = (
            f"Tracked-change ids in this document: {self._format_id_list(change_ids, 'Chg:')}. "
            if change_ids
            else "This document has no tracked changes. "
        )
        return (
            f"{prefix}{act.type} on {echo} — no tracked change with that id exists "
            "(it may already have been accepted or rejected, or the id is stale). " + avail + find_hint
        )

    @staticmethod
    def _expand_group_with_nested(all_ins: set, all_del: set) -> None:
        """
        Extends a resolution group with every <w:ins>/<w:del> NESTED inside a
        group member. Chained edits produce nested revisions (re-deleting text
        a pending insertion introduced stores the transient <w:del> INSIDE the
        <w:ins>); resolving the outer element consumes the nested one with it,
        so its id must be part of the group's bookkeeping — otherwise a batch
        that enumerates every id from a read hard-fails on the nested member
        with "no tracked change with that id exists" (QA round 3, finding 2.1).
        """
        stack = list(all_ins | all_del)
        while stack:
            node = stack.pop()
            for nested in node.findall(f".//{qn('w:ins')}"):
                if nested not in all_ins:
                    all_ins.add(nested)
                    stack.append(nested)
            for nested in node.findall(f".//{qn('w:del')}"):
                if nested not in all_del:
                    all_del.add(nested)
                    stack.append(nested)

    def _resolution_group_ids(self, target_id: str) -> set:
        """
        All revision ids that resolve as ONE unit with `target_id`: the ids of
        every contiguous same-author <w:ins>/<w:del> sibling of its elements
        (a replacement's del+ins pair), plus nested revisions those elements
        contain (chained-edit transients), plus the id itself.
        """
        nodes = [n for n in self.doc.element.findall(f".//{qn('w:ins')}") if n.get(qn("w:id")) == target_id]
        nodes += [n for n in self.doc.element.findall(f".//{qn('w:del')}") if n.get(qn("w:id")) == target_id]
        group = {target_id} if nodes else set()
        group_ins: set = set()
        group_del: set = set()
        for node in nodes:
            (group_ins if node.tag == qn("w:ins") else group_del).add(node)
            for paired in self._get_paired_nodes(node):
                (group_ins if paired.tag == qn("w:ins") else group_del).add(paired)
        self._expand_group_with_nested(group_ins, group_del)
        for member in group_ins | group_del:
            pid = member.get(qn("w:id"))
            if pid:
                group.add(pid)
        return group

    def validate_action_pairing(
        self,
        actions: List[Union[AcceptChange, RejectChange, ReplyComment]],
        indices: Optional[List[int]] = None,
    ) -> List[str]:
        """
        Document-aware validation (QA 2026-07-19 ADEU-QA-004): a replacement's
        del+ins pair carries two distinct ids but resolves as one unit, so a
        batch that accepts one side and rejects the other is contradictory.
        Rejecting it up front — before any action mutates the document — keeps
        the batch transactional; the first-action-silently-wins behavior
        reported the contradictory follow-up as "applied".
        """
        errors: List[str] = []
        group_first: dict = {}  # member id -> (action_pos, action_type, id named by that action)
        for pos, act in enumerate(actions):
            batch_idx = indices[pos] if indices else pos
            if not isinstance(act, (AcceptChange, RejectChange)):
                continue
            raw_id = act.target_id
            if raw_id.startswith("Com:"):
                continue
            target_id = raw_id[4:] if raw_id.startswith("Chg:") else raw_id
            group = self._resolution_group_ids(target_id)
            if not group:
                continue  # unknown ids fail with their own not-found error later
            conflict = None
            for gid in group:
                prior = group_first.get(gid)
                if prior is not None and prior[1] != act.type:
                    conflict = prior
                    break
            if conflict is not None:
                first_pos, first_type, first_id = conflict
                first_batch_idx = indices[first_pos] if indices else first_pos
                errors.append(
                    f"- Action {batch_idx + 1} Failed: conflicting actions on one replacement — Action "
                    f"{first_batch_idx + 1} applies '{first_type}' to Chg:{first_id}, and Chg:{target_id} is "
                    "part of the same change (a replacement's contiguous del+ins pair resolves as one "
                    f"unit, so '{first_type}' already decides both sides). Accepting one side and "
                    "rejecting the other is contradictory — decide the outcome and submit exactly one "
                    "action for the pair."
                )
                continue
            for gid in group:
                group_first.setdefault(gid, (pos, act.type, target_id))
        return errors

    def apply_review_actions(
        self,
        actions: List[Union[AcceptChange, RejectChange, ReplyComment]],
        indices: Optional[List[int]] = None,
    ) -> tuple[int, int, int]:
        """
        Returns (applied, skipped, already_resolved). `applied` counts actions
        that caused an observable state transition; an action naming an id an
        earlier action of this batch already resolved (via its replacement
        pair) is counted in `already_resolved` instead — never as applied
        (QA 2026-07-19 ADEU-QA-004).
        """
        applied = 0
        skipped = 0
        already_resolved = 0
        resolved_history: dict = {}  # revision id -> action type that resolved it

        # Sort actions internally: non-destructive metadata operations (ReplyComment) first,
        # followed by destructive structural operations (AcceptChange, RejectChange).
        # Stable sort preserves the original relative ordering, and we preserve `pos`
        # so diagnostic messages refer to the original array indexes.
        sorted_actions = sorted(enumerate(actions), key=lambda x: 0 if isinstance(x[1], ReplyComment) else 1)

        for pos, act in sorted_actions:
            batch_idx = indices[pos] if indices else pos
            raw_id = act.target_id
            target_id = raw_id

            is_change = False
            is_comment = False

            if raw_id.startswith("Chg:"):
                target_id = raw_id[4:]
                is_change = True
            elif raw_id.startswith("Com:"):
                target_id = raw_id[4:]
                is_comment = True
            else:
                is_change = True
                is_comment = True

            if is_change and isinstance(act, (AcceptChange, RejectChange)) and target_id in resolved_history:
                prior_type = resolved_history[target_id]
                if prior_type == act.type:
                    # Consistent follow-up on the pair: legitimate agent
                    # workflow ("accept both ids of the replacement"), but no
                    # state transition happens — report it accurately.
                    already_resolved += 1
                    self.skipped_details.append(
                        f"- Note: Action {batch_idx + 1} ('{act.type}' on {raw_id}) had no additional effect — "
                        "the change was already resolved together with its replacement pair by an "
                        "earlier action in this batch. Counted as already_resolved, not applied."
                    )
                    continue
                # Contradiction. validate_action_pairing rejects this shape
                # before anything mutates; this guard covers direct callers.
                self.skipped_details.append(
                    f"- Action {batch_idx + 1} Failed: contradictory action — '{act.type}' on {raw_id}, but "
                    f"the change was already resolved as '{prior_type}' together with its replacement "
                    "pair by an earlier action in this batch."
                )
                skipped += 1
                continue

            if is_change and isinstance(act, (AcceptChange, RejectChange)):
                dup_error = self._duplicate_revision_id_error(target_id, act.type, batch_idx=batch_idx)
                if dup_error:
                    self.skipped_details.append(dup_error)
                    skipped += 1
                    continue

            resolved_now = set()
            success = False

            # Accept/reject can delete a comment as a side effect when the
            # comment's anchor falls inside the resolved change. Snapshot the
            # comment ids AND their authors first so a removal is reported
            # explicitly — and attributed — instead of happening silently under
            # "1 applied" (QA 2026-07-22 bug #1; authorship added for
            # BUG_comment_threading_anchoring_and_typography.md B2: "never
            # silently delete a comment authored by someone other than the
            # caller").
            comments_before: set = set()
            comment_authors: Dict[str, str] = {}
            if isinstance(act, (AcceptChange, RejectChange)) and is_change:
                comment_authors = self._comment_authors()
                comments_before = set(comment_authors)

            if isinstance(act, AcceptChange):
                if is_change:
                    resolved_now = self._accept_change(target_id)
                    success = bool(resolved_now)
            elif isinstance(act, RejectChange):
                if is_change:
                    resolved_now = self._reject_change(target_id)
                    success = bool(resolved_now)
            elif isinstance(act, ReplyComment):
                if is_comment:
                    self._reply_threading_error = None
                    success = self._reply_to_comment(target_id, getattr(act, "text", ""))

            if success:
                for rid in resolved_now:
                    if rid:
                        resolved_history[rid] = act.type
                applied += 1
                if comments_before:
                    removed = comments_before - set(self._existing_comment_ids())
                    if removed:
                        self.skipped_details.append(
                            f"- Note: {act.type} on {raw_id} also removed "
                            f"{self._describe_removed_comments(removed, comment_authors)} "
                            "(including any reply thread) because its anchor was inside the resolved "
                            "change. This note is informational — the action itself succeeded."
                        )
            elif isinstance(act, ReplyComment) and self._reply_threading_error:
                # The parent comment exists but the reply could not be threaded
                # onto it. Naming the id kind here would be a lie ("no comment
                # with that id exists"); the caller needs the real reason (B1).
                self.skipped_details.append(
                    f"- Action {batch_idx + 1} Failed: reply on {raw_id} — {self._reply_threading_error}"
                )
                self._reply_threading_error = None
                skipped += 1
            else:
                self.skipped_details.append(self._action_not_found_error(raw_id, target_id, act, batch_idx=batch_idx))
                skipped += 1

        if applied:
            self._mutated_since_load = True
        return applied, skipped, already_resolved

    def _clean_wrapping_comments(self, element, preserve_comments: bool = False):
        """
        Removes comment anchors that tightly wrap this element (or a paired del/ins).
        This prevents orphaned comment ranges from leaking when an edit is accepted/rejected.
        """
        if preserve_comments:
            return
        first_node = element
        while True:
            prev = first_node.getprevious()
            if prev is not None and prev.tag in (qn("w:ins"), qn("w:del")):
                first_node = prev
            else:
                break

        last_node = element
        while True:
            nxt = last_node.getnext()
            if nxt is not None and nxt.tag in (qn("w:ins"), qn("w:del")):
                last_node = nxt
            else:
                break

        starts_to_remove = []
        prev = first_node.getprevious()
        while prev is not None:
            if prev.tag == qn("w:commentRangeStart"):
                starts_to_remove.append(prev)
                prev = prev.getprevious()
            elif prev.tag in (qn("w:rPr"), qn("w:pPr")):
                prev = prev.getprevious()
            else:
                break

        ends_to_remove = []
        nxt = last_node.getnext()
        while nxt is not None:
            if nxt.tag == qn("w:commentRangeEnd"):
                ends_to_remove.append(nxt)
                nxt = nxt.getnext()
            elif nxt.tag == qn("w:r") and nxt.find(f".//{qn('w:commentReference')}") is not None:
                ends_to_remove.append(nxt)
                nxt = nxt.getnext()
            elif nxt.tag == qn("w:commentReference"):
                ends_to_remove.append(nxt)
                nxt = nxt.getnext()
            else:
                break

        end_ids = set()
        for e in ends_to_remove:
            if e.tag == qn("w:commentRangeEnd"):
                end_ids.add(e.get(qn("w:id")))
            else:
                ref = e.find(f".//{qn('w:commentReference')}")
                if ref is None and e.tag == qn("w:commentReference"):
                    ref = e
                if ref is not None:
                    end_ids.add(ref.get(qn("w:id")))

        for s in starts_to_remove:
            c_id = s.get(qn("w:id"))
            if c_id and c_id in end_ids:
                self.comments_manager.delete_comment(c_id)
                if s.getparent() is not None:
                    s.getparent().remove(s)
                for e in ends_to_remove:
                    e_id = None
                    if e.tag == qn("w:commentRangeEnd"):
                        e_id = e.get(qn("w:id"))
                    else:
                        ref = e.find(f".//{qn('w:commentReference')}")
                        if ref is None and e.tag == qn("w:commentReference"):
                            ref = e
                        if ref is not None:
                            e_id = ref.get(qn("w:id"))

                    if e_id == c_id and e.getparent() is not None:
                        e.getparent().remove(e)

    def _delete_comments_in_element(self, element):
        """
        Scans a DOM element scheduled for deletion for strictly encapsulated comment references.
        """
        refs = element.findall(f".//{qn('w:commentReference')}")
        for ref in refs:
            c_id = ref.get(qn("w:id"))
            if c_id:
                self.comments_manager.delete_comment(c_id)
                for tag in ["w:commentRangeStart", "w:commentRangeEnd"]:
                    for node in self.doc.element.findall(f".//{qn(tag)}"):
                        if node.get(qn("w:id")) == c_id and node.getparent() is not None:
                            node.getparent().remove(node)

    def _accept_change(self, target_id: str) -> set:
        primary_ins = [n for n in self.doc.element.findall(f".//{qn('w:ins')}") if n.get(qn("w:id")) == target_id]
        primary_del = [n for n in self.doc.element.findall(f".//{qn('w:del')}") if n.get(qn("w:id")) == target_id]

        if not primary_ins and not primary_del:
            # Format-only tracked changes (w:rPrChange/w:pPrChange) carry ids
            # the projection advertises; they must be actionable by id too
            # (QA round 3, finding 2.2).
            return self._resolve_format_change(target_id, accept=True)

        all_ins = set(primary_ins)
        all_del = set(primary_del)

        for node in primary_ins + primary_del:
            for paired in self._get_paired_nodes(node):
                if paired.tag == qn("w:ins"):
                    all_ins.add(paired)
                elif paired.tag == qn("w:del"):
                    all_del.add(paired)

        # Chained edits nest revisions (a transient <w:del> inside a pending
        # <w:ins>); they resolve together with their host (QA round 3, 2.1).
        self._expand_group_with_nested(all_ins, all_del)

        resolved_ids = set()
        for node in all_ins | all_del:
            resolved_ids.add(node.get(qn("w:id")))

        for ins in all_ins:
            self._clean_wrapping_comments(ins, preserve_comments=True)
            parent = ins.getparent()
            if parent is None:
                continue

            if parent.tag == qn("w:trPr"):
                parent.remove(ins)
                continue

            index = parent.index(ins)
            for child in list(ins):
                parent.insert(index, child)
                index += 1
            parent.remove(ins)

        for d in all_del:
            self._clean_wrapping_comments(d, preserve_comments=False)
            self._delete_comments_in_element(d)
            parent = d.getparent()
            if parent is not None:
                if parent.tag == qn("w:trPr"):
                    row = parent.getparent()
                    if row is not None:
                        row.getparent().remove(row)
                    continue
                # Tracked PARAGRAPH-BREAK deletion (pilcrow del inside
                # pPr/rPr, part of a fully-deleted paragraph — F1, QA
                # 2026-07-23): accepting it removes the paragraph container
                # when no visible content survives, mirroring Safe Paragraph
                # Acceptance in accept_all_revisions. If content survives,
                # only the marker is stripped and the container is preserved.
                grandparent = parent.getparent()
                if parent.tag == qn("w:rPr") and grandparent is not None and grandparent.tag == qn("w:pPr"):
                    p_el = grandparent.getparent()
                    if (
                        p_el is not None
                        and p_el.tag == qn("w:p")
                        and not self._paragraph_has_visible_content(p_el)
                        and not self._is_last_paragraph_in_cell(p_el)
                    ):
                        body = p_el.getparent()
                        if body is not None:
                            body.remove(p_el)
                        continue
                parent.remove(d)

        return resolved_ids

    def _reject_change(self, target_id: str) -> set:
        primary_ins = [n for n in self.doc.element.findall(f".//{qn('w:ins')}") if n.get(qn("w:id")) == target_id]
        primary_del = [n for n in self.doc.element.findall(f".//{qn('w:del')}") if n.get(qn("w:id")) == target_id]

        if not primary_ins and not primary_del:
            # Format-only tracked changes: restore the stored original
            # properties (QA round 3, finding 2.2).
            return self._resolve_format_change(target_id, accept=False)

        all_ins = set(primary_ins)
        all_del = set(primary_del)

        for node in primary_ins + primary_del:
            for paired in self._get_paired_nodes(node):
                if paired.tag == qn("w:ins"):
                    all_ins.add(paired)
                elif paired.tag == qn("w:del"):
                    all_del.add(paired)

        # Chained edits nest revisions (a transient <w:del> inside a pending
        # <w:ins>); they resolve together with their host (QA round 3, 2.1).
        self._expand_group_with_nested(all_ins, all_del)

        resolved_ids = set()
        for node in all_ins | all_del:
            resolved_ids.add(node.get(qn("w:id")))

        for ins in all_ins:
            self._clean_wrapping_comments(ins, preserve_comments=False)
            self._delete_comments_in_element(ins)
            parent = ins.getparent()
            if parent is None:
                continue

            if parent.tag == qn("w:trPr"):
                # Tracked row insertion → reject by removing the row entirely.
                row = parent.getparent()
                if row is not None:
                    row.getparent().remove(row)
                continue

            # Tracked PARAGRAPH-BREAK insertion lives inside <w:pPr>/<w:rPr>.
            # Rejecting the paragraph break means the paragraph itself shouldn't
            # exist — the inserted break created the paragraph boundary.
            # Remove the entire <w:p> rather than just the <w:ins> marker
            # (which would leave behind an empty orphan paragraph).
            grandparent = parent.getparent()
            if parent.tag == qn("w:rPr") and grandparent is not None and grandparent.tag == qn("w:pPr"):
                p_el = grandparent.getparent()
                if p_el is not None and p_el.tag == qn("w:p") and not self._is_last_paragraph_in_cell(p_el):
                    body = p_el.getparent()
                    if body is not None:
                        body.remove(p_el)
                    continue
                # Fallthrough if the structure is unexpected — just remove the
                # marker so we don't leave it behind.
                parent.remove(ins)
                continue

            parent.remove(ins)

        for d in all_del:
            self._clean_wrapping_comments(d, preserve_comments=True)
            parent = d.getparent()
            if parent is None:
                continue

            if parent.tag == qn("w:trPr"):
                parent.remove(d)
                continue

            index = parent.index(d)
            for child in list(d):
                for dt in child.findall(f".//{qn('w:delText')}"):
                    dt.tag = qn("w:t")
                    if dt.text is not None and dt.text.strip() != dt.text:
                        dt.set(qn("xml:space"), "preserve")
                parent.insert(index, child)
                index += 1
            parent.remove(d)

        return resolved_ids

    _FORMAT_CHANGE_TAGS = ("w:rPrChange", "w:pPrChange", "w:sectPrChange")

    def _resolve_format_change(self, target_id: str, accept: bool) -> set:
        """
        Accept/reject a FORMAT-only tracked change (<w:rPrChange>,
        <w:pPrChange>, <w:sectPrChange>) by id. The projection advertises
        these as "[Chg:N format]", so per-id targeting must work exactly like
        it does for insertions/deletions (QA round 3, finding 2.2).

        Accept keeps the new formatting: drop the change element recording
        the original. Reject restores the original: the change element's
        single child holds the pre-change properties — swap them into the
        live properties container.
        """
        resolved: set = set()
        for tag in self._FORMAT_CHANGE_TAGS:
            for change in self.doc.element.findall(f".//{qn(tag)}"):
                if change.get(qn("w:id")) != target_id:
                    continue
                parent = change.getparent()
                if parent is None:
                    continue
                if accept:
                    parent.remove(change)
                else:
                    stored = next(iter(change), None)
                    parent.remove(change)
                    preserved = PROPS_REVERT_PRESERVED_CHILDREN.get(parent.tag, ())
                    for child in list(parent):
                        # A pilcrow revision (w:ins/w:del inside pPr's rPr) is
                        # a separate pending change — never wipe it while
                        # restoring formatting properties.
                        if child.tag == qn("w:rPr") and any(child.find(qn(t)) is not None for t in ("w:ins", "w:del")):
                            continue
                        # Properties the stored record cannot carry (section
                        # header/footer references) would be destroyed rather
                        # than reverted.
                        if child.tag in preserved:
                            continue
                        parent.remove(child)
                    if stored is not None:
                        for child in list(stored):
                            parent.append(deepcopy(child))
                resolved.add(target_id)
        return resolved

    def _reply_to_comment(self, target_id: str, text: str) -> bool:
        if not self.comments_manager.comments_part:
            return False

        existing_comments = self.comments_manager.extract_comments_data()
        if target_id not in existing_comments:
            return False

        try:
            new_comment_id = self.comments_manager.add_comment(self.author, text, parent_id=target_id)
        except CommentThreadingError as exc:
            # A reply that cannot be threaded must NOT be written as a new
            # top-level comment. The old path wrote it anyway and reported
            # success, so the agent believed it had answered the reviewer, saw
            # a stray comment instead, retried, and made the document worse
            # (BUG_comment_threading_anchoring_and_typography.md B1).
            logger.warning("Refusing to write an unthreadable reply", target_id=target_id, error=str(exc))
            self._reply_threading_error = str(exc)
            return False

        self._anchor_reply_comment(target_id, new_comment_id)
        return True

    def _anchor_reply_comment(self, parent_id: str, new_id: str):
        starts = self.doc.element.xpath(f"//w:commentRangeStart[@w:id='{parent_id}']")
        if not starts:
            logger.warning("Parent comment start not found during reply", parent_id=parent_id)
            return

        parent_start = starts[0]
        new_start = create_element("w:commentRangeStart")
        create_attribute(new_start, "w:id", new_id)
        parent_start.addnext(new_start)

        ends = self.doc.element.xpath(f"//w:commentRangeEnd[@w:id='{parent_id}']")
        if not ends:
            return

        parent_end = ends[0]
        new_end = create_element("w:commentRangeEnd")
        create_attribute(new_end, "w:id", new_id)

        parent_refs = self.doc.element.xpath(f"//w:commentReference[@w:id='{parent_id}']")
        insertion_point = parent_end

        if parent_refs:
            ref_el = parent_refs[0]
            if ref_el.getparent().tag == qn("w:r"):
                insertion_point = ref_el.getparent()

        insertion_point.addnext(new_end)

        ref_run = create_element("w:r")
        rPr = create_element("w:rPr")
        rStyle = create_element("w:rStyle")
        create_attribute(rStyle, "w:val", "CommentReference")
        rPr.append(rStyle)
        ref_run.append(rPr)

        ref = create_element("w:commentReference")
        create_attribute(ref, "w:id", new_id)
        ref_run.append(ref)

        new_end.addnext(ref_run)

    # FILE: src/adeu/redline/engine.py
    def accept_all_revisions(self, remove_comments: bool = False) -> dict[str, int]:
        # This rewrites the tree (and non-main parts), so the load-time
        # pristine bytes are no longer this engine's state. Flag it BEFORE the
        # work: a later process_batch would otherwise snapshot
        # BytesIO(self._pristine_bytes) and a validation rollback would
        # resurrect every revision accepted here. Set unconditionally rather
        # than only when a count is non-zero — comment removal and the
        # non-main parts mutate too.
        self._mutated_since_load = True
        parts_to_process = [self.doc.element]

        for part in self.doc.part.package.parts:
            if part == self.doc.part:
                continue
            if "wordprocessingml" in part.content_type and part.content_type.endswith("+xml"):
                element_to_process = None
                if hasattr(part, "_element"):
                    element_to_process = part._element
                else:
                    if not hasattr(part, "_adeu_element"):
                        part._adeu_element = parse_xml(part.blob)  # type: ignore[attr-defined]
                    element_to_process = part._adeu_element  # type: ignore[attr-defined]
                parts_to_process.append(element_to_process)

        # Pre-count revisions and comments before modifying the XML structures.
        # The unit is REVISION ELEMENTS, matching sanitize's
        # transforms.count_tracked_changes so the two surfaces can never report
        # different totals for the same document. Word fragments one logical
        # revision across several w:ins when formatting changes mid-revision
        # (see AI_CONTEXT §10), so this counts marks, not user intentions —
        # said plainly in the CLI --help rather than left for a caller to
        # discover. Formatting revisions (w:rPrChange/w:pPrChange/w:sectPrChange)
        # are accepted by this method too, so they are counted too; omitting
        # them reported 0 changes for a document that demonstrably changed.
        accepted_insertions = 0
        accepted_deletions = 0
        accepted_formatting = 0
        for root_element in parts_to_process:
            accepted_insertions += len(root_element.findall(f".//{qn('w:ins')}"))
            accepted_deletions += len(root_element.findall(f".//{qn('w:del')}"))
            for tag in ("w:rPrChange", "w:pPrChange", "w:sectPrChange"):
                accepted_formatting += len(root_element.findall(f".//{qn(tag)}"))

        # Only claim comments were removed when they actually are — and count
        # EVERY body this call deletes, not just the ones a `remove_comments`
        # request asked for. Accepting a deletion whose range carries a comment
        # anchor removes that comment (Word behaves the same), so hard-coding 0
        # for remove_comments=False reported "nothing happened" while a human's
        # comment was gone — the silent data loss that let the reported run be
        # rationalised as a success (B2).
        comment_authors_before = self._comment_authors()
        comments_before = set(comment_authors_before)
        self.removed_comment_notes = []

        for root_element in parts_to_process:
            for ins in root_element.findall(f".//{qn('w:ins')}"):
                self._clean_wrapping_comments(ins, preserve_comments=True)
                parent = ins.getparent()
                if parent is None:
                    continue

                if parent.tag == qn("w:trPr"):
                    parent.remove(ins)
                    continue

                index = parent.index(ins)
                for child in list(ins):
                    parent.insert(index, child)
                    index += 1
                parent.remove(ins)

            for p in root_element.findall(f".//{qn('w:p')}"):
                pPr = p.find(qn("w:pPr"))
                if pPr is not None:
                    rPr = pPr.find(qn("w:rPr"))
                    del_mark = rPr.find(qn("w:del")) if rPr is not None else None
                    if rPr is not None and del_mark is not None:
                        has_content = False
                        for tag in ["w:t", "w:tab", "w:br"]:
                            for child in p.findall(f".//{qn(tag)}"):
                                if tag == "w:t" and not child.text:
                                    continue
                                is_deleted = False
                                curr = child.getparent()
                                while curr is not None and curr != p:
                                    if curr.tag == qn("w:del"):
                                        is_deleted = True
                                        break
                                    curr = curr.getparent()
                                if not is_deleted:
                                    has_content = True
                                    break
                            if has_content:
                                break

                        if has_content or self._is_last_paragraph_in_cell(p):
                            rPr.remove(del_mark)
                        else:
                            self._clean_wrapping_comments(p, preserve_comments=False)
                            self._delete_comments_in_element(p)
                            if p.getparent() is not None:
                                p.getparent().remove(p)

            for d in root_element.findall(f".//{qn('w:del')}"):
                self._clean_wrapping_comments(d, preserve_comments=False)
                self._delete_comments_in_element(d)
                parent = d.getparent()
                if parent is not None:
                    if parent.tag == qn("w:trPr"):
                        row = parent.getparent()
                        if row is not None:
                            row.getparent().remove(row)
                    else:
                        parent.remove(d)

        # Final pass: remove all comments and eject the comment parts/relationships.
        # accept_all_revisions semantically means "produce a finalized clean document"
        # (per the tool docstring), so all comments (including free-standing ones)
        # must be removed completely when remove_comments is True.
        if remove_comments:
            # 1. Strip all in-body comment anchors and reference runs from all parts to process
            for root_element in parts_to_process:
                for tag in ("w:commentRangeStart", "w:commentRangeEnd"):
                    for el in root_element.findall(f".//{qn(tag)}"):
                        parent = el.getparent()
                        if parent is not None:
                            parent.remove(el)

                for ref in list(root_element.findall(f".//{qn('w:commentReference')}")):
                    parent = ref.getparent()
                    if parent is not None:
                        if parent.tag == qn("w:r"):
                            grandparent = parent.getparent()
                            if grandparent is not None:
                                non_rpr_children = [c for c in list(parent) if c.tag != qn("w:rPr")]
                                if len(non_rpr_children) <= 1:
                                    grandparent.remove(parent)
                                else:
                                    parent.remove(ref)
                            else:
                                parent.remove(ref)
                        else:
                            parent.remove(ref)

            # 2. Completely eject all comment XML parts and relationships from the package
            pkg = self.doc.part.package
            comment_partnames = set()
            for part in pkg.parts:
                if str(part.partname).startswith("/word/comments"):
                    comment_partnames.add(part.partname)

            if comment_partnames:
                # Sever relationships from package root rels
                root_rels_to_remove = [
                    rId
                    for rId, rel in pkg.rels.items()
                    if not rel.is_external and getattr(rel.target_part, "partname", None) in comment_partnames
                ]
                for rId in root_rels_to_remove:
                    del pkg.rels[rId]

                # Sever relationships from all other parts (including main document part)
                for part in pkg.parts:
                    part_rels_to_remove = [
                        rId
                        for rId, rel in part.rels.items()
                        if not rel.is_external and getattr(rel.target_part, "partname", None) in comment_partnames
                    ]
                    for rId in part_rels_to_remove:
                        del part.rels[rId]

                # Remove from package parts list in-place
                if hasattr(pkg, "_parts") and isinstance(pkg._parts, list):
                    pkg._parts[:] = [p for p in pkg._parts if p.partname not in comment_partnames]
                elif hasattr(pkg, "parts") and isinstance(pkg.parts, list):
                    pkg.parts[:] = [p for p in pkg.parts if p.partname not in comment_partnames]

        # Books that match the document: when remove_comments ejected the parts
        # the "after" set is empty, so this still equals the total; when it did
        # not, it counts exactly the anchors this call consumed. Each removal is
        # attributed so no caller can mistake a human's comment for engine
        # bookkeeping (B2).
        removed_ids = comments_before - set(self._existing_comment_ids())
        self.removed_comment_notes = [
            f"Com:{cid} (by {comment_authors_before.get(cid, 'Unknown')})"
            for cid in sorted(removed_ids, key=lambda x: (int(x) if x.isdigit() else 0, x))
        ]

        return {
            "accepted_insertions": accepted_insertions,
            "accepted_deletions": accepted_deletions,
            "accepted_formatting": accepted_formatting,
            "removed_comments": len(removed_ids),
        }

    def reject_all_revisions(self):
        """
        Revert every tracked change, returning the document to the state it had
        before any revision was proposed. The exact inverse of
        accept_all_revisions:

          * <w:ins>  -> removed together with all of its content (the proposed
                        insertion never existed). An inserted table row (an
                        <w:ins> inside <w:trPr>) drops the whole row.
          * <w:del>  -> unwrapped, restoring the original text (<w:delText>
                        becomes <w:t> again). A row-deletion mark inside <w:trPr>
                        is removed so the row survives.
          * paragraph-mark <w:del> in pPr/rPr -> removed, so a proposed paragraph
                        merge is undone and the paragraphs stay split.

        Comments are annotations, not revisions, so standalone comments are left
        in place; only comment anchors stranded inside a rejected insertion are
        cleaned up.

        Insertions are reverted before deletions are restored so that a deletion
        nested inside a foreign author's insertion (<w:ins A><w:del B>…</w:del>
        </w:ins>) is removed wholesale with the insertion — the contingent text
        correctly disappears rather than being promoted to committed body text.

        Known limitation: tracked paragraph STRUCTURE changes (a split recorded
        as a pilcrow <w:ins>, or a merge recorded as a pilcrow <w:del>) are
        reverted only to the extent of dropping/keeping the mark; the original
        paragraph boundary is not reconstructed, because the merge protocol
        coalesces paragraphs destructively at edit time. Reverting run-level
        insertions/deletions (the common case) is exact. This limitation is
        shared with the Node engine.
        """
        # See accept_all_revisions: flag before mutating, or a later batch's
        # rollback restores pre-reject bytes and resurrects the revisions.
        self._mutated_since_load = True
        parts_to_process = [self.doc.element]

        for part in self.doc.part.package.parts:
            if part == self.doc.part:
                continue
            if "wordprocessingml" in part.content_type and part.content_type.endswith("+xml"):
                element_to_process = None
                if hasattr(part, "_element"):
                    element_to_process = part._element
                else:
                    if not hasattr(part, "_adeu_element"):
                        part._adeu_element = parse_xml(part.blob)  # type: ignore[attr-defined]
                    element_to_process = part._adeu_element  # type: ignore[attr-defined]
                parts_to_process.append(element_to_process)

        for root_element in parts_to_process:
            # 1. Reject insertions: drop the <w:ins> and everything inside it.
            #    findall walks in document order, so an outer <w:ins> is handled
            #    before any nested one; removing the outer detaches the inner,
            #    whose later (no-op) processing is guarded by the parent check.
            for ins in root_element.findall(f".//{qn('w:ins')}"):
                parent = ins.getparent()
                if parent is None:
                    continue
                self._clean_wrapping_comments(ins, preserve_comments=False)
                self._delete_comments_in_element(ins)
                if parent.tag == qn("w:trPr"):
                    row = parent.getparent()
                    if row is not None and row.getparent() is not None:
                        row.getparent().remove(row)
                else:
                    parent.remove(ins)

            # 2. Reject paragraph-mark deletions: keep the paragraph break.
            for p in root_element.findall(f".//{qn('w:p')}"):
                pPr = p.find(qn("w:pPr"))
                if pPr is not None:
                    rPr = pPr.find(qn("w:rPr"))
                    if rPr is not None:
                        del_mark = rPr.find(qn("w:del"))
                        if del_mark is not None:
                            rPr.remove(del_mark)

            # 3. Reject deletions: restore the original text.
            for d in root_element.findall(f".//{qn('w:del')}"):
                parent = d.getparent()
                if parent is None:
                    continue
                self._clean_wrapping_comments(d, preserve_comments=True)
                if parent.tag == qn("w:trPr"):
                    parent.remove(d)
                    continue
                for dt in d.findall(f".//{qn('w:delText')}"):
                    dt.tag = qn("w:t")
                index = parent.index(d)
                for child in list(d):
                    parent.insert(index, child)
                    index += 1
                parent.remove(d)
