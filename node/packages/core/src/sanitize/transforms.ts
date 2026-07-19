import { DocumentObject, Part } from '../docx/bridge.js';
import { findAllDescendants, findChild, findChildren } from '../docx/dom.js';
import { extract_comments_data, CommentsManager } from '../comments.js';
import { RedlineEngine } from '../engine.js';

export function findDescendantsByLocalName(element: Element, localName: string): Element[] {
  const result: Element[] = [];
  const all = element.getElementsByTagName('*');
  for (let i = 0; i < all.length; i++) {
    const tag = all[i].tagName;
    if (tag === localName || tag.endsWith(':' + localName)) {
      result.push(all[i]);
    }
  }
  return result;
}

export function coalesce_runs(doc: DocumentObject): string[] {
  let count = 0;

  function areRunsIdentical(rPr1: Element | null, rPr2: Element | null): boolean {
    const xml1 = rPr1 ? rPr1.toString() : '';
    const xml2 = rPr2 ? rPr2.toString() : '';
    return xml1 === xml2;
  }

  function hasSpecialContent(run: Element): boolean {
    const safeTags = ['w:t', 'w:tab', 'w:br', 'w:cr', 'w:delText', 'w:rPr'];
    for (let i = 0; i < run.childNodes.length; i++) {
      const child = run.childNodes[i];
      if (child.nodeType === 1) {
        const tag = (child as Element).tagName;
        if (!safeTags.includes(tag)) return true;
      }
    }
    return false;
  }

  function coalesceContainer(container: Element) {
    const children = Array.from(container.childNodes).filter(n => n.nodeType === 1) as Element[];
    let i = 0;
    while (i < children.length - 1) {
      const curr = children[i];
      const nxt = children[i + 1];

      if (curr.tagName === 'w:r' && nxt.tagName === 'w:r') {
        if (!hasSpecialContent(curr) && !hasSpecialContent(nxt)) {
          const rPr1 = findChild(curr, 'w:rPr');
          const rPr2 = findChild(nxt, 'w:rPr');
          if (areRunsIdentical(rPr1, rPr2)) {
            let last_t: Element | null = null;
            for (let c = 0; c < curr.childNodes.length; c++) {
              const child = curr.childNodes[c];
              if (child.nodeType === 1 && ((child as Element).tagName === 'w:t' || (child as Element).tagName === 'w:delText')) {
                last_t = child as Element;
              }
            }

            const nxtChildren = Array.from(nxt.childNodes).filter(n => n.nodeType === 1) as Element[];
            for (const child of nxtChildren) {
              if (child.tagName === 'w:rPr') continue;
              if ((child.tagName === 'w:t' || child.tagName === 'w:delText') && last_t && last_t.tagName === child.tagName) {
                const t1 = last_t.textContent || '';
                const t2 = child.textContent || '';
                const combined = t1 + t2;
                last_t.textContent = combined;
                if (combined.trim() !== combined) {
                  last_t.setAttribute('xml:space', 'preserve');
                }
              } else {
                curr.appendChild(child);
                if (child.tagName === 'w:t' || child.tagName === 'w:delText') {
                  last_t = child;
                }
              }
            }
            container.removeChild(nxt);
            children.splice(i + 1, 1);
            count++;
            continue; 
          }
        }
      }

      if (['w:ins', 'w:del', 'w:hyperlink', 'w:sdt', 'w:smartTag', 'w:fldSimple', 'w:sdtContent'].includes(curr.tagName)) {
        coalesceContainer(curr);
      }
      i++;
    }
    
    if (children.length > 0) {
      const last = children[children.length - 1];
      if (['w:ins', 'w:del', 'w:hyperlink', 'w:sdt', 'w:smartTag', 'w:fldSimple', 'w:sdtContent'].includes(last.tagName)) {
        coalesceContainer(last);
      }
    }
  }

  const paragraphs = findAllDescendants(doc.element, 'w:p');
  for (const p of paragraphs) coalesceContainer(p);

  return count ? [`Adjacent identical runs coalesced: ${count}`] : [];
}

