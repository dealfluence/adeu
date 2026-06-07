# FILE: src/adeu/redline/engine.py
import datetime
import re
from collections import defaultdict
from copy import deepcopy
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

import structlog
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsmap, qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from adeu.diff import trim_common_context
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
from adeu.redline.comments import CommentsManager
from adeu.redline.mapper import DocumentMapper
from adeu.utils.docx import create_attribute, create_element, strip_bom_from_docx_bytes

logger = structlog.get_logger(__name__)

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


def validate_edit_strings(
    edits: List[Union["ModifyText", "InsertTableRow", "DeleteTableRow"]],
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

    Returns:
        List of error message strings. Empty if all edits pass these checks.
    """
    errors: List[str] = []

    for i, edit in enumerate(edits):
        t_text = edit.target_text or ""
        n_text = getattr(edit, "new_text", "") or ""

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

        # M8: Ensure w16du namespace is declared at the document root to prevent ns0 aliasing
        import re

        import lxml.etree as etree

        w16du_ns_str = 'xmlns:w16du="http://schemas.microsoft.com/office/word/2023/wordml/word16du"'

        parts_to_inject = [self.doc.part]
        for part in self.doc.part.package.parts:
            if part != self.doc.part and "wordprocessingml" in part.content_type and part.content_type.endswith("+xml"):
                parts_to_inject.append(part)

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
        self.skipped_details: List[str] = []

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

        # Numbered Lists (e.g., "1. ", "2. ", "10. ")
        match = re.match(r"^\d+\.\s+", stripped_text)
        if match:
            return stripped_text[match.end() :].strip(), "List Number"

        return text, None

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
    ) -> Tuple[Optional[Any], Optional[Any]]:
        """
        Inserts text. If text contains newlines, splits into multiple paragraphs.

        If `reuse_id` is provided, every <w:ins> element minted by this call
        (the inline insert, per-paragraph block inserts, and paragraph-break
        tracking markers in pPr/rPr) shares that w:id. This collapses multi-
        paragraph and multi-run insertions into a single logical revision
        from the agent's point of view.
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
                    new_p.append(deepcopy(current_p.pPr))

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
        if current_p is not None:
            positional_anchor = (
                ins_elem if ins_elem is not None else (anchor_run._element if anchor_run is not None else None)
            )
            while positional_anchor is not None and positional_anchor.getparent() is not current_p:
                positional_anchor = positional_anchor.getparent()
                if positional_anchor is current_p:
                    positional_anchor = None
                    break

            if positional_anchor is not None:
                relocatable_tags = {qn("w:r"), qn("w:ins"), qn("w:del")}
                nxt = positional_anchor.getnext()
                while nxt is not None:
                    if nxt.tag in relocatable_tags:
                        suffix_nodes.append(nxt)
                    nxt = nxt.getnext()

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

        if parent.tag == qn("w:ins"):
            grandparent = parent.getparent()
            if grandparent is not None:
                parent_index = grandparent.index(parent)
                run_index = parent.index(run._r)

                left_ins = create_element("w:ins")
                for attr, val in parent.attrib.items():
                    left_ins.set(attr, val)

                right_ins = create_element("w:ins")
                for attr, val in parent.attrib.items():
                    right_ins.set(attr, val)

                # Snapshot children to safely extract them across loops
                children = list(parent)
                for child in children[:run_index]:
                    left_ins.append(child)
                # Skip the run being deleted
                for child in children[run_index + 1 :]:
                    right_ins.append(child)

                insert_idx = parent_index
                if len(left_ins) > 0:
                    grandparent.insert(insert_idx, left_ins)
                    insert_idx += 1

                grandparent.insert(insert_idx, del_tag)
                insert_idx += 1

                if len(right_ins) > 0:
                    grandparent.insert(insert_idx, right_ins)

                grandparent.remove(parent)
                return del_tag

        parent.replace(run._r, del_tag)
        return del_tag

    def _attach_comment(self, parent_element, start_element, end_element, text: str):
        if not text:
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

        start_index = parent_element.index(start_element)
        parent_element.insert(start_index, range_start)
        end_index = parent_element.index(end_element)
        parent_element.insert(end_index + 1, range_end)
        parent_element.insert(end_index + 2, ref_run)

    def _attach_comment_spanning(self, start_p, start_el, end_p, end_el, text: str):
        if not text:
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

    def validate_edits(self, edits: List[Union[ModifyText, InsertTableRow, DeleteTableRow]]) -> List[str]:
        """
        Performs an exhaustive dry-run validation of all text edits in the batch.
        Returns a list of error strings. If the list is empty, the batch is safe to apply.
        """
        errors = []

        # Ensure base mapper is ready, but DO NOT rebuild it if it already exists!
        # This saves ~15s of redundant O(N) DOM traversal on large files.
        if not self.mapper.full_text:
            self.mapper._build_map()

        # Category A: document-context-free string-shape validation.
        # Delegated to module-level helper so the Live Word path can call the
        # same checks. See validate_edit_strings docstring for what is checked.
        errors.extend(validate_edit_strings(edits))
        for i, edit in enumerate(edits):
            if not edit.target_text:
                continue  # Skip validation for pure index-based insertions

            matches = self.mapper.find_all_match_indices(edit.target_text)
            active_text = self.mapper.full_text

            # Fallback to Clean View if not found in Raw View (matches heuristic logic)
            if len(matches) == 0:
                if not self.clean_mapper:
                    self.clean_mapper = DocumentMapper(self.doc, clean_view=True)
                matches = self.clean_mapper.find_all_match_indices(edit.target_text)
                if len(matches) > 0:
                    active_text = self.clean_mapper.full_text

            # Since the structural appendix is no longer in the mapper,
            # all matches are valid document body matches.
            valid_matches = matches

            if len(valid_matches) == 0:
                errors.append(f'- Edit {i + 1} Failed: Target text not found in document:\n  "{edit.target_text}"')
            elif len(valid_matches) > 1:
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

            for start, length in valid_matches:
                spans = [s for s in self.mapper.spans if s.end > start and s.start < start + length]
                nested_authors = set()
                for s in spans:
                    if s.ins_id:
                        ins_nodes = self.doc.element.xpath(f"//w:ins[@w:id='{s.ins_id}']")
                        if ins_nodes:
                            auth = ins_nodes[0].get(qn("w:author"))
                            if auth and auth != self.author:
                                nested_authors.add(auth)
                if nested_authors:
                    errors.append(
                        f"- Edit {i + 1} Failed: Modification targets an active insertion from another author "
                        f"({', '.join(nested_authors)}). Accept that change first or scope your edit outside of it."
                    )

        return errors

    def process_batch(self, changes: List[DocumentChange]) -> dict:
        """
        Processes a unified batch of actions and edits safely.
        Actions are applied first, the Virtual DOM map is rebuilt, and then text edits are validated and applied.
        """
        self.skipped_details = []
        actions = [c for c in changes if isinstance(c, (AcceptChange, RejectChange, ReplyComment))]
        edits = [c for c in changes if isinstance(c, (ModifyText, InsertTableRow, DeleteTableRow))]

        applied_actions, skipped_actions = 0, 0
        if actions:
            applied_actions, skipped_actions = self.apply_review_actions(actions)
            if skipped_actions > 0:
                raise BatchValidationError(self.skipped_details)
            if edits:
                self.clean_mapper = None

        if edits:
            errors = self.validate_edits(edits)
            if errors:
                raise BatchValidationError(errors)

        applied_edits, skipped_edits = 0, 0
        if edits:
            applied_edits, skipped_edits = self.apply_edits(edits)

        return {
            "actions_applied": applied_actions,
            "actions_skipped": skipped_actions,
            "edits_applied": applied_edits,
            "edits_skipped": skipped_edits,
            "skipped_details": self.skipped_details,
        }

    def apply_edits(self, edits: List[Union[ModifyText, InsertTableRow, DeleteTableRow]]) -> tuple[int, int]:
        applied = 0
        skipped = 0

        resolved_edits = []

        # Pre-resolve phase: locate all edits against initial clean state
        for edit in edits:
            if edit._match_start_index is not None:
                resolved_edits.append((edit, getattr(edit, "new_text", None)))
            elif isinstance(edit, (InsertTableRow, DeleteTableRow)):
                # Simplified resolution for structural edits
                matches = self.mapper.find_all_match_indices(edit.target_text)
                if not matches:
                    # Try clean view
                    if not self.clean_mapper:
                        self.clean_mapper = DocumentMapper(self.doc, clean_view=True)
                    matches = self.clean_mapper.find_all_match_indices(edit.target_text)

                if matches:
                    # validate_edits already ensured uniqueness
                    edit._match_start_index = matches[0][0]
                    resolved_edits.append((edit, None))
                else:
                    skipped += 1
                    target_snippet = edit.target_text.strip()[:40]
                    self.skipped_details.append(f"- Failed to apply structural edit targeting: '{target_snippet}...'")
            else:
                resolved = self._pre_resolve_heuristic_edit(edit)
                if resolved:
                    if isinstance(resolved, list):
                        for r in resolved:
                            resolved_edits.append((r, r.new_text))
                    else:
                        resolved_edits.append((resolved, edit.new_text))
                else:
                    skipped += 1

                    # N2 Fix: Safe display text fallback for heuristic failures
                    display_text = edit.target_text or "insertion"
                    if not display_text.strip() and hasattr(edit, "_original_target_text"):
                        display_text = edit._original_target_text

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

        # Process all edits backwards in a single O(N) sweep to avoid index drift and map rebuilds
        resolved_edits.sort(key=lambda x: x[0]._match_start_index or 0, reverse=True)
        occupied_ranges: List[Tuple[int, int]] = []

        for edit, orig_new in resolved_edits:
            start = edit._match_start_index or 0
            end = start + (len(edit.target_text) if edit.target_text else 0)

            if any(start < occ_end and end > occ_start for occ_start, occ_end in occupied_ranges):
                logger.warning(f"Skipping overlapping edit at index {start}")
                skipped += 1

                display_text = edit.target_text or "insertion"
                if not display_text.strip() and hasattr(edit, "_original_target_text"):
                    display_text = edit._original_target_text
                target_snippet = display_text.strip()[:40]

                msg = f"- Skipped overlapping edit targeting: '{target_snippet}...'"
                if getattr(edit, "_is_table_edit", False):
                    msg += ". (Note: Overlapping cell edits in tables must be processed in separate batches)."
                self.skipped_details.append(msg)
                continue

            success = False
            if isinstance(edit, InsertTableRow):
                success = self._apply_insert_row(edit)
            elif isinstance(edit, DeleteTableRow):
                success = self._apply_delete_row(edit)
            else:
                success = self._apply_single_edit_indexed(edit, original_new_text=orig_new, rebuild_map=False)

            if success:
                applied += 1
                occupied_ranges.append((start, end))
            else:
                skipped += 1

                display_text = edit.target_text or "insertion"
                if not display_text.strip() and hasattr(edit, "_original_target_text"):
                    display_text = edit._original_target_text
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

        return applied, skipped

    def _apply_insert_row(self, edit: InsertTableRow) -> bool:
        start_idx = edit._match_start_index
        if start_idx is None:
            return False

        target_runs = self.mapper.find_target_runs_by_index(start_idx, len(edit.target_text))
        if not target_runs:
            return False

        row_el = None
        curr = target_runs[0]._element
        while curr is not None:
            if curr.tag == qn("w:tr"):
                row_el = curr
                break
            curr = curr.getparent()

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
        start_idx = edit._match_start_index
        if start_idx is None:
            return False

        target_runs = self.mapper.find_target_runs_by_index(start_idx, len(edit.target_text))
        if not target_runs:
            return False

        row_el = None
        curr = target_runs[0]._element
        while curr is not None:
            if curr.tag == qn("w:tr"):
                row_el = curr
                break
            curr = curr.getparent()

        if row_el is None:
            return False

        # Instead of removing, we mark as deleted
        trPr = row_el.get_or_add_trPr()
        del_el = self._create_track_change_tag("w:del")
        trPr.append(del_el)

        return True

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
        proxy_edit._active_mapper_ref = active_mapper
        # Stash the resolved paragraph for the apply step.
        proxy_edit._target_paragraph = target_para  # type: ignore[attr-defined]
        return proxy_edit

    def _pre_resolve_heuristic_edit(self, edit: ModifyText) -> Union[ModifyText, List[ModifyText], None]:
        if not edit.target_text:
            return None

        start_idx, match_len = self.mapper.find_match_index(edit.target_text)

        # FALLBACK: If Raw View match failed, try matching against Clean View
        use_clean_map = False
        if start_idx == -1:
            if not self.clean_mapper:
                self.clean_mapper = DocumentMapper(self.doc, clean_view=True)

            start_idx, match_len = self.clean_mapper.find_match_index(edit.target_text)
            if start_idx != -1:
                use_clean_map = True
            else:
                return None

        active_mapper = self.clean_mapper if use_clean_map else self.mapper
        assert active_mapper is not None
        # --- Whole-paragraph replacement fast path ---
        # Detects: an edit whose target_text exactly matches one full
        # projected paragraph (heading prefix + content), where new_text is
        # a single new paragraph (no '\n\n') and where at least one side is
        # a heading. In that case we route to the PARAGRAPH_REPLACE op,
        # which track-deletes the entire <w:p> (paragraph break included)
        # and inserts a fresh styled <w:p>. This avoids the
        # context-trimming + block-element-insertion path that produces
        # garbled output for heading-level changes (e.g., '## X' -> '# Y').
        para_replace = self._maybe_paragraph_replace(edit, start_idx, match_len, active_mapper)
        if para_replace is not None:
            return para_replace

        effective_new_text = edit.new_text or ""
        actual_doc_text = self.mapper.full_text[start_idx : start_idx + match_len]

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
                    txt_edit._internal_op = EditOperationType.MODIFICATION
                    txt_edit._active_mapper_ref = active_mapper
                    sub_edits.append(txt_edit)

                if t_url != n_url:
                    t_idx = actual_doc_text.find(t_url)
                    url_edit = ModifyText(type="modify", target_text=t_url, new_text=n_url, comment=None)
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

            if len(actual_cells) == len(new_cells) and len(actual_cells) > 1:
                sub_edits = []

                # We use the real DOM structure (mapper.full_text) to advance offsets,
                # completely bypassing any truncated strings from the LLM.
                search_offset = start_idx

                # Determine which cell should receive the comment (first cell that actually changes, or cell 0)
                target_comment_idx = 0
                for idx, (a, n) in enumerate(zip(actual_cells, new_cells, strict=True)):
                    if a.strip() != n.strip():
                        target_comment_idx = idx
                        break

                for cell_idx, (a_cell, n_cell) in enumerate(zip(actual_cells, new_cells, strict=True)):
                    a_clean = a_cell.strip()
                    n_clean = n_cell.strip()

                    if a_clean:
                        # Align exactly to where this cell's text begins in the real document
                        actual_start = self.mapper.full_text.find(a_clean, search_offset)
                        if actual_start == -1 or actual_start > search_offset + 10:
                            actual_start = search_offset  # fallback if not found cleanly
                    else:
                        actual_start = search_offset

                    should_attach_comment = (edit.comment is not None) and (cell_idx == target_comment_idx)

                    if a_clean != n_clean or should_attach_comment:
                        sub_edit = ModifyText(
                            type="modify",
                            target_text=a_clean,
                            new_text=n_clean,
                            comment=edit.comment if should_attach_comment else None,
                        )
                        sub_edit._active_mapper_ref = active_mapper
                        sub_edit._original_target_text = edit.target_text  # type: ignore[attr-defined]
                        sub_edit._is_table_edit = True  # type: ignore[attr-defined]

                        if a_clean == n_clean:
                            sub_edit._match_start_index = actual_start
                            sub_edit._internal_op = "COMMENT_ONLY"
                        else:
                            prefix_len, suffix_len = trim_common_context(a_clean, n_clean)
                            t_end = len(a_clean) - suffix_len
                            n_end = len(n_clean) - suffix_len

                            final_target = a_clean[prefix_len:t_end]
                            final_new = n_clean[prefix_len:n_end]
                            sub_edit.target_text = final_target
                            sub_edit.new_text = final_new
                            sub_edit._match_start_index = actual_start + prefix_len

                            if not final_target and final_new:
                                sub_edit._internal_op = EditOperationType.INSERTION
                            elif final_target and not final_new:
                                sub_edit._internal_op = EditOperationType.DELETION
                            elif final_target and final_new:
                                sub_edit._internal_op = EditOperationType.MODIFICATION
                            else:
                                sub_edit._internal_op = "COMMENT_ONLY"

                        sub_edits.append(sub_edit)

                    # Advance search_offset to the start of the next cell safely
                    if a_clean:
                        search_offset = actual_start + len(a_clean)

                    next_pipe = self.mapper.full_text.find(" | ", search_offset)
                    if next_pipe != -1 and next_pipe <= search_offset + 10:
                        # The start of the next cell is exactly 3 chars after the pipe index
                        search_offset = next_pipe + 3
                    else:
                        search_offset += len(a_cell) + 1

                return sub_edits
            else:
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

        if actual_doc_text == effective_new_text or edit.target_text == effective_new_text:
            proxy_edit = ModifyText(
                type="modify",
                target_text=actual_doc_text,
                new_text=actual_doc_text,
                comment=edit.comment,
            )
            proxy_edit._match_start_index = start_idx
            proxy_edit._internal_op = "COMMENT_ONLY"
            proxy_edit._active_mapper_ref = active_mapper
            return proxy_edit

        if effective_new_text.startswith(actual_doc_text):
            effective_op = EditOperationType.INSERTION
            final_target = ""
            final_new = effective_new_text[len(actual_doc_text) :]
            effective_start_idx = start_idx + match_len
        elif effective_new_text.startswith(actual_doc_text.rstrip()):
            # Smart Fallback: Handle trailing space omissions (e.g. LLM appended \n without the space)
            effective_op = EditOperationType.INSERTION
            final_target = ""
            final_new = effective_new_text[len(actual_doc_text.rstrip()) :]
            effective_start_idx = start_idx + len(actual_doc_text.rstrip())
        else:
            prefix_len, suffix_len = trim_common_context(actual_doc_text, effective_new_text)

            t_end = len(actual_doc_text) - suffix_len
            n_end = len(effective_new_text) - suffix_len

            final_target = actual_doc_text[prefix_len:t_end]
            final_new = effective_new_text[prefix_len:n_end]
            effective_start_idx = start_idx + prefix_len

            # BUG-23-4: A target that, after trimming, still spans a paragraph
            # boundary (\n\n) with real content on BOTH sides while the
            # replacement collapses that boundary (no \n\n in new_text) cannot
            # be applied without silently merging the two source paragraphs and
            # losing the break. Reject with an actionable error instead of
            # producing a corrupt single-token deletion. (A pure paragraph
            # *merge*, where the trimmed target is only the \n\n separator with
            # no surrounding content, is still handled correctly below.)
            if "\n\n" in final_target and "\n\n" not in final_new:
                before, _, after = final_target.partition("\n\n")
                if before.strip() and after.strip():
                    raise BatchValidationError(
                        [
                            "- Edit Failed: target_text spans a paragraph boundary "
                            "(it crosses a newline between two paragraphs). "
                            "Multi-paragraph targets are not supported \u2014 split the edit "
                            "into one edit per paragraph, or target text within a single "
                            "paragraph."
                        ]
                    )

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
        proxy_edit._match_start_index = effective_start_idx
        proxy_edit._internal_op = effective_op
        proxy_edit._active_mapper_ref = active_mapper
        return proxy_edit

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

        start_idx = edit._match_start_index or 0
        target_text = edit.target_text
        length = len(target_text) if target_text else 0

        logger.debug(f"Applying Edit at [{start_idx}:{start_idx + length}] Op={op}")

        # Whole-paragraph replacement: track-delete the entire source
        # paragraph (content + paragraph break) and emit a new tracked
        # paragraph with the new style.
        if op == EditOperationType.PARAGRAPH_REPLACE:
            return self._apply_paragraph_replace(edit)

        # Allocate logical-edit IDs up front. A single ModifyText that spans
        # multiple XML runs (e.g. a target containing a bold word, which OOXML
        # stores as a separate <w:r> element) or whose new_text spans multiple
        # paragraphs used to mint a fresh w:id per <w:ins>/<w:del> element,
        # surfacing as N [Chg:N] entries in the projected bubble even though
        # Word renders the change as a single review entry. We now allocate one
        # id for the delete side and one for the insert side per logical
        # operation, and reuse them across every <w:ins>/<w:del> element this
        # edit produces. The mapper's _build_merged_meta_block already
        # deduplicates repeated IDs via seen_sigs, so this collapses the bubble
        # automatically without any projection-side change to the engine.
        del_id: Optional[str] = None
        ins_id: Optional[str] = None
        if op in (EditOperationType.DELETION, EditOperationType.MODIFICATION):
            del_id = self._get_next_id()
        if op in (EditOperationType.INSERTION, EditOperationType.MODIFICATION):
            ins_id = self._get_next_id()

        if op == "URL_RETARGET":
            target_spans = [s for s in active_mapper.spans if s.start <= start_idx < s.end]
            if target_spans and target_spans[0].hyperlink_id:
                rel = self.doc.part.rels[target_spans[0].hyperlink_id]
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

                if start_p is not None and end_p is not None:
                    if start_p == end_p:
                        self._attach_comment(start_p, first_el, last_el, edit.comment)
                    else:
                        self._attach_comment_spanning(start_p, first_el, end_p, last_el, edit.comment)
            return True

        if op == EditOperationType.INSERTION:
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
            elif anchor_paragraph:
                parent = anchor_paragraph._element
                for i, child in enumerate(parent):
                    if child.tag == qn("w:pPr"):
                        index = i + 1
                    else:
                        break

            if parent is None:
                return False

            final_new_text = edit.new_text or ""

            if start_idx == 0:
                ins_elem, last_p = self.track_insert(
                    final_new_text,
                    anchor_run=anchor_run,
                    anchor_paragraph=anchor_paragraph,
                    comment=edit.comment,
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
                            last_ins = last_p.findall(f".//{qn('w:ins')}")[-1]
                            self._attach_comment_spanning(actual_parent, ins_elem, last_p, last_ins, edit.comment)
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
                    insert_before=insert_before,
                    reuse_id=ins_id,
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
                            last_ins = last_p.findall(f".//{qn('w:ins')}")[-1]
                            self._attach_comment_spanning(actual_parent, ins_elem, last_p, last_ins, edit.comment)
                        else:
                            self._attach_comment(actual_parent, ins_elem, ins_elem, edit.comment)
            return True

        target_runs = active_mapper.find_target_runs_by_index(start_idx, length, rebuild_map=rebuild_map)
        virtual_spans = []
        if op in (EditOperationType.DELETION, EditOperationType.MODIFICATION):
            virtual_spans = active_mapper.get_virtual_spans_in_range(start_idx, length)

        if not target_runs and not virtual_spans:
            return False

        affected_ps = set()
        for run in target_runs:
            if run._parent and hasattr(run._parent, "_element") and run._parent._element.tag == qn("w:p"):
                affected_ps.add(run._parent._element)

        if op == EditOperationType.DELETION:
            first_del_element = None
            last_del_element = None
            for run in target_runs:
                del_elem = self.track_delete_run(run, reuse_id=del_id)
                if first_del_element is None:
                    first_del_element = del_elem
                last_del_element = del_elem

            if edit.comment and first_del_element is not None and last_del_element is not None:
                start_p = first_del_element.getparent()
                end_p = last_del_element.getparent()
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
            for run in target_runs:
                del_elem = self.track_delete_run(run, reuse_id=del_id)
                if first_del_element is None:
                    first_del_element = del_elem
                last_del_element = del_elem

            if first_del_element is not None and last_del_element is not None and edit.new_text:
                parent = last_del_element.getparent()
                if parent is not None:
                    del_index = parent.index(last_del_element)

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
                        suppress_inherited=False,
                        reuse_id=ins_id,
                    )
                    if ins_elem is not None:
                        parent.insert(del_index + 1, ins_elem)

                    if edit.comment and first_del_element is not None:
                        start_p = first_del_element.getparent()
                        if last_p is not None:
                            end_p = last_p
                            last_ins = last_p.findall(f".//{qn('w:ins')}")[-1]
                            self._attach_comment_spanning(
                                start_p,
                                first_del_element,
                                end_p,
                                last_ins,
                                edit.comment,
                            )
                        elif ins_elem is not None:
                            end_p = ins_elem.getparent()
                            if start_p == end_p:
                                self._attach_comment(parent, first_del_element, ins_elem, edit.comment)
                            else:
                                self._attach_comment_spanning(
                                    start_p,
                                    first_del_element,
                                    end_p,
                                    ins_elem,
                                    edit.comment,
                                )
                        else:
                            self._attach_comment(
                                start_p,
                                first_del_element,
                                last_del_element,
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

            for span in virtual_spans:
                if span.paragraph:
                    p1_element = span.paragraph._element
                    p2_element = p1_element.getnext()
                    while p2_element is not None and p2_element.tag != qn("w:p"):
                        p2_element = p2_element.getnext()

                    if p2_element is not None and p2_element.tag == qn("w:p"):
                        # 1. Track pilcrow deletion in p1
                        pPr = p1_element.find(qn("w:pPr"))
                        if pPr is None:
                            pPr = create_element("w:pPr")
                            p1_element.insert(0, pPr)
                        rPr = pPr.find(qn("w:rPr"))
                        if rPr is None:
                            rPr = create_element("w:rPr")
                            pPr.append(rPr)

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
            has_visible = False
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
                        has_visible = True
                        break
                if has_visible:
                    break

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

    def save_to_stream(self) -> BytesIO:
        import lxml.etree as etree

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

    def apply_review_actions(self, actions: List[Union[AcceptChange, RejectChange, ReplyComment]]) -> tuple[int, int]:
        applied = 0
        skipped = 0
        resolved_history = set()

        for act in actions:
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

            if is_change and target_id in resolved_history:
                applied += 1
                continue

            resolved_now = set()
            success = False

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
                if resolved_now:
                    resolved_history.update(resolved_now)
                applied += 1
            else:
                self.skipped_details.append(f"- Failed to apply action: {act.type} on {target_id}")
                skipped += 1

        return applied, skipped

    def _clean_wrapping_comments(self, element):
        """
        Removes comment anchors that tightly wrap this element (or a paired del/ins).
        This prevents orphaned comment ranges from leaking when an edit is accepted/rejected.
        """
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
            self._clean_wrapping_comments(ins)
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
            self._clean_wrapping_comments(d)
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
            self._clean_wrapping_comments(ins)
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
            self._clean_wrapping_comments(d)
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
    def accept_all_revisions(self):
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
            for ins in root_element.findall(f".//{qn('w:ins')}"):
                self._clean_wrapping_comments(ins)
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
                            self._clean_wrapping_comments(p)
                            self._delete_comments_in_element(p)
                            if p.getparent() is not None:
                                p.getparent().remove(p)

            for d in root_element.findall(f".//{qn('w:del')}"):
                self._clean_wrapping_comments(d)
                self._delete_comments_in_element(d)
                parent = d.getparent()
                if parent is not None:
                    if parent.tag == qn("w:trPr"):
                        row = parent.getparent()
                        if row is not None:
                            row.getparent().remove(row)
                    else:
                        parent.remove(d)

        # Final pass: remove any comments that survived the tracked-change
        # sweeps. accept_all_revisions semantically means "produce a finalized
        # clean document" (per the tool docstring), so free-standing comments
        # — those not tightly wrapping an accepted/rejected revision — must
        # also be removed.
        #
        # We discover comment IDs by scanning the main document body for
        # commentRangeStart / commentRangeEnd / commentReference anchors. The
        # comments part itself is the source of truth for what exists, so we
        # also harvest any remaining <w:comment w:id="..."> entries directly
        # to handle the (rare) case where a comment exists in comments.xml
        # without a body anchor.
        comment_ids: set[str] = set()
        body_root = self.doc.element
        for tag in ("w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"):
            for node in body_root.findall(f".//{qn(tag)}"):
                cid = node.get(qn("w:id"))
                if cid:
                    comment_ids.add(cid)

        comments_part = self.comments_manager._get_existing_part_by_type(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
        )
        if comments_part is not None:
            if hasattr(comments_part, "element"):
                comments_root = comments_part.element
            else:
                comments_root = parse_xml(comments_part.blob)
            for c in comments_root.findall(qn("w:comment")):
                cid = c.get(qn("w:id"))
                if cid:
                    comment_ids.add(cid)

        for cid in comment_ids:
            self.comments_manager.delete_comment(cid)
