import json
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, Field, PrivateAttr, TypeAdapter, WithJsonSchema

from adeu.redline.mapper import DocumentMapper

_MATCH_MODE_SYNONYMS = {
    "strict": "strict",
    "first": "first",
    "all": "all",
    "first_only": "first",
    "firstonly": "first",
    "first-only": "first",
    "all_occurrences": "all",
    "alloccurrences": "all",
    "all-occurrences": "all",
    "every": "all",
}


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
    # Sub-edits produced by splitting one balanced multi-paragraph modification
    # share this id so the batch report counts them as a single applied edit.
    _split_group_id: Optional[int] = PrivateAttr(default=None)
    # True when any resolved sub-edit of this edit failed or was skipped, so
    # partially-applied fan-outs still count as skipped (all-or-nothing).
    _any_sub_failure: bool = PrivateAttr(default=False)
    _pages: list[int] = PrivateAttr(default_factory=list)
    _heading_path: Optional[str] = PrivateAttr(default=None)
    _occurrences_modified: int = PrivateAttr(default=0)
    _is_table_edit: bool = PrivateAttr(default=False)
    _original_target_text: Optional[str] = PrivateAttr(default=None)
    # (before, after) document text around the resolved match, snapshotted
    # before the batch mutates the DOM. Consumed by the preview builder.
    _preview_context: Optional[tuple] = PrivateAttr(default=None)
    # Full-match preview data stashed on the ORIGINAL edit at resolve time:
    # the (start, length) of the first matched occurrence, the exact document
    # text it matched, and the effective replacement. Lets the report preview
    # show the complete logical change instead of just the first word-diff
    # sub-edit of a compound modification.
    _preview_span: Optional[tuple] = PrivateAttr(default=None)
    _preview_matched_text: Optional[str] = PrivateAttr(default=None)
    _preview_new_text: Optional[str] = PrivateAttr(default=None)
    _preview_mapper_ref: Optional[DocumentMapper] = PrivateAttr(default=None)


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

    match_mode: Literal["strict", "first", "all"] = Field(
        default="strict",
        description=(
            "Resolution strategy when target_text matches more than one row. "
            "'strict' (default): fail with an ambiguity error. "
            "'first': anchor on the first matching row only. "
            "'all': insert a row relative to EVERY matching row."
        ),
    )

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _resolved_start_idx: Optional[int] = PrivateAttr(default=None)
    # The mapper whose coordinate space _resolved_start_idx belongs to (the
    # clean-view mapper when the anchor row carries pending tracked changes).
    _active_mapper_ref: Optional[DocumentMapper] = PrivateAttr(default=None)
    _applied_status: bool = PrivateAttr(default=False)
    _error_msg: Optional[str] = PrivateAttr(default=None)
    _any_sub_failure: bool = PrivateAttr(default=False)
    # A match_mode="all" fan-out deep-copies this edit once per matching row;
    # each copy points back here so the batch report counts ONE applied edit
    # and accumulates occurrences on the parent (mirrors ModifyText).
    _parent_edit_ref: Optional["TableRowChange"] = PrivateAttr(default=None)
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

    match_mode: Literal["strict", "first", "all"] = Field(
        default="strict",
        description=(
            "Resolution strategy when target_text matches more than one row. "
            "'strict' (default): fail with an ambiguity error. "
            "'first': delete only the first matching row. "
            "'all': delete EVERY matching row."
        ),
    )

    # Internal use only. PrivateAttr is invisible to the MCP API schema.
    _match_start_index: Optional[int] = PrivateAttr(default=None)
    _resolved_start_idx: Optional[int] = PrivateAttr(default=None)
    # The mapper whose coordinate space _resolved_start_idx belongs to (the
    # clean-view mapper when the anchor row carries pending tracked changes).
    _active_mapper_ref: Optional[DocumentMapper] = PrivateAttr(default=None)
    _applied_status: bool = PrivateAttr(default=False)
    _error_msg: Optional[str] = PrivateAttr(default=None)
    _any_sub_failure: bool = PrivateAttr(default=False)
    # See InsertTableRow._parent_edit_ref.
    _parent_edit_ref: Optional["TableRowChange"] = PrivateAttr(default=None)
    _pages: list[int] = PrivateAttr(default_factory=list)
    _heading_path: Optional[str] = PrivateAttr(default=None)
    _occurrences_modified: int = PrivateAttr(default=0)