export function strip_rsid(doc: DocumentObject): string[] {
  let count = 0;
  const rsidAttrs = ['w:rsidR', 'w:rsidRPr', 'w:rsidRDefault', 'w:rsidP', 'w:rsidDel', 'w:rsidSect', 'w:rsidTr'];
  
  const all = doc.element.getElementsByTagName('*');
  for (let i = 0; i < all.length; i++) {
    for (const attr of rsidAttrs) {
      if (all[i].hasAttribute(attr)) {
        all[i].removeAttribute(attr);
        count++;
      }
    }
  }
  
  const rsidsElements = findAllDescendants(doc.element, 'w:rsids');
  for (const el of rsidsElements) {
    if (el.parentNode) {
      el.parentNode.removeChild(el);
      count++;
    }
  }
  
  return count ? [`rsid attributes: ${count} removed`] : [];
}

export function strip_para_ids(doc: DocumentObject): string[] {
  let count = 0;
  const attrs = ['w14:paraId', 'w14:textId'];
  const all = doc.element.getElementsByTagName('*');
  for (let i = 0; i < all.length; i++) {
    for (const attr of attrs) {
      if (all[i].hasAttribute(attr)) {
        all[i].removeAttribute(attr);
        count++;
      }
    }
  }
  return count ? [`Paragraph/text IDs: ${count} removed`] : [];
}

export function strip_proof_errors(doc: DocumentObject): string[] {
  const elements = findAllDescendants(doc.element, 'w:proofErr');
  elements.forEach(el => el.parentNode?.removeChild(el));
  return elements.length ? [`Spell check markers: ${elements.length} removed`] : [];
}

export function strip_empty_properties(doc: DocumentObject): string[] {
  let count = 0;
  for (const tag of ['w:rPr', 'w:pPr']) {
    const elements = findAllDescendants(doc.element, tag);
    for (const el of elements) {
      if (el.childNodes.length === 0 || (el.childNodes.length === 1 && el.childNodes[0].nodeType === 3 && !el.childNodes[0].textContent?.trim())) {
        el.parentNode?.removeChild(el);
        count++;
      }
    }
  }
  return count ? [`Empty property elements: ${count} removed`] : [];
}

export function strip_hidden_text(doc: DocumentObject): string[] {
  let count = 0;
  const elements = findAllDescendants(doc.element, 'w:rPr');
  for (const rPr of elements) {
    if (findChild(rPr, 'w:vanish') || findChild(rPr, 'w:webHidden')) {
      const run = rPr.parentNode as Element;
      if (run && run.tagName === 'w:r' && run.parentNode) {
        run.parentNode.removeChild(run);
        count++;
      }
    }
  }
  return count ? [`Hidden text runs: ${count} removed`] : [];
}

export function count_tracked_changes(doc: DocumentObject): [number, number, number] {
  const ins = findAllDescendants(doc.element, 'w:ins').length;
  const del = findAllDescendants(doc.element, 'w:del').length;
  const fmt = findAllDescendants(doc.element, 'w:rPrChange').length +
              findAllDescendants(doc.element, 'w:pPrChange').length +
              findAllDescendants(doc.element, 'w:sectPrChange').length;
  return [ins, del, fmt];
}

export function get_track_change_authors(doc: DocumentObject): Set<string> {
  const authors = new Set<string>();
  for (const tag of ['w:ins', 'w:del', 'w:rPrChange', 'w:pPrChange', 'w:sectPrChange']) {
    for (const el of findAllDescendants(doc.element, tag)) {
      const author = el.getAttribute('w:author');
      if (author) authors.add(author);
    }
  }
  return authors;
}

function _getElementText(el: Element): string {
  const texts: string[] = [];
  const ts = findAllDescendants(el, 'w:t');
  for (const t of ts) if (t.textContent) texts.push(t.textContent);
  const dts = findAllDescendants(el, 'w:delText');
  for (const dt of dts) if (dt.textContent) texts.push(dt.textContent);
  return texts.join('');
}

export function _truncate(text: string, maxLen: number = 60): string {
  const clean = text.replace(/\n/g, ' ').trim();
  if (clean.length <= maxLen) return clean;
  return clean.substring(0, maxLen - 3) + "...";
}

