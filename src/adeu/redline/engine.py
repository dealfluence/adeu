import datetime
import re
from copy import deepcopy
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import structlog
from docx import Document
from docx.oxml.ns import nsmap, qn
from docx.text.run import Run

from adeu.models import DocumentEdit, EditOperationType, ReviewAction
from adeu.redline.comments import CommentsManager
from adeu.redline.mapper import DocumentMapper
from adeu.utils.docx import create_attribute, create_element, normalize_docx

logger = structlog.get_logger(__name__)

# Register w16du namespace for dateUtc
w16du_ns = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
if "w16du" not in nsmap:
    nsmap["w16du"] = w16du_ns


def _trim_common_context(target: str, new_val: str) -> tuple[int, int]:
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
        while prefix_len > 0 and not target[prefix_len - 1].isspace() and not target[prefix_len].isspace():
            prefix_len -= 1

    # Safety: Backtrack if we consumed a Markdown Header marker (#)
    temp_len = prefix_len
    while temp_len > 0:
        char = target[temp_len - 1]
        if char == "#":
            prefix_len = temp_len - 1
            while prefix_len > 0 and target[prefix_len - 1] != "\n":
                prefix_len -= 1
            break
        if char == "\n":
            break
        temp_len -= 1

    # Safety: Backtrack if we are inside a Markdown Inline Delimiter (** or _)
    # We must be "balanced" in the prefix to safely trim it.
    # If we have an odd number of delimiters, we are likely inside a block.
    # We backtrack until we are balanced (usually means backtracking to 0 or previous block end).

    def get_unbalanced_index(text_slice: str) -> int:
        # Check **
        # We find all occurrences. If count is odd, return index of last occurrence.
        # Note: This is heuristic. Nested **_..._** might confuse simple counting,
        # but for trimming "context", we generally want to avoid cutting ANY formatting.

        # Check **
        bold_indices = [m.start() for m in re.finditer(r"\*\*", text_slice)]
        if len(bold_indices) % 2 != 0:
            return bold_indices[-1]

        # Check _
        # We only care about _ if it's acting as a delimiter.
        # Ideally we use the same regex as the parser, but counting is a safe conservative proxy.
        # If we mistakenly backtrack because of a snake_case variable, we just re-write the text.
        # This is safer than corrupting the doc.
        underscore_indices = [m.start() for m in re.finditer(r"_", text_slice)]
        if len(underscore_indices) % 2 != 0:
            return underscore_indices[-1]

        return -1

    while prefix_len > 0:
        current_slice = target[:prefix_len]
        unbalanced_idx = get_unbalanced_index(current_slice)
        if unbalanced_idx != -1:
            # Backtrack to BEFORE the unbalanced token
            prefix_len = unbalanced_idx
        else:
            break

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

    # Safety: Backtrack Suffix if unbalanced
    # For suffix, we check the text segment we are about to trim (from the end).
    # e.g. target="...end**", suffix="**". Count is 1. Unbalanced.
    while suffix_len > 0:
        current_slice = target[len(target) - suffix_len :]
        unbalanced_idx = get_unbalanced_index(current_slice)
        if unbalanced_idx != -1:
            # For suffix, "unbalanced" means we included a delimiter but not its pair.
            # However, get_unbalanced_index returns index relative to start of slice.
            # If we are unbalanced, we just shrink suffix_len.
            # A simple approach: reduce suffix_len by 1 and re-check?
            # Or reduce until the offending token is excluded.

            # Since get_unbalanced_index returns the *start* of the last token,
            # we want to trim the suffix so it starts *after* this token.
            # slice: "foo_bar". Unbalanced at 3 (_).
            # We want suffix to be "bar" (len 3)? No, that's still inside.
            # We want to exclude the whole block.

            # Conservative approach: If unbalanced, reduce suffix to 0?
            # Or reduce linearly. Since this loop is fast for short suffixes:
            suffix_len -= 1
        else:
            break

    # CHANGE: If the calculated suffix is purely whitespace, ignore it (set to 0).
    # This prevents fragmenting edits like "word " -> "word2 " into Del("word")-Space-Ins("word2").
    # Instead we get Del("word ") -> Ins("word2 "), which matches Word's native behavior.
    if suffix_len > 0 and target[len(target) - suffix_len :].isspace():
        suffix_len = 0

    return prefix_len, suffix_len


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

    def _scan_existing_ids(self) -> int:
        """
        Scans the document body for existing w:id attributes in w:ins and w:del
        to ensure new IDs do not collide.
        """
        max_id = 0
        # XPath to find all w:ins and w:del tags
        # Note: comments IDs are separate (handled by CommentsManager)
        # But track changes IDs must be unique within the document body context.
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

    def _parse_inline_markdown(
        self, text: str, base_style: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Recursively parses bold (**) and italic (_) markdown.
        Returns a flat list of (text_segment, combined_style_dict).
        Supports arbitrary nesting.
        """
        if base_style is None:
            base_style = {}

        if not text:
            return []

        # Combined Regex for "First match wins" (Left-to-Right scanning)
        # Group 1: Bold (**...**)
        # Group 2: Italic (_..._)
        token_pattern = re.compile(r"(\*\*.*?\*\*)|(_.*?_)")

        match = token_pattern.search(text)

        if not match:
            # No tags found, return clean text
            return [(text, base_style)]

        start, end = match.span()

        # Determine which group matched
        if match.group(1):
            tag_type = "bold"
            inner_raw = match.group(1)
        else:
            tag_type = "italic"
            inner_raw = match.group(2)

        # Split text: [Pre] [Inner] [Post]
        pre_text = text[:start]
        post_text = text[end:]

        results = []

        # 1. Process Pre (with current base style)
        if pre_text:
            results.append((pre_text, base_style))

        # 2. Process Inner (Recursively, with added style)
        new_style = base_style.copy()
        if tag_type == "bold":
            inner_content = inner_raw[2:-2]  # strip **
            new_style["bold"] = True
        else:
            inner_content = inner_raw[1:-1]  # strip _
            new_style["italic"] = True

        results.extend(self._parse_inline_markdown(inner_content, new_style))

        # 3. Process Post (Recursively, with base style)
        results.extend(self._parse_inline_markdown(post_text, base_style))

        return results

    def track_insert(self, text: str, anchor_run: Optional[Run] = None, comment: Optional[str] = None):
        """
        Inserts text. If text contains newlines, splits into multiple paragraphs
        injected after the anchor_run's paragraph.
        Treats one or more newlines as a single paragraph break.

        Args:
            text: Text to insert.
            anchor_run: Context run for style inheritance/position.
            comment: Optional comment text to attach. NOTE: Only used if Block insertion
                     (e.g. Headers) occurs internally. For Inline insertion, caller
                     must handle comment attachment on the returned element.
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

            created_nodes = []  # List of (paragraph_element, ins_element)

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

                # Handle Inline formatting within the header text
                segments = self._parse_inline_markdown(c_text)

                for seg_text, seg_props in segments:
                    new_run = create_element("w:r")
                    if anchor_run and anchor_run._element.rPr is not None:
                        new_run.append(deepcopy(anchor_run._element.rPr))

                    self._apply_run_props(new_run, seg_props)

                    t = create_element("w:t")
                    self._set_text_content(t, seg_text)
                    new_run.append(t)
                    new_ins.append(new_run)

                new_p.append(new_ins)
                body.insert(p_index + 1 + i, new_p)
                created_nodes.append((new_p, new_ins))

            # Handle Comment Attachment for Block Insertions
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

                segments = self._parse_inline_markdown(clean_text)
                for seg_text, seg_props in segments:
                    new_run = create_element("w:r")
                    if anchor_run and anchor_run._element.rPr is not None:
                        new_run.append(deepcopy(anchor_run._element.rPr))

                    self._apply_run_props(new_run, seg_props)

                    t = create_element("w:t")
                    self._set_text_content(t, seg_text)
                    new_run.append(t)
                    new_ins.append(new_run)

                new_p.append(new_ins)
                parent_body.insert(p_index + 1 + i, new_p)

        return ins_elem

    def _apply_run_props(self, run_element, props: Dict[str, Any]):
        """
        Applies specific formatting properties to a run element's rPr.
        """
        if not props:
            return

        rPr = run_element.find(qn("w:rPr"))
        if rPr is None:
            rPr = create_element("w:rPr")
            run_element.insert(0, rPr)

        if props.get("bold"):
            # w:b val="true" or just <w:b/>
            b = create_element("w:b")
            rPr.append(b)

        if props.get("italic"):
            # w:i
            i = create_element("w:i")
            rPr.append(i)

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

        # Parse inline markdown (bold/italic)
        segments = self._parse_inline_markdown(text)

        for seg_text, seg_props in segments:
            run = create_element("w:r")

            # Inherit from anchor if available
            if anchor_run and anchor_run._element.rPr is not None:
                run.append(deepcopy(anchor_run._element.rPr))

            # Apply Markdown Overrides
            self._apply_run_props(run, seg_props)

            t = create_element("w:t")
            self._set_text_content(t, seg_text)
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

        # Insert Start
        try:
            idx_start = start_p.index(start_el)
            start_p.insert(idx_start, range_start)
        except ValueError:
            pass

        # Insert End
        try:
            idx_end = end_p.index(end_el)
            # Insert End AFTER end_el
            end_p.insert(idx_end + 1, range_end)
            end_p.insert(idx_end + 2, ref_run)
        except ValueError:
            pass

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

        start_idx, match_len = self.mapper.find_match_index(edit.target_text)

        start_idx, match_len = self.mapper.find_match_index(edit.target_text)

        # FALLBACK: If Raw View match failed, try matching against Clean View
        use_clean_map = False
        if start_idx == -1:
            # Lazy load clean mapper
            if not self.clean_mapper:
                self.clean_mapper = DocumentMapper(self.doc, clean_view=True)

            start_idx, match_len = self.clean_mapper.find_match_index(edit.target_text)
            if start_idx != -1:
                logger.info("Matched edit against Clean View.")
                use_clean_map = True
            else:
                logger.warning(f"Skipping edit: Target '{edit.target_text[:20]}...' not found (Raw or Clean).")
                return False

        # Select active mapper
        if use_clean_map and self.clean_mapper:
            active_mapper = self.clean_mapper
        else:
            active_mapper = self.mapper

        # --- HEURISTIC NESTED EDIT FIX ---
        # Before trimming, check if the match falls inside an existing Insertion.
        # If so, we must target the WHOLE insertion to avoid corrupting it or losing context.
        context_span = active_mapper.get_context_at_range(start_idx, start_idx + match_len)

        if context_span and context_span.ins_id:
            ins_id = context_span.ins_id
            # Find the full extent of this insertion
            # Note: For clean map, we might not have all spans if we skipped some logic?
            # Actually clean map includes w:ins.
            ins_spans = [s for s in active_mapper.spans if s.ins_id == ins_id]
            if ins_spans:
                ins_start = ins_spans[0].start
                # Reconstruct full text of the insertion
                full_ins_text = "".join(s.text for s in ins_spans)

                # Calculate the relative offset of our match within the insertion
                rel_start = start_idx - ins_start

                # Construct the new FULL text (replace the targeted part within the full text)
                # Logic: [Prefix] + [New Text] + [Suffix]
                # IMPORTANT: use match_len which might differ from edit.target_text length (fuzzy match)
                expanded_new_text = (
                    full_ins_text[:rel_start] + (edit.new_text or "") + full_ins_text[rel_start + match_len :]
                )

                # Create a proxy edit that replaces the WHOLE insertion
                proxy_edit = DocumentEdit(target_text=full_ins_text, new_text=expanded_new_text, comment=edit.comment)
                proxy_edit._match_start_index = ins_start
                # internal_op will be determined by _apply_single_edit_indexed (likely INSERTION/MODIFICATION)

                return self._apply_single_edit_indexed(proxy_edit)
        # ---------------------------------

        effective_new_text = edit.new_text or ""

        # NOTE: Fuzzy matching means user input might NOT
        # equal the actual document text found at [start_idx : start_idx + match_len].
        # We should use the ACTUAL document text for prefix/suffix trimming to be safe.
        actual_doc_text = self.mapper.full_text[start_idx : start_idx + match_len]

        # If the fuzzy match is drastically different in content (e.g. whitespace),
        # we trust the match index but use actual text for operations.

        if actual_doc_text == effective_new_text:
            return True

        if effective_new_text.startswith(actual_doc_text):
            # Pure append?
            # Note: startswith check might fail if whitespace differs slightly.
            # But if we assume the user intends to Append, they usually provide context.
            effective_op = EditOperationType.INSERTION
            final_target = ""
            final_new = effective_new_text[len(actual_doc_text) :]
            effective_start_idx = start_idx + match_len
        else:
            prefix_len, suffix_len = _trim_common_context(actual_doc_text, effective_new_text)

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

        # If we replaced the target text with actual text, we should update the edit
        # to ensure downstream logic (logging, etc) sees reality, though _apply uses indices.
        # However, we must pass the calculated sub-segments.

        # Construct proxy edit
        proxy_edit = DocumentEdit(target_text=final_target, new_text=final_new, comment=edit.comment)
        proxy_edit._match_start_index = effective_start_idx

        # Store which mapper to use for index resolution in _apply_single_edit_indexed
        # We need to pass the mapper instance or a flag?
        # _apply_single_edit_indexed uses self.mapper by default.
        # We should probably pass the runs directly or tell it to use clean mapper.
        # Hack: attach the active mapper to the edit object privately?
        proxy_edit._internal_op = effective_op
        proxy_edit._active_mapper_ref = active_mapper

        return self._apply_single_edit_indexed(proxy_edit)

    def _apply_single_edit_indexed(self, edit: DocumentEdit) -> bool:
        op = edit._internal_op

        # Use the specific mapper if determined during heuristic phase, else default
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

        # Check if the target range is currently inside a Tracked Insertion.
        # If so, prevent XML corruption (w:del inside w:ins) by Rejecting the insertion first.
        if length > 0:
            context_span = self.mapper.get_context_at_range(start_idx, start_idx + length)
            if context_span and context_span.ins_id:
                logger.info(f"Detected edit inside Insertion ID={context_span.ins_id}. Converting to Replace.")
                ins_id = context_span.ins_id

                # 1. Locate the Insertion in the DOM before we delete it
                ins_nodes = self.doc.element.xpath(f"//w:ins[@w:id='{ins_id}']")
                if not ins_nodes:
                    return False

                first_node = ins_nodes[0]
                parent = first_node.getparent()
                index = parent.index(first_node)

                # Capture style from inside if possible (approximate)
                style_source = None
                r = first_node.find(qn("w:r"))
                if r is not None:
                    style_source = Run(r, parent)

                # 2. Reject the Change (Removes the w:ins nodes)
                # This clears the path for our new insertion
                self._reject_change(ins_id)

                # 3. Apply the NEW text as a fresh insertion
                # We use the position of the *first* ins node we found.
                if edit.new_text:
                    ins_elem = self.track_insert(edit.new_text, anchor_run=style_source, comment=edit.comment)
                    if ins_elem is not None:
                        # Insert at the original position
                        parent.insert(index, ins_elem)

                    # For Inline Insertion (track_insert returned elem), we must attach comment here.
                    if edit.comment and ins_elem is not None:
                        self._attach_comment(parent, ins_elem, ins_elem, edit.comment)

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

        # Deletion / Modification
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

                # Style Check: Prevent creating a new paragraph if style matches
                text_to_insert = edit.new_text
                clean_text, style_name = self._parse_markdown_style(text_to_insert)
                if style_name:
                    anchor_para = target_runs[-1]._parent
                    # Normalize style names to avoid loose mismatch. Verify it has a style attr (Paragraph has it).
                    current_style = getattr(anchor_para, "style", None)
                    if current_style and getattr(current_style, "name", "") == style_name:
                        text_to_insert = clean_text  # Strip Markdown Header to force inline

                ins_elem = self.track_insert(
                    text_to_insert,
                    anchor_run=Run(target_runs[-1]._element, target_runs[-1]._parent),
                    comment=edit.comment,
                )
                if ins_elem is not None:
                    parent.insert(del_index + 1, ins_elem)

                # If Inline (ins_elem present) and Comment exists, attach to Del+Ins range
                if edit.comment and ins_elem is not None and first_del_element is not None:
                    # Check if spans across parents (e.g. paragraph boundary)
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

        for act in actions:
            raw_id = act.target_id
            target_id = raw_id

            # Determine type from prefix
            is_change = False
            is_comment = False

            if raw_id.startswith("Chg:"):
                target_id = raw_id[4:]
                is_change = True
            elif raw_id.startswith("Com:"):
                target_id = raw_id[4:]
                is_comment = True
            else:
                # Fallback for legacy/manual inputs
                is_change = True
                is_comment = True

            success = False
            if act.action == "ACCEPT":
                if is_change:
                    success = self._accept_change(target_id)
            elif act.action == "REJECT":
                if is_change:
                    success = self._reject_change(target_id)
            elif act.action == "REPLY":
                if is_comment:
                    success = self._reply_to_comment(target_id, act.text or "")

            if success:
                applied += 1
            else:
                skipped += 1

        return applied, skipped

    def _accept_change(self, target_id: str) -> bool:
        # 1. Try Ins -> Unwrap
        ins_nodes = self.doc.element.xpath(f"//w:ins[@w:id='{target_id}']")
        for ins in ins_nodes:
            parent = ins.getparent()
            index = parent.index(ins)
            for child in list(ins):
                parent.insert(index, child)
                index += 1
            parent.remove(ins)

        # 2. Try Del -> Remove
        del_nodes = self.doc.element.xpath(f"//w:del[@w:id='{target_id}']")
        for d in del_nodes:
            d.getparent().remove(d)

        return bool(ins_nodes or del_nodes)

    def _reject_change(self, target_id: str) -> bool:
        # 1. Try Ins -> Remove
        ins_nodes = self.doc.element.xpath(f"//w:ins[@w:id='{target_id}']")
        for ins in ins_nodes:
            ins.getparent().remove(ins)

        # 2. Try Del -> Unwrap (Restore text)
        del_nodes = self.doc.element.xpath(f"//w:del[@w:id='{target_id}']")
        for d in del_nodes:
            parent = d.getparent()
            index = parent.index(d)
            for child in list(d):
                # w:delText -> w:t
                for dt in child.findall(qn("w:delText")):
                    dt.tag = qn("w:t")
                parent.insert(index, child)
                index += 1
            parent.remove(d)

        return bool(ins_nodes or del_nodes)

    def _reply_to_comment(self, target_id: str, text: str) -> bool:
        """
        Creates a threaded reply to an existing comment using w15:p parent linking.
        Does NOT create new anchors in the document body.
        """
        if not self.comments_manager.comments_part:
            return False

        # Add the comment with parent_id linkage
        new_comment_id = self.comments_manager.add_comment(self.author, text, parent_id=target_id)

        # Anchor the new comment to the same range as the parent
        self._anchor_reply_comment(target_id, new_comment_id)

        return True

    def _anchor_reply_comment(self, parent_id: str, new_id: str):
        # 1. Find Start
        starts = self.doc.element.xpath(f"//w:commentRangeStart[@w:id='{parent_id}']")
        if not starts:
            logger.warning("Parent comment start not found during reply", parent_id=parent_id)
            return

        parent_start = starts[0]
        new_start = create_element("w:commentRangeStart")
        create_attribute(new_start, "w:id", new_id)
        parent_start.addnext(new_start)

        # 2. Find End
        ends = self.doc.element.xpath(f"//w:commentRangeEnd[@w:id='{parent_id}']")
        if not ends:
            return

        parent_end = ends[0]
        new_end = create_element("w:commentRangeEnd")
        create_attribute(new_end, "w:id", new_id)

        # Locate the Reference Run of the parent to insert AFTER it
        # This preserves the order [Ref Parent] [Ref Child] which Word prefers for threading
        parent_refs = self.doc.element.xpath(f"//w:commentReference[@w:id='{parent_id}']")
        insertion_point = parent_end

        if parent_refs:
            # Found the reference element, get its parent run
            ref_el = parent_refs[0]
            if ref_el.getparent().tag == qn("w:r"):
                insertion_point = ref_el.getparent()

        # Insert New End after the insertion point (usually Ref Parent)
        insertion_point.addnext(new_end)

        # 3. Create Reference Run
        ref_run = create_element("w:r")
        rPr = create_element("w:rPr")
        rStyle = create_element("w:rStyle")
        create_attribute(rStyle, "w:val", "CommentReference")
        rPr.append(rStyle)
        ref_run.append(rPr)

        ref = create_element("w:commentReference")
        create_attribute(ref, "w:id", new_id)
        ref_run.append(ref)

        # Insert New Ref after New End
        new_end.addnext(ref_run)

    def accept_all_revisions(self):
        """
        Accepts all tracked changes and removes comments.
        """
        # 1. Accept Insertions: Unwrap them
        for ins in self.doc.element.xpath("//w:ins"):
            parent = ins.getparent()
            index = parent.index(ins)
            for child in list(ins):
                parent.insert(index, child)
                index += 1
            parent.remove(ins)

        # 2. Accept Deletions: Remove them
        for d in self.doc.element.xpath("//w:del"):
            d.getparent().remove(d)

        # 3. Remove Comments (Optional? Usually desired for 'Clean' copy)
        # Removing comments implies removing commentRangeStart/End and References
        for tag in ["w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"]:
            for el in self.doc.element.xpath(f"//{(tag)}"):
                el.getparent().remove(el)
