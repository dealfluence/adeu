import { DocxPackage, Part, DocumentObject } from './docx/bridge.js';
import { findAllDescendants, findChild, parseXml } from './docx/dom.js';

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

export class CommentsManager {
  private _commentsPart: Part | null = null;
  private _extendedPart: Part | null = null;
  private _idsPart: Part | null = null;
  private _extensiblePart: Part | null = null;
  private _nextId: number | null = null;

  constructor(public doc: DocumentObject) {}

  public get commentsPart() {
    if (!this._commentsPart) {
      this._commentsPart = this._getOrCreateCommentsPart();
      this._ensureNamespaces();
    }
    return this._commentsPart;
  }

  public get extendedPart() {
    if (!this._extendedPart) this._extendedPart = this._getOrCreateExtendedPart();
    return this._extendedPart;
  }

  public get idsPart() {
    if (!this._idsPart) this._idsPart = this._getOrCreateIdsPart();
    return this._idsPart;
  }

  public get extensiblePart() {
    if (!this._extensiblePart) this._extensiblePart = this._getOrCreateExtensiblePart();
    return this._extensiblePart;
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

  private _generateHexId(): string {
    return Math.floor(Math.random() * 0xFFFFFFFF).toString(16).toUpperCase().padStart(8, '0');
  }

  private _getInitials(author: string): string {
    if (!author) return '';
    return author.split(' ').filter(Boolean).map(p => p[0]).join('').toUpperCase();
  }

  private _findParaIdForComment(commentId: string): string | null {
    if (!this._commentsPart) return null;
    for (const c of findAllDescendants(this._commentsPart._element, 'w:comment')) {
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

  public addComment(author: string, text: string, parentId: string | null = null): string {
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

    const extPart = this._getExistingPartByType(CT.EXTENDED);
    if (parentId && !extPart) {
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
      const parentParaId = parentId ? this._findThreadRootParaId(parentId) : null;
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
      commentIdEl.setAttribute('w16cid:durableId', this._generateHexId());
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
    if (!this._commentsPart) return;

    let commentEl: Element | null = null;
    for (const c of findAllDescendants(this._commentsPart._element, 'w:comment')) {
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
              for (const c of findAllDescendants(this._commentsPart._element, 'w:comment')) {
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