export function accept_all_tracked_changes(doc: DocumentObject): string[] {
  const lines: string[] = [];
  const insEls = findAllDescendants(doc.element, 'w:ins');
  const delEls = findAllDescendants(doc.element, 'w:del');
  
  for (const ins of insEls) {
    const text = _getElementText(ins).trim();
    if (text) lines.push(`  Accepted insertion: "${_truncate(text, 60)}"`);
  }
  for (const del of delEls) {
    const text = _getElementText(del).trim();
    if (text) lines.push(`  Accepted deletion of: "${_truncate(text, 60)}"`);
  }

  const engine = new RedlineEngine(doc);
  engine.accept_all_revisions();

  for (const tag of ['w:rPrChange', 'w:pPrChange', 'w:sectPrChange']) {
    for (const el of findAllDescendants(doc.element, tag)) {
      el.parentNode?.removeChild(el);
    }
  }

  const total = insEls.length + delEls.length;
  if (total) {
    return [`Tracked changes auto-accepted: ${total}`].concat(lines);
  }
  return [];
}

export function get_comments_summary(doc: DocumentObject): any {
  const data = extract_comments_data(doc.pkg);
  const comments = [];
  let openCount = 0;
  let resolvedCount = 0;

  for (const [cId, info] of Object.entries(data)) {
    if (info.resolved) resolvedCount++;
    else openCount++;
    comments.push({ id: cId, ...info });
  }

  return { total: comments.length, open: openCount, resolved: resolvedCount, comments };
}

export function remove_all_comments(doc: DocumentObject): string[] {
  const data = extract_comments_data(doc.pkg);
  const keys = Object.keys(data);
  if (keys.length === 0) return [];

  const lines: string[] = [];
  const cm = new CommentsManager(doc);

  for (const [cId, info] of Object.entries(data)) {
    const status = info.resolved ? "[Resolved]" : "[Open]";
    lines.push(`  ${status} "${_truncate(info.text || '', 60)}" (${info.author || 'Unknown'})`);
    cm.deleteComment(cId);
  }

  for (const tag of ['w:commentRangeStart', 'w:commentRangeEnd']) {
    for (const el of findAllDescendants(doc.element, tag)) {
      el.parentNode?.removeChild(el);
    }
  }

  const refs = findAllDescendants(doc.element, 'w:commentReference');
  for (const ref of refs) {
    const parent = ref.parentNode as Element | null;
    if (parent) {
      if (parent.tagName === 'w:r' || parent.tagName.endsWith(':r')) {
        const nonRprChildren = Array.from(parent.childNodes).filter(
          (c) => c.nodeType === 1 && (c as Element).tagName !== 'w:rPr' && (c as Element).tagName !== 'rPr'
        );
        if (nonRprChildren.length <= 1) {
          parent.parentNode?.removeChild(parent);
        } else {
          parent.removeChild(ref);
        }
      } else {
        parent.removeChild(ref);
      }
    }
  }

  const resolvedCount = Object.values(data).filter(c => c.resolved).length;
  const openCount = Object.values(data).filter(c => !c.resolved).length;
  return [`Comments removed: ${keys.length} (${resolvedCount} resolved, ${openCount} open)`].concat(lines);
}

