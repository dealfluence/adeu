import datetime
import re
from collections import defaultdict
from copy import deepcopy
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

import lxml.etree as etree
import structlog
from docx import Document
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
)
from adeu.pagination import paginate, split_structural_appendix
from adeu.redline.comments import CommentsManager
from adeu.redline.mapper import DocumentMapper, TextSpan
from adeu.utils.docx import create_attribute, create_element, strip_bom_from_docx_bytes
from adeu.utils.safe_regex import RegexTimeoutError
from adeu.utils.text import PREVIEW_TEXT_CAP, REPORT_ECHO_CAP, truncate_middle

logger = structlog.get_logger(__name__)

# Width of the surrounding-document window shown in redline previews.
PREVIEW_CONTEXT_CHARS = 30

# Register w16du namespace for dateUtc
w16du_ns = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
if "w16du" not in nsmap:
    nsmap["w16du"] = w16du_ns


def _empty_bounds() -> List[Optional[int]]:
    return [None, None]


class BatchValidationError(Exception):
    """Raised when text edits fail location validation."""

    def __init__(self, errors: List[str]):
        super().__init__("Batch validation failed:\n" + "\n".join(errors))
        self.errors = errors


# Characters XML 1.0 cannot represent: C0 controls except tab/newline/CR.
# lxml refuses to serialize them, so without an up-front check they surfaced
# as a raw "All strings must be XML compatible" traceback from deep inside
# lxml instead of a clean per-edit error (QA 2026-07-17 F11).
XML_ILLEGAL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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
        act_type = getattr(act, "type", "")
        target_id = getattr(act, "target_id", "")
        if act_type == "reply":
            reply_text = (getattr(act, "text", "") or "").strip()
            if not reply_text:
                errors.append(
                    f"- Action {i + 1} Failed: reply text for {target_id} is empty or "
                    "whitespace-only. Word would show a blank comment bubble — provide the "
                    "reply content in 'text'."
                )
                continue
            reply_key = (target_id, reply_text)
            if reply_key in seen_replies:
                errors.append(
                    f"- Action {i + 1} Failed: duplicate reply — this batch already replies to "
                    f"{target_id} with the same text. Remove the duplicate action."
                )
            seen_replies.add(reply_key)
        elif act_type in ("accept", "reject"):
            prior = seen_resolutions.get(target_id)
            if prior is not None:
                first_idx, first_type = prior
                if first_type == act_type:
                    errors.append(
                        f"- Action {i + 1} Failed: duplicate action — Action {first_idx + 1} in this "
                        f"batch already applies '{act_type}' to {target_id}. A change can only be "
                        "resolved once; remove the duplicate action."
                    )
                else:
                    errors.append(
                        f"- Action {i + 1} Failed: conflicting actions — Action {first_idx + 1} in "
                        f"this batch applies '{first_type}' to {target_id}, but this action applies "
                        f"'{act_type}'. Decide the outcome and keep exactly one of them."
                    )
            else:
                seen_resolutions[target_id] = (i, act_type)
    return errors


