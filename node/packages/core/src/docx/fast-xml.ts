// FILE: node/packages/core/src/docx/fast-xml.ts
/**
 * Purpose-built XML parser + minimal DOM for WordprocessingML
 * (docs/PERFORMANCE.md §5.4b).
 *
 * The spec-compliant parser spent ~93% of its time on machinery this
 * engine never consults: QName validation regexes, namespace resolution,
 * NamedNodeMap/live-NodeList bookkeeping, locator tracking. Measured on a
 * 45 MB document.xml: 6.70 s full parser vs 0.49 s tokenizer+minimal
 * construction. This module implements EXACTLY the DOM subset the core
 * uses (audited by grep, enforced by the full suite + projection goldens):
 *
 * - tree: childNodes (plain array), firstChild/lastChild, next/previous
 *   Sibling, parentNode, ownerDocument, documentElement
 * - mutation: appendChild, insertBefore, removeChild, replaceChild,
 *   cloneNode, textContent setter — every mutation bumps Document._inc
 *   (the freshness signal cell-anchor caching and the lazy transactional
 *   snapshot rely on)
 * - elements: tagName/nodeName (literal prefixed names, e.g. "w:p" — the
 *   engine matches prefixes literally and never consults namespaceURI),
 *   get/set/has/removeAttribute, getElementsByTagName (snapshot array;
 *   every call site materializes immediately)
 * - text nodes: data/nodeValue/textContent
 * - Document: createElement, createTextNode, getElementsByTagName,
 *   replaceChild (snapshot restore swaps documentElement)
 *
 * Prefixes are treated as opaque name parts; xmlns:* live as ordinary
 * attributes and round-trip verbatim. Comments and CDATA are preserved;
 * PIs/DOCTYPE are skipped (the prolog is re-added at save).
 */

export const ELEMENT_NODE = 1;
export const TEXT_NODE = 3;
export const CDATA_NODE = 4;
export const COMMENT_NODE = 8;
export const DOCUMENT_NODE = 9;

export class FastNode {
  public nodeType = 0;
  public parentNode: FastNode | null = null;
  public ownerDocument: FastDocument | null = null;

  get firstChild(): FastNode | null {
    return null;
  }
  get lastChild(): FastNode | null {
    return null;
  }

  get nextSibling(): FastNode | null {
    const p = this.parentNode as FastElement | FastDocument | null;
    if (!p) return null;
    const sib = p.childNodes;
    const i = sib.indexOf(this);
    return i >= 0 && i + 1 < sib.length ? sib[i + 1] : null;
  }

  get previousSibling(): FastNode | null {
    const p = this.parentNode as FastElement | FastDocument | null;
    if (!p) return null;
    const sib = p.childNodes;
    const i = sib.indexOf(this);
    return i > 0 ? sib[i - 1] : null;
  }
}

export class FastText extends FastNode {
  public nodeType = TEXT_NODE;
  constructor(public data: string) {
    super();
  }
  get nodeName(): string {
    return "#text";
  }
  get nodeValue(): string {
    return this.data;
  }
  set nodeValue(v: string) {
    this.data = v;
  }
  get textContent(): string {
    return this.data;
  }
  set textContent(v: string) {
    this.data = v;
    this.ownerDocument && this.ownerDocument._inc++;
  }
  cloneNode(_deep?: boolean): FastText {
    const t = new FastText(this.data);
    t.ownerDocument = this.ownerDocument;
    return t;
  }
  toString(): string {
    return serializeFastXml(this);
  }
}

export class FastComment extends FastText {
  public nodeType = COMMENT_NODE;
  cloneNode(_deep?: boolean): FastComment {
    const c = new FastComment(this.data);
    c.ownerDocument = this.ownerDocument;
    return c;
  }
}

export class FastCData extends FastText {
  public nodeType = CDATA_NODE;
  cloneNode(_deep?: boolean): FastCData {
    const c = new FastCData(this.data);
    c.ownerDocument = this.ownerDocument;
    return c;
  }
}

