import json
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, Field, PrivateAttr

from adeu.redline.mapper import DocumentMapper


def const_to_enum(schema: Any) -> None:
    """Recursively rewrite JSON-Schema ``const`` to a one-member ``enum``.

    Pydantic v2 serializes a single-value ``Literal["x"]`` to ``{"const": "x"}``.
    Gemini's function-calling API rejects ``const`` outright, so any Gemini-based
    client hitting this server fails — most visibly on the
    ``process_document_batch.changes`` discriminator (a ``Literal`` per variant),
    but also on plain literals nested in unions such as ``read_docx``'s
    ``page`` (``Literal["all"]``). ``{"enum": ["x"]}`` is semantically identical
    (a one-member enum) and is accepted everywhere, matching how the Node server
    declares these. See issue #37.

    Designed to be passed as a Pydantic ``json_schema_extra`` callable: it mutates
    the field's schema dict in place and recurses into nested ``anyOf``/``oneOf``
    branches. Validation behaviour is unchanged — Pydantic still enforces the
    underlying ``Literal``.
    """
    if isinstance(schema, dict):
        if "const" in schema:
            schema["enum"] = [schema.pop("const")]
        for value in schema.values():
            const_to_enum(value)
    elif isinstance(schema, list):
        for item in schema:
            const_to_enum(item)


class EditOperationType:
    """Internal enum for low-level XML manipulation"""

    INSERTION = "INSERTION"
    DELETION = "DELETION"
    MODIFICATION = "MODIFICATION"
    PARAGRAPH_REPLACE = "PARAGRAPH_REPLACE"


class ModifyText(BaseModel):
    """
    Represents a single atomic edit suggested by the LLM.
    The engine treats this as a "Search and Replace" operation.
    """

    type: Literal["modify"] = Field(
        "modify",
        description="Must be 'modify' for text replacements.",
        json_schema_extra=const_to_enum,
    )

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
            "Do NOT manually write CriticMarkup tags ({++...++}, {--...--}, {>>...<<}, {==...==}). "
            "To add a comment, use the 'comment' parameter instead."
        ),
    )

    comment: Optional[str] = Field(
        None,
        description="Text to appear in a comment bubble (Review Pane) linked to this edit.",
    )

    match_mode: Literal["strict", "first", "all"] = Field(
        default="strict",
        description=(
            "Resolution strategy when target_text appears more than once. "
            "'strict' (default): fail with an ambiguity error if there are multiple matches. "
            "'first': modify only the first occurrence. "
            "'all': modify every occurrence. Use 'first'/'all' to resolve an ambiguity error "
            "without having to add more surrounding context to target_text."
        ),
    )
    regex: bool = Field(default=False, description="Treat target_text as a regular expression.")

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _resolved_start_idx: Optional[int] = PrivateAttr(default=None)
    _internal_op: Optional[str] = PrivateAttr(default=None)
    _active_mapper_ref: Optional[DocumentMapper] = PrivateAttr(default=None)
    _applied_status: bool = PrivateAttr(default=False)
    _error_msg: Optional[str] = PrivateAttr(default=None)
    _parent_edit_ref: Optional["ModifyText"] = PrivateAttr(default=None)
    _resolved_proxy_edit: Optional["ModifyText"] = PrivateAttr(default=None)
    _pages: list[int] = PrivateAttr(default_factory=list)
    _heading_path: Optional[str] = PrivateAttr(default=None)
    _occurrences_modified: int = PrivateAttr(default=0)


class AcceptChange(BaseModel):
    type: Literal["accept"] = Field(
        "accept",
        description="Must be 'accept' to finalize a tracked change.",
        json_schema_extra=const_to_enum,
    )
    target_id: str = Field(..., description="The full ID string from the document text (e.g. 'Chg:12').")
    comment: Optional[str] = Field(None, description="Optional rationale.")


