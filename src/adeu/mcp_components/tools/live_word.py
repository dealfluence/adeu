import io
import re
import sys
from pathlib import Path
from typing import Any, List, Optional

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
    import posixpath
    import xml.etree.ElementTree as ET
    import zipfile

    # Match both paired and self-closing pkg:part forms in a single pass.
    # Group 1: attribute string. Group 2: inner body (empty for self-closing).
    part_pattern = re.compile(
        r"<pkg:part\b([^>]*?)(?:/>|>(.*?)</pkg:part>)",
        re.DOTALL,
    )

    parts_meta: list[tuple[str, str]] = []
    parts_data: dict[str, bytes] = {}
    parts_skipped = 0

    # 1. Collect all parts into memory
    for m in part_pattern.finditer(word_open_xml):
        attrs_str = m.group(1)
        content_block = m.group(2) or ""

        name_m = re.search(r'pkg:name="([^"]+)"', attrs_str)
        ctype_m = re.search(r'pkg:contentType="([^"]+)"', attrs_str)

        if not name_m:
            parts_skipped += 1
            continue

        raw_name = name_m.group(1)
        content_type = ctype_m.group(1) if ctype_m else ""

        if not content_type and not raw_name.endswith(".rels"):
            parts_skipped += 1
            continue

        zip_name = raw_name.lstrip("/")

        xml_match = re.search(r"<pkg:xmlData>(.*?)</pkg:xmlData>", content_block, re.DOTALL)
        bin_match = re.search(r"<pkg:binaryData>(.*?)</pkg:binaryData>", content_block, re.DOTALL)

        if xml_match:
            inner_xml = xml_match.group(1).strip()
            payload = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n{inner_xml}').encode("utf-8")
            parts_data[zip_name] = payload
            parts_meta.append((raw_name, content_type))
        elif bin_match:
            b64_data = bin_match.group(1).strip()
            parts_data[zip_name] = base64.b64decode(b64_data)
            parts_meta.append((raw_name, content_type))
        else:
            # Empty/self-closing part dropped by COM
            logger.debug(f"Empty pkg:part (no xmlData/binaryData): {raw_name}")

    valid_zip_names = set(parts_data.keys())

    # 2. Prune broken relationships (e.g. customXml dropped by COM)
    rels_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ET.register_namespace("", rels_ns)

    for zip_name, payload in parts_data.items():
        if zip_name.endswith(".rels"):
            try:
                tree = ET.fromstring(payload)
                modified = False
                for rel in list(tree):
                    target = rel.attrib.get("Target")
                    mode = rel.attrib.get("TargetMode", "Internal")

                    if target and mode == "Internal":
                        d1 = posixpath.dirname(zip_name)
                        d2 = posixpath.dirname(d1)
                        base_dir = "/" + d2
                        resolved = posixpath.normpath(posixpath.join(base_dir, target)).lstrip("/")

                        if resolved not in valid_zip_names:
                            logger.debug(f"Pruning broken relationship to {resolved} from {zip_name}")
                            tree.remove(rel)
                            modified = True

                if modified:
                    parts_data[zip_name] = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
            except Exception as e:
                logger.warning(f"Failed to prune relations in {zip_name}: {e}")

    # 3. Build the ZIP
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        for z_name, data in parts_data.items():
            zf.writestr(z_name, data)

        rels_ct = "application/vnd.openxmlformats-package.relationships+xml"
        overrides = []
        for raw_name, ctype in parts_meta:
            if raw_name.endswith(".rels") or not ctype:
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
        f"Built in-memory DOCX from Flat OPC: {len(parts_data)} parts written, "
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

    from adeu.diff import trim_common_context
    from adeu.markup import _find_match_in_text
    from adeu.models import (
        AcceptChange,
        DocumentChange,
        ModifyText,
        RejectChange,
        ReplyComment,
    )

    def _strip_critic_markup(text: str) -> str:
        """Removes CriticMarkup tags so raw text can be found via Word's native Find."""
        if not text:
            return ""
        text = re.sub(r"\{--.*?--\}", "", text)
        text = re.sub(r"\{>>.*?<<\}", "", text)
        text = re.sub(r"\{\+\+(.*?)\+\+\}", r"\1", text)
        text = re.sub(r"\{==(.*?)==\}", r"\1", text)
        return text

    def _strip_markdown_formatting(text: str) -> str:
        """Strips Markdown bold/italic/header markers so target_text can match plain COM text."""
        if not text:
            return ""
        # Strip bold: **text** or __text__
        text = re.sub(r"\*\*", "", text)
        text = re.sub(r"__", "", text)
        # Strip italic: single * or _ not part of a word
        text = re.sub(r"(?<!\w)\*(?!\*)", "", text)
        text = re.sub(r"(?<!\w)_(?!_)", "", text)
        # Strip header markers at line start
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
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

    _WD_HEADING_STYLE_IDS = {
        1: -2,  # wdStyleHeading1
        2: -3,  # wdStyleHeading2
        3: -4,  # wdStyleHeading3
        4: -5,  # wdStyleHeading4
        5: -6,  # wdStyleHeading5
        6: -7,  # wdStyleHeading6
        7: -8,  # wdStyleHeading7
        8: -9,  # wdStyleHeading8
        9: -10,  # wdStyleHeading9
    }

    def _parse_markdown_heading_prefix(line: str):
        """
        Detects a leading markdown heading marker on a single line.
        Returns (clean_text, heading_level) where heading_level is an int 1..9
        or None if no heading prefix. Strips up to 9 leading '#' chars followed
        by a space, matching the disk engine's _parse_markdown_style.
        """
        if not line.startswith("#"):
            return line, None
        level = 0
        rest = line
        while rest.startswith("#") and level < 9:
            level += 1
            rest = rest[1:]
        if rest.startswith(" "):
            return rest[1:], level
        # '#' with no space: not a heading, treat as literal
        return line, None

    def _is_structured_new_text(new_text: str) -> bool:
        """
        Returns True if `new_text` contains any markdown structure that the
        simple inline-replacement path cannot render: paragraph breaks or
        heading markers. Bold/italic alone is NOT structural.
        """
        if not new_text:
            return False
        # Paragraph boundary via blank line or explicit newline pair
        if "\n" in new_text or "\r" in new_text:
            return True
        # Heading marker at the very start
        stripped = new_text.lstrip()
        if stripped.startswith("#"):
            # confirm it's actually a heading marker ('# ...'), not literal
            _, level = _parse_markdown_heading_prefix(stripped)
            if level is not None:
                return True
        return False

    def _split_new_text_into_lines(new_text: str) -> list[str]:
        """
        Splits new_text into lines for paragraph-wise insertion. Matches
        the disk engine's behaviour of splitting on any run of newline chars.
        Trailing empty elements are dropped so we don't emit a trailing
        empty paragraph.
        """
        lines = re.split(r"[\r\n]+", new_text)
        # Disk engine also pops trailing empty lines in track_insert
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    def _apply_line_formatting(doc, base_start: int, plain_text: str, b_ranges, i_ranges, was_tracking: bool):
        """
        Applies bold/italic ranges to a just-inserted span. Mirrors the
        TrackRevisions-toggle pattern used in the original
        _apply_com_replacement: formatting is applied with tracking OFF so
        we don't pollute the review pane with format revisions, then the
        caller's tracking state is restored.
        """
        doc.TrackRevisions = False
        try:
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

    _WD_STYLE_NORMAL = -1

    def _apply_paragraph_style(doc, position: int, heading_level):
        """
        Applies a paragraph style to the paragraph containing `position`.

        heading_level=None means "plain body paragraph" (apply Normal).
        heading_level=1..9 means Heading N.

        We always apply an explicit style so that subsequent paragraphs
        inserted after a Heading do NOT silently inherit the Heading
        style (Word's default split-paragraph behaviour).
        """
        if heading_level is None:
            style_id = _WD_STYLE_NORMAL
        else:
            looked_up = _WD_HEADING_STYLE_IDS.get(heading_level)
            if looked_up is None:
                return
            style_id = looked_up
        try:
            p = doc.Range(position, position).Paragraphs(1)
            p.Style = style_id
        except Exception as e:
            logger.warning(f"Failed to apply paragraph style (level={heading_level}) at {position}: {e}")

    def _apply_structured_com_replacement(doc, app, target_rng, new_text, comment_text):
        """
        Multi-paragraph tracked replacement via COM. Mirrors engine.track_insert
        semantics: first line replaces target inline (with its heading style
        applied if any), each subsequent line inserted as a new paragraph
        AFTER the previous one.

        Comments rescued from the target range are re-anchored to the full
        resulting insertion span. An explicit comment_text is attached the
        same way.
        """
        was_tracking = doc.TrackRevisions

        # 1. Rescue comments currently anchored to target_rng
        rescued_comments = []
        try:
            for i in range(1, target_rng.Comments.Count + 1):
                c = target_rng.Comments(i)
                rescued_comments.append({"author": c.Author, "text": c.Range.Text})
        except Exception as e:
            logger.warning(f"Failed to rescue comments: {e}")

        # 2. Parse new_text into structured lines
        lines = _split_new_text_into_lines(new_text)
        if not lines:
            # Caller gave us an empty new_text after splitting; nothing to do
            # beyond deleting the target. Fall back to a blank inline replace.
            target_rng.Text = ""
            return

        # 3. First line: inline replacement.
        first_clean, first_level = _parse_markdown_heading_prefix(lines[0])
        first_plain, first_b, first_i = _parse_markdown_for_com(first_clean)

        first_start = target_rng.Start
        target_rng.Text = first_plain

        _apply_paragraph_style(doc, first_start, first_level)
        _apply_line_formatting(doc, first_start, first_plain, first_b, first_i, was_tracking)

        # 4. Remaining lines: insert as new paragraphs AFTER the current paragraph.
        # This mirrors the Disk engine's invariant of appending <w:p> elements to the body
        # rather than splitting the current paragraph, preventing the deleted text from
        # being pushed to the next line.
        para = target_rng.Paragraphs(1)
        insert_idx = para.Range.End
        last_end = first_start + len(first_plain)

        for line in lines[1:]:
            clean, level = _parse_markdown_heading_prefix(line)
            plain, b_ranges, i_ranges = _parse_markdown_for_com(clean)

            try:
                rng = doc.Range(insert_idx, insert_idx)
                rng.Text = plain + "\r"
            except Exception as e:
                logger.error(f"Failed to insert line text: {e}")
                break

            _apply_paragraph_style(doc, insert_idx, level)
            _apply_line_formatting(doc, insert_idx, plain, b_ranges, i_ranges, was_tracking)
            
            last_end = insert_idx + len(plain)
            insert_idx += len(plain) + 1

        # 5. Reattach rescued comments to the full insertion span.
        full_insertion_rng = doc.Range(first_start, last_end)
        current_user = app.UserName
        for c_data in rescued_comments:
            try:
                app.UserName = c_data["author"]
                doc.Comments.Add(full_insertion_rng, c_data["text"])
            except Exception as e:
                logger.warning(f"Failed to re-attach rescued comment: {e}")
        app.UserName = current_user

        # 6. Attach explicit comment if requested.
        if comment_text:
            try:
                doc.Comments.Add(full_insertion_rng, comment_text)
            except Exception as e:
                logger.warning(f"Failed to attach edit comment: {e}")

    def _apply_com_replacement(doc, app, target_rng, new_text, comment_text):
        """
        Routes to simple or structured replacement based on new_text content.

        Simple path: inline Range.Text=... + bold/italic, for single-line
        new_text with no heading markers. Fast, unchanged behaviour.

        Structured path: multi-paragraph tracked insertion with heading
        styles. See _apply_structured_com_replacement.
        """
        if _is_structured_new_text(new_text):
            _apply_structured_com_replacement(doc, app, target_rng, new_text, comment_text)
            return

        # ---- Simple path (original behaviour) ----
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
        doc.TrackRevisions = False
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

    def _process_active_word_batch_core(changes: List[DocumentChange], author_name: str) -> dict[str, Any]:
        """Synchronous core for processing live word batch, decoupled from FastMCP context."""
        stats: dict[str, Any] = {"applied": 0, "failed": 0, "skipped_details": []}
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
        stats["author_overridden_by_word"] = original_user
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
                        clean_target = _strip_markdown_formatting(_strip_critic_markup(change.target_text))
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
                                actual_end = actual_start + len(exact_substring)

                                effective_new = change.new_text or ""

                                # Pure comment detection: skip text replacement to prevent spurious w:del/w:ins tags
                                if change.target_text == effective_new:
                                    if change.comment:
                                        replace_rng = doc.Range(Start=actual_start, End=actual_end)
                                        try:
                                            doc.Comments.Add(replace_rng, change.comment)
                                        except Exception as e:
                                            logger.warning(f"Failed to attach comment for same->same edit: {e}")
                                    stats["applied"] += 1
                                    continue

                                # Bug #8 Fix: Mathematically shrink the replacement range by trimming common context
                                if exact_substring == effective_new:
                                    final_new_text = effective_new
                                else:
                                    p_len, s_len = trim_common_context(exact_substring, effective_new)
                                    actual_start += p_len
                                    actual_end -= s_len

                                    n_end = len(effective_new) - s_len
                                    final_new_text = effective_new[p_len:n_end]

                                replace_rng = doc.Range(Start=actual_start, End=actual_end)
                                _apply_com_replacement(
                                    doc,
                                    app,
                                    replace_rng,
                                    final_new_text,
                                    change.comment,
                                )
                                stats["applied"] += 1
                            else:
                                # Fallback: search entire document
                                doc_rng = doc.Content
                                doc_rng.Find.ClearFormatting()
                                doc_rng.Find.Text = search_text
                                if doc_rng.Find.Execute():
                                    replace_rng = doc.Range(
                                        Start=doc_rng.Start,
                                        End=doc_rng.Start + len(exact_substring),
                                    )

                                    effective_new = change.new_text or ""
                                    # Pure comment detection (Fallback path)
                                    if change.target_text == effective_new:
                                        if change.comment:
                                            try:
                                                doc.Comments.Add(replace_rng, change.comment)
                                            except Exception as e:
                                                logger.warning(
                                                    f"Failed to attach comment for same->same fallback edit: {e}"
                                                )
                                        stats["applied"] += 1
                                        continue

                                    _apply_com_replacement(
                                        doc,
                                        app,
                                        replace_rng,
                                        change.new_text,
                                        change.comment,
                                    )
                                    stats["applied"] += 1
                                else:
                                    stats["failed"] += 1
                                    stats["skipped_details"].append(
                                        f"- Failed to find match in document for: '{change.target_text[:40]}...'"
                                    )
                        else:
                            stats["failed"] += 1
                            stats["skipped_details"].append(
                                f"- Failed to find target text: '{change.target_text[:40]}...'"
                            )
                            logger.warning(f"Could not find target text: '{change.target_text[:30]}...'")

                    elif isinstance(change, AcceptChange):
                        if change.target_id in revisions_map:
                            revisions_map[change.target_id].Accept()
                            stats["applied"] += 1
                        else:
                            stats["failed"] += 1
                            stats["skipped_details"].append(
                                f"- Revision {change.target_id} not found or lost to drift."
                            )
                            logger.warning(f"Revision {change.target_id} not found or lost to drift.")

                    elif isinstance(change, RejectChange):
                        if change.target_id in revisions_map:
                            revisions_map[change.target_id].Reject()
                            stats["applied"] += 1
                        else:
                            stats["failed"] += 1
                            stats["skipped_details"].append(
                                f"- Revision {change.target_id} not found or lost to drift."
                            )
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
                            stats["skipped_details"].append(f"- Comment {change.target_id} not found.")
                            logger.warning(f"Comment {change.target_id} not found. {e}")

                except Exception as e:
                    stats["failed"] += 1
                    stats["skipped_details"].append(
                        f"- Failed to apply change {getattr(change, 'type', 'Unknown')}: {e}"
                    )
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
            res = f"Live Word Batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}."
            if "author_overridden_by_word" in stats:
                res += (
                    f"\n\nWarning: Live Word natively enforces M365 identities. "
                    f"The requested author_name ('{author_name}') may have been overridden "
                    f"by Word with the active user identity ('{stats['author_overridden_by_word']}')."
                )
            if stats.get("skipped_details"):
                res += "\n\nSkipped Details:\n" + "\n".join(stats["skipped_details"])
            return res
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

else:
    # Stubs for non-Windows platforms to satisfy static type checkers (mypy)
    from adeu.models import DocumentChange

    def _read_active_word_document_core(clean_view: bool = False) -> str:
        raise NotImplementedError("Live Word is only supported on Windows.")

    def _process_active_word_batch_core(changes: List[DocumentChange], author_name: str) -> dict[str, Any]:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def read_active_word_document(ctx: Context, clean_view: bool = False) -> ToolResult:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def process_active_word_batch(ctx: Context, changes: List[DocumentChange], author_name: str) -> str:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def open_word_document_impl(ctx: Context, file_path: str, visible: bool = True) -> str:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def save_active_word_document_impl(
        ctx: Context, output_path: Optional[str] = None, close: bool = False
    ) -> str:
        raise NotImplementedError("Live Word is only supported on Windows.")
