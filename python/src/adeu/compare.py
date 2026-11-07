"""
One-step comparison: the difference between two DOCX files written as a
tracked-changes redline of the original.

Shared by `adeu diff a.docx b.docx -o out.docx`, the MCP
`diff_docx_files(output_path=...)` tool and LangChain's `adeu_diff_docx`, so
the three surfaces apply the same edits under the same two rules:

  - Transactional: a computed change that cannot be applied raises and
    nothing is written. A partial redline silently shows the skipped
    passages as unchanged, the false-success shape `adeu apply` and sanitize
    already refuse.
  - Never over an input: the redline may not replace either document it was
    computed from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import List, Sequence, Union

from docx import Document

from adeu.diff import DiffEdit, collect_media_difference_warnings, generate_structured_edits
from adeu.ingest import _extract_text_from_doc
from adeu.redline.engine import RedlineEngine

PathLike = Union[str, "os.PathLike[str]"]


class ComparisonError(Exception):
    """The comparison cannot be written as a faithful redline; nothing was saved."""

    def __init__(self, message: str, details: Sequence[str] = ()):
        super().__init__(message)
        self.details = list(details)


@dataclass
class RedlineComparison:
    data: bytes
    applied: int
    warnings: List[str] = field(default_factory=list)


def _same_file(a: PathLike, b: PathLike) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def refuse_input_alias(output_path: PathLike, original_path: PathLike, modified_path: PathLike) -> None:
    """Raises ComparisonError when `output_path` names either input document."""
    for path, role in ((original_path, "original"), (modified_path, "modified")):
        if _same_file(output_path, path):
            raise ComparisonError(
                f"output_path '{output_path}' is the same file as the {role} document; writing the redline "
                "there would destroy it. Choose a different output path."
            )


def apply_comparison_edits(original: bytes, edits: Sequence[DiffEdit], author: str) -> "tuple[bytes, int]":
    """Applies diff-generated edits to `original` as tracked changes; all of them or none."""
    engine = RedlineEngine(BytesIO(original), author=author)
    applied, skipped = engine.apply_edits(list(edits))  # type: ignore[arg-type]
    if skipped:
        raise ComparisonError(
            f"{skipped} of {len(edits)} computed change(s) could not be written as tracked changes, so no "
            "redline was saved: a partial redline would show those passages as unchanged.",
            engine.skipped_details,
        )
    return engine.save_to_stream().getvalue(), applied


def build_redline_comparison(
    original: bytes, modified: bytes, author: str, compare_clean: bool = True
) -> RedlineComparison:
    """Diffs two DOCX packages part by part and writes the result as a redline of `original`."""
    # Appendix excluded: its generated text is not writable content (QA 2026-07-18 H1).
    text_o, struct_o = _extract_text_from_doc(
        Document(BytesIO(original)), clean_view=compare_clean, include_appendix=False, return_structure=True
    )
    text_m, struct_m = _extract_text_from_doc(
        Document(BytesIO(modified)), clean_view=compare_clean, include_appendix=False, return_structure=True
    )
    edits, warnings = generate_structured_edits(text_o, struct_o, text_m, struct_m)  # type: ignore[arg-type]
    warnings = list(warnings) + collect_media_difference_warnings(original, modified)
    data, applied = apply_comparison_edits(original, edits, author)
    return RedlineComparison(data=data, applied=applied, warnings=warnings)