def validate_edit_strings(
    edits: List[Union["ModifyText", "InsertTableRow", "DeleteTableRow"]],
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
        t_text = edit.target_text or ""
        n_text = getattr(edit, "new_text", "") or ""

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
                        "Use a dedicated structural operation."
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
    def __init__(self, doc_stream: BytesIO, author: str = "Adeu AI"):
        doc_stream.seek(0)
        sanitized_bytes = strip_bom_from_docx_bytes(doc_stream.read())
        self.doc = Document(BytesIO(sanitized_bytes))

        w16du_ns_str = 'xmlns:w16du="http://schemas.microsoft.com/office/word/2023/wordml/word16du"'

        # Only the main document part is stamped with the w16du namespace up
        # front, because that is the only part this engine writes tracked
        # changes (carrying w16du:dateUtc) to in __init__'s normal flow.
        # Eagerly stamping headers/footers/footnotes corrupts the invariant
        # that an UNMODIFIED part stays byte-for-byte untouched (report F9 /
        # TC5): editing document.xml must not add the w16du namespace to a
        # header that was never edited. Parts that later receive a tracked
        # change get the namespace injected at write time as needed.
        parts_to_inject = [self.doc.part]

        for part in parts_to_inject:
            if hasattr(part, "_element"):
                part._adeu_element = part._element  # type: ignore[attr-defined]
            elif not hasattr(part, "_adeu_element"):
                part._adeu_element = parse_xml(part.blob)  # type: ignore[attr-defined]

            xml_bytes = etree.tostring(part._adeu_element, encoding="utf-8", pretty_print=False)  # type: ignore[attr-defined]
            xml_str = xml_bytes.decode("utf-8")

            if 'xmlns:w16du="' not in xml_str and "xmlns:w16du='" not in xml_str:
                xml_str = re.sub(r"(<w:[a-zA-Z0-9_]+ )", r"\1" + w16du_ns_str + " ", xml_str, count=1)
                part._adeu_element = parse_xml(xml_str.encode("utf-8"))  # type: ignore[attr-defined]
                if hasattr(part, "_element"):
                    part._element = part._adeu_element  # type: ignore[attr-defined]
                    if part == self.doc.part:
                        self.doc._element = self.doc.part._element

        self.author = author
        self.timestamp = (
            datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self.current_id = self._scan_existing_ids()
        self.mapper = DocumentMapper(self.doc)
        self.comments_manager = CommentsManager(self.doc)
        self.clean_mapper: Optional[DocumentMapper] = None
        self.original_mapper: Optional[DocumentMapper] = None
        self.skipped_details: List[str] = []

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

    def _build_edit_context_previews(self, edit: Any) -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(edit, ModifyText):
            return None, None
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

    def _get_paired_nodes(self, node):
        """
        Finds all contiguous w:ins/w:del nodes that form a single logical Modification block.
        """
        pairs = set()
        author = node.get(qn("w:author"))

        # Look forward
        nxt = node.getnext()
        while nxt is not None:
            if nxt.tag in (
                qn("w:commentRangeStart"),
                qn("w:commentRangeEnd"),
                qn("w:commentReference"),
                qn("w:rPr"),
                qn("w:pPr"),
            ):
                nxt = nxt.getnext()
                continue
            if nxt.tag in (qn("w:ins"), qn("w:del")):
                # Group contiguous edits by the same author
                if nxt.get(qn("w:author")) == author:
                    pairs.add(nxt)
                    nxt = nxt.getnext()
                    continue
            break

        # Look backward
        prev = node.getprevious()
        while prev is not None:
            if prev.tag in (
                qn("w:commentRangeStart"),
                qn("w:commentRangeEnd"),
                qn("w:commentReference"),
                qn("w:rPr"),
                qn("w:pPr"),
            ):
                prev = prev.getprevious()
                continue
            if prev.tag in (qn("w:ins"), qn("w:del")):
                if prev.get(qn("w:author")) == author:
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

        if first_style:
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
                    pPr_clone = deepcopy(current_p.pPr)
                    pStyle_el = pPr_clone.find(qn("w:pStyle"))
                    if pStyle_el is not None:
                        style_val = pStyle_el.get(qn("w:val"))
                        is_heading = (
                            style_val.startswith("Heading")
                            or style_val == "Title"
                            or style_val.replace(" ", "").startswith("Heading")
                        )
                        if style_val and is_heading:
                            pPr_clone.remove(pStyle_el)
                    outlineLvl_el = pPr_clone.find(qn("w:outlineLvl"))
                    if outlineLvl_el is not None:
                        pPr_clone.remove(outlineLvl_el)
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
                    pPr_clone = deepcopy(current_p.pPr)
                    pStyle_el = pPr_clone.find(qn("w:pStyle"))
                    if pStyle_el is not None:
                        style_val = pStyle_el.get(qn("w:val"))
                        is_heading = (
                            style_val.startswith("Heading")
                            or style_val == "Title"
                            or style_val.replace(" ", "").startswith("Heading")
                        )
                        if style_val and is_heading:
                            pPr_clone.remove(pStyle_el)
                    outlineLvl_el = pPr_clone.find(qn("w:outlineLvl"))
                    if outlineLvl_el is not None:
                        pPr_clone.remove(outlineLvl_el)
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
        # change (mirrors the reuse_id pattern used by track_insert).
        shared_id = self._get_next_id()
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
        p_element.insert(0, pPr)

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

    def validate_edits(
        self,
        edits: List[Union[ModifyText, InsertTableRow, DeleteTableRow]],
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

            # BUG-23-5: a copy of the target that lives entirely inside a tracked
            # deletion (<w:del>) is not a live, editable occurrence and must not
            # count toward ambiguity. Drop matches whose overlapping real text is
            # exclusively deleted. Only applies to the raw mapper (the clean mapper
            # already omits deleted text).
            if active_text == self.mapper.full_text and len(matches) > 1:
                live_matches = []
                for start, length in matches:
                    real_spans = [
                        s
                        for s in target_mapper.spans
                        if s.run is not None and s.end > start and s.start < start + length
                    ]
                    if not real_spans or any(not s.del_id for s in real_spans):
                        live_matches.append((start, length))
                if live_matches:
                    matches = live_matches

            # The structural appendix is not part of the mapper's
            # projection, so all matches are valid document body matches.
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
                        author_hints = []
                        for auth in sorted(ins_authors_to_ids.keys()):
                            sorted_ids = sorted(ins_authors_to_ids[auth], key=lambda x: int(x) if x.isdigit() else 0)
                            id_hints = ", ".join(f"Chg:{cid}" for cid in sorted_ids)
                            author_hints.append(f"{auth} (e.g. {id_hints})" if id_hints else auth)
                        errors.append(
                            f"- Edit {i + 1} Failed: Modification targets an active insertion from another author "
                            f"({', '.join(author_hints)}). Accept that change first or scope your edit outside of it."
                        )
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
        self.clean_mapper = None
        self.original_mapper = None

    def _restore_from_snapshot(self, snapshot: Optional[BytesIO]) -> None:
        """
        Rolls the engine back to a pre-batch snapshot (as produced by
        save_to_stream). Used for transactional rejection: when any edit in a
        sequential batch fails validation, every edit the batch already
        applied is undone before the BatchValidationError propagates.
        """
        if snapshot is None:
            return
        self.__init__(snapshot, author=self.author)  # type: ignore[misc]

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

    def _build_edit_report(self, edit: Any) -> dict:
        """Builds the per-edit result dict after apply_edits ran on the edit."""
        success = getattr(edit, "_applied_status", False)
        edit_error_msg = getattr(edit, "_error_msg", None)
        critic_markup = None
        clean_text = None
        # Punctuation-anchor warning is failure-context only: on success
        # the redline preview below already reports the change cleanly.
        warning = None
        if success:
            critic_markup, clean_text = self._build_edit_context_previews(edit)
        else:
            warning = self._check_punctuation_warning(getattr(edit, "target_text", ""))
        return {
            "status": "applied" if success else "failed",
            "type": getattr(edit, "type", "modify"),
            # Echoes of caller-supplied values are bounded so an oversized edit
            # cannot balloon the report/JSON output (QA C2).
            "target_text": truncate_middle(getattr(edit, "target_text", ""), REPORT_ECHO_CAP),
            "new_text": truncate_middle(self._report_new_text(edit), REPORT_ECHO_CAP),
            "warning": warning,
            "error": edit_error_msg,
            "critic_markup": critic_markup,
            "clean_text": clean_text,
            "pages": getattr(edit, "_pages", []),
            "heading_path": getattr(edit, "_heading_path", ""),
            "occurrences_modified": getattr(edit, "_occurrences_modified", 0),
            "match_mode": getattr(edit, "match_mode", "strict"),
        }

    def process_batch(self, changes: List[DocumentChange], dry_run: bool = False) -> dict:
        """
        Processes a unified batch of actions and edits safely.
        """
        if dry_run:
            dry_engine = RedlineEngine(self.save_to_stream(), author=self.author)
            return dry_engine._process_batch_internal(changes, dry_run_mode=True)
        else:
            return self._process_batch_internal(changes, dry_run_mode=False)

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

    def _process_batch_internal(self, changes: List[DocumentChange], dry_run_mode: bool = False) -> dict:
        """
        Internal execution engine for batches of edits and actions.
        """
        self.skipped_details = []
        actions = [c for c in changes if isinstance(c, (AcceptChange, RejectChange, ReplyComment))]
        edits = [c for c in changes if isinstance(c, (ModifyText, InsertTableRow, DeleteTableRow))]

        applied_actions, skipped_actions, already_resolved_actions = 0, 0, 0
        if actions:
            action_shape_errors = validate_review_action_batch(actions)
            if action_shape_errors:
                raise BatchValidationError(action_shape_errors)
            # Document-aware pairing check BEFORE any action mutates the DOM:
            # accept + reject across one replacement's del+ins pair is a
            # contradiction, not two independent operations (ADEU-QA-004).
            pairing_errors = self.validate_action_pairing(actions)
            if pairing_errors:
                raise BatchValidationError(pairing_errors)
            applied_actions, skipped_actions, already_resolved_actions = self.apply_review_actions(actions)
            if skipped_actions > 0:
                raise BatchValidationError(self.skipped_details)
            if edits:
                self.clean_mapper = None
                self.original_mapper = None

        body_text, _ = split_structural_appendix(self.mapper.full_text)
        pag_res = paginate(body_text, "")
        page_offsets = pag_res.body_page_offsets

        edits_reports = []
        applied_edits, skipped_edits = 0, 0

        if edits:
            # Batches apply SEQUENTIALLY: each edit is validated and applied
            # against the document state produced by the edits before it, so a
            # later edit may target text an earlier edit introduced (chaining).
            # Both modes run this same loop — dry-run on the cloned engine,
            # real mode on this one — so their reports agree by construction
            # (QA M1). Validation failures keep the batch transactional: the
            # real run restores the pre-batch snapshot and rejects everything;
            # dry-run reports the identical outcome per edit.
            pre_batch_snapshot = None if dry_run_mode else self.save_to_stream()

            cloned_edits = [deepcopy(e) for e in edits]

            def _pinned_idx(e: Any) -> Optional[int]:
                if e._resolved_start_idx is not None:
                    return e._resolved_start_idx
                return e._match_start_index

            # Caller-pinned indexes (e.g. generate_edits_from_text output) are
            # coordinates in the INITIAL document state. Apply them first in
            # one descending sweep — positions below an applied edit never
            # move — then let text-anchored edits re-resolve sequentially
            # against the mutated text. Mirrors the Node engine's ordering.
            pinned = [(i, e) for i, e in enumerate(cloned_edits) if _pinned_idx(e) is not None]
            unpinned = [(i, e) for i, e in enumerate(cloned_edits) if _pinned_idx(e) is None]

            reports_by_input: List[Optional[dict]] = [None] * len(cloned_edits)
            validation_errors: List[str] = []
            failed_validation_indices: set = set()

            # Caller-pinned edits resolve by position, so the document-context
            # checks (not-found / ambiguity) don't apply to them — but the
            # string-shape checks do, exactly as the validate_edits docstring
            # promises. Without this, the text-diff path writes raw CriticMarkup
            # (including reviewer names and change IDs) into document bodies as
            # prose (QA 2026-07-17 F8).
            pinned_ok: List[Tuple[int, Any]] = []
            for i, e in pinned:
                shape_errors = validate_edit_strings([e], index_offset=i)
                if shape_errors:
                    validation_errors.extend(shape_errors)
                    failed_validation_indices.add(i)
                    skipped_edits += 1
                    reports_by_input[i] = {
                        "status": "failed",
                        "type": getattr(e, "type", "modify"),
                        "target_text": truncate_middle(getattr(e, "target_text", ""), REPORT_ECHO_CAP),
                        "new_text": truncate_middle(self._report_new_text(e), REPORT_ECHO_CAP),
                        "warning": None,
                        "error": "\n".join(shape_errors),
                        "critic_markup": None,
                        "clean_text": None,
                    }
                else:
                    pinned_ok.append((i, e))

            if pinned_ok:
                p_applied, p_skipped = self.apply_edits([e for _, e in pinned_ok], page_offsets=page_offsets)
                applied_edits += p_applied
                skipped_edits += p_skipped
                for i, e in pinned_ok:
                    reports_by_input[i] = self._build_edit_report(e)
                if p_applied > 0:
                    self._refresh_after_sequential_edit()

            for i, edit in unpinned:
                try:
                    single_errors = self.validate_edits([edit], index_offset=i)
                except RegexTimeoutError as e:
                    # A pathological user pattern must fail as a clean per-edit
                    # validation error, never a hang or traceback (QA F5).
                    single_errors = [f"- Edit {i + 1} Failed: {e}"]
                if single_errors:
                    if applied_edits > 0:
                        hint = (
                            f"\n  Note: {applied_edits} earlier edit(s) in this batch were already "
                            "applied. Batches apply sequentially — each edit must target the document "
                            "text as it reads AFTER the preceding edits (e.g. target the replacement "
                            "text an earlier edit introduced, not the original wording)."
                        )
                        single_errors = [err + hint for err in single_errors]
                    validation_errors.extend(single_errors)
                    failed_validation_indices.add(i)
                    skipped_edits += 1
                    # Punctuation-anchor warning is failure-context only; on
                    # success the redline preview reports the change cleanly.
                    warning = self._check_punctuation_warning(getattr(edit, "target_text", ""))
                    reports_by_input[i] = {
                        "status": "failed",
                        "type": getattr(edit, "type", "modify"),
                        "target_text": truncate_middle(getattr(edit, "target_text", ""), REPORT_ECHO_CAP),
                        "new_text": truncate_middle(self._report_new_text(edit), REPORT_ECHO_CAP),
                        "warning": warning,
                        "error": "\n".join(single_errors),
                        "critic_markup": None,
                        "clean_text": None,
                    }
                    continue

                e_applied, e_skipped = self.apply_edits([edit], page_offsets=page_offsets)
                applied_edits += e_applied
                skipped_edits += e_skipped
                reports_by_input[i] = self._build_edit_report(edit)
                if e_applied > 0:
                    self._refresh_after_sequential_edit()

            if validation_errors:
                if not dry_run_mode:
                    # Transactional rejection: undo every edit this batch
                    # already applied before raising.
                    self._restore_from_snapshot(pre_batch_snapshot)
                    raise BatchValidationError(validation_errors)
                # Dry-run mirrors the rejection: no edit will be applied by the
                # real run, so none may be reported as applied here.
                applied_edits = 0
                skipped_edits = len(cloned_edits)
                for i, report in enumerate(reports_by_input):
                    if report is None or i in failed_validation_indices:
                        continue
                    reports_by_input[i] = {
                        "status": "failed",
                        "type": report.get("type", "modify"),
                        "target_text": report["target_text"],
                        "new_text": report["new_text"],
                        "warning": None,
                        "error": (
                            "Not applied: the batch is transactional and other edits failed "
                            "validation (see their errors). Fix or remove those edits and re-run."
                        ),
                        "critic_markup": None,
                        "clean_text": None,
                    }

            edits_reports = [r for r in reports_by_input if r is not None]

        from adeu import __version__

        return {
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
            "engine": "python",
            "version": __version__,
        }

    def apply_edits(
        self, edits: List[Union[ModifyText, InsertTableRow, DeleteTableRow]], page_offsets: Optional[List[int]] = None
    ) -> tuple[int, int]:
        if page_offsets is None:
            from adeu.pagination import paginate, split_structural_appendix

            body_text, _ = split_structural_appendix(self.mapper.full_text)
            page_offsets = paginate(body_text, "").body_page_offsets

        applied = 0
        skipped = 0

        for edit in edits:
            edit._applied_status = False
            edit._error_msg = None
            edit._any_sub_failure = False

        resolved_edits = []

        # Pre-resolve phase: locate all edits against initial clean state
        for edit in edits:
            if edit._resolved_start_idx is not None:
                resolved_edits.append((edit, getattr(edit, "new_text", None)))
            elif edit._match_start_index is not None:
                edit._resolved_start_idx = edit._match_start_index
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
                    resolved = self._pre_resolve_heuristic_edit(edit)
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
                else:
                    if first_in_group:
                        edit._occurrences_modified = getattr(edit, "_occurrences_modified", 0) + 1
                    path, page = self._get_heading_path_and_page(start, self.mapper.full_text, page_offsets)
                    pages = getattr(edit, "_pages", [])
                    if page not in pages:
                        pages.insert(0, page)
                    edit._pages = pages
                    edit._heading_path = path
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

        # Inject tracked change info
        trPr = new_row_el.get_or_add_trPr()
        ins = self._create_track_change_tag("w:ins")
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

        # Instead of removing, we mark as deleted
        trPr = row_el.get_or_add_trPr()
        del_el = self._create_track_change_tag("w:del")
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

        bounds: Dict[Any, List[Optional[int]]] = defaultdict(_empty_bounds)
        for s in active_mapper.spans:
            if s.paragraph is None:
                continue
            # Skip the inter-paragraph "\n\n" virtual separator
            # (run is None and text == "\n\n" with paragraph != None means
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

        for p, (lo, hi) in bounds.items():
            if lo == start_idx and hi == end_idx:
                target_para = p

                break

        if target_para is None:
            return None

        # At least one side must be a heading. If neither the source
        # paragraph nor the new_text is heading-styled, the existing
        # inline-edit path handles it correctly — don't intercept.
        from adeu.utils.docx import is_heading_paragraph

        source_is_heading = is_heading_paragraph(target_para)

        # Detect whether new_text starts with a heading marker.
        new_clean, new_style = self._parse_markdown_style(new_text)
        new_is_heading = new_style is not None and (new_style.startswith("Heading") or new_style == "Title")

        if not source_is_heading and not new_is_heading:
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

    def _pre_resolve_heuristic_edit(self, edit: ModifyText) -> Union[ModifyText, List[ModifyText], None]:
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
                edit, start_idx, match_len, active_mapper, actual_doc_text, current_effective_new_text
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

    def _resolve_single_match(self, edit, start_idx, match_len, active_mapper, actual_doc_text, effective_new_text):

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

        if effective_new_text.startswith(actual_doc_text.rstrip()):
            # Smart Fallback: Handle trailing space omissions (e.g. LLM appended \n without the space)
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

        # BUG-23-4: Reject boundary-crossing plain-paragraph modifications with text on both sides
        # to prevent structural paragraph-break corruption.
        if "\n\n" in final_target:
            if "\n\n" in final_new:
                before, _, after = actual_doc_text.partition("\n\n")
                if before.strip() and after.strip():
                    raise BatchValidationError(
                        [
                            "- Edit Failed: target_text spans a paragraph "
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
                            "- Edit Failed: target_text spans a paragraph "
                            "boundary with body text on both sides. "
                            "The paragraph break is a structural element, "
                            "not literal text, so it cannot be replaced as a single span "
                            "without corrupting the document. Split this into one edit per paragraph."
                        ]
                    )

        has_markdown = False
        if edit.target_text and ("**" in edit.target_text or "_" in edit.target_text):
            has_markdown = True
        if effective_new_text and ("**" in effective_new_text or "_" in effective_new_text):
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
        op = edit._internal_op
        active_mapper = edit._active_mapper_ref or self.mapper

        if op is None:
            if not edit.target_text and edit.new_text:
                op = EditOperationType.INSERTION
            elif edit.target_text and not edit.new_text:
                op = EditOperationType.DELETION
            else:
                op = EditOperationType.MODIFICATION

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
        del_id: Optional[str] = None
        ins_id: Optional[str] = None
        if op in (EditOperationType.DELETION, EditOperationType.MODIFICATION):
            del_id = self._get_next_id()
        if op in (EditOperationType.INSERTION, EditOperationType.MODIFICATION):
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
            else:
                anchor_run, anchor_paragraph = active_mapper.get_insertion_anchor(start_idx, rebuild_map=rebuild_map)
            if not anchor_run and not anchor_paragraph:
                return False

            insert_before = False
            if anchor_run is None and anchor_paragraph is not None:
                preceding = [s for s in active_mapper.spans if s.end == start_idx and s.paragraph == anchor_paragraph]
                if preceding and preceding[-1].text != "\n\n":
                    insert_before = True

            parent = None
            index = 0
            if anchor_run:
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

            self._mark_fully_deleted_rows_in_range(del_elems, virtual_spans, start_idx, length, active_mapper, del_id)

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
                    del_mark = self._create_track_change_tag("w:del")
                    rPr.append(del_mark)

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
        part is declared eagerly in __init__ and skipped here.
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

    def _duplicate_revision_id_error(self, target_id: str, action_type: str) -> Optional[str]:
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
        return (
            f"- Failed to apply action: {action_type} on Chg:{target_id} is ambiguous. The document "
            f"contains {len(nodes)} tracked-change elements sharing w:id={target_id} from different "
            f"authors ({', '.join(authors)}) — duplicate revision IDs produced outside this engine "
            "(e.g. by a document merge or copy-paste). Acting on this ID would resolve all of them "
            "at once. Resolve these changes individually in Word, or apply the intended outcome as "
            "an explicit text edit instead."
        )

    def _existing_change_ids(self) -> List[str]:
        """Distinct tracked-change ids (w:id on w:ins/w:del) in the main story."""
        ids = {
            n.get(qn("w:id"))
            for tag in ("w:ins", "w:del")
            for n in self.doc.element.findall(f".//{qn(tag)}")
            if n.get(qn("w:id"))
        }
        return sorted(ids, key=lambda x: (int(x) if x.isdigit() else 0, x))

    def _existing_comment_ids(self) -> List[str]:
        """Comment ids present in the document, sorted for display."""
        try:
            ids = list(self.comments_manager.extract_comments_data().keys())
        except Exception:
            ids = []
        return sorted(ids, key=lambda x: (int(x) if x.isdigit() else 0, x))

    @staticmethod
    def _format_id_list(ids: List[str], prefix: str, limit: int = 20) -> str:
        shown = ids[:limit]
        rendered = ", ".join(f"{prefix}{i}" for i in shown)
        if len(ids) > len(shown):
            rendered += f", … (+{len(ids) - len(shown)} more)"
        return rendered

    def _action_not_found_error(self, raw_id: str, target_id: str, act) -> str:
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
        find_hint = (
            "Run `adeu markup <file> -i` or `adeu extract <file>` to list the current "
            "change (Chg:) and comment (Com:) ids."
        )

        if isinstance(act, ReplyComment):
            # Echo the id the caller passed (normalizing a bare id to Com:N).
            echo = raw_id if has_prefix else f"Com:{target_id}"
            if target_id in change_ids:
                return (
                    f"- Failed to apply action: reply on {echo} — Chg:{target_id} is a tracked-change "
                    "id, not a comment. `reply` adds to an existing comment thread (Com:…); to comment "
                    "on a change instead, apply a modify with a `comment`. " + find_hint
                )
            avail = (
                f"Comment ids in this document: {self._format_id_list(comment_ids, 'Com:')}. "
                if comment_ids
                else "This document has no comments to reply to. "
            )
            return f"- Failed to apply action: reply on {echo} — no comment with that id exists. " + avail + find_hint

        # AcceptChange / RejectChange
        echo = raw_id if has_prefix else f"Chg:{target_id}"
        if target_id in comment_ids:
            return (
                f"- Failed to apply action: {act.type} on {echo} — Com:{target_id} is a comment id, "
                f"not a tracked change. accept/reject act on tracked changes (Chg:…); to respond to a "
                f"comment use `reply`. " + find_hint
            )
        avail = (
            f"Tracked-change ids in this document: {self._format_id_list(change_ids, 'Chg:')}. "
            if change_ids
            else "This document has no tracked changes. "
        )
        return (
            f"- Failed to apply action: {act.type} on {echo} — no tracked change with that id exists "
            "(it may already have been accepted or rejected, or the id is stale). " + avail + find_hint
        )

    def _resolution_group_ids(self, target_id: str) -> set:
        """
        All revision ids that resolve as ONE unit with `target_id`: the ids of
        every contiguous same-author <w:ins>/<w:del> sibling of its elements
        (a replacement's del+ins pair), plus the id itself.
        """
        nodes = [n for n in self.doc.element.findall(f".//{qn('w:ins')}") if n.get(qn("w:id")) == target_id]
        nodes += [n for n in self.doc.element.findall(f".//{qn('w:del')}") if n.get(qn("w:id")) == target_id]
        group = {target_id} if nodes else set()
        for node in nodes:
            for paired in self._get_paired_nodes(node):
                pid = paired.get(qn("w:id"))
                if pid:
                    group.add(pid)
        return group

    def validate_action_pairing(self, actions: List[Union[AcceptChange, RejectChange, ReplyComment]]) -> List[str]:
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
                errors.append(
                    f"- Action {pos + 1} Failed: conflicting actions on one replacement — Action "
                    f"{first_pos + 1} applies '{first_type}' to Chg:{first_id}, and Chg:{target_id} is "
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
        self, actions: List[Union[AcceptChange, RejectChange, ReplyComment]]
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
                        f"- Note: Action {pos + 1} ('{act.type}' on {raw_id}) had no additional effect — "
                        "the change was already resolved together with its replacement pair by an "
                        "earlier action in this batch. Counted as already_resolved, not applied."
                    )
                    continue
                # Contradiction. validate_action_pairing rejects this shape
                # before anything mutates; this guard covers direct callers.
                self.skipped_details.append(
                    f"- Action {pos + 1} Failed: contradictory action — '{act.type}' on {raw_id}, but "
                    f"the change was already resolved as '{prior_type}' together with its replacement "
                    "pair by an earlier action in this batch."
                )
                skipped += 1
                continue

            if is_change and isinstance(act, (AcceptChange, RejectChange)):
                dup_error = self._duplicate_revision_id_error(target_id, act.type)
                if dup_error:
                    self.skipped_details.append(dup_error)
                    skipped += 1
                    continue

            resolved_now = set()
            success = False

            # Accept/reject can delete a comment as a side effect when the
            # comment's anchor falls inside the resolved change. Snapshot the
            # comment ids first so a removal is reported explicitly instead of
            # happening silently under "1 applied" (QA 2026-07-22 bug #1).
            comments_before: set = set()
            if isinstance(act, (AcceptChange, RejectChange)) and is_change:
                comments_before = set(self._existing_comment_ids())

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
                    success = self._reply_to_comment(target_id, getattr(act, "text", ""))

            if success:
                for rid in resolved_now:
                    if rid:
                        resolved_history[rid] = act.type
                applied += 1
                if comments_before:
                    removed = comments_before - set(self._existing_comment_ids())
                    if removed:
                        removed_list = ", ".join(f"Com:{c}" for c in sorted(removed))
                        self.skipped_details.append(
                            f"- Note: {act.type} on {raw_id} also removed comment {removed_list} "
                            "(including any reply thread) because its anchor was inside the resolved "
                            "change. This note is informational — the action itself succeeded."
                        )
            else:
                self.skipped_details.append(self._action_not_found_error(raw_id, target_id, act))
                skipped += 1

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

        all_ins = set(primary_ins)
        all_del = set(primary_del)

        for node in primary_ins + primary_del:
            for paired in self._get_paired_nodes(node):
                if paired.tag == qn("w:ins"):
                    all_ins.add(paired)
                elif paired.tag == qn("w:del"):
                    all_del.add(paired)

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
                else:
                    parent.remove(d)

        return resolved_ids

    def _reject_change(self, target_id: str) -> set:
        primary_ins = [n for n in self.doc.element.findall(f".//{qn('w:ins')}") if n.get(qn("w:id")) == target_id]
        primary_del = [n for n in self.doc.element.findall(f".//{qn('w:del')}") if n.get(qn("w:id")) == target_id]

        all_ins = set(primary_ins)
        all_del = set(primary_del)

        for node in primary_ins + primary_del:
            for paired in self._get_paired_nodes(node):
                if paired.tag == qn("w:ins"):
                    all_ins.add(paired)
                elif paired.tag == qn("w:del"):
                    all_del.add(paired)

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
                if p_el is not None and p_el.tag == qn("w:p"):
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

    def _reply_to_comment(self, target_id: str, text: str) -> bool:
        if not self.comments_manager.comments_part:
            return False

        existing_comments = self.comments_manager.extract_comments_data()
        if target_id not in existing_comments:
            return False

        new_comment_id = self.comments_manager.add_comment(self.author, text, parent_id=target_id)

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

        # Only claim comments were removed when they actually are.
        removed_comments = 0
        if remove_comments:
            try:
                removed_comments = len(self.comments_manager.extract_comments_data())
            except Exception:
                removed_comments = 0

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

                        if has_content:
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

        return {
            "accepted_insertions": accepted_insertions,
            "accepted_deletions": accepted_deletions,
            "accepted_formatting": accepted_formatting,
            "removed_comments": removed_comments,
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
