export interface ModifyText {
  type: 'modify';
  target_text: string;
  new_text: string;
  comment?: string | null;
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
  comment?: string | null;
}

export interface RejectChange {
  type: 'reject';
  target_id: string;
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

export type DocumentChange = 
  | ModifyText 
  | AcceptChange 
  | RejectChange 
  | ReplyComment 
  | InsertTableRow 
  | DeleteTableRow;