# Either structural row operation. Both fan out the same way under
# match_mode="all", so a sub-edit's parent is one of these two.
TableRowChange = Union[InsertTableRow, DeleteTableRow]


class FlatDocumentChange(BaseModel):
    """
    A single unified flat change schema for client/platform JSON Schema exposure,
    avoiding complex oneOf/anyOf unions which break some MCP hosts.
    """

    type: Literal["accept", "reject", "reply", "modify", "insert_row", "delete_row"] = Field(
        ...,
        description="The type of document change operation.",
        json_schema_extra=const_to_enum,
    )
    target_text: Optional[str] = Field(
        None,
        description=(
            "Exact text to find. If the text appears multiple times (e.g. 'Fee'), include surrounding context. "
            "You can include CriticMarkup {==...==} in the target to match text inside existing markup."
        ),
    )
    new_text: Optional[str] = Field(
        None,
        description=(
            "The desired text replacement. You may use Markdown formatting: "
            "'# Title' for headers, '**bold**' for bold, '_italic_' for italic. "
            "Do NOT manually write CriticMarkup tags ({++...++}, {--...--}, {>>...<<}, {==...==}). "
            "To add a comment, use the 'comment' parameter instead."
        ),
    )
    target_id: Optional[str] = Field(
        None,
        description="The full ID string from the document text (e.g. 'Chg:12', 'Com:5').",
    )
    text: Optional[str] = Field(
        None,
        description="The content of the reply body.",
    )
    cells: Optional[list[str]] = Field(
        None,
        description="A list of Markdown strings representing the contents of the new cells.",
    )
    position: Optional[Literal["above", "below"]] = Field(
        None,
        description="Whether to insert the new row above or below the anchor row.",
    )
    regex: Optional[bool] = Field(
        None,
        description="Treat target_text as a regular expression.",
    )
    comment: Optional[str] = Field(
        None,
        description="Text to appear in a comment bubble (Review Pane) linked to this edit or optional rationale.",
    )
    match_mode: Optional[Literal["strict", "first", "all"]] = Field(
        None,
        description=(
            "Resolution strategy when target_text appears more than once. "
            "'strict' (default): fail with an ambiguity error if there are multiple matches. "
            "'first': modify only the first occurrence. "
            "'all': modify every occurrence. Use 'first'/'all' to resolve an ambiguity error "
            "without having to add more surrounding context to target_text."
        ),
    )


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

# Same union, published as ONE flat object instead of a discriminated oneOf.
# Some MCP hosts cannot consume oneOf/anyOf schemas at all, so the MCP tool
# boundary advertises this. It is strictly the weaker contract — every field
# becomes optional, so "modify requires target_text + new_text" stops being
# expressed — which is why it is opt-in per surface rather than attached to
# DocumentChange itself. Validation is unaffected: the real discriminated
# union still runs, and the CLI (StrictBatchChanges) keeps the precise schema.
FlatSchemaDocumentChange = Annotated[
    DocumentChange,
    WithJsonSchema(TypeAdapter(FlatDocumentChange).json_schema()),
]


def _coerce_match_mode_in_place(item: dict) -> None:
    """
    Normalize a `match_mode` value on a modify-dict.

    - canonical values ("strict"/"first"/"all") pass through
    - recognized synonyms map to canonical
    - anything else (null, "banana", the help-string echo) is left in place so
      the Pydantic Literal REJECTS it with a clear enum error. Silently
      falling back to a default meant a caller could believe an option took
      effect when it was ignored (QA 2026-07-19 F-12); an invalid-option
      error is recoverable, a silently wrong resolution strategy is not.
    """
    if "match_mode" not in item:
        return
    raw = item["match_mode"]
    if not isinstance(raw, str):
        # Non-string (null, number): leave for the Literal validator to reject.
        return
    mapped = _MATCH_MODE_SYNONYMS.get(raw.strip().lower())
    if mapped is not None:
        item["match_mode"] = mapped


