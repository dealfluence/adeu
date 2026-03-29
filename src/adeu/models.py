from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, PrivateAttr

from adeu.redline.mapper import DocumentMapper


class EditOperationType:
    """Internal enum for low-level XML manipulation"""

    INSERTION = "INSERTION"
    DELETION = "DELETION"
    MODIFICATION = "MODIFICATION"


class ModifyText(BaseModel):
    """
    Represents a single atomic edit suggested by the LLM.
    The engine treats this as a "Search and Replace" operation.
    """

    type: Literal["modify"] = Field("modify", description="Must be 'modify' for text replacements.")

    target_text: str = Field(
        ...,
        description=(
            "Exact text to find. If the text appears multiple times (e.g. 'Fee'), include surrounding context. "
            "You can include CriticMarkup {==...==} in the target to match text inside existing markup."
        ),
    )

    new_text: str = Field(
        ...,
        description=(
            "The desired text replacement. You may use Markdown formatting: "
            "'# Title' for headers, '**bold**' for bold, '_italic_' for italic. "
            "Do NOT try to manually write {++...++} tags; the engine handles tracking."
        ),
    )

    comment: Optional[str] = Field(
        None,
        description="Text to appear in a comment bubble (Review Pane) linked to this edit.",
    )

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _internal_op: Optional[str] = PrivateAttr(default=None)
    _active_mapper_ref: Optional[DocumentMapper] = PrivateAttr(default=None)


class AcceptChange(BaseModel):
    type: Literal["accept"] = Field("accept", description="Must be 'accept' to finalize a tracked change.")
    target_id: str = Field(..., description="The full ID string from the document text (e.g. 'Chg:12').")
    comment: Optional[str] = Field(None, description="Optional rationale.")


class RejectChange(BaseModel):
    type: Literal["reject"] = Field("reject", description="Must be 'reject' to revert a tracked change.")
    target_id: str = Field(..., description="The full ID string from the document text (e.g. 'Chg:12').")
    comment: Optional[str] = Field(None, description="Optional rationale.")


class ReplyComment(BaseModel):
    type: Literal["reply"] = Field("reply", description="Must be 'reply' to respond to a comment.")
    target_id: str = Field(..., description="The full ID string from the document text (e.g. 'Com:5').")
    text: str = Field(..., description="The content of the reply body.")


DocumentChange = Annotated[Union[AcceptChange, RejectChange, ReplyComment, ModifyText], Field(discriminator="type")]