abstract class FastParent extends FastNode {
  public childNodes: FastNode[] = [];

  get firstChild(): FastNode | null {
    return this.childNodes.length ? this.childNodes[0] : null;
  }
  get lastChild(): FastNode | null {
    return this.childNodes.length
      ? this.childNodes[this.childNodes.length - 1]
      : null;
  }
  hasChildNodes(): boolean {
    return this.childNodes.length > 0;
  }

  private bump() {
    const d =
      this.nodeType === DOCUMENT_NODE
        ? (this as unknown as FastDocument)
        : this.ownerDocument;
    if (d) d._inc++;
  }

  appendChild<T extends FastNode>(child: T): T {
    if (child.parentNode) {
      (child.parentNode as FastParent).removeChild(child);
    }
    this.childNodes.push(child);
    child.parentNode = this;
    this.bump();
    return child;
  }

  insertBefore<T extends FastNode>(child: T, ref: FastNode | null): T {
    if (!ref) return this.appendChild(child);
    const i = this.childNodes.indexOf(ref);
    if (i < 0) throw new Error("insertBefore: reference node not a child");
    if (child.parentNode) {
      (child.parentNode as FastParent).removeChild(child);
    }
    // Re-locate: removing the child may have shifted the reference index
    // when both share this parent.
    const j = this.childNodes.indexOf(ref);
    this.childNodes.splice(j, 0, child);
    child.parentNode = this;
    this.bump();
    return child;
  }

  removeChild<T extends FastNode>(child: T): T {
    const i = this.childNodes.indexOf(child);
    if (i < 0) throw new Error("removeChild: node is not a child");
    this.childNodes.splice(i, 1);
    child.parentNode = null;
    this.bump();
    return child;
  }

  replaceChild<T extends FastNode>(newChild: FastNode, oldChild: T): T {
    const i = this.childNodes.indexOf(oldChild);
    if (i < 0) throw new Error("replaceChild: node is not a child");
    if (newChild.parentNode) {
      (newChild.parentNode as FastParent).removeChild(newChild);
    }
    const j = this.childNodes.indexOf(oldChild);
    this.childNodes[j] = newChild;
    newChild.parentNode = this;
    oldChild.parentNode = null;
    this.bump();
    return oldChild;
  }

  /** Snapshot (non-live) preorder descendant scan; "*" matches all. Every
   * core call site materializes the list immediately, so live semantics
   * are unnecessary. */
  getElementsByTagName(tagName: string): FastElement[] {
    const out: FastElement[] = [];
    const wild = tagName === "*";
    const stack: FastNode[] = [];
    for (let i = this.childNodes.length - 1; i >= 0; i--) {
      stack.push(this.childNodes[i]);
    }
    while (stack.length) {
      const n = stack.pop()!;
      if (n.nodeType === ELEMENT_NODE) {
        const el = n as FastElement;
        if (wild || el.tagName === tagName) out.push(el);
        for (let i = el.childNodes.length - 1; i >= 0; i--) {
          stack.push(el.childNodes[i]);
        }
      }
    }
    return out;
  }
}

export class FastElement extends FastParent {
  public nodeType = ELEMENT_NODE;
  /** [name, value] pairs in document order — WordprocessingML elements
   * carry a handful of attributes, linear scans beat map overhead. */
  public attrs: Array<[string, string]> = [];

  constructor(public tagName: string) {
    super();
  }
  get nodeName(): string {
    return this.tagName;
  }
  get localName(): string {
    const i = this.tagName.indexOf(":");
    return i < 0 ? this.tagName : this.tagName.slice(i + 1);
  }
  get prefix(): string | null {
    const i = this.tagName.indexOf(":");
    return i < 0 ? null : this.tagName.slice(0, i);
  }
  get nodeValue(): null {
    return null;
  }
  /** NamedNodeMap-shaped view for consumers like the xpath test helper:
   * indexed access + length + item(), each attr an Attr-shaped object. */
  get attributes() {
    const list: any[] = this.attrs.map(([name, value]) => ({
      nodeType: 2,
      name,
      value,
      nodeName: name,
      nodeValue: value,
      localName: name.indexOf(":") < 0 ? name : name.slice(name.indexOf(":") + 1),
      prefix: name.indexOf(":") < 0 ? null : name.slice(0, name.indexOf(":")),
      ownerElement: this,
    }));
    (list as any).item = (i: number) => list[i] ?? null;
    return list as any;
  }
  /** xmldom nodes stringify to their XML — tests and callers rely on it. */
  toString(): string {
    return serializeFastXml(this);
  }