export function eject_comment_parts(doc: DocumentObject) {
  const pkg = doc.pkg;
  
  // 1. Find all comment-related partnames
  const comment_partnames = new Set<string>();
  for (const part of pkg.parts) {
    if (part.partname.toLowerCase().includes("comments")) {
      comment_partnames.add(part.partname);
      const withSlash = part.partname.startsWith("/") ? part.partname : "/" + part.partname;
      const withoutSlash = part.partname.startsWith("/") ? part.partname.substring(1) : part.partname;
      comment_partnames.add(withSlash);
      comment_partnames.add(withoutSlash);
    }
  }

  if (comment_partnames.size === 0) return;

  // 2. Sever relationships referencing these parts from all parts in the package
  for (const part of pkg.parts) {
    if (part.partname.endsWith(".rels")) {
      const rels = findAllDescendants(part._element, "Relationship");
      const toRemove: Element[] = [];
      for (const rel of rels) {
        const target = rel.getAttribute("Target") || "";
        if (target.toLowerCase().includes("comments")) {
          toRemove.push(rel);
          
          const sourcePath = part.partname.replace("/_rels/", "/").replace(".rels", "");
          const sourcePart = pkg.getPartByPath(sourcePath);
          if (sourcePart) {
            const relId = rel.getAttribute("Id");
            if (relId) sourcePart.rels.delete(relId);
          }
        }
      }
      for (const relEl of toRemove) {
        relEl.parentNode?.removeChild(relEl);
      }
    }
  }

  // 3. Remove overrides from [Content_Types].xml
  const ctPart = pkg.getPartByPath("[Content_Types].xml");
  if (ctPart) {
    const overrides = findAllDescendants(ctPart._element, "Override");
    const toRemove: Element[] = [];
    for (const override of overrides) {
      const partName = override.getAttribute("PartName") || "";
      if (comment_partnames.has(partName) || partName.toLowerCase().includes("comments")) {
        toRemove.push(override);
      }
    }
    for (const overrideEl of toRemove) {
      overrideEl.parentNode?.removeChild(overrideEl);
    }
  }

  // 4. Remove comment parts from pkg.parts
  pkg.parts = pkg.parts.filter(p => !p.partname.toLowerCase().includes("comments"));

  // 5. Remove comment files from pkg.unzipped as well
  for (const key of Object.keys(pkg.unzipped)) {
    if (key.toLowerCase().includes("comments")) {
      delete pkg.unzipped[key];
    }
  }
}

export function replace_comment_authors(doc: DocumentObject, newAuthor: string): string[] {
  const cm = new CommentsManager(doc);
  if (!cm.commentsPart) return [];

  const original = new Set<string>();
  const comments = findAllDescendants(cm.commentsPart._element, 'w:comment');
  for (const c of comments) {
    const author = c.getAttribute('w:author');
    if (author) {
      original.add(author);
      c.setAttribute('w:author', newAuthor);
    }
    if (c.hasAttribute('w:initials')) {
      const initials = newAuthor.split(' ').filter(Boolean).map(p => p[0]).join('').toUpperCase();
      c.setAttribute('w:initials', initials);
    }
  }
  return original.size ? [`Comment authors replaced: ${Array.from(original).sort().join(', ')} → "${newAuthor}"`] : [];
}

export function replace_change_authors(doc: DocumentObject, newAuthor: string): string[] {
  const original = new Set<string>();
  for (const tag of ['w:ins', 'w:del', 'w:rPrChange', 'w:pPrChange']) {
    for (const el of findAllDescendants(doc.element, tag)) {
      const author = el.getAttribute('w:author');
      if (author) {
        original.add(author);
        el.setAttribute('w:author', newAuthor);
      }
    }
  }
  return original.size ? [`Track change authors replaced: ${Array.from(original).sort().join(', ')} → "${newAuthor}"`] : [];
}

export function normalize_change_dates(doc: DocumentObject): string[] {
  let count = 0;
  const fixed = "2025-01-01T00:00:00Z";
  for (const tag of ['w:ins', 'w:del', 'w:rPrChange', 'w:pPrChange']) {
    for (const el of findAllDescendants(doc.element, tag)) {
      if (el.hasAttribute('w:date')) {
        el.setAttribute('w:date', fixed);
        count++;
      }
    }
  }
  return count ? [`Track change timestamps: ${count} normalized`] : [];
}

/**
 * Normalize timestamps on RETAINED comments to the fixed sanitize date.
 * Keep-markup sanitize normalized core and tracked-change timestamps but
 * left the original comment timestamps in word/comments.xml (w:date) and
 * word/commentsExtensible.xml (w16cex:dateUtc) — visible through any
 * extraction and carrying exactly the when-did-they-work signal the other
 * normalizations remove (QA 2026-07-19 F-09).
 */
export function normalize_comment_dates(doc: DocumentObject): string[] {
  let count = 0;
  const fixed = "2025-01-01T00:00:00Z";
  for (const part of doc.pkg.parts) {
    if (!part.contentType.includes('comments')) continue;
    for (const el of findAllDescendants(part._element, 'w:comment')) {
      if (el.hasAttribute('w:date')) {
        el.setAttribute('w:date', fixed);
        count++;
      }
    }
    for (const el of findAllDescendants(part._element, 'w16cex:commentExtensible')) {
      if (el.hasAttribute('w16cex:dateUtc')) {
        el.setAttribute('w16cex:dateUtc', fixed);
        count++;
      }
    }
  }
  return count ? [`Comment timestamps: ${count} normalized`] : [];
}

