# FILE: src/adeu/redline/mapper.py
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import structlog
from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from adeu.redline.comments import CommentsManager
from adeu.utils.docx import (
    DocxEvent,
    get_paragraph_prefix,
    get_run_style_markers,
    get_run_text,
    is_heading_paragraph,
    is_native_heading,
    iter_block_items,
    iter_document_parts,
    iter_paragraph_content,
)
from adeu.utils.safe_regex import user_finditer, user_search

logger = structlog.get_logger(__name__)


@dataclass
class TextSpan:
    start: int
    end: int
    text: str
    run: Optional[Run]
    paragraph: Optional[Paragraph]
    ins_id: Optional[str] = None
    del_id: Optional[str] = None
    hyperlink_id: Optional[str] = None
    comment_ids: Optional[List[str]] = None


def renumber_snapshot_ids(doc) -> tuple[dict[str, str], dict[str, str]]:
    """
    Rewrites w:id attributes on a snapshot Document to mirror the disk path's
    two-pool numbering scheme:
      - w:ins / w:del elements form a sequential "Chg" pool starting at 1
      - w:comment elements form a separate sequential "Com" pool starting at 1

    Updates all cross-references so the document remains internally consistent:
      - w:commentReference, w:commentRangeStart, w:commentRangeEnd in document.xml
        get their w:id values remapped to the new Com pool
      - w15:p (legacy comment threading parent attribute) gets remapped
      - commentsExtended.xml's w15:paraIdParent linking is preserved verbatim
        because it's keyed by paraId (a separate identifier) — no remap needed

    Why: Live Word allocates IDs from a single shared counter for both revisions
    and comments. Disk path uses two independent counters. An agent that reads
    via the disk path and writes via Live Word (or vice versa) can target the
    wrong element because Com:N from one path may not match Com:N from the
    other. Renumbering the Live Word snapshot to match disk's two-pool scheme
    eliminates this collision (Bug 5).

    The remapping is fully deterministic: IDs are assigned in document order
    of the elements, so two reads of the same unmodified snapshot produce
    identical renumbered projections.

    Args:
        doc: a python-docx Document built from a Live Word snapshot.

    Returns:
        (chg_id_remap, com_id_remap): two dicts mapping original w:id strings
        to new w:id strings. Useful for callers that need to translate IDs
        across the renumber, though most consumers can ignore them — the
        mapper reads the renumbered IDs directly from the mutated doc.
    """

    # --- Renumber w:ins / w:del (Chg pool) ---
    chg_remap: dict[str, str] = {}
    next_chg = 1
    body_root = doc.element

    # Find ins/del elements in document order. We walk in tree order to ensure
    # determinism — XPath findall returns in document order for python-docx.
    for tag in (qn("w:ins"), qn("w:del")):
        for elem in body_root.iter(tag):
            old_id = elem.get(qn("w:id"))
            if old_id is None:
                continue
            if old_id in chg_remap:
                # Same id might appear on multiple elements (rare but possible —
                # e.g. paired ins/del from a single revision). Keep them paired.
                elem.set(qn("w:id"), chg_remap[old_id])
                continue
            new_id = str(next_chg)
            chg_remap[old_id] = new_id
            elem.set(qn("w:id"), new_id)
            next_chg += 1

    # --- Renumber w:comment (Com pool) ---
    # Comments live in a separate part — find it via the package.
    com_remap: dict[str, str] = {}
    next_com = 1
    comments_part = None
    for part in doc.part.package.parts:
        if part.content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml":
            comments_part = part
            break

    if comments_part is not None:
        # The comments part may be a generic Part or an XmlPart depending on
        # how python-docx loaded it. Use the same lazy-element pattern that
        # CommentsManager uses elsewhere in the codebase.
        if hasattr(comments_part, "element"):
            comments_root = comments_part.element
        else:
            from docx.oxml import parse_xml

            if not hasattr(comments_part, "_adeu_element"):
                comments_part._adeu_element = parse_xml(comments_part.blob)
            comments_root = comments_part._adeu_element

        for c in comments_root.findall(qn("w:comment")):
            old_id = c.get(qn("w:id"))
            if old_id is None:
                continue
            if old_id in com_remap:
                c.set(qn("w:id"), com_remap[old_id])
                continue
            new_id = str(next_com)
            com_remap[old_id] = new_id
            c.set(qn("w:id"), new_id)
            next_com += 1

    # --- Update cross-references in document.xml to use new Com IDs ---
    # commentReference, commentRangeStart, commentRangeEnd all carry w:id
    # pointing into the comments part.
    for tag in (
        qn("w:commentReference"),
        qn("w:commentRangeStart"),
        qn("w:commentRangeEnd"),
    ):
        for elem in body_root.iter(tag):
            old_id = elem.get(qn("w:id"))
            if old_id is not None and old_id in com_remap:
                elem.set(qn("w:id"), com_remap[old_id])

    # Legacy threading: w:comment elements may carry w15:p pointing at the
    # parent comment id. Remap if present.
    if comments_part is not None:
        w15_p_attr = "{http://schemas.microsoft.com/office/word/2012/wordml}p"
        for c in comments_root.findall(qn("w:comment")):
            parent_id = c.get(w15_p_attr)
            if parent_id is not None and parent_id in com_remap:
                c.set(w15_p_attr, com_remap[parent_id])

    return chg_remap, com_remap


