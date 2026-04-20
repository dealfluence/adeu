import logging
import re
import sys
import gc
from typing import Annotated, List

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

if sys.platform == "win32":
    import pythoncom
    import win32com.client
    from fastmcp.tools import tool

    from adeu.markup import _find_match_in_text
    from adeu.models import AcceptChange, DocumentChange, ModifyText, RejectChange, ReplyComment
    from adeu.mcp_components.shared import MARKDOWN_UI_URI

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

    @tool(
        description=(
            "Reads the currently active, visible document in Microsoft Word on Windows. "
            "Extracts text with inline CriticMarkup representing Tracked Changes and Comments. "
            "Set clean_view=True to ignore redlines and comments."
        ),
        annotations={"readOnlyHint": True},
        meta={"ui": {"resourceUri": MARKDOWN_UI_URI}},
    )
    async def read_active_word_document(
        ctx: Context,
        clean_view: Annotated[bool, "If True, returns clean text without markup."] = False,
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
                return ToolResult(
                    content="",
                    structured_content={"markdown": "", "title": "Live Word Document"}
                )

            if clean_view:
                clean_text = raw_text.replace("\r", "\n")
                return ToolResult(
                    content=clean_text,
                    structured_content={"markdown": clean_text, "title": "Live Word Document"}
                )

            annotations = []

            # 1. Extract Revisions
            for i in range(1, doc.Revisions.Count + 1):
                try:
                    rev = doc.Revisions(i)
                    if rev.Type in (1, 2):  # 1: Insert, 2: Delete
                        text = rev.Range.Text or ""
                        annotations.append(
                            {
                                "start": rev.Range.Start,
                                "end": rev.Range.End,
                                "text": text,
                                "type": "insert" if rev.Type == 1 else "delete",
                                "id": f"Chg:{i}",
                            }
                        )
                except Exception as e:
                    logger.warning(f"Failed to read revision {i}: {e}")

            # 2. Extract Comments
            for i in range(1, doc.Comments.Count + 1):
                try:
                    com = doc.Comments(i)
                    text = com.Scope.Text or ""
                    annotations.append(
                        {
                            "start": com.Scope.Start,
                            "end": com.Scope.End,
                            "text": text,
                            "type": "comment",
                            "id": f"Com:{i}",
                            "content": com.Range.Text.strip().replace("\r", " "),
                            "author": com.Author,
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to read comment {i}: {e}")

            # 3. Anchor annotations to raw_text to bypass COM offset drift
            mapped_annotations = []
            for ann in annotations:
                text_to_find = ann["text"]
                start_hint = ann["start"]

                if not text_to_find:
                    # Point-comment or empty revision
                    ann["mapped_start"] = min(start_hint, len(raw_text))
                    ann["mapped_end"] = ann["mapped_start"]
                    ann["inner_override"] = ""
                    mapped_annotations.append(ann)
                    continue

                # Slice a search window around the expected offset
                window_start = max(0, start_hint - 200)
                window_end = min(len(raw_text), start_hint + len(text_to_find) + 200)
                window_text = raw_text[window_start:window_end]

                rel_start, rel_end = _find_match_in_text(
                    window_text.replace("\r", "\n"), text_to_find.replace("\r", "\n")
                )

                if rel_start != -1:
                    ann["mapped_start"] = window_start + rel_start
                    ann["mapped_end"] = window_start + rel_end
                else:
                    # Fallback
                    ann["mapped_start"] = min(start_hint, len(raw_text))
                    ann["mapped_end"] = ann["mapped_start"]
                    ann["inner_override"] = text_to_find

                mapped_annotations.append(ann)

            # Convert overlapping annotations into a sequence of insertion events
            # to safely build the final string without index drift or markup corruption.
            events = []
            for ann in mapped_annotations:
                start = ann["mapped_start"]
                end = ann["mapped_end"]
                inner_override = ann.get("inner_override")

                if ann["type"] == "insert":
                    prefix = "{++"
                    suffix = f"++}}{{>>[Edit:{ann['id']}]<<}}"
                elif ann["type"] == "delete":
                    prefix = "{--"
                    suffix = f"--}}{{>>[Edit:{ann['id']}]<<}}"
                elif ann["type"] == "comment":
                    prefix = "{=="
                    suffix = f"==}}{{>>{ann['author']}: {ann['content']} [Edit:{ann['id']}]<<}}"
                else:
                    continue

                if start == end:
                    # Point annotations
                    inner = inner_override if inner_override is not None else ""
                    events.append((start, 1, 0, ann["id"], prefix + inner + suffix))
                else:
                    length = end - start
                    # Open events (type 2) sort with -length so longer spans open first
                    events.append((start, 2, -length, ann["id"], prefix))
                    # Close events (type 0) sort with length so shorter spans close first
                    events.append((end, 0, length, ann["id"], suffix))

            events.sort()

            result_parts = []
            last_idx = 0
            for idx, _, _, _, text_to_insert in events:
                if idx > last_idx:
                    result_parts.append(raw_text[last_idx:idx])
                    last_idx = idx
                result_parts.append(text_to_insert)
            
            if last_idx < len(raw_text):
                result_parts.append(raw_text[last_idx:])

            final_text = "".join(result_parts).replace("\r", "\n")
            return ToolResult(
                content=final_text,
                structured_content={
                    "markdown": final_text,
                    "title": "Live Word Document",
                }
            )

        finally:
            # Pytest tracebacks hold COM locals, causing GC access violations if we tear down early.
            # We intentionally omit CoUninitialize() and let the process exit handle cleanup.
            pass

    @tool(
        description=(
            "Applies redlines, text modifications, and review actions directly to the user's active MS Word window. "
            "Use this for real-time live editing and Copilot interactions."
        ),
        annotations={"destructiveHint": True},
    )
    async def process_active_word_batch(
        ctx: Context,
        changes: Annotated[List[DocumentChange], "List of changes to apply."],
        author_name: Annotated[str, "Name to appear in Track Changes (e.g. 'Reviewer AI')."],
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
            # Modifying text adds new Revisions, which shifts collection indices.
            # By holding the COM reference now, we can accept/reject safely later.
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
                            current_text = doc.Content.Text.replace("\r", "\n")
                            start_idx, end_idx = _find_match_in_text(current_text, clean_target)

                            if start_idx != -1:
                                # Get literal string, preserving \r for native Find execution
                                exact_substring = doc.Content.Text[start_idx:end_idx]

                                search_start = max(0, start_idx - 200)
                                search_end = min(doc.Content.End, end_idx + 200)
                                rng = doc.Range(Start=search_start, End=search_end)

                                # Word Find limit is 255 chars
                                search_text = exact_substring[:250] if len(exact_substring) > 250 else exact_substring

                                rng.Find.ClearFormatting()
                                rng.Find.Text = search_text
                                rng.Find.Forward = True
                                rng.Find.Wrap = 0  # wdFindStop

                                if rng.Find.Execute():
                                    actual_start = rng.Start
                                    replace_rng = doc.Range(Start=actual_start, End=actual_start + len(exact_substring))
                                    replace_rng.Text = change.new_text.replace("\n", "\r")
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
                                        replace_rng.Text = change.new_text.replace("\n", "\r")
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
                            idx = int(change.target_id.split(":")[1])
                            com = doc.Comments(idx)
                            try:
                                com.Replies.Add(com.Range, change.text)
                            except Exception:
                                doc.Comments.Add(com.Range, change.text)
                            stats["applied"] += 1

                    except Exception as e:
                        stats["failed"] += 1
                        await ctx.error(f"Failed to apply change {getattr(change, 'type', 'Unknown')}: {e}")

            finally:
                app.UserName = original_user
                doc.TrackRevisions = original_track_revisions

            return f"Live Word Batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}."

        finally:
            # Omitted CoUninitialize() to prevent GC access violations.
            pass
