import io
import re
import sys
from pathlib import Path
from typing import List, Optional

import structlog
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

logger = structlog.get_logger(__name__)


def _build_mock_docx_stream(word_open_xml: str) -> io.BytesIO:
    """
    Wraps an extracted Flat OPC XML string (doc.WordOpenXML) into a standard ZIP-based
    DOCX stream so python-docx can parse it natively.

    Key insight: Flat OPC does NOT contain a [Content_Types].xml part. Instead, each
    <pkg:part> declares its `pkg:contentType` attribute. We must synthesize
    [Content_Types].xml from those attributes before python-docx can open the archive.

    Uses regex to prevent xml.etree from mangling namespaces and dropping elements.
    Handles both paired (<pkg:part>...</pkg:part>) and self-closing (<pkg:part .../>)
    forms, which Word emits for empty parts.

    This function is pure-Python (no COM) and lives at module scope so it can be
    regression-tested cross-platform, independently of the Windows COM path that
    consumes it.

    Set ADEU_DEBUG_FLATOPC=1 to dump the generated zip to a temp file for inspection.
    """
    import base64
    import os
    import zipfile

    # Match both paired and self-closing pkg:part forms in a single pass.
    # Group 1: attribute string. Group 2: inner body (empty for self-closing).
    part_pattern = re.compile(
        r"<pkg:part\b([^>]*?)(?:/>|>(.*?)</pkg:part>)",
        re.DOTALL,
    )

    parts_meta: list[tuple[str, str]] = []
    parts_written = 0
    parts_skipped = 0

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in part_pattern.finditer(word_open_xml):
            attrs_str = m.group(1)
            content_block = m.group(2) or ""

            name_m = re.search(r'pkg:name="([^"]+)"', attrs_str)
            ctype_m = re.search(r'pkg:contentType="([^"]+)"', attrs_str)
            if not name_m or not ctype_m:
                parts_skipped += 1
                logger.debug(f"Skipping pkg:part with missing name/contentType: {attrs_str[:80]!r}")
                continue

            raw_name = name_m.group(1)
            content_type = ctype_m.group(1)

            # ZIP entries must not have a leading slash
            zip_name = raw_name.lstrip("/")

            xml_match = re.search(r"<pkg:xmlData>(.*?)</pkg:xmlData>", content_block, re.DOTALL)
            bin_match = re.search(r"<pkg:binaryData>(.*?)</pkg:binaryData>", content_block, re.DOTALL)

            if xml_match:
                inner_xml = xml_match.group(1).strip()
                payload = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n{inner_xml}').encode("utf-8")
                zf.writestr(zip_name, payload)
                parts_written += 1
                parts_meta.append((raw_name, content_type))
            elif bin_match:
                b64_data = bin_match.group(1).strip()
                zf.writestr(zip_name, base64.b64decode(b64_data))
                parts_written += 1
                parts_meta.append((raw_name, content_type))
            else:
                # Empty self-closing part — legitimate but no body to write.
                logger.debug(f"Empty pkg:part (no xmlData/binaryData): {raw_name}")

        rels_ct = "application/vnd.openxmlformats-package.relationships+xml"
        overrides = []
        for raw_name, ctype in parts_meta:
            if raw_name.endswith(".rels"):
                continue
            safe_name = raw_name.replace("&", "&amp;").replace('"', "&quot;")
            safe_ct = ctype.replace("&", "&amp;").replace('"', "&quot;")
            overrides.append(f'  <Override PartName="{safe_name}" ContentType="{safe_ct}"/>')

        ct_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\r\n'
            f'  <Default Extension="rels" ContentType="{rels_ct}"/>\r\n' + "\r\n".join(overrides) + "\r\n</Types>\r\n"
        )
        zf.writestr("[Content_Types].xml", ct_xml.encode("utf-8"))

    size_bytes = stream.tell()
    logger.info(
        f"Built in-memory DOCX from Flat OPC: {parts_written} parts written, "
        f"{parts_skipped} malformed parts skipped, {size_bytes} bytes total."
    )

    if os.environ.get("ADEU_DEBUG_FLATOPC"):
        import tempfile

        dbg_path = Path(tempfile.gettempdir()) / "adeu_flatopc_debug.docx"
        with open(dbg_path, "wb") as f:
            f.write(stream.getvalue())
        logger.info(f"ADEU_DEBUG_FLATOPC: dumped reconstructed DOCX to {dbg_path}")

    stream.seek(0)
    return stream