export function scrub_doc_properties(doc: DocumentObject): string[] {
  const lines: string[] = [];
  const corePart = doc.pkg.getPartByPath('docProps/core.xml');
  if (corePart) {
    const creators = findDescendantsByLocalName(corePart._element, 'creator');
    creators.forEach(c => { if (c.textContent) { lines.push(`Author: ${c.textContent}`); c.textContent = ""; }});

    const modifiers = findDescendantsByLocalName(corePart._element, 'lastModifiedBy');
    modifiers.forEach(c => { if (c.textContent) { lines.push(`Last modified by: ${c.textContent}`); c.textContent = ""; }});

    // Title is often intentional, but can leak. Report it, don't strip
    // (QA 2026-07-17 F4; mirrors Python).
    const titles = findDescendantsByLocalName(corePart._element, 'title');
    titles.forEach(c => { if (c.textContent) { lines.push(`Title kept (review manually): "${c.textContent}"`); }});

    // Classification-style properties are textbook leak vectors ("Project
    // Falcon", "confidential,merger,..."). Unlike title they carry no
    // legitimate outbound formatting value, so strip them and say so.
    // dc:identifier, dc:language and cp:version are scrubbed with them:
    // identifiers are DMS/matter-ID carriers.
    const leakFields: Array<[string, string]> = [
      ['category', 'Category'],
      ['keywords', 'Keywords'],
      ['subject', 'Subject'],
      ['contentStatus', 'Content status'],
      ['description', 'Description/comments'],
      ['identifier', 'Identifier'],
      ['language', 'Language'],
      ['version', 'Version'],
    ];
    for (const [local, label] of leakFields) {
      findDescendantsByLocalName(corePart._element, local).forEach(c => {
        if (c.textContent) {
          lines.push(`${label}: ${c.textContent}`);
          c.textContent = "";
        }
      });
    }

    const revisions = findDescendantsByLocalName(corePart._element, 'revision');
    revisions.forEach(c => { if (c.textContent && parseInt(c.textContent) > 1) { lines.push(`Revision count: ${c.textContent} → 1`); c.textContent = "1"; }});
  }

  const appPart = doc.pkg.getPartByPath('docProps/app.xml');
  if (appPart) {
    const docEl = appPart._element;
    const intFields = ["TotalTime", "Words", "Characters", "Paragraphs", "Lines", "CharactersWithSpaces"];
    for (const f of intFields) {
      findDescendantsByLocalName(docEl, f).forEach(el => {
        if (el.textContent && el.textContent !== "0") {
          if (f === "TotalTime") lines.push(`Total editing time: ${el.textContent} minutes`);
          el.textContent = "0";
        }
      });
    }
    const strFields = ["Template", "Manager", "Company"];
    for (const f of strFields) {
      findDescendantsByLocalName(docEl, f).forEach(el => {
        if (el.textContent) {
          lines.push(`${f}: ${el.textContent}`);
          el.textContent = "";
        }
      });
    }
  }

  return lines.length ? ["Metadata scrubbed:", ...lines.map(l => `  ${l}`)] : [];
}

export function scrub_timestamps(doc: DocumentObject): string[] {
  let modified = false;
  const epoch = "1970-01-01T00:00:00Z";
  const corePart = doc.pkg.getPartByPath('docProps/core.xml');
  if (corePart) {
    for (const tag of ['created', 'modified', 'lastPrinted']) {
      findDescendantsByLocalName(corePart._element, tag).forEach(el => {
        if (el.textContent && el.textContent !== epoch) {
          el.textContent = epoch;
          modified = true;
        }
      });
    }
  }
  return modified ? ["Timestamps normalized to epoch"] : [];
}

/**
 * Fully ejects package members whose path matches `matcher`: the parsed part
 * list, the raw `pkg.unzipped` bytes (save() re-zips EVERY unzipped
 * member, so skipping this ships the original bytes in the output), the
 * [Content_Types].xml overrides, and every .rels Relationship whose
 * target matches `relTargetMatcher`.
 */
