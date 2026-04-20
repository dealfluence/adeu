import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

if sys.platform == "win32":
    import pythoncom
    import win32com.client

    from adeu.ingest import _build_merged_meta_block, _get_wrappers
    from adeu.markup import _find_match_in_text
    from adeu.models import AcceptChange, DocumentChange, ModifyText, RejectChange, ReplyComment
    from adeu.utils.docx import DocxEvent, apply_formatting_to_segments

    logger = logging.getLogger(__name__)

    def _strip_critic_markup(text: str) -> str:
        """Removes CriticMarkup tags so raw text can be found via Word's native Find."""
        if not text:
            return ""
        text = re.sub(r"\{--.*?--\}", "", text)
        text = re.sub(r"\{>>.*?<<\}", "", text)
        text = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", text)
        text = re.sub(r"\{==(.*?)==\}", r"\1", text)
        return text

    def _parse_markdown_for_com(text: str):
        """Parses bold and italic markdown, returning plain text and index ranges."""
        bold_ranges = []
        italic_ranges = []

        while True:
            m = re.search(r"\*\*(.*?)\*\*", text)
            if not m:
                break
            start = m.start()
            inner = m.group(1)
            text = text[:start] + inner + text[m.end() :]
            bold_ranges.append((start, start + len(inner)))

        while True:
            m = re.search(r"_(.*?)_", text)
            if not m:
                break
            start = m.start()
            inner = m.group(1)
            text = text[:start] + inner + text[m.end() :]
            italic_ranges.append((start, start + len(inner)))

        return text, bold_ranges, italic_ranges

    def _apply_com_replacement(doc, app, target_rng, new_text, comment_text):
        rescued_comments = []
        try:
            for i in range(1, target_rng.Comments.Count + 1):
                c = target_rng.Comments(i)
                rescued_comments.append({"author": c.Author, "text": c.Range.Text})
        except Exception as e:
            logger.warning(f"Failed to rescue comments: {e}")

        plain_text, b_ranges, i_ranges = _parse_markdown_for_com(new_text.replace("\n", "\r"))
        target_rng.Text = plain_text

        was_tracking = doc.TrackRevisions
        doc.TrackRevisions = False  # Suppress formatting clutter in the review pane
        try:
            base_start = target_rng.Start
            for b_start, b_end in b_ranges:
                fmt_rng = doc.Range(base_start + b_start, base_start + b_end)
                fmt_rng.Font.Bold = True
            for i_start, i_end in i_ranges:
                fmt_rng = doc.Range(base_start + i_start, base_start + i_end)
                fmt_rng.Font.Italic = True
        except Exception as e:
            logger.warning(f"Failed to apply formatting: {e}")
        finally:
            doc.TrackRevisions = was_tracking

        current_user = app.UserName
        for c_data in rescued_comments:
            try:
                app.UserName = c_data["author"]
                doc.Comments.Add(target_rng, c_data["text"])
            except Exception:
                pass
        app.UserName = current_user

        if comment_text:
            doc.Comments.Add(target_rng, comment_text)

    async def read_active_word_document(
        ctx: Context,
        clean_view: bool = False,
    ) -> ToolResult:
        await ctx.info("Connecting to active Word application...")
        pythoncom.CoInitialize()
        try:
            try:
                app = win32com.client.GetActiveObject("Word.Application")
                doc = app.ActiveDocument
            except Exception as e:
                raise ToolError(f"Could not connect to active Word document. {e}") from e

            raw_text = doc.Content.Text
            if raw_text is None:
                return ToolResult(content="", structured_content={"markdown": "", "title": "Live Word Document"})

            if clean_view:
                clean_text = raw_text.replace("\r", "\n")
                return ToolResult(
                    content=clean_text, structured_content={"markdown": clean_text, "title": "Live Word Document"}
                )

            annotations = []

            # Helper to get exact bounds, bypassing formatting wrappers if possible
            def _get_bounds(obj):
                return getattr(obj.Range, "Start", 0), getattr(obj.Range, "End", 0)

            # 1. Extract Revisions
            for i in range(1, doc.Revisions.Count + 1):
                try:
                    rev = doc.Revisions(i)
                    if rev.Type in (1, 2):  # 1: Insert, 2: Delete
                        text = rev.Range.Text or ""
                        date_obj = getattr(rev, "Date", None)
                        try:
                            date = date_obj.strftime("%Y-%m-%dT%H:%M:%SZ") if date_obj else ""
                        except Exception:
                            date = str(date_obj) if date_obj else ""
                        annotations.append(
                            {
                                "start": rev.Range.Start,
                                "end": rev.Range.End,
                                "text": text,
                                "type": "insert" if rev.Type == 1 else "delete",
                                "id": str(i),
                                "author": rev.Author,
                                "date": date,
                            }
                        )
                except Exception as e:
                    logger.warning(f"Failed to read revision {i}: {e}")

            # 2. Extract Comments
            comments_data = {}
            for i in range(1, doc.Comments.Count + 1):
                try:
                    com = doc.Comments(i)
                    text = com.Scope.Text or ""
                    author = com.Author

                    cid = str(i - 1)

                    date_obj = getattr(com, "Date", None)
                    try:
                        date = date_obj.strftime("%Y-%m-%dT%H:%M:%SZ") if date_obj else ""
                    except Exception:
                        date = str(date_obj) if date_obj else ""
                    content = com.Range.Text.strip().replace("\r", " ")

                    annotations.append(
                        {
                            "start": com.Scope.Start,
                            "end": com.Scope.End,
                            "text": text,
                            "type": "comment",
                            "id": cid,
                            "author": author,
                            "date": date,
                        }
                    )
                    comments_data[cid] = {
                        "author": author,
                        "text": content,
                        "date": date,
                        "resolved": False,
                        "parent_id": None,
                    }
                except Exception as e:
                    logger.warning(f"Failed to read comment {i}: {e}")

            # 2b. Extract Formats (Fast COM Path using Collapse)
            bold_ranges: List[Tuple[int, int]] = []
            italic_ranges: List[Tuple[int, int]] = []
            try:
                for fmt_name, fmt_list in [("bold", bold_ranges), ("italic", italic_ranges)]:
                    rng = doc.Content
                    rng.Find.ClearFormatting()
                    if fmt_name == "bold":
                        rng.Find.Font.Bold = True
                    else:
                        rng.Find.Font.Italic = True
                    rng.Find.Forward = True
                    rng.Find.Wrap = 0  # wdFindStop
                    rng.Find.Format = True
                    rng.Find.Text = ""
                    while rng.Find.Execute():
                        start = rng.Start
                        end = rng.End

                        is_explicit = True
                        try:
                            style_font = rng.Style.Font
                            if fmt_name == "bold" and getattr(style_font, "Bold", 0) == -1:
                                is_explicit = False
                            elif fmt_name == "italic" and getattr(style_font, "Italic", 0) == -1:
                                is_explicit = False
                        except Exception:
                            pass
                        try:
                            # Strip trailing structural characters from formatting boundaries
                            text_val = rng.Text
                            if text_val:
                                trim_count = 0
                                while trim_count < len(text_val) and text_val[-(trim_count + 1)] in (
                                    "\r",
                                    "\x07",
                                    "\x0b",
                                    "\x0c",
                                ):
                                    trim_count += 1
                                if trim_count > 0:
                                    end -= trim_count
                        except Exception:
                            pass

                        if start < end and is_explicit:
                            fmt_list.append((start, end))
                        rng.Collapse(0)  # wdCollapseEnd
            except Exception as e:
                logger.warning(f"Failed to read formatting: {e}")

            # 2c. Extract Headings (Fast COM Path using Style Enumeration)
            heading_events = []
            try:
                # win32com constants: wdStyleHeading1 = -2, wdStyleHeading9 = -10
                for lvl in range(1, 10):
                    rng = doc.Content
                    rng.Find.ClearFormatting()
                    rng.Find.Style = -1 - lvl
                    rng.Find.Forward = True
                    rng.Find.Wrap = 0
                    rng.Find.Format = True
                    rng.Find.Text = ""
                    while rng.Find.Execute():
                        heading_events.append((rng.Start, lvl))
                        rng.Collapse(0)
            except Exception as e:
                logger.warning(f"Failed to read headings: {e}")

            # 3. Build sequential Event list at exact COM indices
            events_by_idx: Dict[int, List[DocxEvent]] = {}
            for ann in annotations:
                start = ann["start"]
                end = ann["end"]
                uid = ann["id"]
                auth = ann.get("author", "Unknown")
                date = ann.get("date", "")

                if ann["type"] == "insert":
                    events_by_idx.setdefault(start, []).append(DocxEvent("ins_start", uid, auth, date))
                    events_by_idx.setdefault(end, []).append(DocxEvent("ins_end", uid))
                elif ann["type"] == "delete":
                    events_by_idx.setdefault(start, []).append(DocxEvent("del_start", uid, auth, date))
                    events_by_idx.setdefault(end, []).append(DocxEvent("del_end", uid))
                elif ann["type"] == "comment":
                    events_by_idx.setdefault(start, []).append(DocxEvent("start", uid))
                    events_by_idx.setdefault(end, []).append(DocxEvent("end", uid))

            for start, end in bold_ranges:
                events_by_idx.setdefault(start, []).append(DocxEvent("fmt_start", "**"))
                events_by_idx.setdefault(end, []).append(DocxEvent("fmt_end", "**"))

            for start, end in italic_ranges:
                events_by_idx.setdefault(start, []).append(DocxEvent("fmt_start", "_"))
                events_by_idx.setdefault(end, []).append(DocxEvent("fmt_end", "_"))

            for start, lvl in heading_events:
                events_by_idx.setdefault(start, []).append(DocxEvent("heading", "#" * lvl + " "))

            items = []
            last_idx = 0
            doc_end = doc.Content.End
            indices = [0, doc_end] + list(events_by_idx.keys())
            indices = sorted(list(set([i for i in indices if 0 <= i <= doc_end])))

            for idx in indices:
                if idx > last_idx:
                    try:
                        text_seg = doc.Range(last_idx, idx).Text
                        if text_seg:
                            items.append(text_seg)
                    except Exception:
                        pass
                    last_idx = idx

                if idx in events_by_idx:
                    evts = events_by_idx[idx]

                    def evt_sort_key(e):
                        if isinstance(e, str):
                            return 0
                        t = getattr(e, "type", "")
                        if t == "heading":
                            return -3
                        if t == "fmt_start":
                            return -2
                        if "start" in t or t == "start":
                            return -1
                        if t == "fmt_end":
                            return 3
                        if "end" in t or t == "end":
                            return 2
                        return 0

                    evts.sort(key=evt_sort_key)
                    items.extend(evts)

            # 4. Mirror the ingest.py State Machine
            active_ins: Dict[str, DocxEvent] = {}
            active_del: Dict[str, DocxEvent] = {}
            active_comments: Set[str] = set()
            active_bold = 0
            active_italic = 0
            deferred_meta_states = []
            pending_text = ""
            current_wrappers = ("", "")
            parts = []

            for i, item in enumerate(items):
                if isinstance(item, str):
                    seg = item

                    if clean_view and active_del:
                        continue

                    if seg:
                        prefix = ""
                        suffix = ""
                        if active_bold > 0:
                            prefix += "**"
                            suffix = "**" + suffix
                        if active_italic > 0:
                            prefix += "_"
                            suffix = "_" + suffix

                        seg = apply_formatting_to_segments(seg, prefix, suffix)

                        if clean_view:
                            new_wrappers = ("", "")
                        else:
                            new_wrappers = _get_wrappers(active_ins, active_del, active_comments)

                        if pending_text and new_wrappers == current_wrappers:
                            pending_text += seg
                        else:
                            if pending_text:
                                s_tok, e_tok = current_wrappers
                                parts.append(f"{s_tok}{pending_text}{e_tok}")
                            pending_text = seg
                            current_wrappers = new_wrappers

                        if not clean_view:
                            current_state = (active_ins.copy(), active_del.copy(), active_comments.copy())
                            deferred_meta_states.append(current_state)

                            should_defer = False
                            is_redline = bool(active_ins) or bool(active_del)
                            if is_redline:
                                j = i + 1
                                next_is_redline = False
                                temp_ins = len(active_ins)
                                temp_del = len(active_del)
                                while j < len(items):
                                    next_item = items[j]
                                    if isinstance(next_item, str):
                                        if temp_ins > 0 or temp_del > 0:
                                            next_is_redline = True
                                        break
                                    elif isinstance(next_item, DocxEvent):
                                        if next_item.type == "ins_start":
                                            temp_ins += 1
                                        elif next_item.type == "ins_end":
                                            temp_ins = max(0, temp_ins - 1)
                                        elif next_item.type == "del_start":
                                            temp_del += 1
                                        elif next_item.type == "del_end":
                                            temp_del = max(0, temp_del - 1)
                                    j += 1

                                if next_is_redline:
                                    should_defer = True

                            if not should_defer:
                                if pending_text:
                                    s_tok, e_tok = current_wrappers
                                    parts.append(f"{s_tok}{pending_text}{e_tok}")
                                    pending_text = ""
                                    current_wrappers = ("", "")

                                meta_block = _build_merged_meta_block(deferred_meta_states, comments_data)
                                if meta_block:
                                    parts.append(f"{{>>{meta_block}<<}}")
                                deferred_meta_states = []

                elif isinstance(item, DocxEvent):
                    if item.type in ("fmt_start", "fmt_end", "heading"):
                        if item.type == "fmt_start":
                            if item.id == "**":
                                active_bold += 1
                            elif item.id == "_":
                                active_italic += 1
                        elif item.type == "fmt_end":
                            if item.id == "**":
                                active_bold = max(0, active_bold - 1)
                            elif item.id == "_":
                                active_italic = max(0, active_italic - 1)
                        elif item.type == "heading":
                            if pending_text:
                                s_tok, e_tok = current_wrappers
                                parts.append(f"{s_tok}{pending_text}{e_tok}")
                                pending_text = ""
                                current_wrappers = ("", "")
                            parts.append(item.id)
                        continue
                    if pending_text:
                        s_tok, e_tok = current_wrappers
                        parts.append(f"{s_tok}{pending_text}{e_tok}")
                        pending_text = ""
                        current_wrappers = ("", "")

                    if item.type == "start":
                        active_comments.add(item.id)
                    elif item.type == "end":
                        active_comments.discard(item.id)
                    elif item.type == "ins_start":
                        active_ins[item.id] = item
                    elif item.type == "ins_end":
                        active_ins.pop(item.id, None)
                    elif item.type == "del_start":
                        active_del[item.id] = item
                    elif item.type == "del_end":
                        active_del.pop(item.id, None)

            if pending_text:
                s_tok, e_tok = current_wrappers
                parts.append(f"{s_tok}{pending_text}{e_tok}")

            if deferred_meta_states:
                meta_block = _build_merged_meta_block(deferred_meta_states, comments_data)
                if meta_block:
                    parts.append(f"{{>>{meta_block}<<}}")

            final_text = "".join(parts)
            final_text = (
                final_text.replace("\r\x07\r\x07", "\n")
                .replace("\r\x07", " | ")
                .replace("\x07", " | ")
                .replace("\r", "\n")
            )

            return ToolResult(
                content=final_text,
                structured_content={
                    "markdown": final_text,
                    "title": "Live Word Document",
                },
            )

        finally:
            # Omitted CoUninitialize() to prevent GC access violations.
            pass

    async def process_active_word_batch(
        ctx: Context,
        changes: List[DocumentChange],
        author_name: str,
    ) -> str:
        if not changes:
            return "No changes provided."

        if not author_name or not author_name.strip():
            return "Error: author_name cannot be empty."

        await ctx.info(f"Applying {len(changes)} changes to live Word document...")
        pythoncom.CoInitialize()
        try:
            try:
                app = win32com.client.GetActiveObject("Word.Application")
                doc = app.ActiveDocument
            except Exception as e:
                raise ToolError(f"Could not connect to active Word document. {e}") from e

            original_track_revisions = doc.TrackRevisions
            doc.TrackRevisions = True

            original_user = app.UserName
            app.UserName = author_name

            stats = {"applied": 0, "failed": 0}

            # Pre-resolve Revision objects to prevent index drift.
            revisions_map = {}
            try:
                for i in range(1, doc.Revisions.Count + 1):
                    revisions_map[f"Chg:{i}"] = doc.Revisions(i)
            except Exception as e:
                logger.warning(f"Failed to pre-resolve revisions: {e}")

            try:
                for change in changes:
                    try:
                        if isinstance(change, ModifyText):
                            clean_target = _strip_critic_markup(change.target_text)
                            raw_text = doc.Content.Text

                            clean_chars = []
                            mapping = []
                            i = 0
                            while i < len(raw_text):
                                if raw_text[i : i + 4] == "\r\x07\r\x07":
                                    clean_chars.append("\n")
                                    mapping.append(i)
                                    i += 4
                                elif raw_text[i : i + 2] == "\r\x07":
                                    clean_chars.extend([" ", "|", " "])
                                    mapping.extend([i, i, i])
                                    i += 2
                                elif raw_text[i] == "\x07":
                                    clean_chars.extend([" ", "|", " "])
                                    mapping.extend([i, i, i])
                                    i += 1
                                elif raw_text[i] == "\r":
                                    clean_chars.append("\n")
                                    mapping.append(i)
                                    i += 1
                                else:
                                    clean_chars.append(raw_text[i])
                                    mapping.append(i)
                                    i += 1
                            mapping.append(len(raw_text))
                            current_text = "".join(clean_chars)

                            start_idx, end_idx = _find_match_in_text(current_text, clean_target)

                            if start_idx != -1:
                                actual_start = mapping[start_idx]
                                actual_end = mapping[end_idx]
                                exact_substring = raw_text[actual_start:actual_end]

                                search_start = max(0, actual_start - 200)
                                search_end = min(doc.Content.End, actual_end + 200)
                                rng = doc.Range(Start=search_start, End=search_end)

                                search_text = exact_substring[:250] if len(exact_substring) > 250 else exact_substring

                                rng.Find.ClearFormatting()
                                rng.Find.Text = search_text
                                rng.Find.Forward = True
                                rng.Find.Wrap = 0  # wdFindStop

                                if rng.Find.Execute():
                                    actual_start = rng.Start
                                    replace_rng = doc.Range(Start=actual_start, End=actual_start + len(exact_substring))
                                    _apply_com_replacement(doc, app, replace_rng, change.new_text, change.comment)
                                    stats["applied"] += 1
                                else:
                                    # Fallback: search entire document
                                    doc_rng = doc.Content
                                    doc_rng.Find.ClearFormatting()
                                    doc_rng.Find.Text = search_text
                                    if doc_rng.Find.Execute():
                                        replace_rng = doc.Range(
                                            Start=doc_rng.Start, End=doc_rng.Start + len(exact_substring)
                                        )
                                        _apply_com_replacement(doc, app, replace_rng, change.new_text, change.comment)
                                        stats["applied"] += 1
                                    else:
                                        stats["failed"] += 1
                            else:
                                stats["failed"] += 1
                                await ctx.warning(f"Could not find target text: '{change.target_text[:30]}...'")

                        elif isinstance(change, AcceptChange):
                            if change.target_id in revisions_map:
                                revisions_map[change.target_id].Accept()
                                stats["applied"] += 1
                            else:
                                stats["failed"] += 1
                                await ctx.warning(f"Revision {change.target_id} not found or lost to drift.")

                        elif isinstance(change, RejectChange):
                            if change.target_id in revisions_map:
                                revisions_map[change.target_id].Reject()
                                stats["applied"] += 1
                            else:
                                stats["failed"] += 1
                                await ctx.warning(f"Revision {change.target_id} not found or lost to drift.")

                        elif isinstance(change, ReplyComment):
                            try:
                                target_idx = int(change.target_id.split(":")[1]) + 1
                                com_to_reply = doc.Comments(target_idx)
                                try:
                                    com_to_reply.Replies.Add(com_to_reply.Range, change.text)
                                except Exception:
                                    doc.Comments.Add(com_to_reply.Range, change.text)
                                stats["applied"] += 1
                            except Exception as e:
                                stats["failed"] += 1
                                await ctx.warning(f"Comment {change.target_id} not found. {e}")

                    except Exception as e:
                        stats["failed"] += 1
                        await ctx.error(f"Failed to apply change {getattr(change, 'type', 'Unknown')}: {e}")

            finally:
                app.UserName = original_user
                doc.TrackRevisions = original_track_revisions

            return f"Live Word Batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}."

        finally:
            pass

    async def open_word_document_impl(ctx: Context, file_path: str, visible: bool = True) -> str:
        await ctx.info(f"Opening {file_path} in Word...")
        pythoncom.CoInitialize()
        try:
            abs_path = str(Path(file_path).resolve())

            # Dispatch starts a new instance or connects to an existing one
            app = win32com.client.Dispatch("Word.Application")
            app.Visible = visible
            if visible:
                try:
                    app.Activate()
                except Exception:
                    pass

            app.Documents.Open(abs_path)
            return f"Successfully opened {abs_path} in Microsoft Word."
        except Exception as e:
            raise ToolError(f"Failed to open document in Word. {e}") from e
        finally:
            pass

    async def save_active_word_document_impl(
        ctx: Context, output_path: Optional[str] = None, close: bool = False
    ) -> str:
        await ctx.info("Saving active Word document...")
        pythoncom.CoInitialize()
        try:
            app = win32com.client.GetActiveObject("Word.Application")
            doc = app.ActiveDocument

            if output_path:
                abs_path = str(Path(output_path).resolve())
                doc.SaveAs2(abs_path)
                msg = f"Successfully saved active document as: {abs_path}"
            else:
                doc.Save()
                msg = "Successfully saved active document."

            if close:
                doc.Close(0)  # 0 = wdDoNotSaveChanges (since we just saved it)
                msg += " Document closed."

            return msg
        except Exception as e:
            raise ToolError(f"Failed to save active Word document. {e}") from e
        finally:
            pass