if sys.platform == "win32":
    # NOTE: None of the Windows entry points below call pythoncom.CoUninitialize() or
    # app.Quit() on teardown. This is intentional — see AI_CONTEXT.md §9 (COM Apartment
    # Lifecycle): FastMCP / pytest hold COM proxies unpredictably, and explicit teardown
    # causes fatal RPC/Access Violations (0x800706be). We let the OS handle it.
    import pythoncom
    import win32com.client

    from adeu.markup import _find_match_in_text
    from adeu.models import AcceptChange, DocumentChange, ModifyText, RejectChange, ReplyComment

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

    def _read_active_word_document_core(clean_view: bool = False) -> str:
        """
        Reads the live active Word document by extracting its Flat OPC XML via
        doc.WordOpenXML, wrapping it into an in-memory DOCX zip stream, and routing
        it through the same ingest pipeline used for disk files.

        This unifies the live and disk paths for both normal and clean_view reads
        and avoids the COM round-trip overhead that dominated the old character-by-
        character traversal.
        """
        from adeu.ingest import extract_text_from_stream

        pythoncom.CoInitialize()
        try:
            app = win32com.client.GetActiveObject("Word.Application")
            doc = app.ActiveDocument
        except Exception as e:
            raise RuntimeError(f"Could not connect to active Word document. {e}") from e

        xml_str = doc.WordOpenXML
        stream = _build_mock_docx_stream(xml_str)
        return extract_text_from_stream(stream, filename="LiveWord", clean_view=clean_view)

    async def read_active_word_document(
        ctx: Context,
        clean_view: bool = False,
    ) -> ToolResult:
        await ctx.info(f"Extracting live Word document via WordOpenXML (clean_view={clean_view})")
        try:
            final_text = _read_active_word_document_core(clean_view)
            await ctx.info(f"Live Word extraction successful: {len(final_text)} characters.")
            return ToolResult(
                content=final_text,
                structured_content={
                    "markdown": final_text,
                    "title": "Live Word Document",
                },
            )
        except Exception as e:
            raise ToolError(str(e)) from e

    def _process_active_word_batch_core(changes: List[DocumentChange], author_name: str) -> dict:
        """Synchronous core for processing live word batch, decoupled from FastMCP context."""
        stats = {"applied": 0, "failed": 0}
        if not changes:
            return stats

        if not author_name or not author_name.strip():
            raise ValueError("author_name cannot be empty.")

        pythoncom.CoInitialize()
        try:
            app = win32com.client.GetActiveObject("Word.Application")
            doc = app.ActiveDocument
        except Exception as e:
            raise RuntimeError(f"Could not connect to active Word document. {e}") from e

        original_track_revisions = doc.TrackRevisions
        doc.TrackRevisions = True

        original_user = app.UserName
        app.UserName = author_name

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
                            logger.warning(f"Could not find target text: '{change.target_text[:30]}...'")

                    elif isinstance(change, AcceptChange):
                        if change.target_id in revisions_map:
                            revisions_map[change.target_id].Accept()
                            stats["applied"] += 1
                        else:
                            stats["failed"] += 1
                            logger.warning(f"Revision {change.target_id} not found or lost to drift.")

                    elif isinstance(change, RejectChange):
                        if change.target_id in revisions_map:
                            revisions_map[change.target_id].Reject()
                            stats["applied"] += 1
                        else:
                            stats["failed"] += 1
                            logger.warning(f"Revision {change.target_id} not found or lost to drift.")

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
                            logger.warning(f"Comment {change.target_id} not found. {e}")

                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"Failed to apply change {getattr(change, 'type', 'Unknown')}: {e}")

        finally:
            app.UserName = original_user
            doc.TrackRevisions = original_track_revisions

        return stats

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
        try:
            stats = _process_active_word_batch_core(changes, author_name)
            await ctx.info(f"Live Word batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}.")
            return f"Live Word Batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}."
        except Exception as e:
            raise ToolError(str(e)) from e

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
            await ctx.info(f"Opened {abs_path} successfully.")
            return f"Successfully opened {abs_path} in Microsoft Word."
        except Exception as e:
            raise ToolError(f"Failed to open document in Word. {e}") from e

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

            await ctx.info(msg)
            return msg
        except Exception as e:
            raise ToolError(f"Failed to save active Word document. {e}") from e