function ejectPackageMembers(
  doc: DocumentObject,
  matcher: (path: string) => boolean,
  relTargetMatcher: (target: string) => boolean,
) {
  const pkg = doc.pkg;
  const normalized = (p: string) => (p.startsWith('/') ? p.substring(1) : p);

  // 1. Sever relationships from every .rels part (and the in-memory rels maps)
  for (const part of pkg.parts) {
    if (!part.partname.endsWith('.rels')) continue;
    const toRemove: Element[] = [];
    for (const rel of findAllDescendants(part._element, 'Relationship')) {
      const target = rel.getAttribute('Target') || '';
      if (relTargetMatcher(target)) {
        toRemove.push(rel);
        const sourcePath = part.partname.replace('/_rels/', '/').replace('.rels', '');
        const sourcePart = pkg.getPartByPath(sourcePath);
        if (sourcePart) {
          const relId = rel.getAttribute('Id');
          if (relId) sourcePart.rels.delete(relId);
        }
      }
    }
    toRemove.forEach(r => r.parentNode?.removeChild(r));
  }

  // 2. Remove [Content_Types].xml overrides
  const ctPart = pkg.getPartByPath('[Content_Types].xml');
  if (ctPart) {
    const toRemove: Element[] = [];
    for (const override of findAllDescendants(ctPart._element, 'Override')) {
      const partName = override.getAttribute('PartName') || '';
      if (matcher(normalized(partName))) toRemove.push(override);
    }
    toRemove.forEach(o => o.parentNode?.removeChild(o));
  }

  // 3. Drop the parts from the serialization list AND the raw zip map
  pkg.parts = pkg.parts.filter(p => !matcher(normalized(p.partname)));
  for (const key of Object.keys(pkg.unzipped)) {
    if (matcher(normalized(key))) delete pkg.unzipped[key];
  }
}

export function strip_custom_xml(doc: DocumentObject): string[] {
  const isCustomXml = (path: string) => path.startsWith('customXml/');
  const customParts = doc.pkg.parts.filter(p =>
    isCustomXml(p.partname.startsWith('/') ? p.partname.substring(1) : p.partname),
  );
  const leakedMembers = Object.keys(doc.pkg.unzipped).filter(isCustomXml);
  if (customParts.length === 0 && leakedMembers.length === 0) return [];

  ejectPackageMembers(doc, isCustomXml, target => target.includes('customXml'));

  for (const sdtPr of findAllDescendants(doc.element, 'w:sdtPr')) {
    findChildren(sdtPr, 'w:dataBinding').forEach(b => sdtPr.removeChild(b));
  }

  return [`Custom XML parts: ${Math.max(customParts.length, leakedMembers.length)} removed`];
}

const CUSTOM_PROPS_PATH = 'docProps/custom.xml';

/**
 * Remove the custom document properties part (docProps/custom.xml) and its
 * package relationship. Custom properties are a standard home for matter
 * numbers, client names, DMS identifiers and workflow secrets; a package
 * that keeps them can never be reported CLEAN.
 */
export function strip_custom_properties(doc: DocumentObject): string[] {
  const part = doc.pkg.getPartByPath(CUSTOM_PROPS_PATH);
  const leaked = Object.keys(doc.pkg.unzipped).includes(CUSTOM_PROPS_PATH);
  if (!part && !leaked) return [];

  // Enumerate what is being removed so the report discloses it.
  const lines: string[] = [];
  if (part) {
    for (const prop of findDescendantsByLocalName(part._element, 'property')) {
      const name = prop.getAttribute('name') || '(unnamed)';
      const value = (prop.textContent || '').trim();
      lines.push(`  Custom property removed: ${name} = "${_truncate(value, 60)}"`);
    }
  }

  ejectPackageMembers(
    doc,
    path => path === CUSTOM_PROPS_PATH,
    target => target.includes('docProps/custom.xml'),
  );

  const count = Math.max(lines.length, 1);
  return [`Custom document properties: ${count} removed (docProps/custom.xml)`].concat(lines);
}

