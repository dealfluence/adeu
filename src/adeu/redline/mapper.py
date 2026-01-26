import re
from copy import deepcopy
from dataclasses import dataclass
from typing import List, Optional, Tuple

import structlog
from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    DocxEvent,
    get_paragraph_prefix,
    get_run_style_markers,
    get_run_text,
    iter_document_parts,
    iter_paragraph_content,
)

logger = structlog.get_logger(__name__)


@dataclass
class TextSpan:
    start: int
    end: int
    text: str
    run: Optional[Run]
    paragraph: Paragraph
    ins_id: Optional[str] = None
    del_id: Optional[str] = None


class DocumentMapper:
    def __init__(self, doc: DocumentObject, clean_view: bool = False):
        self.doc = doc
        self.clean_view = clean_view
        self.comments_mgr = CommentsManager(doc)
        self.comments_map = self.comments_mgr.extract_comments_data()
        self.full_text = ""
        self.spans: List[TextSpan] = []
        self._build_map()

    def _build_map(self):
        current_offset = 0
        self.spans = []
        self.full_text = ""

        for part in iter_document_parts(self.doc):
            # 1. Loose Paragraphs
            for p in part.paragraphs:
                prefix = get_paragraph_prefix(p)
                if prefix:
                    self._add_virtual_text(prefix, current_offset, p)
                    current_offset += len(prefix)

                current_offset = self._map_paragraph_content(p, current_offset)

                self._add_virtual_text("\n\n", current_offset, p)
                current_offset += 2

            # 2. Tables
            for table in part.tables:
                for row in table.rows:
                    row_has_content = False
                    for cell in row.cells:
                        cell_paras = list(cell.paragraphs)
                        # Check visible content (approximate)
                        cell_non_empty = any(p.runs for p in cell_paras)

                        if cell_non_empty:
                            if row_has_content:
                                self._add_virtual_text(" | ", current_offset, cell_paras[0])
                                current_offset += 3

                            row_has_content = True

                            for j, p in enumerate(cell_paras):
                                if j > 0:
                                    self._add_virtual_text("\n", current_offset, p)
                                    current_offset += 1

                                prefix = get_paragraph_prefix(p)
                                if prefix:
                                    self._add_virtual_text(prefix, current_offset, p)
                                    current_offset += len(prefix)

                                current_offset = self._map_paragraph_content(p, current_offset)

                    if row_has_content:
                        self._add_virtual_text("\n\n", current_offset, row.cells[0].paragraphs[0])
                        current_offset += 2

        if self.spans and self.spans[-1].text == "\n\n":
            self.spans.pop()
            self.full_text = self.full_text[:-2]

    def _map_paragraph_content(self, paragraph: Paragraph, start_offset: int) -> int:
        """
        Maps Runs to Spans, handling Flattened CriticMarkup generation.
        Matches logic in ingest.py _build_paragraph_text.
        """
        current = start_offset

        active_ids: set[str] = set()
        active_ins_event: Optional[DocxEvent] = None
        active_del_event: Optional[DocxEvent] = None

        # Buffers for lookahead flushing
        deferred_meta_states: List[Tuple] = []

        # State for Run Coalescing (Must match ingest.py behavior)
        current_wrappers = ("", "")  # (start, end)
        pending_runs: List[Tuple[str, str, Optional[Run], Optional[str], Optional[str]]] = []
        # Store: (kind, text, run_obj, ins_id, del_id)

        items = list(iter_paragraph_content(paragraph))

        for i, item in enumerate(items):
            if isinstance(item, Run):
                # 1. Prepare Content
                prefix, suffix = get_run_style_markers(item)
                run_parts: List[Tuple[str, str, Optional[Run]]] = []

                text = get_run_text(item)

                # Handle Splitting Formatting across Newlines (Bugfix)
                if "\n" in text and (prefix or suffix):
                    parts = text.split("\n")
                    for idx, part in enumerate(parts):
                        if idx > 0:
                            run_parts.append(("real", "\n", item))
                        if part:
                            if prefix:
                                run_parts.append(("virtual", prefix, None))
                            run_parts.append(("real", part, item))
                            if suffix:
                                run_parts.append(("virtual", suffix, None))
                else:
                    if prefix:
                        run_parts.append(("virtual", prefix, None))
                    if text:
                        run_parts.append(("real", text, item))
                    if suffix:
                        run_parts.append(("virtual", suffix, None))

                # Clean View Logic: Skip deleted text
                if self.clean_view and active_del_event:
                    # Even though we skip mapping this text to the full_text buffer,
                    # we proceed to event handling loop to keep state consistent.
                    # BUT, we must NOT append to spans or full_text.
                    pass

                # Reconstruct the raw segment text used for coalescing checks
                # We use the parts we just built to be consistent
                full_seg_text = "".join(x[1] for x in run_parts)

                # Initialize IDs safely (used for lookahead logic even if text is empty)
                curr_ins_id = active_ins_event.id if active_ins_event else None
                curr_del_id = active_del_event.id if active_del_event else None

                if full_seg_text and not (self.clean_view and curr_del_id):
                    # Check wrapper tokens
                    if self.clean_view:
                        new_wrappers = ("", "")
                    else:
                        start_token, end_token = self._get_wrappers(curr_ins_id, curr_del_id, active_ids)
                        new_wrappers = (start_token, end_token)

                    # --- COALESCING LOGIC ---
                    if pending_runs and new_wrappers == current_wrappers:
                        # Same state -> Buffer the parts
                        for kind, txt, r_obj in run_parts:
                            pending_runs.append((kind, txt, r_obj, curr_ins_id, curr_del_id))
                    else:
                        # Flush pending
                        if pending_runs:
                            s_tok, e_tok = current_wrappers
                            # Output Start Token
                            if s_tok:
                                self._add_virtual_text(s_tok, current, paragraph)
                                current += len(s_tok)
                            # Output Buffered Parts
                            for kind, txt, r_obj, i_id, d_id in pending_runs:
                                if kind == "virtual":
                                    self._add_virtual_text(txt, current, paragraph)
                                else:
                                    span = TextSpan(
                                        start=current,
                                        end=current + len(txt),
                                        text=txt,
                                        run=r_obj,
                                        paragraph=paragraph,
                                        ins_id=i_id,
                                        del_id=d_id,
                                    )
                                    self.spans.append(span)
                                    self.full_text += txt
                                current += len(txt)
                            # Output End Token
                            if e_tok:
                                self._add_virtual_text(e_tok, current, paragraph)
                                current += len(e_tok)

                        # Start new buffer
                        current_wrappers = new_wrappers
                        pending_runs = []
                        for kind, txt, r_obj in run_parts:
                            pending_runs.append((kind, txt, r_obj, curr_ins_id, curr_del_id))
                    # ------------------------

                # Metadata Handling (Deferral Logic)
                if not self.clean_view:
                    # Snapshot state
                    state_snapshot = (
                        {active_ins_event.id: active_ins_event} if active_ins_event else {},
                        {active_del_event.id: active_del_event} if active_del_event else {},
                        active_ids.copy(),
                    )
                    deferred_meta_states.append(state_snapshot)

                    should_defer = False
                    is_redline = bool(curr_ins_id) or bool(curr_del_id)

                    if is_redline:
                        # Lookahead
                        j = i + 1
                        next_is_redline = False
                        temp_ins = bool(curr_ins_id)
                        temp_del = bool(curr_del_id)

                        while j < len(items):
                            next_item = items[j]
                            if isinstance(next_item, Run):
                                if temp_ins or temp_del:
                                    next_is_redline = True
                                break
                            elif isinstance(next_item, DocxEvent):
                                if next_item.type == "ins_start":
                                    temp_ins = True
                                elif next_item.type == "ins_end":
                                    temp_ins = False
                                elif next_item.type == "del_start":
                                    temp_del = True
                                elif next_item.type == "del_end":
                                    temp_del = False
                            j += 1

                        if next_is_redline:
                            should_defer = True

                    if not should_defer:
                        # Flush Pending Text Buffer before Metadata
                        if pending_runs:
                            s_tok, e_tok = current_wrappers
                            # Output Start Token
                            if s_tok:
                                self._add_virtual_text(s_tok, current, paragraph)
                                current += len(s_tok)
                            # Output Buffered Parts
                            for kind, txt, r_obj, i_id, d_id in pending_runs:
                                if kind == "virtual":
                                    self._add_virtual_text(txt, current, paragraph)
                                else:
                                    span = TextSpan(
                                        start=current,
                                        end=current + len(txt),
                                        text=txt,
                                        run=r_obj,
                                        paragraph=paragraph,
                                        ins_id=i_id,
                                        del_id=d_id,
                                    )
                                    self.spans.append(span)
                                    self.full_text += txt
                                current += len(txt)
                            # Output End Token
                            if e_tok:
                                self._add_virtual_text(e_tok, current, paragraph)
                                current += len(e_tok)
                            pending_runs = []
                            current_wrappers = ("", "")

                        # Flush Metadata
                        meta_block = self._build_merged_meta_block(deferred_meta_states)
                        if meta_block:
                            full_meta = f"{{>>{meta_block}<<}}"
                            self._add_virtual_text(full_meta, current, paragraph)
                            current += len(full_meta)
                        deferred_meta_states = []

            elif isinstance(item, DocxEvent):
                # Event -> Must flush pending text
                if pending_runs:
                    s_tok, e_tok = current_wrappers
                    if s_tok:
                        self._add_virtual_text(s_tok, current, paragraph)
                        current += len(s_tok)
                    for kind, txt, r_obj, i_id, d_id in pending_runs:
                        if kind == "virtual":
                            self._add_virtual_text(txt, current, paragraph)
                        else:
                            span = TextSpan(
                                start=current,
                                end=current + len(txt),
                                text=txt,
                                run=r_obj,
                                paragraph=paragraph,
                                ins_id=i_id,
                                del_id=d_id,
                            )
                            self.spans.append(span)
                            self.full_text += txt
                        current += len(txt)
                    if e_tok:
                        self._add_virtual_text(e_tok, current, paragraph)
                        current += len(e_tok)
                    pending_runs = []
                    current_wrappers = ("", "")

                # Update State
                if item.type == "start":
                    active_ids.add(item.id)
                elif item.type == "end":
                    if item.id in active_ids:
                        active_ids.remove(item.id)
                elif item.type == "ins_start":
                    active_ins_event = item
                elif item.type == "ins_end":
                    active_ins_event = None
                elif item.type == "del_start":
                    active_del_event = item
                elif item.type == "del_end":
                    active_del_event = None

        # Final Flush
        if pending_runs:
            s_tok, e_tok = current_wrappers
            if s_tok:
                self._add_virtual_text(s_tok, current, paragraph)
                current += len(s_tok)
            for kind, txt, r_obj, i_id, d_id in pending_runs:
                if kind == "virtual":
                    self._add_virtual_text(txt, current, paragraph)
                else:
                    span = TextSpan(
                        start=current,
                        end=current + len(txt),
                        text=txt,
                        run=r_obj,
                        paragraph=paragraph,
                        ins_id=i_id,
                        del_id=d_id,
                    )
                    self.spans.append(span)
                    self.full_text += txt
                current += len(txt)
            if e_tok:
                self._add_virtual_text(e_tok, current, paragraph)
                current += len(e_tok)

        if deferred_meta_states:
            meta_block = self._build_merged_meta_block(deferred_meta_states)
            if meta_block:
                full_meta = f"{{>>{meta_block}<<}}"
                self._add_virtual_text(full_meta, current, paragraph)
                current += len(full_meta)

        return current

    def _get_wrappers(self, ins_id, del_id, active_ids):
        if del_id:
            return "{--", "--}"
        elif ins_id:
            return "{++", "++}"
        elif active_ids:
            return "{==", "==}"
        return "", ""

    def _build_merged_meta_block(self, states_list) -> str:
        """
        Combines metadata from multiple states, removing duplicates.
        Canonical Order: Changes first, then Comments.
        """
        change_lines = []
        comment_lines = []
        seen_sigs = set()

        for ins_map, del_map, comments_set in states_list:
            # 1. Changes
            for map_obj in (ins_map, del_map):
                for uid, meta in map_obj.items():
                    sig = f"Chg:{uid}"
                    if sig not in seen_sigs:
                        auth = meta.author or "Unknown"
                        change_lines.append(f"[{sig}] {auth}")
                        seen_sigs.add(sig)

            # 2. Comments
            sorted_ids = sorted(list(comments_set))
            for c_id in sorted_ids:
                if c_id not in self.comments_map:
                    continue
                sig = f"Com:{c_id}"
                if sig not in seen_sigs:
                    data = self.comments_map[c_id]
                    header = f"[{sig}] {data['author']}"
                    if data["date"]:
                        short_date = data["date"].split("T")[0]
                        header += f" @ {short_date}"
                    if data["resolved"]:
                        header += "(RESOLVED)"
                    comment_lines.append(f"{header}: {data['text']}")
                    seen_sigs.add(sig)

        return "\n".join(change_lines + comment_lines)

    def _add_virtual_text(self, text: str, offset: int, context_paragraph: Paragraph):
        span = TextSpan(
            start=offset,
            end=offset + len(text),
            text=text,
            run=None,  # Virtual
            paragraph=context_paragraph,
        )
        self.spans.append(span)
        self.full_text += text

    def _replace_smart_quotes(self, text: str) -> str:
        return text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

    def _make_fuzzy_regex(self, target_text: str) -> str:
        """
        Constructs a regex pattern from target text that permits:
        - Variable whitespace (\\s+)
        - Variable underscores (_+)
        - Smart quote variation
        """
        # Normalize quotes in target for consistency
        target_text = self._replace_smart_quotes(target_text)

        parts = []
        # Tokenize: Underscores, Whitespace, Quotes
        token_pattern = re.compile(r"(_+)|(\s+)|(['\"])")

        last_idx = 0
        for match in token_pattern.finditer(target_text):
            # Add literal text
            literal = target_text[last_idx : match.start()]
            if literal:
                parts.append(re.escape(literal))

            g_underscore, g_space, g_quote = match.groups()

            if g_underscore:
                parts.append(r"_+")
            elif g_space:
                parts.append(r"\s+")
            elif g_quote:
                if g_quote == "'":
                    parts.append(r"['‘’]")
                else:
                    parts.append(r"[\"“”]")

            last_idx = match.end()

        remaining = target_text[last_idx:]
        if remaining:
            parts.append(re.escape(remaining))

        return "".join(parts)

    def find_match_index(self, target_text: str) -> Tuple[int, int]:
        """
        Returns (start_index, match_length).
        Returns (-1, 0) if not found.
        """
        # 1. Exact Match
        start_idx = self.full_text.find(target_text)
        if start_idx != -1:
            return start_idx, len(target_text)

        # 2. Smart Quote Normalization
        norm_full = self._replace_smart_quotes(self.full_text)
        norm_target = self._replace_smart_quotes(target_text)
        start_idx = norm_full.find(norm_target)
        if start_idx != -1:
            # Since smart quote replacement is 1:1, length matches target_text
            return start_idx, len(target_text)

        # 3. Fuzzy Regex Match
        try:
            pattern = self._make_fuzzy_regex(target_text)
            match = re.search(pattern, self.full_text)
            if match:
                return match.start(), match.end() - match.start()
        except re.error:
            pass

        return -1, 0

    def find_target_runs(self, target_text: str) -> List[Run]:
        start_idx, length = self.find_match_index(target_text)
        if start_idx == -1:
            return []
        return self._resolve_runs_at_range(start_idx, start_idx + length)

    def find_target_runs_by_index(self, start_index: int, length: int) -> List[Run]:
        end_index = start_index + length
        return self._resolve_runs_at_range(start_index, end_index)

    def _resolve_runs_at_range(self, start_idx: int, end_idx: int) -> List[Run]:
        affected_spans = [s for s in self.spans if s.end > start_idx and s.start < end_idx]
        if not affected_spans:
            return []

        working_runs = [s.run for s in affected_spans if s.run is not None]
        if not working_runs:
            return []

        dom_modified = False

        # 1. Start Split
        first_real_span = next((s for s in affected_spans if s.run is not None), None)
        start_split_adjustment = 0

        if first_real_span:
            local_start = start_idx - first_real_span.start
            if local_start > 0:
                idx_in_working = 0
                _, right_run = self._split_run_at_index(working_runs[idx_in_working], local_start)
                working_runs[idx_in_working] = right_run
                dom_modified = True
                start_split_adjustment = local_start

        # 2. End Split
        last_real_span = next((s for s in reversed(affected_spans) if s.run is not None), None)

        if last_real_span:
            is_same_run = first_real_span is last_real_span
            run_to_split = working_runs[-1]
            overlap_end = min(last_real_span.end, end_idx)
            local_end = overlap_end - last_real_span.start

            if is_same_run and start_split_adjustment > 0:
                local_end -= start_split_adjustment

            if 0 < local_end < len(run_to_split.text):
                left_run, _ = self._split_run_at_index(run_to_split, local_end)
                working_runs[-1] = left_run
                dom_modified = True

        if dom_modified:
            self._build_map()

        return working_runs

    def get_insertion_anchor(self, index: int) -> Optional[Run]:
        preceding = [s for s in self.spans if s.end == index]
        if preceding:
            if preceding[-1].run:
                return preceding[-1].run
        containing = [s for s in self.spans if s.start < index < s.end]
        if containing:
            span = containing[0]
            if span.run is None:
                pass
            else:
                offset = index - span.start
                left, _ = self._split_run_at_index(span.run, offset)
                return left

        if index == 0 and self.spans:
            for s in self.spans:
                if s.run:
                    return s.run
            return None

        preceding_gap = [s for s in self.spans if s.end < index]
        if preceding_gap:
            for s in reversed(preceding_gap):
                if s.run:
                    return s.run
        return None

    def _split_run_at_index(self, run: Run, split_index: int) -> Tuple[Run, Run]:
        text = run.text
        left_text = text[:split_index]
        right_text = text[split_index:]

        run.text = left_text
        new_r_element = deepcopy(run._element)
        t_list = new_r_element.findall(qn("w:t"))
        for t in t_list:
            new_r_element.remove(t)

        new_t = OxmlElement("w:t")
        new_t.text = right_text
        if right_text.strip() != right_text:
            new_t.set(qn("xml:space"), "preserve")
        new_r_element.append(new_t)
        run._element.addnext(new_r_element)
        new_run = Run(new_r_element, run._parent)
        return run, new_run

    def get_context_at_range(self, start_idx: int, end_idx: int) -> Optional[TextSpan]:
        """
        Returns the first real TextSpan in the range to check context.
        Useful for detecting if we are editing inside an Insertion.
        """
        real_spans = [s for s in self.spans if s.run and s.end > start_idx and s.start < end_idx]
        if real_spans:
            return real_spans[0]
        return None
