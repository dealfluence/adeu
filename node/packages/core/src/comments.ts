import { DocxPackage, Part, DocumentObject } from './docx/bridge.js';
import { findAllDescendants, findChild, parseXml } from './docx/dom.js';
import {
  generateLongHexNumber,
  isWordReadableLongHexNumber,
  toLongHexNumber,
} from './docx/long-hex-number.js';

const NS = {
  w: 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
  w14: 'http://schemas.microsoft.com/office/word/2010/wordml',
  w15: 'http://schemas.microsoft.com/office/word/2012/wordml',
  w16cid: 'http://schemas.microsoft.com/office/word/2016/wordml/cid',
  w16cex: 'http://schemas.microsoft.com/office/word/2018/wordml/cex',
  mc: 'http://schemas.openxmlformats.org/markup-compatibility/2006'
};

const CT = {
  COMMENTS: 'application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml',
  EXTENDED: 'application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml',
  IDS: 'application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml',
  EXTENSIBLE: 'application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml'
};

const RT = {
  COMMENTS: 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments',
  EXTENDED: 'http://schemas.microsoft.com/office/2011/relationships/commentsExtended',
  IDS: 'http://schemas.microsoft.com/office/2016/09/relationships/commentsIds',
  EXTENSIBLE: 'http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible'
};

// ---------------------------------------------------------------------------
// Repairing ST_LongHexNumbers Adeu did not mint
// ---------------------------------------------------------------------------
//
// The generators guarantee that every id Adeu MINTS is one Word will keep. They
// can say nothing about the ids Adeu READS. A document arriving with
// `w14:paraId="D2AEAE20"` - legal against the schema, discarded by Word on load
// - takes the reply threaded onto it down with it, and no amount of correct
// minting prevents that (2026-08-12 B6, western-district demo).
//
// Repairing means rewriting a value that other parts point AT, so the attribute
// groups below exist to keep a repair from breaking the references it was
// supposed to preserve. Mirrors comments.py.

/** One logical paragraph identity, spelled four ways across three parts. Word
 *  consults all of them; repair them together or the comment drops out of the
 *  modern-comments path exactly as if it had not been repaired at all. */
export const PARA_ID_ATTRIBUTES = [
  'w14:paraId',
  'w15:paraId',
  'w15:paraIdParent',
  'w16cid:paraId',
];

/** The comment's durable identity: commentsIds mints it, commentsExtensible
 *  points back at it. Out of range, the anchor collapses to a point (B3). */
export const DURABLE_ID_ATTRIBUTES = ['w16cid:durableId', 'w16cex:durableId'];

/** ST_LongHexNumbers nothing else references, so they can be folded in place.
 *  Folding (rather than re-minting) keeps equal rsids equal, which is the only
 *  thing an rsid means, and keeps `w14:textId` on the element whose
 *  `w14:paraId` it versions - [MS-DOCX] 2.6.2.6 requires the two to travel
 *  together. */
export const STANDALONE_ID_ATTRIBUTES = [
  'w14:textId',
  'w:rsidR',
  'w:rsidRPr',
  'w:rsidRDefault',
  'w:rsidP',
  'w:rsidDel',
  'w:rsidTr',
];

/**
 * Raised when a reply cannot be threaded onto its parent comment.
 *
 * A `reply` that quietly becomes a new top-level thread is worse than a failed
 * call: apply_review_actions reports success, the agent believes it answered
 * the reviewer, and it keeps acting on a success it never got
 * (BUG_comment_threading_anchoring_and_typography.md B1). So threading is
 * resolved BEFORE any XML is written, and an unresolvable parent is loud.
 */
export class CommentThreadingError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CommentThreadingError';
  }
}

/** Depth-first walk over an element and every element descendant. */
function* walkElements(element: Element): Generator<Element> {
  yield element;
  for (let i = 0; i < element.childNodes.length; i++) {
    const child = element.childNodes[i] as Element;
    if (child.nodeType === 1) yield* walkElements(child);
  }
}

