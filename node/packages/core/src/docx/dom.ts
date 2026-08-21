import { parseFastXml, serializeFastXml } from "./fast-xml.js";

/**
 * Simulates docx.oxml.ns.qn. Namespace prefixes are preserved in tagName.
 */
export const qn = (name: string) => name;

/**
 * Simulates lxml element.find("w:tag") - strictly searches DIRECT children only.
 */
export function findChild(element: Element, tagName: string): Element | null {
  for (let i = 0; i < element.childNodes.length; i++) {
    const child = element.childNodes[i];
    if (
      child.nodeType === 1 /* ELEMENT_NODE */ &&
      (child as Element).tagName === tagName
    ) {
      return child as Element;
    }
  }
  return null;
}

/**
 * Simulates lxml element.findall("w:tag") - strictly searches DIRECT children only.
 */
export function findChildren(element: Element, tagName: string): Element[] {
  const result: Element[] = [];
  for (let i = 0; i < element.childNodes.length; i++) {
    const child = element.childNodes[i];
    if (child.nodeType === 1 && (child as Element).tagName === tagName) {
      result.push(child as Element);
    }
  }
  return result;
}

/**
 * Direct children of `element` named `tagName` (or any of `tagName`),
 * transparently descending through structured document tags (content
 * controls).
 *
 * Word wraps table rows/cells in `w:sdt > w:sdtContent` whenever a template
 * uses content controls, and nests a second level for repeating sections
 * (`w15:repeatingSection > w15:repeatingSectionItem`). Those wrappers are
 * pure containers: the `w:tr`/`w:tc` inside belongs to the enclosing
 * `w:tbl`/`w:tr` exactly as if the wrapper were not there.
 *
 * Recursion stops at `tagName`, so a nested `w:tbl` inside a `w:tc` keeps its
 * own rows instead of donating them to the outer table.
 *
 * Mirrors `_iter_sdt_transparent_children` in python/src/adeu/utils/docx.py.
 */
export function findChildrenSdtTransparent(
  element: Element,
  tagName: string | readonly string[],
): Element[] {
  const wanted = typeof tagName === "string" ? [tagName] : tagName;
  const result: Element[] = [];
  const visit = (parent: Element, depth: number): void => {
    // Defensive: content controls nest a couple of levels deep in practice
    // (repeating sections). Anything beyond this is malformed or hostile,
    // and we must not blow the stack on untrusted input.
    if (depth > MAX_SDT_NESTING_DEPTH) return;
    for (let i = 0; i < parent.childNodes.length; i++) {
      const child = parent.childNodes[i];
      if (child.nodeType !== 1) continue;
      const el = child as Element;
      if (wanted.includes(el.tagName)) {
        result.push(el);
      } else if (el.tagName === "w:sdt" || el.tagName === "w:sdtContent") {
        visit(el, depth + 1);
      }
    }
  };
  visit(element, 0);
  return result;
}

const MAX_SDT_NESTING_DEPTH = 100;

/**
 * Simulates lxml element.findall(".//w:tag") - searches ALL descendants.
 */
export function findAllDescendants(
  element: Element,
  tagName: string,
): Element[] {
  return Array.from(element.getElementsByTagName(tagName));
}

/**
 * Parses raw XML strings into fast-xml Documents (docs/PERFORMANCE.md
 * \u00A75.4b \u2014 the spec parser spent ~93% of its time on machinery the engine
 * never consults; measured 6.70s -> ~0.5s on a 45MB document.xml).
 */
export function parseXml(xmlString: string): Document {
  // Strip UTF-8 BOM if present
  if (xmlString.startsWith("\uFEFF")) {
    xmlString = xmlString.slice(1);
  }
  return parseFastXml(xmlString) as unknown as Document;
}

/**
 * Serializes a Document or Element back to a string,
 * enforcing deterministic attribute ordering on the root element.
 */
export function serializeXml(node: Node): string {
  let xml = serializeFastXml(node as any);

  // BUG-11: Deterministic namespace ordering on root elements.
  const rootTagRegex = /<([a-zA-Z0-9_:]+)(\s+[^>]+?)(>|\/>)/;
  const match = rootTagRegex.exec(xml);

  if (match && !match[1].startsWith("?")) {
    const index = match.index;
    const textBefore = xml.substring(0, index);

    // Ensure this is the absolute root tag (only <?xml...?> allowed before it)
    const isRoot =
      !textBefore.includes("<") ||
      (textBefore.trim().startsWith("<?xml") &&
        (textBefore.match(/</g) || []).length === 1);

    if (isRoot) {
      const fullTag = match[0];
      const elemStart = `<${match[1]}`;
      const attrsStr = match[2];
      const tagEnd = match[3];

      // Robust extraction matching any quote style and internal spacing
      const attrRegex = /([a-zA-Z0-9_:]+)\s*=\s*(["'])(.*?)\2/g;
      const attrs: string[] = [];
      let m;
      while ((m = attrRegex.exec(attrsStr)) !== null) {
        attrs.push(m[0].trim());
      }

      // Sort attributes: xmlns definitions first, then standard attributes
      attrs.sort((a, b) => {
        const aName = a.split("=")[0].trim();
        const bName = b.split("=")[0].trim();
        const aIsXmlns = aName.startsWith("xmlns");
        const bIsXmlns = bName.startsWith("xmlns");
        if (aIsXmlns && !bIsXmlns) return -1;
        if (!aIsXmlns && bIsXmlns) return 1;
        return aName < bName ? -1 : aName > bName ? 1 : 0;
      });

      const newTag =
        attrs.length > 0
          ? `${elemStart} ${attrs.join(" ")}${tagEnd}`
          : `${elemStart}${tagEnd}`;
      xml =
        xml.substring(0, index) +
        newTag +
        xml.substring(index + fullTag.length);
    }
  }

  return xml;
}
