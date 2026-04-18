"""
Sanitize report generation.

Produces human-readable reports showing what was stripped, what was kept,
and any warnings — enabling review and sign-off before sending.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SanitizeReport:
    """Collects report data during sanitization, then renders as text."""

    filename: str
    mode: str = "full"  # "full", "keep-markup", "baseline"
    author: Optional[str] = None

    # Tracked changes
    tracked_changes_found: int = 0
    tracked_changes_accepted: int = 0
    tracked_changes_kept: int = 0
    change_lines: list[str] = field(default_factory=list)

    # Comments
    comments_removed: int = 0
    comments_kept: int = 0
    removed_comment_lines: list[str] = field(default_factory=list)
    kept_comment_lines: list[str] = field(default_factory=list)

    # Metadata
    metadata_lines: list[str] = field(default_factory=list)

    # Structural
    structural_lines: list[str] = field(default_factory=list)

    # Warnings
    warnings: list[str] = field(default_factory=list)

    # Status
    status: str = "clean"  # "clean", "clean_with_warnings", "blocked"
    blocked_reason: Optional[str] = None

    def add_transform_lines(self, lines: list[str]):
        """Route transform output to the appropriate report section."""
        for line in lines:
            lower = line.lower()
            if any(k in lower for k in ["tracked change", "insertion", "deletion", "accepted"]):
                self.change_lines.append(line)
            elif any(k in lower for k in ["comment"]):
                if "kept" in lower or "visible" in lower:
                    self.kept_comment_lines.append(line)
                else:
                    self.removed_comment_lines.append(line)
            elif any(
                k in lower for k in ["author", "template", "company", "manager", "metadata", "timestamp", "custom xml"]
            ):
                self.metadata_lines.append(line)
            elif any(k in lower for k in ["hyperlink", "warning"]):
                self.warnings.append(line)
            else:
                self.structural_lines.append(line)

    def render(self) -> str:
        """Render the full report as text."""
        sep = "═" * 50
        lines = [sep, f"Sanitize Report: {self.filename}"]

        flags = []
        if self.mode == "keep-markup":
            flags.append("--keep-markup")
        elif self.mode == "baseline":
            flags.append("--baseline")
        if self.author:
            flags.append(f'--author "{self.author}"')
        if self.tracked_changes_accepted > 0:
            flags.append("--accept-all")

        if flags:
            lines.append(" ".join(flags))
        lines.append(sep)

        if self.status == "blocked":
            lines.append("")
            lines.append(f"BLOCKED: {self.blocked_reason}")
            lines.append(sep)
            return "\n".join(lines)

        # Visible to counterparty section (for keep-markup / baseline modes)
        if self.mode in ("keep-markup", "baseline") and (self.tracked_changes_kept > 0 or self.comments_kept > 0):
            lines.append("")
            lines.append("VISIBLE TO COUNTERPARTY")
            if self.tracked_changes_kept > 0:
                lines.append(f"  Tracked changes: {self.tracked_changes_kept}")
            if self.comments_kept > 0:
                lines.append(f"  Open comments: {self.comments_kept}")
                for cl in self.kept_comment_lines:
                    lines.append(f"    {cl}")
            if self.author:
                lines.append(f'  Author on all markup: "{self.author}"')

        # Tracked changes section
        if self.change_lines:
            lines.append("")
            lines.append("TRACKED CHANGES")
            for cl in self.change_lines:
                lines.append(f"  {cl}")

        # Stripped section
        stripped_lines = self.removed_comment_lines
        if stripped_lines:
            lines.append("")
            lines.append("COMMENTS (stripped)")
            for cl in stripped_lines:
                lines.append(f"  {cl}")

        # Metadata section
        if self.metadata_lines:
            lines.append("")
            lines.append("METADATA")
            for ml in self.metadata_lines:
                lines.append(f"  {ml}")

        # Structural section
        if self.structural_lines:
            lines.append("")
            lines.append("STRUCTURAL")
            for sl in self.structural_lines:
                lines.append(f"  {sl}")

        # Warnings
        if self.warnings:
            lines.append("")
            lines.append("WARNINGS")
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")

        # Result
        lines.append("")
        lines.append(sep)
        if self.warnings:
            result = f"Result: CLEAN ({len(self.warnings)} warning{'s' if len(self.warnings) > 1 else ''})"
        else:
            result = "Result: CLEAN"
        lines.append(result)
        lines.append(sep)

        return "\n".join(lines)