def _infer_type_in_place(item: dict) -> None:
    """
    Fill a missing `type` discriminator on a change-dict, but ONLY when exactly
    one variant fits the key signature unambiguously.

    Safe inferences:
      - has `cells`                       -> "insert_row"
      - has `text` and `target_id`        -> "reply"
      - has `target_text` and `new_text`  -> "modify"

    Deliberately NOT inferred (left absent so validation rejects with a clear
    discriminator error):
      - `target_id` alone -> ambiguous between accept/reject (semantic choice)
      - `target_text` alone (no `new_text`) -> ambiguous between delete_row and
        a modify with empty new_text
    """
    if not isinstance(item, dict) or "type" in item:
        return
    if "cells" in item:
        item["type"] = "insert_row"
    elif "text" in item and "target_id" in item:
        item["type"] = "reply"
    elif "target_text" in item and "new_text" in item:
        item["type"] = "modify"
    # else: leave absent; validation will surface a clear error.


def _coerce_changes(value: Any, *, infer_types: bool) -> Any:
    """
    Tolerate LLM clients (notably Gemini) that wrap each object in the `changes`
    array as a JSON-encoded string instead of passing a real object:

        "changes": ["{\"type\": \"modify\", ...}", "{\"type\": \"accept\", ...}"]

    Without this, Pydantic rejects the call with an opaque discriminator error
    and the agent has no way to recover. We decode any string elements so the
    discriminated-union validator downstream sees real dicts.

    On each decoded/passed-through dict we additionally:
      - infer a missing `type` discriminator when unambiguous
        (_infer_type_in_place) — only when `infer_types` is True
      - normalize a malformed `match_mode` to a canonical value or drop it
        (_coerce_match_mode_in_place)

    Behaviour:
      - Non-list inputs are passed through untouched (Pydantic will raise its
        normal "expected list" error).
      - String elements are json.loads()'d. If decoding fails, or the decoded
        value is not a dict, the original string is left in place so Pydantic
        produces a clear "expected object, got string" error at that index.
      - Non-string elements (dicts, already-validated models) pass through;
        plain dicts still receive the type/match_mode normalization.
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
                if infer_types:
                    _infer_type_in_place(decoded)
                _coerce_match_mode_in_place(decoded)
                coerced.append(decoded)
            else:
                coerced.append(item)
        elif isinstance(item, dict):
            if infer_types:
                _infer_type_in_place(item)
            _coerce_match_mode_in_place(item)
            coerced.append(item)
        else:
            # Already-validated model instance or other; pass through untouched.
            coerced.append(item)
    return coerced


def coerce_stringified_changes(value: Any) -> Any:
    """Interactive-LLM tolerance: decode stringified items, infer an
    unambiguous missing `type`, normalize `match_mode` synonyms."""
    return _coerce_changes(value, infer_types=True)


def coerce_stringified_changes_strict(value: Any) -> Any:
    """
    Authored-artifact contract: decode stringified items and normalize
    `match_mode` synonyms, but NEVER infer a missing `type` — the CLI
    documents `type` as required on every change, and silently treating a
    typeless object as `modify` weakens agent-output validation
    (QA 2026-07-19 v8 F-03). The discriminated union then rejects it with
    "Change #N is missing the required 'type' field."
    """
    return _coerce_changes(value, infer_types=False)


# The MCP boundary keeps the documented LLM-tolerance layer (its schema says
# a missing `type` is inferred when unambiguous); files fed to the CLI are
# authored artifacts and validate strictly.
BatchChanges = Annotated[list[DocumentChange], BeforeValidator(coerce_stringified_changes)]
StrictBatchChanges = Annotated[list[DocumentChange], BeforeValidator(coerce_stringified_changes_strict)]
