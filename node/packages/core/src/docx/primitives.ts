import { findChild, findChildrenSdtTransparent } from './dom.js';

export class Paragraph {
  constructor(public _element: Element, public _parent: any) {}
  
  get text(): string {
    let t = '';
    const texts = this._element.getElementsByTagName('w:t');
    for (let i = 0; i < texts.length; i++) {
      t += texts[i].textContent || '';
    }
    return t;
  }
}

export class Run {
  /**
   * Projected text that OVERRIDES the run's own `w:t` content.
   *
   * Set for exactly one thing today: the mark inside a checkbox control's
   * `[x]` / `[ ]` token (spec-projection.md §4), where a `U+2612` glyph run
   * projects as `x`. The substitution is one character for one character, so
   * every offset and span-splitting calculation downstream is unaffected.
   *
   * The python twin reaches this through `ProjectedRun.proj_text`, which is
   * computed during traversal; node's `Run` is a lazy wrapper, so the override
   * rides on the instance instead. `get_run_text` honours it, and
   * `get_run_style_markers` returns no markers for it — the mark is chrome and
   * a bold glyph run must not project `[**x**]`.
   */
  public projTextOverride?: string;

  constructor(public _element: Element, public _parent: any) {}
}

export class Cell {
  constructor(public _element: Element, public _parent: any) {}
}

export class Row {
  public cells: Cell[] = [];
  constructor(public _element: Element, public _parent: any) {
    // Direct children only (sdt-transparent). getElementsByTagName() is
    // recursive and would pull the cells of a nested table into this row.
    for (const tc of findChildrenSdtTransparent(this._element, 'w:tc')) {
      this.cells.push(new Cell(tc, this));
    }
  }
}

export class Table {
  public rows: Row[] = [];
  constructor(public _element: Element, public _parent: any) {
    // Direct children only (sdt-transparent). getElementsByTagName() is
    // recursive and would re-emit a nested table's rows as rows of this table.
    for (const tr of findChildrenSdtTransparent(this._element, 'w:tr')) {
      this.rows.push(new Row(tr, this));
    }
  }
}

export class NotesPart {
  public _element: Element;
  constructor(public part: any, public note_type: 'fn' | 'en') {
    this._element = part._element;
  }
}

export class FootnoteItem {
  public id: string;
  public part: any;
  constructor(public _element: Element, public _parent: any, public note_type: 'fn' | 'en') {
    this.id = _element.getAttribute('w:id') || '';
    this.part = _parent.part;
  }
}

export interface DocxEvent {
  type: string;
  id: string;
  author?: string;
  date?: string;
}