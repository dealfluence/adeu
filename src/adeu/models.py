# FILE: src/adeu/models.py

from typing import Optional

from pydantic import BaseModel, Field, PrivateAttr


class EditOperationType:
    """Internal enum for low-level XML manipulation"""

    INSERTION = "INSERTION"
    DELETION = "DELETION"
    MODIFICATION = "MODIFICATION"


class DocumentEdit(BaseModel):
    """
    Represents a single atomic edit suggested by the LLM.
    The engine treats this as a "Search and Replace" operation.
    """

    target_text: str = Field(
        ...,
        description=(
            "Exact text to find. If the text appears multiple times (e.g. 'Fee'), include surrounding context. "
            "NOTE: The text may contain Markdown formatting for Headers (#), Bold (**), and Italic (_). "
            "You MUST include these markers if they exist in the text representation."
        ),
    )

    new_text: Optional[str] = Field(
        "",
        description=(
            "The desired text replacement. You may use Markdown formatting: "
            "'# Title' for headers, '**bold**' for bold, '_italic_' for italic. "
            "For insertions, include anchor context from target_text."
        ),
    )

    comment: Optional[str] = Field(
        None,
        description="Text to appear in a comment bubble (Review Pane) linked to this edit.",
    )

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _internal_op: Optional[str] = PrivateAttr(default=None)