export class CommentsManager {
  private _commentsPart: Part | null = null;
  private _extendedPart: Part | null = null;
  private _idsPart: Part | null = null;
  private _extensiblePart: Part | null = null;
  private _nextId: number | null = null;

  constructor(public doc: DocumentObject) {}

  public get commentsPart(): Part {
    if (!this._commentsPart) {
      this._commentsPart = this._getOrCreateCommentsPart();
      this._ensureNamespaces();
    }
    return this._commentsPart!;
  }

  public get extendedPart(): Part {
    if (!this._extendedPart) this._extendedPart = this._getOrCreateExtendedPart();
    return this._extendedPart!;
  }

  public get idsPart(): Part {
    if (!this._idsPart) this._idsPart = this._getOrCreateIdsPart();
    return this._idsPart!;
  }

  public get extensiblePart(): Part {
    if (!this._extensiblePart) this._extensiblePart = this._getOrCreateExtensiblePart();
    return this._extensiblePart!;
  }

  public get nextId(): number {
    if (this._nextId === null) this._nextId = this._getNextCommentId();
    return this._nextId;
  }

  public set nextId(value: number) {
    this._nextId = value;
  }

  private _getExistingPartByType(contentType: string): Part | null {
    return this.doc.pkg.parts.find(p => p.contentType === contentType) || null;
  }

  private _linkPart(part: Part, relType: string): Part {
    for (const rel of this.doc.part.rels.values()) {
      if (!rel.isExternal && rel.target === part.partname.split('/').pop()) {
        return part;
      }
    }
    this.doc.relateTo(part, relType);
    return part;
  }

  private _getOrCreateCommentsPart(): Part {
    let part = this._getExistingPartByType(CT.COMMENTS);
    if (part) return this._linkPart(part, RT.COMMENTS);

    const partname = this.doc.pkg.nextPartname('/word/comments%d.xml');
    const xml = `<w:comments xmlns:w="${NS.w}" xmlns:w14="${NS.w14}" xmlns:w15="${NS.w15}" xmlns:w16cid="${NS.w16cid}" xmlns:w16cex="${NS.w16cex}" xmlns:mc="${NS.mc}" mc:Ignorable="w14 w15 w16cid w16cex"></w:comments>`;
    part = this.doc.pkg.addPart(partname, CT.COMMENTS, xml);
    this.doc.relateTo(part, RT.COMMENTS);
    return part;
  }

  private _getOrCreateExtendedPart(): Part {
    let part = this._getExistingPartByType(CT.EXTENDED);
    if (part) return this._linkPart(part, RT.EXTENDED);

    const partname = this.doc.pkg.nextPartname('/word/commentsExtended%d.xml');
    const xml = `<w15:commentsEx xmlns:w15="${NS.w15}"></w15:commentsEx>`;
    part = this.doc.pkg.addPart(partname, CT.EXTENDED, xml);
    this.doc.relateTo(part, RT.EXTENDED);
    return part;
  }

  private _getOrCreateIdsPart(): Part {
    let part = this._getExistingPartByType(CT.IDS);
    if (part) return this._linkPart(part, RT.IDS);

    const partname = this.doc.pkg.nextPartname('/word/commentsIds%d.xml');
    const xml = `<w16cid:commentsIds xmlns:w16cid="${NS.w16cid}"></w16cid:commentsIds>`;
    part = this.doc.pkg.addPart(partname, CT.IDS, xml);
    this.doc.relateTo(part, RT.IDS);
    return part;
  }

  private _getOrCreateExtensiblePart(): Part {
    let part = this._getExistingPartByType(CT.EXTENSIBLE);
    if (part) return this._linkPart(part, RT.EXTENSIBLE);

    const partname = this.doc.pkg.nextPartname('/word/commentsExtensible%d.xml');
    const xml = `<w16cex:commentsExtensible xmlns:w16cex="${NS.w16cex}"></w16cex:commentsExtensible>`;
    part = this.doc.pkg.addPart(partname, CT.EXTENSIBLE, xml);
    this.doc.relateTo(part, RT.EXTENSIBLE);
    return part;
  }

