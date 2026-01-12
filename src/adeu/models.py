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
            "Exact text to find. If the text appears multiple times (e.g. 'Fee'), include surrounding context "
            "(e.g. 'Section 2: Fee') to ensure the correct instance is matched. For INSERTION, this is the "
            "anchor immediately PRECEDING the new content."
        ),
    )

    new_text: Optional[str] = Field(
        "",
        description=(
            "The desired text to replace target_text with. For deletions, leave empty. For insertions, include the "
            "anchor context from target_text (e.g. target='Section 1', new='Section 1\\nNew Section')."
        ),
    )

    comment: Optional[str] = Field(
        None,
        description="Text to appear in a comment bubble (Review Pane) linked to this edit.",
    )

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _internal_op: Optional[str] = PrivateAttr(default=None)
