# FILE: src/adeu/redline/engine.py

import datetime
import re
from copy import deepcopy
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import structlog
from docx import Document
from docx.oxml.ns import nsmap, qn
from docx.text.run import Run

from adeu.diff import trim_common_context
from adeu.models import DocumentEdit, EditOperationType, ReviewAction
from adeu.redline.comments import CommentsManager
from adeu.redline.mapper import DocumentMapper
from adeu.utils.docx import create_attribute, create_element, normalize_docx

logger = structlog.get_logger(__name__)

# Register w16du namespace for dateUtc
w16du_ns = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
if "w16du" not in nsmap:
    nsmap["w16du"] = w16du_ns


class RedlineEngine:
    def __init__(self, doc_stream: BytesIO, author: str = "Adeu AI"):
        self.doc = Document(doc_stream)
        normalize_docx(self.doc)
        self.author = author
        self.timestamp = (
            datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self.current_id = self._scan_existing_ids()
        self.mapper = DocumentMapper(self.doc)
        self.comments_manager = CommentsManager(self.doc)
        self.clean_mapper: Optional[DocumentMapper] = None

    def _get_paired_nodes(self, node):
        """
        Finds all contiguous w:ins/w:del nodes that form a single logical Modification block.
        This handles cases where a modification spans multiple runs (producing multiple w:del tags)
        followed by a w:ins tag, ensuring they are accepted/rejected atomically.
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

    def _create_track_change_tag(self, tag_name: str, author: str = ""):
        tag = create_element(tag_name)
        create_attribute(tag, "w:id", self._get_next_id())
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
        Detects if text starts with markdown header (e.g. '## Title').
        Returns (clean_text, style_name).
        """
        if text.startswith("#"):
            level = 0
            while text.startswith("#"):
                level += 1
                text = text[1:]

            if text.startswith(" "):
                return text.strip(), f"Heading {level}"

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
        comment: Optional[str] = None,
        suppress_inherited: bool = False,
    ):
        """
        Inserts text. If text contains newlines, splits into multiple paragraphs.
        """
        lines = re.split(r"[\r\n]+", text)
        if not lines:
            return None

        # 0. Check if FIRST line implies a block element (Header)
        first_clean, first_style = self._parse_markdown_style(lines[0])

        if first_style:
            if not anchor_run:
                return None

            # Robustly traverse up to the actual w:p tag, bypassing w:ins/w:del wrappers
            current_p = anchor_run._element.getparent()
            while current_p is not None and current_p.tag != qn("w:p"):
                current_p = current_p.getparent()

            if current_p is None and hasattr(anchor_run, "_parent"):
                p_obj = anchor_run._parent
                if hasattr(p_obj, "_element") and p_obj._element.tag == qn("w:p"):
                    current_p = p_obj._element

            if current_p is None:
                return None

            body = current_p.getparent()
            if body is None:
                return None

            try:
                p_index = body.index(current_p)
            except ValueError:
                return None

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

                new_ins = self._create_track_change_tag("w:ins")

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
                # RESTORED +1: Insert AFTER the anchor paragraph, matching existing test expectations
                body.insert(p_index + 1 + i, new_p)
                created_nodes.append((new_p, new_ins))

            if comment and created_nodes:
                start_p, start_ins = created_nodes[0]
                end_p, end_ins = created_nodes[-1]
                if start_p == end_p:
                    self._attach_comment(start_p, start_ins, start_ins, comment)
                else:
                    self._attach_comment_spanning(start_p, start_ins, end_p, end_ins, comment)

            return None

        # 1. Inline Logic
        first_line = lines[0]
        ins_elem = self._track_insert_inline(first_line, anchor_run, suppress_inherited=suppress_inherited)

        remaining_lines = lines[1:]
        if remaining_lines and remaining_lines[-1] == "":
            remaining_lines.pop()

        if remaining_lines:
            if not anchor_run:
                return ins_elem

            # Robustly traverse up to the actual w:p tag, bypassing w:ins/w:del wrappers
            current_p_element = anchor_run._element.getparent()
            while current_p_element is not None and current_p_element.tag != qn("w:p"):
                current_p_element = current_p_element.getparent()

            if current_p_element is None and hasattr(anchor_run, "_parent"):
                p_obj = anchor_run._parent
                if hasattr(p_obj, "_element") and p_obj._element.tag == qn("w:p"):
                    current_p_element = p_obj._element

            if current_p_element is None:
                return ins_elem

            parent_body = current_p_element.getparent()
            if parent_body is None:
                return ins_elem

            try:
                p_index = parent_body.index(current_p_element)
            except ValueError:
                return ins_elem

            for i, line_text in enumerate(remaining_lines):
                clean_text, style_name = self._parse_markdown_style(line_text)
                new_p = create_element("w:p")
                if style_name:
                    self._set_paragraph_style(new_p, style_name)
                elif current_p_element.pPr is not None:
                    new_p.append(deepcopy(current_p_element.pPr))

                new_ins = self._create_track_change_tag("w:ins")

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

        return ins_elem

    def _apply_run_props(self, run_element, props: Dict[str, Any], suppress_inherited: bool = False) -> None:
        """
        Applies Bold/Italic properties to a run.
        Fix 5.3: When suppress_inherited=True, explicitly turns off properties not in props,
        preventing inheritance bleed from deepcopied runs.
        """
        if not props:
            if not suppress_inherited:
                return
            props = {}

        rPr = run_element.find(qn("w:rPr"))
        if rPr is None:
            rPr = create_element("w:rPr")
            run_element.insert(0, rPr)

        # Handle Bold
        b_tag = rPr.find(qn("w:b"))
        if props.get("bold"):
            if b_tag is None:
                b_tag = create_element("w:b")
                rPr.append(b_tag)
            b_tag.set(qn("w:val"), "1")
        elif suppress_inherited:
            if b_tag is not None:
                b_tag.set(qn("w:val"), "0")

        # Handle Italic
        i_tag = rPr.find(qn("w:i"))
        if props.get("italic"):
            if i_tag is None:
                i_tag = create_element("w:i")
                rPr.append(i_tag)
            i_tag.set(qn("w:val"), "1")
        elif suppress_inherited:
            if i_tag is not None:
                i_tag.set(qn("w:val"), "0")

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
    ):
        ins = self._create_track_change_tag("w:ins")

        segments = self._parse_inline_markdown(text)

        for seg_text, seg_props in segments:
            run = create_element("w:r")

            if anchor_run and anchor_run._element.rPr is not None:
                run.append(deepcopy(anchor_run._element.rPr))

            self._apply_run_props(run, seg_props, suppress_inherited=suppress_inherited)

            t = create_element("w:t")
            self._set_text_content(t, seg_text)
            run.append(t)
            ins.append(run)

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

    def track_delete_run(self, run: Run):
        del_tag = self._create_track_change_tag("w:del")
        new_run = create_element("w:r")
        if run._r.rPr is not None:
            new_run.append(deepcopy(run._r.rPr))
        text_content = run.text
        del_text = create_element("w:delText")
        self._set_text_content(del_text, text_content)
        new_run.append(del_text)
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

    def validate_edits(self, edits: List[DocumentEdit]) -> List[str]:
        """
        Performs an exhaustive dry-run validation of all text edits in the batch.
        Returns a list of error strings. If the list is empty, the batch is safe to apply.
        """
        errors = []

        # Ensure base mapper is ready
        self.mapper._build_map()

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

            if len(matches) == 0:
                errors.append(f'- Edit {i + 1} Failed: Target text not found in document:\n  "{edit.target_text}"')
            elif len(matches) > 1:
                error_msg = [
                    f"- Edit {i + 1} Failed: Ambiguous match. Target text appears "
                    f"{len(matches)} times. Occurrences found at:"
                ]

                for idx, (start, length) in enumerate(matches):
                    end = start + length
                    # Extract context (~50 chars before and after to ensure full clause names are captured)
                    pre_context = active_text[max(0, start - 50) : start].replace("\n", " ")
                    post_context = active_text[end : min(len(active_text), end + 50)].replace("\n", " ")
                    match_text = active_text[start:end].replace("\n", " ")

                    # Truncate match_text if it's extremely long for the error report
                    if len(match_text) > 50:
                        match_text = match_text[:25] + "..." + match_text[-20:]

                    error_msg.append(f'    {idx + 1}. "...{pre_context}[{match_text}]{post_context}..."')

                error_msg.append(
                    "  Please provide more surrounding context in your target_text to uniquely identify the location."
                )
                errors.append("\n".join(error_msg))

        return errors

    def apply_edits(self, edits: List[DocumentEdit]) -> tuple[int, int]:
        indexed_edits = [e for e in edits if e._match_start_index is not None]
        unindexed_edits = [e for e in edits if e._match_start_index is None]

        applied = 0
        skipped = 0
        occupied_ranges: List[Tuple[int, int]] = []

        # Indexed First (Reverse Order)
        indexed_edits.sort(key=lambda x: x._match_start_index or 0, reverse=True)
        for edit in indexed_edits:
            # Fix 5.6: Prevent collisions from overlapping edits
            start = edit._match_start_index or 0
            end = start + (len(edit.target_text) if edit.target_text else 0)
            if any(start < occ_end and end > occ_start for occ_start, occ_end in occupied_ranges):
                logger.warning(f"Skipping overlapping edit at index {start}")
                skipped += 1
                continue
            if self._apply_single_edit_indexed(edit):
                applied += 1
                occupied_ranges.append((start, end))
            else:
                skipped += 1

        # Heuristic Second
        if unindexed_edits:
            unindexed_edits.sort(key=lambda x: len(x.target_text), reverse=True)
            self.mapper._build_map()
            for edit in unindexed_edits:
                # Fix 5.6: Check for overlaps in heuristic path too
                if edit.target_text:
                    start_idx, match_len = self.mapper.find_match_index(edit.target_text)
                    if start_idx != -1:
                        end_idx = start_idx + match_len
                        if any(start_idx < occ_end and end_idx > occ_start for occ_start, occ_end in occupied_ranges):
                            logger.warning(f"Skipping overlapping heuristic edit at index {start_idx}")
                            skipped += 1
                            continue
                        if self._apply_single_edit_heuristic(edit):
                            applied += 1
                            occupied_ranges.append((start_idx, end_idx))
                            self.mapper._build_map()
                        else:
                            skipped += 1
                        continue
                if self._apply_single_edit_heuristic(edit):
                    applied += 1
                    self.mapper._build_map()
                else:
                    skipped += 1
        return applied, skipped

    def _apply_single_edit_heuristic(self, edit: DocumentEdit) -> bool:
        if not edit.target_text:
            logger.warning("Skipping heuristic edit: target_text is empty.")
            return False

        start_idx, match_len = self.mapper.find_match_index(edit.target_text)

        # FALLBACK: If Raw View match failed, try matching against Clean View
        use_clean_map = False
        if start_idx == -1:
            if not self.clean_mapper:
                self.clean_mapper = DocumentMapper(self.doc, clean_view=True)

            start_idx, match_len = self.clean_mapper.find_match_index(edit.target_text)
            if start_idx != -1:
                logger.info("Matched edit against Clean View.")
                use_clean_map = True
            else:
                logger.warning(f"Skipping edit: Target '{edit.target_text[:20]}...' not found (Raw or Clean).")
                return False

        if use_clean_map and self.clean_mapper:
            active_mapper = self.clean_mapper
        else:
            active_mapper = self.mapper

        effective_new_text = edit.new_text or ""
        actual_doc_text = self.mapper.full_text[start_idx : start_idx + match_len]

        if actual_doc_text == effective_new_text:
            return True

        if effective_new_text.startswith(actual_doc_text):
            effective_op = EditOperationType.INSERTION
            final_target = ""
            final_new = effective_new_text[len(actual_doc_text) :]
            effective_start_idx = start_idx + match_len
        else:
            prefix_len, suffix_len = trim_common_context(actual_doc_text, effective_new_text)

            t_end = len(actual_doc_text) - suffix_len
            n_end = len(effective_new_text) - suffix_len

            final_target = actual_doc_text[prefix_len:t_end]
            final_new = effective_new_text[prefix_len:n_end]
            effective_start_idx = start_idx + prefix_len

            if not final_target and final_new:
                effective_op = EditOperationType.INSERTION
            elif final_target and not final_new:
                effective_op = EditOperationType.DELETION
            elif final_target and final_new:
                effective_op = EditOperationType.MODIFICATION
            else:
                return True

        proxy_edit = DocumentEdit(target_text=final_target, new_text=final_new, comment=edit.comment)
        proxy_edit._match_start_index = effective_start_idx
        proxy_edit._internal_op = effective_op
        proxy_edit._active_mapper_ref = active_mapper

        return self._apply_single_edit_indexed(proxy_edit)

    def _apply_single_edit_indexed(self, edit: DocumentEdit) -> bool:
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

        if op == EditOperationType.INSERTION:
            anchor_run = self.mapper.get_insertion_anchor(start_idx)
            if not anchor_run:
                return False

            parent = anchor_run._element.getparent()
            index = parent.index(anchor_run._element)

            final_new_text = edit.new_text or ""

            if start_idx == 0:
                ins_elem = self.track_insert(final_new_text, anchor_run=anchor_run, comment=edit.comment)
                if ins_elem is not None:
                    if parent.tag == qn("w:ins"):
                        self._insert_and_split_ins(parent, index, ins_elem)
                        actual_parent = parent.getparent()
                    else:
                        parent.insert(index, ins_elem)
                        actual_parent = parent

                    if edit.comment:
                        self._attach_comment(actual_parent, ins_elem, ins_elem, edit.comment)
            else:
                next_run = self._get_next_run(anchor_run)
                style_run = self._determine_style_source(anchor_run, next_run, final_new_text)
                ins_elem = self.track_insert(final_new_text, anchor_run=style_run, comment=edit.comment)
                if ins_elem is not None:
                    if parent.tag == qn("w:ins"):
                        self._insert_and_split_ins(parent, index + 1, ins_elem)
                        actual_parent = parent.getparent()
                    else:
                        parent.insert(index + 1, ins_elem)
                        actual_parent = parent

                    if edit.comment:
                        self._attach_comment(actual_parent, ins_elem, ins_elem, edit.comment)
            return True

        if op == EditOperationType.INSERTION:
            anchor_run = self.mapper.get_insertion_anchor(start_idx)
            if not anchor_run:
                return False

            parent = anchor_run._element.getparent()
            index = parent.index(anchor_run._element)

            final_new_text = edit.new_text or ""

            if start_idx == 0:
                ins_elem = self.track_insert(final_new_text, anchor_run=anchor_run, comment=edit.comment)
                if ins_elem is not None:
                    parent.insert(index, ins_elem)
                if edit.comment and ins_elem is not None:
                    self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
            else:
                next_run = self._get_next_run(anchor_run)
                style_run = self._determine_style_source(anchor_run, next_run, final_new_text)
                ins_elem = self.track_insert(final_new_text, anchor_run=style_run, comment=edit.comment)
                if ins_elem is not None:
                    parent.insert(index + 1, ins_elem)
                if edit.comment and ins_elem is not None:
                    self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
            return True

        target_runs = active_mapper.find_target_runs_by_index(start_idx, length)
        if not target_runs:
            return False

        if op == EditOperationType.DELETION:
            for run in target_runs:
                self.track_delete_run(run)

        elif op == EditOperationType.MODIFICATION:
            first_del_element = None
            last_del_element = None
            for run in target_runs:
                del_elem = self.track_delete_run(run)
                if first_del_element is None:
                    first_del_element = del_elem
                last_del_element = del_elem

            if last_del_element is not None and edit.new_text:
                parent = last_del_element.getparent()
                del_index = parent.index(last_del_element)

                text_to_insert = edit.new_text
                clean_text, style_name = self._parse_markdown_style(text_to_insert)
                if style_name:
                    anchor_para = target_runs[-1]._parent
                    current_style = getattr(anchor_para, "style", None)
                    if current_style and getattr(current_style, "name", "") == style_name:
                        text_to_insert = clean_text

                # Fix 5.3: Suppress inherited formatting if new text has no markdown markers
                _has_markdown = bool(re.search(r"\*\*|_", text_to_insert))
                ins_elem = self.track_insert(
                    text_to_insert,
                    anchor_run=Run(target_runs[-1]._element, target_runs[-1]._parent),
                    comment=edit.comment,
                    suppress_inherited=not _has_markdown,
                )
                if ins_elem is not None:
                    parent.insert(del_index + 1, ins_elem)

                if edit.comment and ins_elem is not None and first_del_element is not None:
                    start_p = first_del_element.getparent()
                    end_p = ins_elem.getparent()

                    if start_p == end_p:
                        self._attach_comment(parent, first_del_element, ins_elem, edit.comment)
                    else:
                        self._attach_comment_spanning(start_p, first_del_element, end_p, ins_elem, edit.comment)
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
        output = BytesIO()
        self.doc.save(output)
        output.seek(0)
        return output

    def apply_review_actions(self, actions: List[ReviewAction]) -> tuple[int, int]:
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

            # If this edit was already swept up in a paired resolution, mark as applied and skip
            if is_change and target_id in resolved_history:
                applied += 1
                continue

            resolved_now = set()
            success = False

            if act.action == "ACCEPT":
                if is_change:
                    resolved_now = self._accept_change(target_id)
                    success = bool(resolved_now)
            elif act.action == "REJECT":
                if is_change:
                    resolved_now = self._reject_change(target_id)
                    success = bool(resolved_now)
            elif act.action == "REPLY":
                if is_comment:
                    success = self._reply_to_comment(target_id, act.text or "")

            if success:
                if resolved_now:
                    resolved_history.update(resolved_now)
                applied += 1
            else:
                skipped += 1

        if applied > 0:
            normalize_docx(self.doc)

        return applied, skipped

    def _clean_wrapping_comments(self, element):
        """
        Removes comment anchors that tightly wrap this element (or a paired del/ins).
        This prevents orphaned comment ranges from leaking when an edit is accepted/rejected.
        """
        # 1. Collect tightly adjacent Start anchors (looking backwards)
        starts_to_remove = []
        prev = element.getprevious()
        while prev is not None:
            if prev.tag == qn("w:commentRangeStart"):
                starts_to_remove.append(prev)
                prev = prev.getprevious()
            elif prev.tag in (qn("w:rPr"), qn("w:pPr")):
                prev = prev.getprevious()
            else:
                break

        # 2. Collect tightly adjacent End/Ref anchors (looking forwards)
        ends_to_remove = []
        nxt = element.getnext()
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
            elif nxt.tag in (qn("w:ins"), qn("w:del")):
                # Skip over the rest of the paired edit block to find the ending anchor
                nxt = nxt.getnext()
            else:
                break

        # 3. Match pairs and delete
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
            index = parent.index(ins)
            for child in list(ins):
                parent.insert(index, child)
                index += 1
            parent.remove(ins)

        for d in all_del:
            self._clean_wrapping_comments(d)
            self._delete_comments_in_element(d)
            if d.getparent() is not None:
                d.getparent().remove(d)

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
            if ins.getparent() is not None:
                ins.getparent().remove(ins)

        for d in all_del:
            self._clean_wrapping_comments(d)
            parent = d.getparent()
            if parent is None:
                continue
            index = parent.index(d)
            for child in list(d):
                for dt in child.findall(f".//{qn('w:delText')}"):
                    dt.tag = qn("w:t")
                parent.insert(index, child)
                index += 1
            parent.remove(d)

        return resolved_ids

    def _reply_to_comment(self, target_id: str, text: str) -> bool:
        if not self.comments_manager.comments_part:
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

    def accept_all_revisions(self):
        for ins in self.doc.element.xpath("//w:ins"):
            parent = ins.getparent()
            index = parent.index(ins)
            for child in list(ins):
                parent.insert(index, child)
                index += 1
            parent.remove(ins)

        for d in self.doc.element.xpath("//w:del"):
            self._delete_comments_in_element(d)
            d.getparent().remove(d)

        # 1. Purge all remaining comments from the comment manager XML parts
        # FIX: Use findall with qn()
        for ref in self.doc.element.findall(f".//{qn('w:commentReference')}"):
            c_id = ref.get(qn("w:id"))
            if c_id:
                self.comments_manager.delete_comment(c_id)

        # 2. Strip all stray comment tags from the document body
        for tag in ["w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"]:
            # FIX: Use findall with qn()
            for el in self.doc.element.findall(f".//{qn(tag)}"):
                if el.getparent() is not None:
                    el.getparent().remove(el)