  private _ensureNamespaces() {
    // When the comments part already existed (e.g. a legacy or pandoc-produced
    // document) its root <w:comments> may omit the namespaces we rely on —
    // most importantly w14, which qualifies the w14:paraId / w14:textId
    // attributes we write on each comment paragraph. Without the declaration
    // the serialised XML is invalid ("Namespace prefix w14 ... is not defined").
    // Declare any missing namespace prefixes on the existing root element.
    const root = this._commentsPart?._element;
    if (!root) return;

    const required: [string, string][] = [
      ['xmlns:w', NS.w],
      ['xmlns:w14', NS.w14],
      ['xmlns:w15', NS.w15],
      ['xmlns:w16cid', NS.w16cid],
      ['xmlns:w16cex', NS.w16cex],
      ['xmlns:mc', NS.mc],
    ];
    for (const [attr, uri] of required) {
      if (!root.getAttribute(attr)) {
        root.setAttribute(attr, uri);
      }
    }
  }

  private _getNextCommentId(): number {
    const ids = [0];
    const part = this._getExistingPartByType(CT.COMMENTS);
    if (part) {
      const comments = findAllDescendants(part._element, 'w:comment');
      for (const c of comments) {
        const idStr = c.getAttribute('w:id');
        if (idStr) ids.push(parseInt(idStr, 10) || 0);
      }
    }
    return Math.max(...ids) + 1;
  }

  // Every id below is an ST_LongHexNumber and comes from ONE generator.
  //
  // These stay as named aliases only so the call sites read as what they mint;
  // they must never diverge. Word parses ST_LongHexNumber as a SIGNED 32-bit
  // integer for ALL of them, silently discarding and regenerating anything
  // outside (0x00000000, 0x80000000) — an out-of-range paraId drops replies
  // out of their thread (B5), an out-of-range durableId collapses the comment's
  // anchor (B3), and a zero paraId makes Word reject the file outright. The
  // earlier belief that only durableId was constrained is what produced B5;
  // see docx/long-hex-number.ts and BUG_paraId_signed_int32_thread_collapse.md.

  /** `w14:paraId` (threading identity) and `w:rsid*` (revision grouping). */
  private _generateHexId(): string {
    return generateLongHexNumber();
  }

  /** `w16cid:durableId` — the identity the comment anchor binds to. */
  private _generateDurableId(): string {
    return generateLongHexNumber();
  }

  private _getInitials(author: string): string {
    if (!author) return '';
    return author.split(' ').filter(Boolean).map(p => p[0]).join('').toUpperCase();
  }

  /**
   * True when the package already carries a comments part (loaded or not).
   *
   * Read paths must use this guard instead of testing the raw backing field
   * `_commentsPart`: on a fresh manager that field is null until the lazy
   * `commentsPart` getter populates it, so guarding on it silently no-ops even
   * though the document HAS comments (Python's _has_comments_part, QA
   * 2026-07-17 F3 — the Node twin kept the stale field check). Checking the
   * package keeps the other guarantee: a document with no comments part never
   * has one created as a side effect of a read.
   */
  private _hasCommentsPart(): boolean {
    return (
      this._commentsPart !== null || this._getExistingPartByType(CT.COMMENTS) !== null
    );
  }

  private _findParaIdForComment(commentId: string): string | null {
    if (!this._hasCommentsPart()) return null;
    for (const c of findAllDescendants(this.commentsPart._element, 'w:comment')) {
      if (c.getAttribute('w:id') === commentId) {
        for (const p of findAllDescendants(c, 'w:p')) {
          const pid = p.getAttribute('w14:paraId');
          if (pid) return pid;
        }
      }
    }
    return null;
  }

