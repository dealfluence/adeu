import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from docx import Document

from adeu.diff import generate_edits_via_paragraph_alignment
from adeu.ingest import _extract_text_from_doc
from adeu.models import DocumentChange
from adeu.redline.engine import RedlineEngine
from adeu.utils.docx import strip_bom_from_docx_bytes

_CRITICMARKUP_TOKENS = (
    "{++",
    "++}",
    "{--",
    "--}",
    "{~~",
    "~>",
    "~~}",
    "{==",
    "==}",
    "{>>",
    "<<}",
)

_EXTRACT_HEADER_RE = re.compile(r"^> \*\*File Path:\*\*[^\n]*\n+")
_PAGE_BANNER_RE = re.compile(r"^> \*\*Page (\d+) of (\d+)\*\*[^\n]*\n+(?:---\n+)?")
_PAGE_FOOTER_RE = re.compile(r"\n+---\n+> \*\*Continues on page (\d+) of (\d+)\.\*\*[^\n]*\s*$")
_APPENDIX_POINTER_RE = re.compile(r"\n+---\n+> \*\*Appendix available\.\*\*[^\n]*\s*$")

_MACHINE_ACCOUNT_NAMES = {"root", "admin", "administrator", "system", "daemon", "nobody"}


class TextRevisionError(Exception):
    """Base exception for text revision errors."""

    pass