/**
 * Remove Word document variables (<w:docVars>/<w:docVar>) from
 * word/settings.xml. Document variables are invisible in Word's UI but are a
 * standard carrier for matter references, DMS identifiers, template state and
 * integration tokens — a package that keeps them can never be reported CLEAN
 * (QA 2026-07-19 ADEU-QA-001). Variables are disclosed by NAME only: their
 * values are exactly the secrets a sanitize report must not echo.
 */
export function strip_document_variables(doc: DocumentObject): string[] {
  const settingsPart = doc.pkg.getPartByPath('word/settings.xml');
  if (!settingsPart) return [];

  const names: string[] = [];
  let removedAny = false;

  for (const container of findDescendantsByLocalName(settingsPart._element, 'docVars')) {
    for (const child of Array.from(container.childNodes)) {
      const el = child as Element;
      const tag = el.tagName || '';
      if (tag === 'docVar' || tag.endsWith(':docVar')) {
        names.push(el.getAttribute('w:name') || el.getAttribute('name') || '(unnamed)');
      }
    }
    container.parentNode?.removeChild(container);
    removedAny = true;
  }

  // Defensive: stray <w:docVar> elements outside a <w:docVars> container.
  for (const stray of findDescendantsByLocalName(settingsPart._element, 'docVar')) {
    names.push(stray.getAttribute('w:name') || stray.getAttribute('name') || '(unnamed)');
    stray.parentNode?.removeChild(stray);
    removedAny = true;
  }

  if (!removedAny) return [];
  const shown = Array.from(new Set(names)).sort().join(', ') || '(unnamed)';
  return [`Document variables: ${names.length} removed (${shown})`];
}

export function strip_image_alt_text(doc: DocumentObject): string[] {
  let count = 0;
  for (const docPr of findDescendantsByLocalName(doc.element, 'docPr')) {
    const descr = docPr.getAttribute('descr');
    if (descr) {
      const isShort = descr.length < 10;
      const isFile = descr.includes('.') && descr.length < 60;
      if (isShort || isFile) {
        docPr.removeAttribute('descr');
        count++;
      }
    }
  }
  return count ? [`Image alt text: ${count} auto-generated descriptions removed`] : [];
}

/**
 * Detect watermark-like embedded text objects (VML shapes with a textpath,
 * Word's native watermark mechanism) in headers, footers and the body.
 * Non-destructive: returns warnings so the report never declares a document
 * fully clean while such an object silently remains (QA 2026-07-18 M3).
 */
export function detect_watermarks(doc: DocumentObject): string[] {
  const warnings: string[] = [];
  const seen = new Set<string>();

  for (const part of doc.pkg.parts) {
    const name = String(part.partname);
    let location: string;
    if (name.startsWith("/word/header")) location = "header";
    else if (name.startsWith("/word/footer")) location = "footer";
    else if (name === "/word/document.xml") location = "body";
    else continue;

    let textpaths: Element[];
    try {
      textpaths = findDescendantsByLocalName(part._element, "textpath");
    } catch {
      continue;
    }
    for (const tp of textpaths) {
      const text = (tp.getAttribute("string") || "").trim();
      const key = `${location}\x1f${text}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const label = text ? `"${_truncate(text, 60)}"` : "(no text)";
      warnings.push(
        `Watermark-like text object in ${location}: ${label} — NOT removed. ` +
          "It remains in the document package and may render in other applications; " +
          "review and remove it in Word if it must not reach the counterparty.",
      );
    }
  }
  return warnings;
}

export function audit_hyperlinks(doc: DocumentObject): string[] {
  const internal = ["sharepoint.com", "onedrive.com", ".internal", "intranet", "localhost", "10.", "192.168.", "172.16."];
  const warnings: string[] = [];
  
  const docRels = doc.pkg.getOrCreateRelsPart(doc.part.partname);
  for (const rel of findAllDescendants(docRels._element, 'Relationship')) {
    if (rel.getAttribute('TargetMode') === 'External') {
      const url = rel.getAttribute('Target') || '';
      for (const pattern of internal) {
        if (url.toLowerCase().includes(pattern.toLowerCase())) {
          warnings.push(`Hyperlink targets internal URL: ${_truncate(url, 80)}`);
          break;
        }
      }
    }
  }
  return warnings;
}