  private _findThreadRootParaId(commentId: string): string | null {
    const directParaId = this._findParaIdForComment(commentId);
    const extPart = this._getExistingPartByType(CT.EXTENDED);
    if (!directParaId || !extPart) return directParaId;

    for (let i = 0; i < extPart._element.childNodes.length; i++) {
      const child = extPart._element.childNodes[i] as Element;
      if (child.nodeType !== 1) continue;
      if (child.getAttribute('w15:paraId') === directParaId) {
        const parent = child.getAttribute('w15:paraIdParent');
        if (parent) return parent;
      }
    }
    return directParaId;
  }

  /**
   * Gives an existing comment a modern paragraph identity so a reply can thread
   * onto it, returning that paraId (null if the comment does not exist, or has
   * no paragraph to identify).
   *
   * A comment written by pre-2013 Word — or by any generator that skips the
   * modern-comments extensions — has no `w14:paraId`, so `_findThreadRootParaId`
   * resolves nothing and `w15:paraIdParent` never gets written: the "reply"
   * silently becomes a second top-level thread (B1). Minting the missing
   * identity is the repair; it is idempotent and leaves the comment's body,
   * author and date untouched.
   *
   * The paraId is registered in commentsExtended AND commentsIds together: Word
   * consults both, and a paraId present in one but not the other drops the
   * comment out of the modern-comments path entirely.
   */
  /**
   * Every comment part that ALREADY exists, as a mutable element.
   *
   * Deliberately not the `commentsPart` / `extendedPart` getters: those CREATE
   * the part they cannot find, and a repair pass that invents a
   * commentsExtended part for a document that has no comments would be a side
   * effect nobody asked for.
   */
  private _existingCommentPartElements(): Element[] {
    const elements: Element[] = [];
    for (const contentType of [CT.COMMENTS, CT.EXTENDED, CT.IDS, CT.EXTENSIBLE]) {
      const part = this._getExistingPartByType(contentType);
      if (part) elements.push(part._element);
    }
    return elements;
  }

  /**
   * A legal id for `value` that the comment parts are not already using.
   *
   * Folding first (clearing the top bit, which is what Word does to the value
   * anyway) keeps the repair DETERMINISTIC: the same document repaired twice
   * produces the same ids, so a re-run is a no-op rather than a fresh set of
   * anchors. `D2AEAE20 -> 52AEAE20`, Word-verified against the western-district
   * document.
   *
   * The collision check is not belt-and-braces. [MS-DOCX] 2.6.2.4 requires
   * `w14:paraId` to be unique within the part, and folding is exactly the
   * operation that can violate it: `D2AEAE20` and `52AEAE20` fold to the same
   * value, so a document containing both would end up with one id naming two
   * paragraphs and a `w15:paraIdParent` that no longer says which thread it
   * means.
   */
  private _freeLongHexNumber(value: string, taken: Set<string>): string {
    const parsed = parseInt(value, 16);
    let candidate = Number.isNaN(parsed)
      ? generateLongHexNumber()
      : toLongHexNumber(parsed);
    while (taken.has(candidate)) candidate = generateLongHexNumber();
    return candidate;
  }

