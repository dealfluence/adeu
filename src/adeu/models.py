from enum import Enum
from typing import Optional
from pydantic import BaseModel

class EditOperationType(str, Enum):
    INSERTION = "INSERTION"
    DELETION = "DELETION"
    MODIFICATION = "MODIFICATION"

class ComplianceEdit(BaseModel):
    """
    Represents a single atomic edit suggested by the LLM.
    """
    operation: EditOperationType
    # The exact text in the original document to anchor to or replace
    target_text_to_change_or_anchor: str 
    # The new text to insert (None for pure deletions)
    proposed_new_text: Optional[str] = None
    # Reasoning provided by the AI (optional, for logging/comments)
    thought_process: Optional[str] = None
    # Source policy quote (optional)
    verbatim_policy_quote: Optional[str] = None