  getAttribute(name: string): string | null {
    const a = this.attrs;
    for (let i = 0; i < a.length; i++) {
      if (a[i][0] === name) return a[i][1];
    }
    return null;
  }
  hasAttribute(name: string): boolean {
    return this.getAttribute(name) !== null;
  }
  setAttribute(name: string, value: string): void {
    const a = this.attrs;
    for (let i = 0; i < a.length; i++) {
      if (a[i][0] === name) {
        a[i][1] = String(value);
        this.ownerDocument && this.ownerDocument._inc++;
        return;
      }
    }
    a.push([name, String(value)]);
    this.ownerDocument && this.ownerDocument._inc++;
  }
  removeAttribute(name: string): void {
    const a = this.attrs;
    for (let i = 0; i < a.length; i++) {
      if (a[i][0] === name) {
        a.splice(i, 1);
        this.ownerDocument && this.ownerDocument._inc++;
        return;
      }
    }
  }

  get textContent(): string {
    let out = "";
    const stack: FastNode[] = [];
    for (let i = this.childNodes.length - 1; i >= 0; i--) {
      stack.push(this.childNodes[i]);
    }
    while (stack.length) {
      const n = stack.pop()!;
      if (n.nodeType === TEXT_NODE || n.nodeType === CDATA_NODE) {
        out += (n as FastText).data;
      } else if (n.nodeType === ELEMENT_NODE) {
        const el = n as FastElement;
        for (let i = el.childNodes.length - 1; i >= 0; i--) {
          stack.push(el.childNodes[i]);
        }
      }
    }
    return out;
  }
  set textContent(v: string) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes.length = 0;
    if (v !== "" && v !== null && v !== undefined) {
      const t = new FastText(String(v));
      t.ownerDocument = this.ownerDocument;
      t.parentNode = this;
      this.childNodes.push(t);
    }
    this.ownerDocument && this.ownerDocument._inc++;
  }

  cloneNode(deep = false): FastElement {
    const el = new FastElement(this.tagName);
    el.ownerDocument = this.ownerDocument;
    if (this.attrs.length) {
      el.attrs = this.attrs.map(([n, v]) => [n, v] as [string, string]);
    }
    if (deep) {
      for (const c of this.childNodes) {
        const cc = (c as any).cloneNode(true) as FastNode;
        cc.parentNode = el;
        el.childNodes.push(cc);
      }
    }
    return el;
  }
}

export class FastDocument extends FastParent {
  public nodeType = DOCUMENT_NODE;
  /** Mutation counter — the freshness contract consumed by
   * cell-anchor caching and the engine's lazy snapshot. */
  public _inc = 1;
  public documentElement: FastElement | null = null;

  get nodeName(): string {
    return "#document";
  }

  createElement(tagName: string): FastElement {
    const el = new FastElement(tagName);
    el.ownerDocument = this;
    return el;
  }
  /** Prefix-literal model: the namespace URI is not consulted anywhere in
   * this engine — the qualified name (already carrying its prefix, or
   * inheriting the container's default namespace on serialize) is the
   * identity. Mirrors how the OPC rels/comments parts are built. */
  createElementNS(_ns: string | null, qualifiedName: string): FastElement {
    return this.createElement(qualifiedName);
  }
  createTextNode(data: string): FastText {
    const t = new FastText(String(data));
    t.ownerDocument = this;
    return t;
  }
  createComment(data: string): FastComment {
    const c = new FastComment(String(data));
    c.ownerDocument = this;
    return c;
  }
  toString(): string {
    return serializeFastXml(this);
  }