  /**
   * Bring every ST_LongHexNumber in the comment parts into range.
   *
   * B5 masked the generators, which fixed every id Adeu mints and none of the
   * ids it inherits. B6 is the second kind: the western-district demo was
   * handed a comment carrying `w14:paraId="D2AEAE20"`,
   * `_adoptIntoModernComments` reused it verbatim because it was present, and
   * the reply's `w15:paraIdParent` was written to point at a value Word
   * discards on load. Every check passed on the way out - the reply IS
   * parented, `CommentThreadingError` correctly did not fire - and the thread
   * still collapsed the moment the document was opened.
   *
   * Whole-part, not just the comment being replied to: Word renumbers a PART
   * when it finds a bad id in it, so leaving one bad rsid behind in
   * comments.xml re-arms the renumbering pass that de-threads the reply.
   *
   * A no-op on a healthy document. It must stay that way: a pass that re-mints
   * unconditionally would churn every paraId on every save and invalidate every
   * `{#cell:<paraId>}` anchor the caller is holding, which is the damage it
   * exists to prevent.
   */
  private _repairInheritedLongHexNumbers(): void {
    const elements = this._existingCommentPartElements();
    if (elements.length === 0) return;

    const remapFor = (attributes: string[]): Map<string, string> => {
      const taken = new Set<string>();
      const broken: string[] = [];
      for (const root of elements) {
        for (const el of walkElements(root)) {
          for (const attribute of attributes) {
            const value = el.getAttribute(attribute);
            if (!value) continue;
            if (isWordReadableLongHexNumber(value)) taken.add(value.toUpperCase());
            else if (!broken.includes(value)) broken.push(value);
          }
        }
      }
      const remap = new Map<string, string>();
      for (const value of broken) {
        const repaired = this._freeLongHexNumber(value, taken);
        taken.add(repaired);
        remap.set(value, repaired);
      }
      return remap;
    };

    const paraRemap = remapFor(PARA_ID_ATTRIBUTES);
    const durableRemap = remapFor(DURABLE_ID_ATTRIBUTES);

    for (const root of elements) {
      for (const el of walkElements(root)) {
        for (const attribute of PARA_ID_ATTRIBUTES) {
          const repaired = paraRemap.get(el.getAttribute(attribute) ?? '');
          if (repaired) el.setAttribute(attribute, repaired);
        }
        for (const attribute of DURABLE_ID_ATTRIBUTES) {
          const repaired = durableRemap.get(el.getAttribute(attribute) ?? '');
          if (repaired) el.setAttribute(attribute, repaired);
        }
        for (const attribute of STANDALONE_ID_ATTRIBUTES) {
          const value = el.getAttribute(attribute);
          if (value && !isWordReadableLongHexNumber(value)) {
            el.setAttribute(attribute, this._freeLongHexNumber(value, new Set()));
          }
        }
      }
    }
  }

  private _adoptIntoModernComments(commentId: string): string | null {
    if (!this._hasCommentsPart()) return null;

    let commentEl: Element | null = null;
    for (const c of findAllDescendants(this.commentsPart._element, 'w:comment')) {
      if (c.getAttribute('w:id') === commentId) {
        commentEl = c;
        break;
      }
    }
    if (!commentEl) return null;

    const paragraphs = findAllDescendants(commentEl, 'w:p');
    if (paragraphs.length === 0) return null;

    let paraId: string | null = null;
    for (const p of paragraphs) {
      const pid = p.getAttribute('w14:paraId');
      if (pid) {
        paraId = pid;
        break;
      }
    }
    if (!paraId) {
      paraId = this._generateHexId();
      paragraphs[0].setAttribute('w14:paraId', paraId);
    }

    const hasChild = (part: Part, attr: string): boolean => {
      for (let i = 0; i < part._element.childNodes.length; i++) {
        const child = part._element.childNodes[i] as Element;
        if (child.nodeType === 1 && child.getAttribute(attr) === paraId) return true;
      }
      return false;
    };

    if (!hasChild(this.extendedPart, 'w15:paraId')) {
      // Thread ROOT: no w15:paraIdParent.
      const exDoc = this.extendedPart._element.ownerDocument!;
      const commentEx = exDoc.createElement('w15:commentEx');
      commentEx.setAttribute('w15:paraId', paraId);
      commentEx.setAttribute('w15:done', '0');
      this.extendedPart._element.appendChild(commentEx);
    }

    if (!hasChild(this.idsPart, 'w16cid:paraId')) {
      const idsDoc = this.idsPart._element.ownerDocument!;
      const commentIdEl = idsDoc.createElement('w16cid:commentId');
      commentIdEl.setAttribute('w16cid:paraId', paraId);
      commentIdEl.setAttribute('w16cid:durableId', this._generateDurableId());
      this.idsPart._element.appendChild(commentIdEl);
    }

    return paraId;
  }

