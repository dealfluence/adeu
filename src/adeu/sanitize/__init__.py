"""
adeu sanitize — DOCX metadata scrubber.

Strips dangerous metadata from DOCX files and produces an audit report
proving what was removed.
"""

from adeu.sanitize.core import SanitizeMode, SanitizeResult, sanitize_docx

__all__ = ["sanitize_docx", "SanitizeResult", "SanitizeMode"]
