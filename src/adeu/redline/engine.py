import datetime
import re
from copy import deepcopy
from io import BytesIO
from typing import List, Optional

import structlog
from docx import Document
from docx.oxml.ns import qn
from docx.text.run import Run

from adeu.models import DocumentEdit, EditOperationType
from adeu.redline.comments import CommentsManager
from adeu.redline.mapper import DocumentMapper
from adeu.utils.docx import create_attribute, create_element, normalize_docx

logger = structlog.get_logger(__name__)


def _trim_common_context(target: str, new_val: str) -> tuple[int, int]:
    """
    Calculates overlapping prefix/suffix lengths between target and new_val.
    Returns (prefix_len, suffix_len).
    Ensures that we only trim at word boundaries (whitespace).
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
        while prefix_len > 0 and not target[prefix_len - 1].isspace() and not target[prefix_len].isspace():
            prefix_len -= 1

    # Safety: Backtrack if we consumed a Markdown Header marker (#)
    # This ensures track_insert sees the '#' and triggers block logic.
    temp_len = prefix_len
    # Scan backwards in the matched prefix
    while temp_len > 0:
        char = target[temp_len - 1]
        if char == "#":
            # Found a hash in the prefix. Backtrack to start of line/block.
            prefix_len = temp_len - 1
            while prefix_len > 0 and target[prefix_len - 1] != "\n":
                prefix_len -= 1
            break
        if char == "\n":
            # We hit a newline safely without seeing a hash. Stop checking.
            break
        temp_len -= 1

    # 2. Suffix with Word Boundary Check
    suffix_len = 0
    target_rem_len = len(target) - prefix_len
    new_rem_len = len(new_val) - prefix_len

    limit_suffix = min(target_rem_len, new_rem_len)
    while suffix_len < limit_suffix and target[-(suffix_len + 1)] == new_val[-(suffix_len + 1)]:
        suffix_len += 1

    # Backtrack suffix if we split a word
    if suffix_len > 0 and suffix_len < len(target):
        while suffix_len > 0 and not target[-(suffix_len + 1)].isspace() and not target[-(suffix_len)].isspace():
            suffix_len -= 1

    return prefix_len, suffix_len


class RedlineEngine:
    def __init__(self, doc_stream: BytesIO, author: str = "Adeu AI"):
        self.doc = Document(doc_stream)
        normalize_docx(self.doc)
        self.author = author
        self.timestamp = datetime.datetime.now().replace(microsecond=0).isoformat() + "Z"
        self.current_id = 0
        self.mapper = DocumentMapper(self.doc)
        self.comments_manager = CommentsManager(self.doc)

    def _get_next_id(self):
        self.current_id += 1
        return str(self.current_id)

    def _create_track_change_tag(self, tag_name: str, author: str = ""):
        tag = create_element(tag_name)
        create_attribute(tag, "w:id", self._get_next_id())
        create_attribute(tag, "w:author", author or self.author)
        create_attribute(tag, "w:date", self.timestamp)
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
        # Support headers up to Level 6 (standard Markdown) or even 9 (Word max)
        if text.startswith("#"):
            level = 0
            while text.startswith("#"):
                level += 1
                text = text[1:]

            # Ensure there was a space after the hashes (e.g. "# Title")
            if text.startswith(" "):
                return text.strip(), f"Heading {level}"

        return text, None

    def track_insert(self, text: str, anchor_run: Optional[Run] = None):
        """
        Inserts text. If text contains newlines, splits into multiple paragraphs
        injected after the anchor_run's paragraph.
        Treats one or more newlines as a single paragraph break.
        """
        # Split by one or more newlines
        lines = re.split(r"[\r\n]+", text)
        if not lines:
            return None

        # 0. Check if FIRST line implies a block element (Header)
        first_clean, first_style = self._parse_markdown_style(lines[0])

        if first_style:
            if not anchor_run:
                return None

            # Robustly find parent paragraph.
            current_p = anchor_run._element.getparent()
            if current_p is None and hasattr(anchor_run, "_parent"):
                current_p = getattr(anchor_run._parent, "_element", None)

            if current_p is None:
                return None

            body = current_p.getparent()
            if body is None:
                return None

            try:
                p_index = body.index(current_p)
            except ValueError:
                return None

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
                new_run = create_element("w:r")
                if anchor_run and anchor_run._element.rPr is not None:
                    new_run.append(deepcopy(anchor_run._element.rPr))

                t = create_element("w:t")
                self._set_text_content(t, c_text)
                new_run.append(t)
                new_ins.append(new_run)
                new_p.append(new_ins)

                body.insert(p_index + 1 + i, new_p)

            return None

        # 1. Inline Logic
        first_line = lines[0]
        ins_elem = self._track_insert_inline(first_line, anchor_run)

        remaining_lines = lines[1:]
        if remaining_lines and remaining_lines[-1] == "":
            remaining_lines.pop()

        if remaining_lines:
            if not anchor_run:
                return ins_elem

            current_p_element = anchor_run._element.getparent()
            if current_p_element is None and hasattr(anchor_run, "_parent"):
                current_p_element = getattr(anchor_run._parent, "_element", None)

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
                new_run = create_element("w:r")
                if anchor_run and anchor_run._element.rPr is not None:
                    new_run.append(deepcopy(anchor_run._element.rPr))

                t = create_element("w:t")
                self._set_text_content(t, clean_text)
                new_run.append(t)
                new_ins.append(new_run)
                new_p.append(new_ins)

                parent_body.insert(p_index + 1 + i, new_p)

        return ins_elem

    def _set_paragraph_style(self, p_element, style_name: str):
        existing_pPr = p_element.find(qn("w:pPr"))
        if existing_pPr is not None:
            p_element.remove(existing_pPr)
        pPr = create_element("w:pPr")
        pStyle = create_element("w:pStyle")

        # Resolve Style Name to ID (e.g. "Heading 1" -> "Heading1")
        try:
            style_id = self.doc.styles[style_name].style_id
        except (KeyError, ValueError):
            style_id = style_name.replace(" ", "")

        create_attribute(pStyle, "w:val", style_id)
        pPr.append(pStyle)
        p_element.insert(0, pPr)

    def _track_insert_inline(self, text: str, anchor_run: Optional[Run] = None):
        ins = self._create_track_change_tag("w:ins")
        run = create_element("w:r")
        if anchor_run and anchor_run._element.rPr is not None:
            run.append(deepcopy(anchor_run._element.rPr))
        t = create_element("w:t")
        self._set_text_content(t, text)
        run.append(t)
        ins.append(run)
        return ins

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
        ref = create_element("w:commentReference")
        create_attribute(ref, "w:id", comment_id)
        ref_run.append(ref)

        start_index = parent_element.index(start_element)
        parent_element.insert(start_index, range_start)
        end_index = parent_element.index(end_element)
        parent_element.insert(end_index + 1, range_end)
        parent_element.insert(end_index + 2, ref_run)

    def apply_edits(self, edits: List[DocumentEdit]) -> tuple[int, int]:
        indexed_edits = [e for e in edits if e._match_start_index is not None]
        unindexed_edits = [e for e in edits if e._match_start_index is None]

        applied = 0
        skipped = 0

        # Indexed First (Reverse Order)
        indexed_edits.sort(key=lambda x: x._match_start_index or 0, reverse=True)
        for edit in indexed_edits:
            if self._apply_single_edit_indexed(edit):
                applied += 1
            else:
                skipped += 1

        # Heuristic Second
        if unindexed_edits:
            unindexed_edits.sort(key=lambda x: len(x.target_text), reverse=True)
            self.mapper._build_map()
            for edit in unindexed_edits:
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

        start_idx = self.mapper.find_match_index(edit.target_text)

        if start_idx == -1:
            logger.warning(f"Skipping edit: Target '{edit.target_text[:20]}...' not found.")
            return False

        effective_new_text = edit.new_text or ""
        effective_target_text = edit.target_text

        if effective_target_text == effective_new_text:
            return True

        if effective_new_text.startswith(effective_target_text):
            effective_op = EditOperationType.INSERTION
            final_target = ""
            final_new = effective_new_text[len(effective_target_text) :]
            effective_start_idx = start_idx + len(effective_target_text)
        else:
            prefix_len, suffix_len = _trim_common_context(effective_target_text, effective_new_text)

            t_end = len(effective_target_text) - suffix_len
            n_end = len(effective_new_text) - suffix_len

            final_target = effective_target_text[prefix_len:t_end]
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

        return self._apply_single_edit_indexed(proxy_edit)

    def _apply_single_edit_indexed(self, edit: DocumentEdit) -> bool:
        op = edit._internal_op

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
                ins_elem = self.track_insert(final_new_text, anchor_run=anchor_run)
                if ins_elem is not None:
                    parent.insert(index, ins_elem)
                if edit.comment and ins_elem is not None:
                    self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
            else:
                next_run = self._get_next_run(anchor_run)
                style_run = self._determine_style_source(anchor_run, next_run, final_new_text)
                ins_elem = self.track_insert(final_new_text, anchor_run=style_run)
                if ins_elem is not None:
                    parent.insert(index + 1, ins_elem)
                if edit.comment and ins_elem is not None:
                    self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
            return True

        # Deletion / Modification
        target_runs = self.mapper.find_target_runs_by_index(start_idx, length)
        if not target_runs:
            return False

        if op == EditOperationType.DELETION:
            for run in target_runs:
                self.track_delete_run(run)

        elif op == EditOperationType.MODIFICATION:
            last_del_element = None
            for run in target_runs:
                last_del_element = self.track_delete_run(run)

            if last_del_element is not None and edit.new_text:
                parent = last_del_element.getparent()
                del_index = parent.index(last_del_element)

                ins_elem = self.track_insert(
                    edit.new_text, anchor_run=Run(target_runs[-1]._element, target_runs[-1]._parent)
                )
                if ins_elem is not None:
                    parent.insert(del_index + 1, ins_elem)
                if edit.comment and ins_elem is not None:
                    self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
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