  /**
   * The paraId a reply to `parentId` must point at, repairing a parent that
   * predates modern comments. Null means threading is impossible and the caller
   * must fail loudly rather than mint a top-level comment.
   *
   * The repair pass runs FIRST, because every lookup below reads paraIds and a
   * lookup that returns an id Word discards is worse than one that returns
   * nothing: `null` raises CommentThreadingError and leaves the document alone,
   * while a doomed id is reported as a successful reply and collapses the
   * thread on load (B6).
   *
   * The root lookup runs next so a reply-to-a-reply still flattens onto the
   * thread root (modern Word's model). The adoption pass then runs
   * unconditionally — it is idempotent, and it also backfills a parent that HAS
   * a w14:paraId but is missing from commentsExtended / commentsIds: Word
   * consults both, so a paraIdParent pointing at an unregistered paragraph
   * drops the reply out of its thread just as surely as a missing attribute
   * would.
   */
  public resolveThreadParentParaId(parentId: string): string | null {
    this._repairInheritedLongHexNumbers();
    const rootParaId = this._findThreadRootParaId(parentId);
    const adoptedParaId = this._adoptIntoModernComments(parentId);
    return rootParaId ?? adoptedParaId;
  }

  public addComment(author: string, text: string, parentId: string | null = null): string {
    // Before anything else, and for top-level comments too: the paraIds this
    // document arrived with are about to share a part with the ones we are
    // about to mint, and Word renumbers the whole part if any of them is out of
    // range (B6).
    this._repairInheritedLongHexNumbers();

    // Snapshot the modern-comments state BEFORE resolving threading: the legacy
    // `w15:p` fallback below keys on whether the document was already on the
    // modern path, and repairing a legacy parent may create the
    // commentsExtended part as a side effect.
    const extPartExisted = this._getExistingPartByType(CT.EXTENDED) !== null;

    // Resolve threading BEFORE writing anything. A reply whose parent cannot be
    // resolved used to be written anyway, minus its w15:paraIdParent — i.e. as
    // a brand-new top-level thread, reported as applied (B1). Throwing here
    // leaves the document untouched.
    let parentParaId: string | null = null;
    if (parentId !== null && parentId !== undefined) {
      parentParaId = this.resolveThreadParentParaId(parentId);
      if (!parentParaId) {
        throw new CommentThreadingError(
          `Cannot thread a reply onto comment Com:${parentId}: the comment has no resolvable ` +
            `paragraph identity (w14:paraId) in word/comments.xml, so Word would render the ` +
            `reply as a separate top-level comment instead of a reply. Refusing to create an ` +
            `unthreaded comment.`,
        );
      }
    }

    const commentId = this.nextId.toString();
    this.nextId++;
    const now = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');

    const doc = this.commentsPart._element.ownerDocument!;
    const comment = doc.createElement('w:comment');
    comment.setAttribute('w:id', commentId);
    comment.setAttribute('w:author', author);
    comment.setAttribute('w:date', now);
    
    const initials = this._getInitials(author);
    if (initials) comment.setAttribute('w:initials', initials);

    if (parentId && !extPartExisted) {
      comment.setAttribute('w15:p', parentId);
    }

    const paraId = this._generateHexId();
    const rsid = this._generateHexId();

    const p = doc.createElement('w:p');
    p.setAttribute('w14:paraId', paraId);
    p.setAttribute('w14:textId', '77777777');
    p.setAttribute('w:rsidR', rsid);
    p.setAttribute('w:rsidRDefault', rsid);
    p.setAttribute('w:rsidP', rsid);

    const pPr = doc.createElement('w:pPr');
    const pStyle = doc.createElement('w:pStyle');
    pStyle.setAttribute('w:val', 'CommentText');
    pPr.appendChild(pStyle);
    p.appendChild(pPr);

    const rRef = doc.createElement('w:r');
    const rPrRef = doc.createElement('w:rPr');
    const rStyleRef = doc.createElement('w:rStyle');
    rStyleRef.setAttribute('w:val', 'CommentReference');
    rPrRef.appendChild(rStyleRef);
    rRef.appendChild(rPrRef);
    rRef.appendChild(doc.createElement('w:annotationRef'));
    p.appendChild(rRef);

    const r = doc.createElement('w:r');
    const t = doc.createElement('w:t');
    t.textContent = text;
    r.appendChild(t);
    p.appendChild(r);
    comment.appendChild(p);

    this.commentsPart._element.appendChild(comment);

    if (this.extendedPart) {
      // parentParaId was resolved (and any legacy parent repaired) at the top of
      // this method, so it is either a real thread root or the call already
      // threw. Never re-resolve here: silently falling back to null is exactly
      // how a reply became a thread root (B1).
      const exDoc = this.extendedPart._element.ownerDocument!;
      const commentEx = exDoc.createElement('w15:commentEx');
      commentEx.setAttribute('w15:paraId', paraId);
      if (parentParaId) commentEx.setAttribute('w15:paraIdParent', parentParaId);
      commentEx.setAttribute('w15:done', '0');
      this.extendedPart._element.appendChild(commentEx);
    }

    if (this.idsPart) {
      const idsDoc = this.idsPart._element.ownerDocument!;
      const commentIdEl = idsDoc.createElement('w16cid:commentId');
      commentIdEl.setAttribute('w16cid:paraId', paraId);
      commentIdEl.setAttribute('w16cid:durableId', this._generateDurableId());
      this.idsPart._element.appendChild(commentIdEl);
    }

    if (this.extensiblePart) {
      let durableId: string | null = null;
      for (let i = 0; i < this.idsPart._element.childNodes.length; i++) {
        const child = this.idsPart._element.childNodes[i] as Element;
        if (child.nodeType === 1 && child.getAttribute('w16cid:paraId') === paraId) {
          durableId = child.getAttribute('w16cid:durableId');
          break;
        }
      }
      if (durableId) {
        const cexDoc = this.extensiblePart._element.ownerDocument!;
        const extEl = cexDoc.createElement('w16cex:commentExtensible');
        extEl.setAttribute('w16cex:durableId', durableId);
        extEl.setAttribute('w16cex:dateUtc', now);
        this.extensiblePart._element.appendChild(extEl);
      }
    }

    return commentId;
  }

