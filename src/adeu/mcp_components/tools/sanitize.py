# FILE: src/adeu/mcp_components/tools/sanitize.py
from pathlib import Path
from typing import Annotated, Optional

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool

from adeu.mcp_components.shared import _read_file_bytes


@tool(
    description=(
        "Sanitizes a DOCX file by stripping dangerous metadata (rsids, author names, "
        "template paths, DMS metadata, hidden text, orphaned content) and producing "
        "an audit report of everything removed. Use this before sending documents to "
        "external parties. Supports three modes: full scrub (for signing/closing), "
        "keep-markup (preserves your track changes and open comments), or baseline "
        "(recomputes your delta against the original document)."
    ),
    annotations={"destructiveHint": True},
)
async def sanitize_docx(
    file_path: Annotated[str, "Absolute path to the DOCX file to sanitize."],
    ctx: Context,
    output_path: Annotated[
        Optional[str],
        "Output path for the sanitized file. Defaults to <stem>_sanitized.docx.",
    ] = None,
    keep_markup: Annotated[
        bool,
        "Keep existing track changes and open comments. Strips resolved comments and all metadata. "
        "Use this when sending a redline to counterparty.",
    ] = False,
    baseline_path: Annotated[
        Optional[str],
        "Path to the original/baseline document. When provided, the tool recomputes your changes "
        "as a clean delta against this baseline. Use when Track Changes was off, or to collapse "
        "multiple rounds of markup into a single clean redline.",
    ] = None,
    author: Annotated[
        Optional[str],
        "Replace all author names on track changes and comments with this value. "
        "Used with keep_markup or baseline_path.",
    ] = None,
    accept_all: Annotated[
        bool,
        "Accept all unresolved track changes (full sanitize mode only). "
        "Required if the document contains unresolved changes. "
        "The report will list every change that was auto-accepted.",
    ] = False,
) -> dict:
    from adeu.sanitize.core import SanitizeError, sanitize_docx as _sanitize

    await ctx.info(
        f"Sanitizing document: {Path(file_path).name}",
        extra={
            "file_path": file_path,
            "keep_markup": keep_markup,
            "baseline_path": baseline_path,
            "author": author,
            "accept_all": accept_all,
        },
    )

    # Verify input file exists
    p = Path(file_path)
    if not p.exists():
        raise ToolError(f"File not found: {file_path}")

    if baseline_path and not Path(baseline_path).exists():
        raise ToolError(f"Baseline file not found: {baseline_path}")

    try:
        result = _sanitize(
            input_path=file_path,
            output_path=output_path,
            keep_markup=keep_markup,
            baseline_path=baseline_path,
            author=author,
            accept_all=accept_all,
        )

        await ctx.info(
            "Sanitization complete",
            extra={
                "output_path": result.output_path,
                "status": result.status,
                "tracked_changes_found": result.tracked_changes_found,
                "comments_removed": result.comments_removed,
            },
        )

        return {
            "output_path": result.output_path,
            "status": result.status,
            "tracked_changes_found": result.tracked_changes_found,
            "tracked_changes_accepted": result.tracked_changes_accepted,
            "comments_removed": result.comments_removed,
            "comments_kept": result.comments_kept,
            "metadata_stripped": result.metadata_stripped,
            "warnings": result.warnings,
            "report_text": result.report_text,
        }

    except SanitizeError as e:
        await ctx.warning(
            "Sanitization blocked",
            extra={"reason": str(e)},
        )
        raise ToolError(str(e)) from e

    except Exception as e:
        await ctx.error(
            "Sanitization failed",
            extra={"error": str(e)},
        )
        raise ToolError(f"Error sanitizing document: {str(e)}") from e