class TextRevisionVerificationError(TextRevisionError):
    """Raised when clean-text post-apply verification fails."""

    def __init__(self, message: str, unverified_path: Path, output_path: Path, stats: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.unverified_path = unverified_path
        self.output_path = output_path
        self.stats = stats or {}


def get_default_author(author: Optional[str] = None) -> str:
    """Returns the effective author name for track changes."""
    if author and author.strip():
        return author.strip()
    env_author = (os.environ.get("ADEU_AUTHOR") or "").strip()
    if env_author:
        return env_author
    try:
        import getpass

        user = getpass.getuser()
    except Exception:
        return "Adeu AI"
    if not user or user.strip().lower() in _MACHINE_ACCOUNT_NAMES:
        return "Adeu AI"
    return user


def check_criticmarkup(text: str) -> None:
    """Refuses revised text input if it contains CriticMarkup syntax."""
    if any(tok in text for tok in _CRITICMARKUP_TOKENS):
        raise ValueError(
            "Revised text contains CriticMarkup tokens ({++..++}, {--..--}, {~~..~>..~~}, {==..==}, "
            "{>>..<<}). `apply_text_revision` compares text against the document's CLEAN view, "
            "so CriticMarkup tokens would be diffed into the document as literal prose."
        )


def _strip_page_chrome(text: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Strips extract header, banners, and footers from text input."""
    text = _EXTRACT_HEADER_RE.sub("", text, count=1)
    page = total = None
    banner = _PAGE_BANNER_RE.match(text)
    if banner:
        page, total = int(banner.group(1)), int(banner.group(2))
        text = text[banner.end() :]
    text = _APPENDIX_POINTER_RE.sub("", text)
    footer = _PAGE_FOOTER_RE.search(text)
    if footer:
        if page is None:
            page = int(footer.group(1)) - 1
        if total is None:
            total = int(footer.group(2))
        text = text[: footer.start()]
    return text, page, total


def check_major_deletions(
    original_text: str,
    revised_text: str,
    allow_major_deletions: bool = False,
) -> None:
    """Refuses major deletions (>30% characters or >50 paragraphs) unless allowed."""
    if allow_major_deletions:
        return

    orig_len = len(original_text)
    rev_len = len(revised_text)

    if orig_len > 0:
        char_deletion_ratio = (orig_len - rev_len) / orig_len
        if char_deletion_ratio > 0.30:
            pct = int(char_deletion_ratio * 100)
            raise ValueError(
                f"Revised text is ~{pct}% shorter than the document's clean text "
                f"({rev_len:,} vs {orig_len:,} characters, threshold is >30% deletion). "
                "Applying it would delete a major portion of the document. "
                "If this mass deletion is intentional, pass allow_major_deletions=True."
            )

    orig_paras = len(original_text.splitlines())
    rev_paras = len(revised_text.splitlines())
    paras_deleted = orig_paras - rev_paras
    if paras_deleted > 50:
        raise ValueError(
            f"Revised text deletes {paras_deleted} paragraphs from the original document "
            "(threshold is >50 paragraphs deleted). "
            "If this mass deletion is intentional, pass allow_major_deletions=True."
        )


def _extract_clean_text_from_doc(doc: Any) -> str:
    """Extracts clean accepted text from a DOCX Document object."""
    res = _extract_text_from_doc(doc, clean_view=True, include_appendix=False)
    if isinstance(res, tuple):
        return str(res[0])
    return str(res)


def _normalize_virtual_projection_text(text: str) -> str:
    """Normalizes Markdown heading chrome for clean-text verification."""
    return re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)


def verify_clean_text(
    engine_doc: Any,
    expected_text: str,
) -> Tuple[bool, Optional[str]]:
    """Verifies that the clean view of engine_doc matches expected_text."""
    actual_clean = _extract_clean_text_from_doc(engine_doc)
    expected = expected_text.strip()
    actual = actual_clean.strip()

    actual_norm = _normalize_virtual_projection_text(actual)
    expected_norm = _normalize_virtual_projection_text(expected)

    if actual_norm != expected_norm:
        div = next(
            (k for k, (a, b) in enumerate(zip(actual_norm, expected_norm, strict=False)) if a != b),
            min(len(actual_norm), len(expected_norm)),
        )
        msg = (
            "Post-apply verification failed: the applied document's clean text does not match "
            f"the supplied text (first divergence at character {div}: "
            f"applied reads {actual_norm[div : div + 40]!r}, supplied text reads "
            f"{expected_norm[div : div + 40]!r}). The document structure could not fully realize "
            "the requested text (e.g. headings or table cells cannot be deleted via text replacement)."
        )
        return False, msg
    return True, None


def _load_docx_from_path(path: Union[str, Path]) -> Tuple[Any, bytes]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"DOCX file not found: {p}")
    raw_bytes = p.read_bytes()
    sanitized_bytes = strip_bom_from_docx_bytes(raw_bytes)
    doc = Document(BytesIO(sanitized_bytes))
    return doc, sanitized_bytes


def _write_output_bytes(path: Path, data: bytes) -> None:
    import tempfile

    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent or "."))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fb:
            fb.write(data)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def apply_text_revision_core(
    file_path: Union[str, Path],
    revised_text: str,
    output_path: Optional[Union[str, Path]] = None,
    author: Optional[str] = None,
    allow_major_deletions: bool = False,
) -> Tuple[Dict[str, Any], Path]:
    """Core whole-text diff->tracked-changes primitive with clean-text verification gate."""
    p_input = Path(file_path)
    text_clean_input, page, total = _strip_page_chrome(revised_text)
    if total is not None and total > 1:
        raise ValueError(
            f"Text revision looks like page {page or '?'} of {total} of a paginated extract — "
            "it contains only part of the document, and applying it would delete every page "
            "not present. Re-extract the ENTIRE document first with --page all --clean-view."
        )

    check_criticmarkup(text_clean_input)

    doc, doc_bytes = _load_docx_from_path(p_input)
    text_orig = _extract_clean_text_from_doc(doc)

    check_major_deletions(text_orig, text_clean_input, allow_major_deletions=allow_major_deletions)

    changes: list[DocumentChange] = list(generate_edits_via_paragraph_alignment(text_orig, text_clean_input))

    author_name = get_default_author(author)
    engine = RedlineEngine(BytesIO(doc_bytes), author=author_name)
    stats = engine.process_batch(changes)

    target_output: Path
    if output_path:
        target_output = Path(output_path)
    else:
        if p_input.stem.endswith("_redlined") or p_input.stem.endswith("_processed"):
            target_output = p_input
        else:
            target_output = p_input.with_name(f"{p_input.stem}_redlined.docx")

    verified, err_msg = verify_clean_text(engine.doc, text_clean_input)

    if not verified:
        unverified_path = target_output.with_name(f"{target_output.stem}.unverified.docx")
        unverified_bytes = engine.save_to_stream().getvalue()
        _write_output_bytes(unverified_path, unverified_bytes)

        full_err = (
            f"{err_msg} Nothing was written to '{target_output}'; a diagnostic copy was kept "
            f"at '{unverified_path}' — it is NOT the requested document."
        )
        stats["verified"] = False
        stats["verification_error"] = full_err
        stats["edits_skipped"] = stats.get("edits_applied", 0) + stats.get("edits_skipped", 0)
        stats["edits_applied"] = 0
        stats["actions_skipped"] = stats.get("actions_applied", 0) + stats.get("actions_skipped", 0)
        stats["actions_applied"] = 0
        stats["output_path"] = None
        stats["unverified_output_path"] = str(unverified_path)
        if stats.get("edits"):
            for report in stats["edits"]:
                report["status"] = "failed"
                report["error"] = "Not applied: post-apply verification failed."
                report["critic_markup"] = None
                report["clean_text"] = None

        raise TextRevisionVerificationError(
            full_err, unverified_path=unverified_path, output_path=target_output, stats=stats
        )

    # Save output
    output_bytes = engine.save_to_stream().getvalue()
    _write_output_bytes(target_output, output_bytes)

    stats["output_path"] = str(target_output)
    stats["verified"] = True

    return stats, target_output