  public deleteComment(commentId: string) {
    if (!this.commentsPart) return;

    let commentEl: Element | null = null;
    for (const c of findAllDescendants(this.commentsPart._element, 'w:comment')) {
      if (c.getAttribute('w:id') === commentId) {
        commentEl = c;
        break;
      }
    }

    if (!commentEl) return;

    let paraId: string | null = null;
    for (const p of findAllDescendants(commentEl, 'w:p')) {
      const pid = p.getAttribute('w14:paraId');
      if (pid) {
        paraId = pid;
        break;
      }
    }

    if (paraId) {
      const repliesToDelete: string[] = [];
      if (this.extendedPart) {
        for (let i = 0; i < this.extendedPart._element.childNodes.length; i++) {
          const child = this.extendedPart._element.childNodes[i] as Element;
          if (child.nodeType !== 1) continue;
          
          if (child.getAttribute('w15:paraIdParent') === paraId) {
            const childParaId = child.getAttribute('w15:paraId');
            if (childParaId) {
              for (const c of findAllDescendants(this.commentsPart._element, 'w:comment')) {
                for (const p of findAllDescendants(c, 'w:p')) {
                  if (p.getAttribute('w14:paraId') === childParaId) {
                    const cid = c.getAttribute('w:id');
                    if (cid) repliesToDelete.push(cid);
                    break;
                  }
                }
              }
            }
          }
        }
      }

      for (const repId of repliesToDelete) {
        this.deleteComment(repId);
      }

      let durableId: string | null = null;

      if (this.idsPart) {
        const toRemove: Element[] = [];
        for (let i = 0; i < this.idsPart._element.childNodes.length; i++) {
          const child = this.idsPart._element.childNodes[i] as Element;
          if (child.nodeType === 1 && child.getAttribute('w16cid:paraId') === paraId) {
            durableId = child.getAttribute('w16cid:durableId');
            toRemove.push(child);
          }
        }
        toRemove.forEach(c => this.idsPart!._element.removeChild(c));
      }

      if (this.extendedPart) {
        const toRemove: Element[] = [];
        for (let i = 0; i < this.extendedPart._element.childNodes.length; i++) {
          const child = this.extendedPart._element.childNodes[i] as Element;
          if (child.nodeType === 1 && child.getAttribute('w15:paraId') === paraId) {
            toRemove.push(child);
          }
        }
        toRemove.forEach(c => this.extendedPart!._element.removeChild(c));
      }

      if (durableId && this.extensiblePart) {
        const toRemove: Element[] = [];
        for (let i = 0; i < this.extensiblePart._element.childNodes.length; i++) {
          const child = this.extensiblePart._element.childNodes[i] as Element;
          if (child.nodeType === 1 && child.getAttribute('w16cex:durableId') === durableId) {
            toRemove.push(child);
          }
        }
        toRemove.forEach(c => this.extensiblePart!._element.removeChild(c));
      }
    }

    if (commentEl.parentNode) {
      commentEl.parentNode.removeChild(commentEl);
    }
  }
}