  appendChild<T extends FastNode>(child: T): T {
    const r = super.appendChild(child);
    if ((child as unknown as FastNode).nodeType === ELEMENT_NODE) {
      this.documentElement = child as unknown as FastElement;
    }
    return r;
  }
  replaceChild<T extends FastNode>(newChild: FastNode, oldChild: T): T {
    const r = super.replaceChild(newChild, oldChild);
    if (
      (oldChild as unknown as FastNode) ===
        (this.documentElement as unknown as FastNode) &&
      newChild.nodeType === ELEMENT_NODE
    ) {
      this.documentElement = newChild as FastElement;
    }
    return r;
  }
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

const ENTITY_RE = /&(?:amp|lt|gt|quot|apos|#x?[0-9a-fA-F]+);/g;
function decodeEntities(s: string): string {
  if (s.indexOf("&") < 0) return s;
  return s.replace(ENTITY_RE, (m) => {
    switch (m) {
      case "&amp;":
        return "&";
      case "&lt;":
        return "<";
      case "&gt;":
        return ">";
      case "&quot;":
        return '"';
      case "&apos;":
        return "'";
      default: {
        const body = m.slice(2, -1);
        const code =
          body[0] === "x" || body[0] === "X"
            ? parseInt(body.slice(1), 16)
            : parseInt(body, 10);
        return Number.isFinite(code) ? String.fromCodePoint(code) : m;
      }
    }
  });
}

const WS = new Set([32, 9, 10, 13]);

/**
 * Parses machine-generated OOXML. Throws on structural malformation
 * (unclosed tags, mismatched close tags, junk before the root) so corrupt
 * containers still surface as load errors, matching the previous parser's
 * strictness closely enough for the boundary diagnosis.
 */
export function parseFastXml(s: string): FastDocument {
  if (s.charCodeAt(0) === 0xfeff) s = s.slice(1);
  const doc = new FastDocument();
  let cur: FastParent = doc;
  let i = 0;
  const n = s.length;
  let sawRoot = false;

  while (i < n) {
    const lt = s.indexOf("<", i);
    if (lt < 0) {
      if (cur !== doc) throw new Error("Unexpected end of XML");
      // Trailing whitespace after the root is legal; anything else is junk.
      if (/\S/.test(s.slice(i))) throw new Error("Content after document root");
      break;
    }
    if (lt > i && cur !== doc) {
      const raw = s.slice(i, lt);
      const t = new FastText(decodeEntities(raw));
      t.ownerDocument = doc;
      t.parentNode = cur;
      cur.childNodes.push(t);
    } else if (lt > i && /\S/.test(s.slice(i, lt))) {
      throw new Error("Text content outside document root");
    }
    i = lt + 1;
    const c = s.charCodeAt(i);

    if (c === 47 /* / */) {
      const gt = s.indexOf(">", i);
      if (gt < 0) throw new Error("Unterminated close tag");
      const name = s.slice(i + 1, gt).trim();
      if (
        cur.nodeType !== ELEMENT_NODE ||
        (cur as unknown as FastElement).tagName !== name
      ) {
        throw new Error(`Mismatched close tag </${name}>`);
      }
      cur = (cur.parentNode as FastParent) ?? doc;
      i = gt + 1;
      continue;
    }

    if (c === 63 /* ? */) {
      const end = s.indexOf("?>", i);
      if (end < 0) throw new Error("Unterminated processing instruction");
      i = end + 2;
      continue;
    }

    if (c === 33 /* ! */) {
      if (s.startsWith("!--", i)) {
        const end = s.indexOf("-->", i + 3);
        if (end < 0) throw new Error("Unterminated comment");
        const cm = new FastComment(s.slice(i + 3, end));
        cm.ownerDocument = doc;
        if (cur !== doc) {
          cm.parentNode = cur;
          cur.childNodes.push(cm);
        }
        i = end + 3;
        continue;
      }
      if (s.startsWith("![CDATA[", i)) {
        const end = s.indexOf("]]>", i + 8);
        if (end < 0) throw new Error("Unterminated CDATA section");
        if (cur !== doc) {
          const cd = new FastCData(s.slice(i + 8, end));
          cd.ownerDocument = doc;
          cd.parentNode = cur;
          cur.childNodes.push(cd);
        }
        i = end + 3;
        continue;
      }
      // DOCTYPE / other declarations: skip to the matching '>'.
      const end = s.indexOf(">", i);
      if (end < 0) throw new Error("Unterminated declaration");
      i = end + 1;
      continue;
    }

    // Element open tag.
    let j = i;
    while (j < n) {
      const cc = s.charCodeAt(j);
      if (WS.has(cc) || cc === 62 /* > */ || cc === 47 /* / */) break;
      j++;
    }
    if (j === i) throw new Error(`Invalid tag at offset ${lt}`);
    const el = new FastElement(s.slice(i, j));
    el.ownerDocument = doc;
    if (cur === doc) {
      if (sawRoot) throw new Error("Multiple document roots");
      sawRoot = true;
      doc.childNodes.push(el);
      el.parentNode = doc;
      doc.documentElement = el;
    } else {
      el.parentNode = cur;
      cur.childNodes.push(el);
    }
    i = j;

    let selfClosed = false;
    while (i < n) {
      const cc = s.charCodeAt(i);
      if (cc === 62 /* > */) {
        i++;
        break;
      }
      if (cc === 47 /* / */) {
        if (s.charCodeAt(i + 1) !== 62) throw new Error("Malformed self-close");
        selfClosed = true;
        i += 2;
        break;
      }
      if (WS.has(cc)) {
        i++;
        continue;
      }
      // Attribute: name = "value" (or 'value').
      const eq = s.indexOf("=", i);
      if (eq < 0) throw new Error("Malformed attribute");
      const name = s.slice(i, eq).trim();
      let k = eq + 1;
      while (k < n && WS.has(s.charCodeAt(k))) k++;
      const q = s.charCodeAt(k);
      if (q !== 34 && q !== 39) throw new Error("Unquoted attribute value");
      const endQ = s.indexOf(String.fromCharCode(q), k + 1);
      if (endQ < 0) throw new Error("Unterminated attribute value");
      el.attrs.push([name, decodeEntities(s.slice(k + 1, endQ))]);
      i = endQ + 1;
    }
    if (!selfClosed) cur = el;
  }

  if (cur !== doc) throw new Error("Unclosed element at end of document");
  if (!doc.documentElement) throw new Error("Empty XML document");
  return doc;
}

// ---------------------------------------------------------------------------
// Serialization
// ---------------------------------------------------------------------------

function escText(s: string): string {
  if (!/[&<>]/.test(s)) return s;
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escAttr(s: string): string {
  if (!/[&<>"\t\n\r]/.test(s)) return s;
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/\t/g, "&#9;")
    .replace(/\n/g, "&#10;")
    .replace(/\r/g, "&#13;");
}

export function serializeFastXml(node: FastNode): string {
  const out: string[] = [];
  const write = (n: FastNode): void => {
    switch (n.nodeType) {
      case ELEMENT_NODE: {
        const el = n as FastElement;
        out.push("<", el.tagName);
        for (let i = 0; i < el.attrs.length; i++) {
          const [an, av] = el.attrs[i];
          out.push(" ", an, '="', escAttr(av), '"');
        }
        if (el.childNodes.length === 0) {
          out.push("/>");
        } else {
          out.push(">");
          for (const c of el.childNodes) write(c);
          out.push("</", el.tagName, ">");
        }
        break;
      }
      case TEXT_NODE:
        out.push(escText((n as FastText).data));
        break;
      case CDATA_NODE:
        out.push("<![CDATA[", (n as FastText).data, "]]>");
        break;
      case COMMENT_NODE:
        out.push("<!--", (n as FastText).data, "-->");
        break;
      case DOCUMENT_NODE:
        for (const c of (n as FastDocument).childNodes) write(c);
        break;
    }
  };
  write(node);
  return out.join("");
}
