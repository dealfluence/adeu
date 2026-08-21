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