// Keep the global extraction function matching Python behavior
export function extract_comments_data(pkg: DocxPackage): Record<string, any> {
  // Temporary bridge to use the new class
  const docObj = {
    pkg,
    part: pkg.mainDocumentPart,
    relateTo: () => {} // Mock since extraction is read-only
  } as unknown as DocumentObject;
  
  const mgr = new CommentsManager(docObj);
  const data: Record<string, any> = {};
  
  const part = pkg.parts.find(p => p.contentType === CT.COMMENTS);
  if (!part) return data;

  const para_id_to_cid: Record<string, string> = {};
  const comments = findAllDescendants(part._element, 'w:comment');

  for (const c of comments) {
    const c_id = c.getAttribute('w:id');
    if (!c_id) continue;

    const c_author = c.getAttribute('w:author') || 'Unknown';
    const c_date = c.getAttribute('w:date') || '';
    
    let is_resolved = false;
    const val = c.getAttribute('w15:done');
    if (val === '1' || val === 'true' || val === 'on') is_resolved = true;

    let parent_id = c.getAttribute('w15:p') || null;

    const p_elems = findAllDescendants(c, 'w:p');
    for (const p of p_elems) {
      const pid = p.getAttribute('w14:paraId');
      if (pid) para_id_to_cid[pid] = c_id;
    }

    const text_parts: string[] = [];
    for (const p of p_elems) {
      const t_elems = findAllDescendants(p, 'w:t');
      for (const t of t_elems) {
        if (t.textContent) text_parts.push(t.textContent);
      }
      text_parts.push('\n');
    }
    const full_text = text_parts.join('').trim();

    data[c_id] = {
      author: c_author,
      text: full_text,
      date: c_date,
      resolved: is_resolved,
      parent_id: parent_id,
    };
  }

  const extPart = pkg.parts.find(p => p.contentType === CT.EXTENDED);
  if (extPart) {
    const children = extPart._element.childNodes;
    for (let i = 0; i < children.length; i++) {
      const child = children[i] as Element;
      if (child.nodeType !== 1) continue;

      const para_id = child.getAttribute('w15:paraId');
      const parent_para_id = child.getAttribute('w15:paraIdParent');
      const done_val = child.getAttribute('w15:done');

      if (para_id) {
        const c_id = para_id_to_cid[para_id];
        if (c_id && data[c_id]) {
          if (parent_para_id) {
            const p_id = para_id_to_cid[parent_para_id];
            if (p_id) data[c_id].parent_id = p_id;
          }
          if (done_val === '1' || done_val === 'true' || done_val === 'on') {
            data[c_id].resolved = true;
          }
        }
      }
    }
  }

  return data;
}