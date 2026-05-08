# FILE: src/adeu/mcp_components/tools/live_word.py
import io
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

import structlog
from docx import Document as load_document
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult

from adeu import RedlineEngine
from adeu.mcp_components._response_builders import (
    build_appendix_response,
    build_outline_response,
    build_paginated_response,
)
from adeu.models import DeleteTableRow, InsertTableRow
from adeu.redline.engine import validate_edit_strings
from adeu.redline.mapper import DocumentMapper, renumber_snapshot_ids

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    pass


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

    class LiveDocumentNotOpenError(Exception):
        """Raised when a specific file path is not found in the open Word documents."""

        pass

    from adeu.diff import trim_common_context
    from adeu.markup import _find_match_in_text
    from adeu.mcp_components.tools.live_word_ops import (
        apply_com_replacement,
        strip_critic_markup,
        strip_markdown_formatting,
    )
    from adeu.models import (
        AcceptChange,
        DocumentChange,
        ModifyText,
        RejectChange,
        ReplyComment,
    )

    def _get_word_doc(app: Any, file_path: Optional[str] = None) -> Any:
        """Gets the requested document from Word, or the ActiveDocument if no path provided."""
        if not file_path:
            try:
                return app.ActiveDocument
            except Exception as e:
                raise RuntimeError("No active document found in Word.") from e

        target_path = str(Path(file_path).resolve()).lower()
        for i in range(1, app.Documents.Count + 1):
            doc = app.Documents(i)
            if doc.FullName and str(Path(doc.FullName).resolve()).lower() == target_path:
                return doc

        raise LiveDocumentNotOpenError(f"Document {file_path} is not open in Word.")

    def _read_active_word_document_core(
        clean_view: bool = False,
        file_path: Optional[str] = None,
        include_appendix: bool = True,
        return_paragraph_offsets: bool = False,
    ) -> Tuple:
        """
        Reads the live active Word document (or specific open file) by extracting its
        Flat OPC XML via doc.WordOpenXML, wrapping it into an in-memory DOCX zip stream,
        and routing it through the same ingest pipeline used for disk files.

        Args:
            clean_view: simulate "Accept All Changes" view.
            include_appendix: when False, skip building the structural appendix.
                Callers that won't ship the appendix in their response (mode='full',
                mode='outline') should pass False to save ~8.5s on large docs.

        Returns (extracted_text, absolute_file_path, python_docx_document).

        The Document object is returned so callers that need structural traversal
        (e.g. outline mode) can reuse it without a second WordOpenXML extraction.
        Pagination-only callers can ignore the third element.

        This unifies the live and disk paths for both normal and clean_view reads
        and avoids the COM round-trip overhead that dominated the old character-by-
        character traversal.
        """
        from docx import Document as load_document

        from adeu.ingest import _extract_text_from_doc

        pythoncom.CoInitialize()
        try:
            app = win32com.client.GetActiveObject("Word.Application")
        except Exception as e:  # Catch pywintypes.com_error
            raise RuntimeError(f"Could not connect to active Word document. {e}") from e

        word_doc = _get_word_doc(app, file_path)
        xml_str = word_doc.WordOpenXML
        stream = _build_mock_docx_stream(xml_str)
        actual_path = word_doc.FullName

        py_doc = load_document(stream)

        # Bug 5: renumber snapshot IDs to the disk path's two-pool scheme so
        # an agent that reads via Live Word sees the same shape of Chg:N /
        # Com:N IDs that the disk path emits, eliminating cross-path
        # collision when the agent later targets a tracked change or comment.

        renumber_snapshot_ids(py_doc)

        # Caller may also request paragraph offsets for the fast outline path.
        # We piggyback on the same _extract_text_from_doc call.
        if return_paragraph_offsets:
            text, paragraph_offsets = _extract_text_from_doc(
                py_doc,
                clean_view=clean_view,
                include_appendix=include_appendix,
                return_paragraph_offsets=True,
            )
            return text, actual_path, py_doc, paragraph_offsets
        text = _extract_text_from_doc(py_doc, clean_view=clean_view, include_appendix=include_appendix)
        return text, actual_path, py_doc

    async def read_active_word_document(
        ctx: Context,
        clean_view: bool = False,
        file_path: Optional[str] = None,
        mode: str = "full",
        page: int = 1,
        outline_max_level: int = 2,
        outline_verbose: bool = False,
    ) -> ToolResult:
        await ctx.info(
            f"Extracting live Word document via WordOpenXML "
            f"(clean_view={clean_view}, path={file_path}, mode={mode}, page={page}, "
            f"outline_max_level={outline_max_level}, outline_verbose={outline_verbose})"
        )
        try:
            # Step 3: Only mode='appendix' actually consumes the structural appendix
            # in the response. Skip building it for the other modes (~8.5s saved on
            # large docs).
            needs_appendix = mode == "appendix"
            # Step 4 / Option A: outline mode uses paragraph offsets to avoid
            # re-projecting each paragraph.
            needs_offsets = mode == "outline"

            # Note: extraction errors (LiveDocumentNotOpenError, "Could not connect to
            # active Word", etc.) are NOT caught here. They propagate as their original
            # exception types so the disk-fallback dispatcher in document.py can
            # distinguish "Word doesn't have this doc open, try disk" from
            # "Live Word read it fine but the request was invalid (e.g. page OOR)".
            paragraph_offsets = None
            if needs_offsets:
                final_text, actual_path, py_doc, paragraph_offsets = _read_active_word_document_core(
                    clean_view,
                    file_path,
                    include_appendix=needs_appendix,
                    return_paragraph_offsets=True,
                )
            else:
                final_text, actual_path, py_doc = _read_active_word_document_core(
                    clean_view, file_path, include_appendix=needs_appendix
                )
            await ctx.info(f"Live Word extraction successful: {len(final_text)} characters.")

            try:
                if mode == "outline":
                    res = build_outline_response(
                        py_doc,
                        final_text,
                        actual_path,
                        outline_max_level=outline_max_level,
                        outline_verbose=outline_verbose,
                        paragraph_offsets=paragraph_offsets,
                    )
                elif mode == "appendix":
                    res = build_appendix_response(final_text, page, actual_path)
                else:
                    res = build_paginated_response(final_text, page, actual_path)
            except ToolError:
                # Post-extraction errors (e.g. page out of range) propagate as-is —
                # the document was read successfully; the user's request was bad.
                raise
            except Exception as e:
                raise ToolError(str(e)) from e

            return res
        except ToolError:
            raise

    def _resolve_com_revision(doc: Any, xml_id: str, mapper: Any, mapping: List[int]) -> Any:
        """Finds the COM Revision by combining Semantic Text Matching with Physical Proximity."""
        spans = [s for s in mapper.spans if s.ins_id == xml_id or s.del_id == xml_id]
        if not spans:
            return None

        virt_start = spans[0].start
        target_text = "".join(s.text for s in spans)
        clean_target = "".join(c.lower() for c in target_text if c.isalnum())

        # Guess the COM coordinate using the map
        approx_com_start = mapping[virt_start] if virt_start < len(mapping) else mapping[-1]

        best_match = None
        best_score = float("inf")

        for i in range(1, doc.Revisions.Count + 1):
            rev = doc.Revisions(i)
            try:
                com_text = rev.Range.Text
                clean_com = "".join(c.lower() for c in com_text if c.isalnum())

                is_match = False
                if not clean_target and not clean_com:
                    is_match = True
                elif clean_target and clean_com and (clean_target in clean_com or clean_com in clean_target):
                    is_match = True

                if is_match:
                    dist = abs(rev.Range.Start - approx_com_start)
                    if dist < best_score:
                        best_score = dist
                        best_match = rev
            except Exception:
                pass
        return best_match

    def _resolve_com_comment(doc: Any, xml_id: str, mapper: Any) -> Any:
        """Finds the COM Comment by matching its semantic text."""
        if xml_id not in mapper.comments_map:
            return None
        target_text = mapper.comments_map[xml_id]["text"]
        clean_target = "".join(c.lower() for c in target_text if c.isalnum())

        for i in range(1, doc.Comments.Count + 1):
            c = doc.Comments(i)
            try:
                clean_com = "".join(ch.lower() for ch in c.Range.Text if ch.isalnum())
                if clean_target == clean_com or clean_target in clean_com or clean_com in clean_target:
                    return c
            except Exception:
                pass
        return None

    def _process_active_word_batch_core(
        changes: List[DocumentChange], author_name: str, file_path: Optional[str] = None
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {"applied": 0, "failed": 0, "skipped_details": []}
        if not changes:
            return stats

        if not author_name or not author_name.strip():
            raise ValueError("author_name cannot be empty.")

        pythoncom.CoInitialize()
        try:
            app = win32com.client.GetActiveObject("Word.Application")
        except Exception as e:
            raise RuntimeError(f"Could not connect to active Word document. {e}") from e

        doc = _get_word_doc(app, file_path)

        original_track_revisions = doc.TrackRevisions
        doc.TrackRevisions = True

        original_user = app.UserName
        app.UserName = author_name

        has_local_user_info = False
        original_use_local_info = False
        try:
            if hasattr(app.Options, "UseLocalUserInfo"):
                has_local_user_info = True
                original_use_local_info = app.Options.UseLocalUserInfo
                app.Options.UseLocalUserInfo = True
        except Exception:
            pass

        original_smart_cut_paste = True
        try:
            if hasattr(app.Options, "SmartCutPaste"):
                original_smart_cut_paste = app.Options.SmartCutPaste
                app.Options.SmartCutPaste = False
        except Exception as e:
            logger.warning(f"Could not disable SmartCutPaste: {e}")

        if not has_local_user_info:
            stats["author_overridden_by_word"] = original_user

        cached_raw_text: Optional[str] = None
        cached_current_text: Optional[str] = None
        cached_mapping: Optional[List[int]] = None

        def _get_haystack() -> Tuple[str, str, List[int]]:
            nonlocal cached_raw_text, cached_current_text, cached_mapping
            if cached_raw_text is None:
                cached_raw_text = doc.Content.Text
                cached_current_text, cached_mapping = _clean_chars(cached_raw_text)
            assert cached_raw_text is not None
            assert cached_current_text is not None
            assert cached_mapping is not None
            return cached_raw_text, cached_current_text, cached_mapping

        def _invalidate_haystack() -> None:
            nonlocal cached_raw_text, cached_current_text, cached_mapping
            cached_raw_text = None
            cached_current_text = None
            cached_mapping = None

        actions = [c for c in changes if isinstance(c, (AcceptChange, RejectChange, ReplyComment))]
        edits = [c for c in changes if isinstance(c, (ModifyText, InsertTableRow, DeleteTableRow))]

        # Category A: document-context-free string-shape validation.
        category_a_errors = validate_edit_strings(edits)
        if category_a_errors:
            stats["failed"] = len(category_a_errors)
            stats["skipped_details"].extend(category_a_errors)
            return stats

        # Category B: document-aware validation (target found, unambiguous,
        # not in Structural Appendix). Build a snapshot of the live document
        # via the same Flat OPC -> python-docx pipeline used by the read path,
        # then run RedlineEngine.validate_edits against it.
        if edits:
            try:
                xml_str = doc.WordOpenXML
                snapshot_stream = _build_mock_docx_stream(xml_str)
                snapshot_engine = RedlineEngine(snapshot_stream, author=author_name)
                # Bug 5: renumber the snapshot's IDs so that any error messages
                # mention the same Chg:N / Com:N values the agent saw when it
                # last read the document via Live Word.
                renumber_snapshot_ids(snapshot_engine.doc)
                # Force the mapper to rebuild against the renumbered doc.
                snapshot_engine.mapper = type(snapshot_engine.mapper)(snapshot_engine.doc)
                category_b_errors = snapshot_engine.validate_edits(edits)
            except Exception as e:
                logger.warning(f"Could not run Category B validation: {e}")
                category_b_errors = []

            if category_b_errors:
                stats["failed"] = len(category_b_errors)
                stats["skipped_details"].extend(category_b_errors)
                return stats

        # --- Validation passed; proceed with COM-based application ---
        try:
            # --- FIX 1: PROCESS ACTIONS FIRST & SURVIVE DRIFT ---
            if actions:
                # Build virtual map to translate the LLM's Chg:N IDs
                xml_str = doc.WordOpenXML
                stream = _build_mock_docx_stream(xml_str)
                py_doc = load_document(stream)
                # Bug 5: renumber to the disk-style two-pool scheme so the
                # agent's Chg:N / Com:N target_id values resolve to the same
                # XML elements the agent saw when reading via Live Word.
                renumber_snapshot_ids(py_doc)
                mapper = DocumentMapper(py_doc)
                _, _, mapping = _get_haystack()

                for act in actions:
                    try:
                        xml_id = act.target_id.split(":")[-1]
                        if isinstance(act, (AcceptChange, RejectChange)):
                            rev = _resolve_com_revision(doc, xml_id, mapper, mapping)
                            if rev:
                                if isinstance(act, AcceptChange):
                                    rev.Accept()
                                else:
                                    rev.Reject()
                                stats["applied"] += 1
                            else:
                                stats["failed"] += 1
                                stats["skipped_details"].append(
                                    f"- Revision {act.target_id} not found or lost to drift."
                                )
                        elif isinstance(act, ReplyComment):
                            com = _resolve_com_comment(doc, xml_id, mapper)
                            if com:
                                try:
                                    com.Replies.Add(com.Range, act.text)
                                except Exception:
                                    doc.Comments.Add(com.Range, act.text)
                                stats["applied"] += 1
                            else:
                                stats["failed"] += 1
                                stats["skipped_details"].append(f"- Comment {act.target_id} not found.")
                    except Exception as e:
                        stats["failed"] += 1
                        stats["skipped_details"].append(f"- Failed to apply action {act.type}: {e}")

                if stats["applied"] > 0:
                    _invalidate_haystack()

            # --- PROCESS EDITS ---
            for change in edits:
                try:
                    if isinstance(change, (InsertTableRow, DeleteTableRow)):
                        stats["failed"] += 1
                        stats["skipped_details"].append(
                            f"- Structural table edits ({change.type}) are currently only "
                            "supported for disk-based DOCX files."
                        )
                        continue

                    if isinstance(change, ModifyText):
                        clean_target = strip_markdown_formatting(strip_critic_markup(change.target_text))
                        raw_text, current_text, mapping = _get_haystack()
                        start_idx, end_idx = _find_match_in_text(current_text, clean_target)

                        if start_idx != -1:
                            # --- AMBIGUITY CHECK (uses shared formatter for parity with disk path) ---
                            from adeu.markup import format_ambiguity_error

                            # Enumerate ALL matches, not just the second one,
                            # so we can produce the same multi-example message
                            # the disk path produces.
                            all_positions: list[tuple[int, int]] = [(start_idx, end_idx)]
                            search_offset = end_idx
                            while True:
                                rel_start, rel_end = _find_match_in_text(current_text[search_offset:], clean_target)
                                if rel_start == -1:
                                    break
                                abs_start = search_offset + rel_start
                                abs_end = search_offset + rel_end
                                all_positions.append((abs_start, abs_end))
                                search_offset = abs_end

                            if len(all_positions) > 1:
                                stats["failed"] += 1
                                # The Live Word edit loop tracks the index of
                                # the current change in `change`. We mirror the
                                # disk path's 1-based numbering using the
                                # position of `change` in the edits list.
                                edit_index = edits.index(change) + 1
                                stats["skipped_details"].append(
                                    format_ambiguity_error(
                                        edit_index=edit_index,
                                        target_text=change.target_text,
                                        haystack=current_text,
                                        match_positions=all_positions,
                                    )
                                )
                                continue

                            is_table_edit = "|" in clean_target
                            table_edit_success = False

                            if is_table_edit:
                                t_cells = [c.strip() for c in change.target_text.split("|")]
                                n_cells = [c.strip() for c in (change.new_text or "").split("|")]

                                if len(t_cells) == len(n_cells):
                                    anchor_idx = -1
                                    anchor_text = ""
                                    for i, c in enumerate(t_cells):
                                        if c:
                                            anchor_idx = i
                                            anchor_text = c
                                            break

                                    if anchor_idx != -1:
                                        clean_anchor = strip_markdown_formatting(strip_critic_markup(anchor_text))
                                        local_anchor_start = clean_target.find(clean_anchor)
                                        if local_anchor_start == -1:
                                            local_anchor_start = 0

                                        anchor_start_idx = start_idx + local_anchor_start
                                        anchor_end_idx = anchor_start_idx + len(clean_anchor)

                                        actual_anchor_start = mapping[anchor_start_idx]
                                        actual_anchor_end = mapping[anchor_end_idx]

                                        exact_anchor_substring = raw_text[actual_anchor_start:actual_anchor_end]

                                        search_start = max(0, actual_anchor_start - 5000)
                                        search_end = min(doc.Content.End, actual_anchor_end + 5000)
                                        rng = doc.Range(Start=search_start, End=search_end)

                                        search_text = (
                                            exact_anchor_substring[:250]
                                            if len(exact_anchor_substring) > 250
                                            else exact_anchor_substring
                                        )
                                        rng.Find.ClearFormatting()
                                        rng.Find.Text = search_text
                                        rng.Find.Forward = True
                                        rng.Find.Wrap = 0

                                        if rng.Find.Execute() and rng.Information(12):
                                            table_edit_success = True
                                            anchor_cell = rng.Cells(1)

                                            target_comment_idx = 0
                                            for i, (t, n) in enumerate(zip(t_cells, n_cells, strict=True)):
                                                if t != n:
                                                    target_comment_idx = i
                                                    break

                                            cells_updated = 0
                                            for i in range(len(t_cells)):
                                                t_c = t_cells[i]
                                                n_c = n_cells[i]

                                                should_comment = (change.comment is not None) and (
                                                    i == target_comment_idx
                                                )

                                                if t_c != n_c or should_comment:
                                                    target_cell = anchor_cell
                                                    diff = i - anchor_idx
                                                    if diff > 0:
                                                        for _ in range(diff):
                                                            if target_cell:
                                                                target_cell = target_cell.Next
                                                    elif diff < 0:
                                                        for _ in range(-diff):
                                                            if target_cell:
                                                                target_cell = target_cell.Previous

                                                    if not target_cell:
                                                        continue

                                                    cell_rng = target_cell.Range
                                                    cell_rng.End -= 1

                                                    actual_start = cell_rng.Start
                                                    actual_end = cell_rng.End
                                                    exact_substring = cell_rng.Text

                                                    if not t_c:
                                                        actual_end = actual_start
                                                        exact_substring = ""

                                                    if t_c == n_c:
                                                        if should_comment:
                                                            try:
                                                                doc.Comments.Add(
                                                                    cell_rng,
                                                                    change.comment,
                                                                )
                                                            except Exception as e:
                                                                logger.warning(f"Failed to attach comment to cell: {e}")
                                                        cells_updated += 1
                                                        continue

                                                    (
                                                        final_start,
                                                        final_end,
                                                        final_new_text,
                                                    ) = _shrink_replacement_range(
                                                        exact_substring,
                                                        n_c,
                                                        actual_start,
                                                        actual_end,
                                                        t_c,
                                                    )

                                                    replace_rng = doc.Range(Start=final_start, End=final_end)
                                                    apply_com_replacement(
                                                        doc,
                                                        app,
                                                        replace_rng,
                                                        final_new_text,
                                                        (change.comment if should_comment else None),
                                                    )
                                                    cells_updated += 1

                                            if cells_updated > 0:
                                                stats["applied"] += 1
                                                _invalidate_haystack()

                            if not table_edit_success:
                                actual_start = mapping[start_idx]
                                actual_end = mapping[end_idx]
                                exact_substring = raw_text[actual_start:actual_end]

                                search_start = max(0, actual_start - 5000)
                                search_end = min(doc.Content.End, actual_end + 5000)
                                rng = doc.Range(Start=search_start, End=search_end)

                                search_text = exact_substring[:250] if len(exact_substring) > 250 else exact_substring

                                rng.Find.ClearFormatting()
                                rng.Find.Text = search_text
                                rng.Find.Forward = True
                                rng.Find.Wrap = 0

                                if rng.Find.Execute():
                                    actual_start = rng.Start
                                    actual_end = actual_start + len(exact_substring)

                                    effective_new = change.new_text or ""

                                    if change.target_text == effective_new:
                                        if change.comment:
                                            replace_rng = doc.Range(Start=actual_start, End=actual_end)
                                            try:
                                                doc.Comments.Add(replace_rng, change.comment)
                                            except Exception as e:
                                                logger.warning(f"Failed to attach comment for same->same edit: {e}")
                                        stats["applied"] += 1
                                        _invalidate_haystack()
                                        continue

                                    actual_start, actual_end, final_new_text = _shrink_replacement_range(
                                        exact_substring,
                                        effective_new,
                                        actual_start,
                                        actual_end,
                                        change.target_text,
                                    )

                                    replace_rng = doc.Range(Start=actual_start, End=actual_end)
                                    apply_com_replacement(
                                        doc,
                                        app,
                                        replace_rng,
                                        final_new_text,
                                        change.comment,
                                    )
                                    stats["applied"] += 1
                                    _invalidate_haystack()
                                else:
                                    doc_rng = doc.Content
                                    doc_rng.Find.ClearFormatting()
                                    doc_rng.Find.Text = search_text
                                    if doc_rng.Find.Execute():
                                        replace_rng = doc.Range(
                                            Start=doc_rng.Start,
                                            End=doc_rng.Start + len(exact_substring),
                                        )

                                        effective_new = change.new_text or ""
                                        if change.target_text == effective_new:
                                            if change.comment:
                                                try:
                                                    doc.Comments.Add(replace_rng, change.comment)
                                                except Exception as e:
                                                    logger.warning(
                                                        f"Failed to attach comment for same->same fallback edit: {e}"
                                                    )
                                            stats["applied"] += 1
                                            _invalidate_haystack()
                                            continue

                                        actual_start, actual_end, final_new_text = _shrink_replacement_range(
                                            exact_substring,
                                            effective_new,
                                            doc_rng.Start,
                                            doc_rng.Start + len(exact_substring),
                                            change.target_text,
                                        )

                                        replace_rng = doc.Range(Start=actual_start, End=actual_end)
                                        apply_com_replacement(
                                            doc,
                                            app,
                                            replace_rng,
                                            final_new_text,
                                            change.comment,
                                        )
                                        stats["applied"] += 1
                                        _invalidate_haystack()
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

                except Exception as e:
                    stats["failed"] += 1
                    stats["skipped_details"].append(
                        f"- Failed to apply change {getattr(change, 'type', 'Unknown')}: {e}"
                    )
                    logger.error(f"Failed to apply change {getattr(change, 'type', 'Unknown')}: {e}")

        finally:
            app.UserName = original_user
            if has_local_user_info:
                try:
                    app.Options.UseLocalUserInfo = original_use_local_info
                except Exception:
                    pass
            try:
                if hasattr(app.Options, "SmartCutPaste"):
                    app.Options.SmartCutPaste = original_smart_cut_paste
            except Exception:
                pass
            doc.TrackRevisions = original_track_revisions

        return stats

    def _shrink_replacement_range(
        exact_substring: str,
        effective_new: str,
        actual_start: int,
        actual_end: int,
        target_text_markdown: str,
    ):
        if exact_substring == effective_new:
            return actual_start, actual_end, effective_new

        p_len_md, s_len_md = trim_common_context(target_text_markdown, effective_new)

        # Isolate the exact markdown hunks
        t_hunk = target_text_markdown[
            p_len_md : (len(target_text_markdown) - s_len_md if s_len_md else len(target_text_markdown))
        ]
        n_hunk = effective_new[p_len_md : len(effective_new) - s_len_md if s_len_md else len(effective_new)]

        # Build offset map for exact_substring -> normalized current_text format
        norm_exact = ""
        map_norm_to_exact = []
        i = 0
        while i < len(exact_substring):
            if exact_substring[i : i + 4] == "\r\x07\r\x07":
                norm_exact += "\n"
                map_norm_to_exact.append(i)
                i += 4
            elif exact_substring[i : i + 2] == "\r\x07":
                norm_exact += " | "
                map_norm_to_exact.extend([i, i, i])
                i += 2
            elif exact_substring[i] == "\x07":
                norm_exact += " | "
                map_norm_to_exact.extend([i, i, i])
                i += 1
            elif exact_substring[i] == "\r":
                norm_exact += "\n"
                map_norm_to_exact.append(i)
                i += 1
            elif exact_substring[i] == "\x0b":
                norm_exact += "\n"
                map_norm_to_exact.append(i)
                i += 1
            else:
                norm_exact += exact_substring[i]
                map_norm_to_exact.append(i)
                i += 1
        map_norm_to_exact.append(len(exact_substring))

        # Calculate prefix length in the normalized space
        md_prefix = target_text_markdown[:p_len_md]
        clean_prefix = strip_markdown_formatting(strip_critic_markup(md_prefix))
        clean_prefix = clean_prefix.replace("\n\n", "\n")
        p_len_norm = len(clean_prefix)

        # Calculate match length in the normalized space
        clean_t_hunk = strip_markdown_formatting(strip_critic_markup(t_hunk))
        clean_t_hunk = clean_t_hunk.replace("\n\n", "\n")
        match_len_norm = len(clean_t_hunk)

        # Map back to exact_substring bounds safely
        original_actual_start = actual_start
        if p_len_norm < len(map_norm_to_exact) and (p_len_norm + match_len_norm) < len(map_norm_to_exact):
            actual_start = original_actual_start + map_norm_to_exact[p_len_norm]
            actual_end = original_actual_start + map_norm_to_exact[p_len_norm + match_len_norm]
            return actual_start, actual_end, n_hunk

        return actual_start, actual_end, effective_new

    def _clean_chars(raw_text: str) -> Tuple[str, List[int]]:
        i = 0
        clean_chars = []
        mapping = []
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
        return "".join(clean_chars), mapping

    def _process_accept_change(stats, revisions_map, change):
        if change.target_id in revisions_map:
            revisions_map[change.target_id].Accept()
            stats["applied"] += 1
        else:
            stats["failed"] += 1
            stats["skipped_details"].append(f"- Revision {change.target_id} not found or lost to drift.")
            logger.warning(f"Revision {change.target_id} not found or lost to drift.")

    def _process_reject_change(stats, revisions_map, change):
        if change.target_id in revisions_map:
            revisions_map[change.target_id].Reject()
            stats["applied"] += 1
        else:
            stats["failed"] += 1
            stats["skipped_details"].append(f"- Revision {change.target_id} not found or lost to drift.")
            logger.warning(f"Revision {change.target_id} not found or lost to drift.")

    def _process_reply_comment(stats, doc, change):
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

    async def process_active_word_batch(
        ctx: Context,
        changes: List[DocumentChange],
        author_name: str,
        file_path: Optional[str] = None,
    ) -> str:
        if not changes:
            return "No changes provided."

        if not author_name or not author_name.strip():
            return "Error: author_name cannot be empty."

        await ctx.info(f"Applying {len(changes)} changes to live Word document...")
        try:
            stats = _process_active_word_batch_core(changes, author_name, file_path)
            await ctx.info(f"Live Word batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}.")
            res = f"[Live Word Mode] Batch complete. Applied: {stats['applied']}, Failed: {stats['failed']}."
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

    def _read_active_word_document_core(
        clean_view: bool = False,
        file_path: Optional[str] = None,
        include_appendix: bool = True,
        return_paragraph_offsets: bool = False,
    ) -> Tuple:
        raise NotImplementedError("Live Word is only supported on Windows.")

    def _process_active_word_batch_core(
        changes: List[DocumentChange], author_name: str, file_path: Optional[str] = None
    ) -> dict[str, Any]:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def read_active_word_document(
        ctx: Context,
        clean_view: bool = False,
        file_path: Optional[str] = None,
        mode: str = "full",
        page: int = 1,
        outline_max_level: int = 2,
        outline_verbose: bool = False,
    ) -> ToolResult:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def process_active_word_batch(
        ctx: Context,
        changes: List[DocumentChange],
        author_name: str,
        file_path: Optional[str] = None,
    ) -> str:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def open_word_document_impl(ctx: Context, file_path: str, visible: bool = True) -> str:
        raise NotImplementedError("Live Word is only supported on Windows.")

    async def save_active_word_document_impl(
        ctx: Context, output_path: Optional[str] = None, close: bool = False
    ) -> str:
        raise NotImplementedError("Live Word is only supported on Windows.")