class RejectChange(BaseModel):
    type: Literal["reject"] = Field(
        "reject",
        description="Must be 'reject' to revert a tracked change.",
        json_schema_extra=const_to_enum,
    )
    target_id: str = Field(..., description="The full ID string from the document text (e.g. 'Chg:12').")
    comment: Optional[str] = Field(None, description="Optional rationale.")


class ReplyComment(BaseModel):
    type: Literal["reply"] = Field(
        "reply",
        description="Must be 'reply' to respond to a comment.",
        json_schema_extra=const_to_enum,
    )
    target_id: str = Field(..., description="The full ID string from the document text (e.g. 'Com:5').")
    text: str = Field(..., description="The content of the reply body.")


class InsertTableRow(BaseModel):
    type: Literal["insert_row"] = Field("insert_row", json_schema_extra=const_to_enum)

    target_text: str = Field(
        ...,
        description=(
            "Text inside an existing row to use as an anchor. The new row will be inserted relative to this row."
        ),
    )

    position: Literal["above", "below"] = Field(
        "below",
        description="Whether to insert the new row above or below the anchor row.",
    )

    cells: list[str] = Field(
        ...,
        description="A list of Markdown strings representing the contents of the new cells.",
    )

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _resolved_start_idx: Optional[int] = PrivateAttr(default=None)
    _applied_status: bool = PrivateAttr(default=False)
    _error_msg: Optional[str] = PrivateAttr(default=None)
    _pages: list[int] = PrivateAttr(default_factory=list)
    _heading_path: Optional[str] = PrivateAttr(default=None)
    _occurrences_modified: int = PrivateAttr(default=0)


class DeleteTableRow(BaseModel):
    type: Literal["delete_row"] = Field("delete_row", json_schema_extra=const_to_enum)

    target_text: str = Field(
        ...,
        description=(
            "Text inside the row you wish to delete. The engine will delete the entire row containing this match."
        ),
    )

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _resolved_start_idx: Optional[int] = PrivateAttr(default=None)
    _applied_status: bool = PrivateAttr(default=False)
    _error_msg: Optional[str] = PrivateAttr(default=None)
    _pages: list[int] = PrivateAttr(default_factory=list)
    _heading_path: Optional[str] = PrivateAttr(default=None)
    _occurrences_modified: int = PrivateAttr(default=0)


DocumentChange = Annotated[
    Union[
        AcceptChange,
        RejectChange,
        ReplyComment,
        ModifyText,
        InsertTableRow,
        DeleteTableRow,
    ],
    Field(discriminator="type"),
]


def coerce_stringified_changes(value: Any) -> Any:
    """
    Tolerate LLM clients (notably Gemini) that wrap each object in the `changes`
    array as a JSON-encoded string instead of passing a real object:

        "changes": ["{\"type\": \"modify\", ...}", "{\"type\": \"accept\", ...}"]

    Without this, Pydantic rejects the call with an opaque discriminator error
    and the agent has no way to recover. We decode any string elements so the
    discriminated-union validator downstream sees real dicts.

    Behaviour:
      - Non-list inputs are passed through untouched (Pydantic will raise its
        normal "expected list" error).
      - String elements are json.loads()'d. If decoding fails, or the decoded
        value is not a dict, the original string is left in place so Pydantic
        produces a clear "expected object, got string" error at that index.
      - Non-string elements (dicts, already-validated models) pass through.
    """
    if not isinstance(value, list):
        return value

    coerced: list[Any] = []
    for item in value:
        if isinstance(item, str):
            try:
                decoded = json.loads(item)
            except (json.JSONDecodeError, ValueError):
                coerced.append(item)
                continue
            if isinstance(decoded, dict):
                coerced.append(decoded)
            else:
                coerced.append(item)
        else:
            coerced.append(item)
    return coerced


BatchChanges = Annotated[list[DocumentChange], BeforeValidator(coerce_stringified_changes)]
