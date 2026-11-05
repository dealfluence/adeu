/** One comment on an edit, optionally under its own author. */
export interface EditComment {
  text: string;
  /** Defaults to the engine author. */
  author?: string | null;
}

/** A comment on an edit: one string, or several comments on the same change. */
export type EditCommentValue = string | EditComment[];

/** A comment value as its list of non-empty entries (a string is one entry). */
export function comment_entries(comment: EditCommentValue | null | undefined): EditComment[] {
  if (typeof comment === 'string') return comment ? [{ text: comment }] : [];
  return (comment || []).filter((entry) => entry && entry.text);
}

/** A comment value as plain text: list entries joined by blank lines. */
export function comment_text(comment: EditCommentValue | null | undefined): string | null {
  if (comment === null || comment === undefined || typeof comment === 'string') return comment ?? null;
  return comment_entries(comment).map((entry) => entry.text).join('\n\n') || null;
}

/**
 * Why `comment` is not a valid comment value, or null when it is: a string,
 * or a list of `{text: string, author?: string}`. Callers outside the MCP
 * schema (SDK, n8n) reach the engine unvalidated, and a malformed comment
 * used to crash deep inside it with a TypeError.
 */
export function describe_invalid_comment(comment: unknown): string | null {
  if (comment === null || comment === undefined || typeof comment === 'string') return null;
  if (!Array.isArray(comment)) {
    return '`comment` must be a string or a list of {text, author?} objects.';
  }
  for (const [i, entry] of comment.entries()) {
    if (!entry || typeof entry !== 'object' || typeof (entry as any).text !== 'string') {
      return `\`comment[${i}]\` must be an object with a string \`text\` (and an optional string \`author\`).`;
    }
    const author = (entry as any).author;
    if (author !== undefined && author !== null && typeof author !== 'string') {
      return `\`comment[${i}].author\` must be a string.`;
    }
  }
  return null;
}

export interface ModifyText {
  type: 'modify';
  target_text: string;
  new_text: string;
  comment?: EditCommentValue | null;
  match_mode?: 'strict' | 'first' | 'all';
  regex?: boolean;
  _match_start_index?: number | null;
  _internal_op?: string | null;
  _active_mapper_ref?: any | null; // Typed as DocumentMapper later
  _original_target_text?: string;
  _is_table_edit?: boolean;
}

export interface AcceptChange {
  type: 'accept';
  target_id: string;
  /**
   * OPC part the change lives in, e.g. "word/header1.xml". Revision ids are
   * numbered PER PART, so the same target_id can name unrelated changes in
   * different parts (issue #114); the engine refuses such a bare id and this
   * field disambiguates it. Omit whenever the id is unique in the package.
   */
  part?: string | null;
  comment?: string | null;
}

export interface RejectChange {
  type: 'reject';
  target_id: string;
  /** As on AcceptChange: disambiguates a target_id present in several OPC
   *  parts (issue #114). Omit whenever the id is unique in the package. */
  part?: string | null;
  comment?: string | null;
}

export interface ReplyComment {
  type: 'reply';
  target_id: string;
  text: string;
}

export interface InsertTableRow {
  type: 'insert_row';
  target_text: string;
  position: 'above' | 'below';
  cells: string[];
  _match_start_index?: number | null;
}

export interface DeleteTableRow {
  type: 'delete_row';
  target_text: string;
  _match_start_index?: number | null;
}

/**
 * Fill a content control the way Word fills it (spec-set-field.md).
 *
 * The explicit, batchable form of what a text-first edit at a control's
 * sanctioned surface already does. Both routes desugar to the same tracked
 * replacement, so `set_field` gets no special pass through the gates and
 * needs no parallel writer.
 */
export interface SetField {
  type: 'set_field';
  /** The 'CC:<N>' id, the control's tag, or its alias. */
  field: string;
  /** The value to write. Empty string clears the field. */
  value: string;
  match_mode?: 'strict' | 'first' | 'all';
  comment?: string | null;
  _match_start_index?: number | null;
  _active_mapper_ref?: any | null;
}

export type DocumentChange = 
  | ModifyText 
  | AcceptChange 
  | RejectChange 
  | ReplyComment 
  | InsertTableRow 
  | DeleteTableRow
  | SetField;