# Markdown style delimiters the projection emits as VIRTUAL spans around
# formatted runs (see get_run_style_markers). Literal asterisks/underscores
# typed in the document live inside real (run-backed) spans and are never
# confused with these.
_STYLE_MARKER_TEXTS = frozenset({"**", "__", "*", "_"})


class DocumentMapper:
    def __init__(self, doc: DocumentObject, clean_view: bool = False, original_view: bool = False):
        self.doc = doc
        self.clean_view = clean_view
        self.original_view = original_view
        self.comments_mgr = CommentsManager(doc)
        self.comments_map = self.comments_mgr.extract_comments_data()
        self.full_text = ""
        self.spans: List[TextSpan] = []
        self.appendix_start_index: int = -1
        self._plain_projection: Optional[Tuple[str, List[int]]] = None
        self._build_map()

    def _build_map(self):
        current_offset = 0
        self.spans = []
        self._text_chunks: List[str] = []
        self.full_text = ""
        self._plain_projection = None

        for part in iter_document_parts(self.doc):
            current_offset = self._map_blocks(part, current_offset)

            # Add part separator if needed, or rely on block separators
            if self.spans and self.spans[-1].text != "\n\n":
                self._add_virtual_text("\n\n", current_offset, None)
                current_offset += 2

        # Cleanup trailing newlines
        while self.spans and self.spans[-1].text == "\n\n":
            self.spans.pop()
            self._text_chunks.pop()

        self.full_text = "".join(self._text_chunks)
        # The appendix is not part of the mapping engine's projection —
        # an O(N) calculation redlining never needs.
        self.appendix_start_index = -1

    def _map_blocks(self, container, offset: int) -> int:
        current = offset
        c_type = type(container).__name__

        part = getattr(container, "part", container)
        from adeu.utils.docx import _get_style_cache

        style_cache, default_pstyle = _get_style_cache(part)

        if c_type == "NotesPart":
            header = "## Footnotes" if container.note_type == "fn" else "## Endnotes"
            sep = f"---\n{header}"
            self._add_virtual_text(sep, current, None)
            current += len(sep)
            self._add_virtual_text("\n\n", current, None)
            current += 2

        is_first_para = True

        previous_item: Any = None
        for item in iter_block_items(container):
            i_type = type(item).__name__

            if i_type == "FootnoteItem":
                current = self._map_blocks(item, current)
            elif isinstance(item, Paragraph):
                if not is_first_para:
                    # Attach the newline to the previous paragraph so merges work correctly
                    prev_para = previous_item if isinstance(previous_item, Paragraph) else None
                    self._add_virtual_text("\n\n", current, prev_para)
                    current += 2

                prefix = get_paragraph_prefix(item, style_cache, default_pstyle)
                if is_first_para and c_type == "FootnoteItem":
                    prefix = f"[^{container.note_type}-{container.id}]: " + prefix
                if prefix:
                    self._add_virtual_text(prefix, current, item)
                    current += len(prefix)

                current = self._map_paragraph_content(item, current, style_cache, default_pstyle)
                is_first_para = False
                previous_item = item
            elif isinstance(item, Table):
                if not is_first_para:
                    # Attach the newline to the previous paragraph so merges work correctly
                    prev_para = previous_item if isinstance(previous_item, Paragraph) else None
                    self._add_virtual_text("\n\n", current, prev_para)
                    current += 2

                current = self._map_table(item, current)
                is_first_para = False
                previous_item = item

        return current

    def _map_table(self, table: Table, offset: int) -> int:
        current = offset
        rows_processed = 0

        for row in table.rows:
            # Structural Row Tracking
            tr = row._element
            trPr = tr.find(qn("w:trPr"))
            ins = trPr.find(qn("w:ins")) if trPr is not None else None
            del_node = trPr.find(qn("w:del")) if trPr is not None else None

            if self.clean_view and del_node is not None:
                continue
            if self.original_view and ins is not None:
                continue

            if rows_processed > 0:
                # Newline separator BETWEEN rows (matches "\n".join in ingest)
                self._add_virtual_text("\n", current, None)
                current += 1

            if ins is not None and not self.clean_view and not self.original_view:
                self._add_virtual_text("{++ ", current, None)
                current += 4
            elif del_node is not None and not self.clean_view and not self.original_view:
                self._add_virtual_text("{-- ", current, None)
                current += 4

            seen_cells = set()
            cells_processed = 0

            for cell in row.cells:
                if cell in seen_cells:
                    continue
                seen_cells.add(cell)

                if cells_processed > 0:
                    self._add_virtual_text(" | ", current, None)
                    current += 3

                current = self._map_blocks(cell, current)
                cells_processed += 1

            if ins is not None and not self.clean_view and not self.original_view:
                suffix = f" |Chg:{ins.get(qn('w:id'))}++}}"
                self._add_virtual_text(suffix, current, None)
                current += len(suffix)
            elif del_node is not None and not self.clean_view and not self.original_view:
                suffix = f" |Chg:{del_node.get(qn('w:id'))}--}}"
                self._add_virtual_text(suffix, current, None)
                current += len(suffix)

            rows_processed += 1

        return current

    def _strip_markdown_formatting(self, text: str) -> str:
        """
        Strips markdown formatting markers from text for matching purposes.
        Handles: **bold**, __bold__, _italic_, *italic*, # headers
        Only strips when content looks like actual formatted text (2+ word chars).
        """
        result = text

        # Strip header markers at start of lines
        result = re.sub(r"^#+\s*", "", result, flags=re.MULTILINE)

        # Strip bold markers - only when wrapping word content (not single chars)
        result = re.sub(r"\*\*(\w[\w\s]*\w|\w{2,})\*\*", r"\1", result)
        result = re.sub(r"__(\w[\w\s]*\w|\w{2,})__", r"\1", result)

        # Strip italic markers - only when wrapping word content
        result = re.sub(r"(?<!\w)_(\w[\w\s]*\w|\w{2,})_(?!\w)", r"\1", result)
        result = re.sub(r"(?<!\w)\*(\w[\w\s]*\w|\w{2,})\*(?!\w)", r"\1", result)

        return result

    def _map_paragraph_content(
        self,
        paragraph: Paragraph,
        start_offset: int,
        style_cache: Optional[dict] = None,
        default_pstyle: Optional[str] = None,
    ) -> int:
        """
        Maps Runs to Spans, handling Flattened CriticMarkup generation.
        """
        current = start_offset

        span = TextSpan(start=current, end=current, text="", run=None, paragraph=paragraph)
        self.spans.append(span)

        active_ids: set[str] = set()
        active_ins: dict[str, DocxEvent] = {}
        active_del: dict[str, DocxEvent] = {}
        active_fmt: dict[str, DocxEvent] = {}

        deferred_meta_states: List[Tuple] = []
        current_wrappers = ("", "")
        current_style = ("", "")
        active_hyperlink_id = None
        pending_runs: List[Tuple[str, str, Optional[Run], Optional[str], Optional[str], List[str]]] = []

        def flush_pending_runs():
            nonlocal current, pending_runs
            if not pending_runs:
                return
            s_tok, e_tok = current_wrappers
            if s_tok:
                self._add_virtual_text(s_tok, current, paragraph)
                current += len(s_tok)
            for kind, txt, r_obj, i_id, d_id, c_ids in pending_runs:
                if kind == "virtual":
                    self._add_virtual_text(txt, current, paragraph, hyperlink_id=active_hyperlink_id)
                else:
                    span = TextSpan(
                        start=current,
                        end=current + len(txt),
                        text=txt,
                        run=r_obj,
                        paragraph=paragraph,
                        ins_id=i_id,
                        del_id=d_id,
                        hyperlink_id=active_hyperlink_id,
                        comment_ids=c_ids if c_ids else None,
                    )
                    self.spans.append(span)
                    self._text_chunks.append(txt)
                current += len(txt)
            if e_tok:
                self._add_virtual_text(e_tok, current, paragraph)
                current += len(e_tok)
            pending_runs = []

        items = list(iter_paragraph_content(paragraph))

        is_heading = is_heading_paragraph(paragraph, style_cache, default_pstyle)
        native_heading = is_native_heading(paragraph, style_cache, default_pstyle)
        leading_strip_active = is_heading

        for i, item in enumerate(items):
            if isinstance(item, Run):
                prefix, suffix = get_run_style_markers(item, native_heading)
                run_parts: List[Tuple[str, str, Optional[Run]]] = []

                text = get_run_text(item)

                if leading_strip_active:
                    if text == "" or text.isspace():
                        continue
                    leading_strip_active = False

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

                if self.clean_view and active_del:
                    pass
                if self.original_view and active_ins:
                    pass

                full_seg_text = "".join(x[1] for x in run_parts)

                curr_ins_id = list(active_ins.keys())[-1] if active_ins else None
                curr_del_id = list(active_del.keys())[-1] if active_del else None

                if full_seg_text and not (self.clean_view and curr_del_id) and not (self.original_view and curr_ins_id):
                    if self.clean_view or self.original_view:
                        new_wrappers = ("", "")
                    else:
                        start_token, end_token = self._get_wrappers(curr_ins_id, curr_del_id, active_ids, active_fmt)
                        new_wrappers = (start_token, end_token)
                    new_style = (prefix, suffix)

                    if pending_runs and new_wrappers == current_wrappers:
                        skip_leading_prefix = False
                        if (
                            new_style == current_style
                            and current_style != ("", "")
                            and pending_runs
                            and pending_runs[-1][0] == "virtual"
                            and pending_runs[-1][1] == current_style[1]
                        ):
                            pending_runs.pop()
                            skip_leading_prefix = True

                        curr_comment_ids = list(active_ids)
                        for kind, txt, r_obj in run_parts:
                            if skip_leading_prefix and kind == "virtual" and txt == new_style[0]:
                                skip_leading_prefix = False
                                continue
                            pending_runs.append((kind, txt, r_obj, curr_ins_id, curr_del_id, curr_comment_ids))

                        current_style = new_style
                    else:
                        flush_pending_runs()
                        current_wrappers = new_wrappers
                        current_style = new_style
                        curr_comment_ids = list(active_ids)
                        for kind, txt, r_obj in run_parts:
                            pending_runs.append((kind, txt, r_obj, curr_ins_id, curr_del_id, curr_comment_ids))

                if not self.clean_view and not self.original_view:
                    has_meta = active_ins or active_del or active_ids or active_fmt
                    if has_meta:
                        state_snapshot = (
                            active_ins.copy() if active_ins else {},
                            active_del.copy() if active_del else {},
                            active_ids.copy() if active_ids else set(),
                            active_fmt.copy() if active_fmt else {},
                        )
                        deferred_meta_states.append(state_snapshot)

                    should_defer = False
                    has_any_meta = bool(curr_ins_id) or bool(curr_del_id) or bool(active_fmt) or bool(active_ids)

                    if has_any_meta:
                        j = i + 1
                        next_has_meta = False
                        temp_ins_count = len(active_ins)
                        temp_del_count = len(active_del)
                        temp_fmt_count = len(active_fmt)
                        temp_comment_ids = set(active_ids)

                        while j < len(items):
                            next_item = items[j]
                            if isinstance(next_item, Run):
                                if not get_run_text(next_item):
                                    j += 1
                                    continue
                                if (
                                    temp_ins_count > 0
                                    or temp_del_count > 0
                                    or temp_fmt_count > 0
                                    or len(temp_comment_ids) > 0
                                ):
                                    next_has_meta = True
                                break
                            elif isinstance(next_item, DocxEvent):
                                if next_item.type == "ins_start":
                                    temp_ins_count += 1
                                elif next_item.type == "ins_end":
                                    temp_ins_count = max(0, temp_ins_count - 1)
                                elif next_item.type == "del_start":
                                    temp_del_count += 1
                                elif next_item.type == "del_end":
                                    temp_del_count = max(0, temp_del_count - 1)
                                elif next_item.type == "fmt_start":
                                    temp_fmt_count += 1
                                elif next_item.type == "fmt_end":
                                    temp_fmt_count = max(0, temp_fmt_count - 1)
                                elif next_item.type == "start":
                                    temp_comment_ids.add(next_item.id)
                                elif next_item.type == "end":
                                    temp_comment_ids.discard(next_item.id)
                            j += 1

                        if next_has_meta:
                            should_defer = True

                    if not should_defer and deferred_meta_states:
                        meta_block = self._build_merged_meta_block(deferred_meta_states)
                        if meta_block:
                            flush_pending_runs()
                            current_wrappers = ("", "")
                            current_style = ("", "")
                            full_meta = f"{{>>{meta_block}<<}}"
                            self._add_virtual_text(full_meta, current, paragraph)
                            current += len(full_meta)
                        deferred_meta_states = []

            elif isinstance(item, DocxEvent):
                leading_strip_active = False
                flush_pending_runs()
                current_wrappers = ("", "")
                current_style = ("", "")

                if item.type == "start":
                    active_ids.add(item.id)
                elif item.type == "end":
                    if item.id in active_ids:
                        active_ids.remove(item.id)
                elif item.type == "ins_start":
                    active_ins[item.id] = item
                elif item.type == "ins_end":
                    active_ins.pop(item.id, None)
                elif item.type == "del_start":
                    active_del[item.id] = item
                elif item.type == "del_end":
                    active_del.pop(item.id, None)
                elif item.type == "fmt_start":
                    active_fmt[item.id] = item
                elif item.type == "fmt_end":
                    active_fmt.pop(item.id, None)
                elif item.type in ("footnote", "endnote"):
                    flush_pending_runs()
                    current_wrappers = ("", "")
                    current_style = ("", "")
                    prefix_str = "fn" if item.type == "footnote" else "en"
                    txt = f"[^{prefix_str}-{item.id}]"
                    self._add_virtual_text(txt, current, paragraph)
                    current += len(txt)
                elif item.type == "hyperlink_start":
                    flush_pending_runs()
                    current_wrappers = ("", "")
                    current_style = ("", "")
                    self._add_virtual_text("[", current, paragraph, hyperlink_id=item.id)
                    current += 1
                    active_hyperlink_id = item.id
                elif item.type == "hyperlink_end":
                    flush_pending_runs()
                    current_wrappers = ("", "")
                    current_style = ("", "")
                    txt = f"]({item.date})"
                    self._add_virtual_text(txt, current, paragraph, hyperlink_id=item.id)
                    current += len(txt)
                    active_hyperlink_id = None
                elif item.type == "xref_start":
                    flush_pending_runs()
                    current_wrappers = ("", "")
                    current_style = ("", "")
                    self._add_virtual_text("[~", current, paragraph)
                    current += 2
                elif item.type == "xref_end":
                    flush_pending_runs()
                    current_wrappers = ("", "")
                    current_style = ("", "")
                    txt = f"~](#{item.id})"
                    self._add_virtual_text(txt, current, paragraph)
                    current += len(txt)
                elif item.type == "bookmark":
                    flush_pending_runs()
                    current_wrappers = ("", "")
                    current_style = ("", "")
                    txt = f"{{#{item.id}}}"
                    self._add_virtual_text(txt, current, paragraph)
                    current += len(txt)

        flush_pending_runs()

        if deferred_meta_states:
            meta_block = self._build_merged_meta_block(deferred_meta_states)
            if meta_block:
                full_meta = f"{{>>{meta_block}<<}}"
                self._add_virtual_text(full_meta, current, paragraph)
                current += len(full_meta)

        return current

    def _get_wrappers(self, ins_id, del_id, active_ids, active_fmt):
        if del_id:
            return "{--", "--}"
        elif ins_id:
            return "{++", "++}"
        elif active_ids or active_fmt:
            return "{==", "==}"
        return "", ""

    def _build_merged_meta_block(self, states_list) -> str:
        change_lines = []
        comment_lines = []
        seen_sigs = set()

        for ins_map, del_map, comments_set, fmt_map in states_list:
            for uid, meta in ins_map.items():
                sig = f"Chg:{uid}"
                if sig not in seen_sigs:
                    auth = meta.author or "Unknown"
                    change_lines.append(f"[{sig} insert] {auth}")
                    seen_sigs.add(sig)
            for uid, meta in del_map.items():
                sig = f"Chg:{uid}"
                if sig not in seen_sigs:
                    auth = meta.author or "Unknown"
                    change_lines.append(f"[{sig} delete] {auth}")
                    seen_sigs.add(sig)
            for uid, meta in fmt_map.items():
                sig = f"Chg:{uid}"
                if sig not in seen_sigs:
                    auth = meta.author or "Unknown"
                    change_lines.append(f"[{sig} format] {auth}")
                    seen_sigs.add(sig)

            sorted_ids = sorted(list(comments_set))
            for c_id in sorted_ids:
                if c_id not in self.comments_map:
                    continue
                sig = f"Com:{c_id}"
                if sig not in seen_sigs:
                    data = self.comments_map[c_id]
                    header = f"[{sig}] {data['author']}"
                    if data["date"]:
                        header += f" @ {data['date']}"
                    if data["resolved"]:
                        header += "(RESOLVED)"
                    comment_lines.append(f"{header}: {data['text']}")
                    seen_sigs.add(sig)

        return "\n".join(change_lines + comment_lines)

    def _add_virtual_text(
        self,
        text: str,
        offset: int,
        context_paragraph: Optional[Paragraph],
        hyperlink_id: Optional[str] = None,
    ):
        span = TextSpan(
            start=offset,
            end=offset + len(text),
            text=text,
            run=None,  # Virtual
            paragraph=context_paragraph,
            hyperlink_id=hyperlink_id,
        )
        self.spans.append(span)
        self._text_chunks.append(text)

    def _replace_smart_quotes(self, text: str) -> str:
        return text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

    def _make_fuzzy_regex(self, target_text: str) -> str:
        """
        Constructs a regex from target text permitting variable whitespace,
        variable underscores in placeholders, smart quotes, intervening
        markdown markers, and punctuation boundaries.
        """
        target_text = self._strip_markdown_formatting(target_text)
        target_text = self._replace_smart_quotes(target_text)

        parts = []
        token_pattern = re.compile(r"(\[_+\])|(\s+)|(['\"])|([.,;:])")

        last_idx = 0
        for match in token_pattern.finditer(target_text):
            literal = target_text[last_idx : match.start()]
            if literal:
                escaped = re.escape(literal)
                parts.append(escaped)

            g_placeholder, g_space, g_quote, g_punct = match.groups()

            if g_placeholder:
                parts.append(r"\[_+\]")
            elif g_space:
                parts.append(r"(?>\*\*|__|\*|_)?")
                parts.append(r"\s+")
                parts.append(r"(?>\*\*|__|\*|_)?")
            elif g_quote:
                if g_quote == "'":
                    parts.append(r"[\u2018\u2019']")
                else:
                    parts.append(r"[\"\u201c\u201d]")
            elif g_punct:
                parts.append(r"(?>\*\*|__|\*|_)?")
                parts.append(re.escape(g_punct))
                parts.append(r"(?>\*\*|__|\*|_)?")

            last_idx = match.end()

        remaining = target_text[last_idx:]
        if remaining:
            parts.append(re.escape(remaining))

        return "".join(parts)

    def _get_plain_projection(self) -> Tuple[str, List[int]]:
        """
        Returns (plain_text, offset_map) where plain_text is full_text with the
        VIRTUAL markdown style delimiters (bold/italic markers emitted around
        formatted runs) removed, and offset_map[i] is the full_text index of
        plain_text[i].

        Formatting run boundaries can fall mid-word (e.g. a paragraph projected
        as "**Al**pha"), where neither exact matching nor the whitespace-anchored
        fuzzy regex can find the plain target "Alpha". Matching against this
        projection and mapping the span back to full_text closes that gap.

        Built lazily and invalidated by _build_map(): most batches never need it.
        """
        if self._plain_projection is None:
            chunks: List[str] = []
            offsets: List[int] = []
            for s in self.spans:
                if s.run is None and s.paragraph is not None and s.text in _STYLE_MARKER_TEXTS:
                    continue
                chunks.append(s.text)
                offsets.extend(range(s.start, s.end))
            self._plain_projection = ("".join(chunks), offsets)
        return self._plain_projection

    def _find_plain_projection_matches(self, target_text: str, flags: int = 0) -> List[Tuple[int, int]]:
        """
        Matches a markdown-stripped target against the plain projection and maps
        each hit back to a (start, length) span in full_text. Interior style
        markers end up inside the returned span (so "Alpha" over "**Al**pha"
        resolves to the "Al**pha" range); markers just outside the matched
        characters are excluded.
        """
        plain_text, offsets = self._get_plain_projection()
        if len(plain_text) == len(self.full_text):
            return []  # No virtual style markers anywhere; nothing new to find.
        norm_target = self._replace_smart_quotes(self._strip_markdown_formatting(target_text))
        if not norm_target:
            return []
        norm_plain = self._replace_smart_quotes(plain_text)
        results: List[Tuple[int, int]] = []
        for m in re.finditer(re.escape(norm_target), norm_plain, flags=flags):
            p_start, p_end = m.span()
            raw_start = offsets[p_start]
            raw_end = offsets[p_end - 1] + 1
            results.append((raw_start, raw_end - raw_start))
        return results

    def _range_in_deletion(self, start: int, length: int) -> bool:
        """
        BUG-23-5: Returns True if the [start, start+length) range falls entirely
        inside tracked-deleted (w:del) real text. Such text is not 'live' and
        must not be treated as a match for new edits, nor counted toward the
        ambiguity check. A range qualifies only when there is at least one real
        (run-bearing) span overlapping it and every such span carries a del_id.
        """
        end = start + length
        real_spans = [s for s in self.spans if s.run is not None and s.end > start and s.start < end]
        if not real_spans:
            return False
        return all(s.del_id for s in real_spans)

    def _first_live_index(self, haystack: str, needle: str) -> int:
        """
        Like str.find, but skips occurrences that fall inside tracked deletions.
        Returns -1 if no live occurrence exists.
        """
        idx = haystack.find(needle)
        while idx != -1:
            if not self._range_in_deletion(idx, len(needle)):
                return idx
            idx = haystack.find(needle, idx + 1)
        return -1

    def find_match_index(
        self, target_text: str, is_regex: bool = False, case_sensitive: bool = True
    ) -> Tuple[int, int]:
        """
        Returns (start_index, match_length).
        Returns (-1, 0) if not found.
        """
        flags = 0 if case_sensitive else re.IGNORECASE
        if is_regex:
            # User/LLM-supplied pattern: run it under a wall-clock budget so a
            # catastrophic pattern cannot hang the process (QA 2026-07-17 F5).
            # RegexTimeoutError propagates to the caller for a clean per-edit
            # error; only invalid-pattern errors mean "no match" here.
            try:
                match = user_search(target_text, self.full_text, flags=flags)
                if match and not self._range_in_deletion(match.start(), match.end() - match.start()):
                    return match.start(), match.end() - match.start()
            except re.error:
                pass
            return -1, 0

        # 1. Exact Match (skipping any occurrence buried inside a w:del)
        start_idx = self._first_live_index(self.full_text, target_text)
        if start_idx != -1:
            return start_idx, len(target_text)
        # 2. Smart Quote Normalization
        norm_full = self._replace_smart_quotes(self.full_text)
        norm_target = self._replace_smart_quotes(target_text)
        start_idx = self._first_live_index(norm_full, norm_target)
        if start_idx != -1:
            return start_idx, len(target_text)
        stripped_target = self._strip_markdown_formatting(target_text)
        if stripped_target in self.full_text:
            start_idx = self.full_text.find(stripped_target)
            return start_idx, len(stripped_target)

        # 3.5 Plain-projection match: the target crosses a formatting run
        # boundary (possibly mid-word), so the projection carries style markers
        # the plain target doesn't have.
        for start, length in self._find_plain_projection_matches(target_text, flags=flags):
            if not self._range_in_deletion(start, length):
                return start, length

        # 4. Fuzzy Regex Match
        try:
            pattern = self._make_fuzzy_regex(target_text)
            match = re.search(pattern, self.full_text, flags=flags)
            if match:
                return match.start(), match.end() - match.start()
        except re.error:
            pass

        return -1, 0

    def find_all_match_indices(
        self, target_text: str, is_regex: bool = False, case_sensitive: bool = True
    ) -> List[Tuple[int, int]]:
        """
        Returns a list of all non-overlapping matches as (start_index, match_length).
        Returns an empty list if not found.
        """
        if not target_text:
            return []

        flags = 0 if case_sensitive else re.IGNORECASE

        if is_regex:
            # Budgeted like find_match_index above (QA 2026-07-17 F5).
            try:
                return [
                    (m.start(), m.end() - m.start()) for m in user_finditer(target_text, self.full_text, flags=flags)
                ]
            except re.error:
                return []

        # 1. Exact Match
        matches = [m.span() for m in re.finditer(re.escape(target_text), self.full_text, flags=flags)]
        if matches:
            return [(s, e - s) for s, e in matches]

        # 2. Smart Quote Normalization
        norm_full = self._replace_smart_quotes(self.full_text)
        norm_target = self._replace_smart_quotes(target_text)
        matches = [m.span() for m in re.finditer(re.escape(norm_target), norm_full, flags=flags)]
        if matches:
            return [(s, e - s) for s, e in matches]

        # 3. Strip markdown from target
        stripped_target = self._strip_markdown_formatting(target_text)
        matches = [m.span() for m in re.finditer(re.escape(stripped_target), self.full_text, flags=flags)]
        if matches:
            return [(s, e - s) for s, e in matches]

        # 3.5 Plain-projection match (target spans a bold/italic run boundary,
        # possibly mid-word). See _find_plain_projection_matches.
        plain_matches = self._find_plain_projection_matches(target_text, flags=flags)
        if plain_matches:
            return plain_matches

        # 4. Fuzzy Regex Match
        try:
            pattern = self._make_fuzzy_regex(target_text)
            matches = [m.span() for m in re.finditer(pattern, self.full_text, flags=flags)]
            if matches:
                return [(s, e - s) for s, e in matches]
        except re.error:
            pass

        return []

    def find_target_runs(self, target_text: str) -> List[Run]:
        start_idx, length = self.find_match_index(target_text)
        if start_idx == -1:
            return []
        return self._resolve_runs_at_range(start_idx, start_idx + length)

    def find_target_runs_by_index(self, start_index: int, length: int, rebuild_map: bool = True) -> List[Run]:
        end_index = start_index + length
        return self._resolve_runs_at_range(start_index, end_index, rebuild_map=rebuild_map)

    def get_virtual_spans_in_range(self, start_index: int, length: int) -> List[TextSpan]:
        """
        Returns any virtual spans (run is None) that fall completely within the
        provided range. Used primarily for detecting deleted paragraph boundaries.
        """
        end_index = start_index + length
        return [
            s
            for s in self.spans
            if s.run is None and s.text == "\n\n" and s.start >= start_index and s.end <= end_index
        ]

    def _resolve_runs_at_range(self, start_idx: int, end_idx: int, rebuild_map: bool = True) -> List[Run]:
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

        if dom_modified and rebuild_map:
            self._build_map()

        return working_runs

    def get_insertion_anchor(self, index: int, rebuild_map: bool = True) -> Tuple[Optional[Run], Optional[Paragraph]]:
        preceding = [s for s in self.spans if s.end == index]
        if preceding:
            for s in reversed(preceding):
                if s.run:
                    return s.run, s.paragraph
            for s in reversed(preceding):
                if s.paragraph:
                    # Every span ending exactly here is virtual (CriticMarkup
                    # wrappers, {>>...<<} meta blocks, prefixes). If real text
                    # precedes this index in the SAME paragraph, anchor after
                    # its last run: falling back to the bare paragraph would
                    # drop the insertion at paragraph start, ahead of the very
                    # redlines/comment ranges that fence off the true position.
                    real_before = [
                        prev
                        for prev in self.spans
                        if prev.end <= index and prev.run is not None and prev.paragraph is s.paragraph
                    ]
                    if real_before:
                        return real_before[-1].run, real_before[-1].paragraph
                    return None, s.paragraph

        containing = [s for s in self.spans if s.start < index < s.end]
        if containing:
            span = containing[0]
            if span.run is None:
                if span.paragraph is None:
                    # We are inside a virtual string (like " | " or "\n").
                    # Push the insertion point to the end of this virtual boundary.
                    return self.get_insertion_anchor(span.end, rebuild_map=False)
                return None, span.paragraph
            else:
                offset = index - span.start
                left, _ = self._split_run_at_index(span.run, offset)
                if rebuild_map:
                    self._build_map()
                return left, span.paragraph

        if index == 0 and self.spans:
            for s in self.spans:
                if s.run:
                    return s.run, s.paragraph
            for s in self.spans:
                if s.paragraph:
                    return None, s.paragraph
            return None, None

        preceding_gap = [s for s in self.spans if s.end < index]
        if preceding_gap:
            for s in reversed(preceding_gap):
                if s.run:
                    return s.run, s.paragraph
            for s in reversed(preceding_gap):
                if s.paragraph:
                    return None, s.paragraph
        return None, None

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
        real_spans = [s for s in self.spans if s.run and s.end > start_idx and s.start < end_idx]
        if real_spans:
            return real_spans[0]
        return None
