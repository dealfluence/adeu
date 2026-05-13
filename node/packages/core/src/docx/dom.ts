import { DOMParser, XMLSerializer } from "@xmldom/xmldom";

/**
 * Simulates docx.oxml.ns.qn. In xmldom, namespaces are preserved in tagName.
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
 * Simulates lxml element.findall(".//w:tag") - searches ALL descendants.
 */
export function findAllDescendants(
  element: Element,
  tagName: string,
): Element[] {
  return Array.from(element.getElementsByTagName(tagName));
}

/**
 * Parses raw XML strings into xmldom Documents.
 */
export function parseXml(xmlString: string): Document {
  return new DOMParser().parseFromString(xmlString, "text/xml");
}

/**
 * Serializes an xmldom Document or Element back to a string,
 * enforcing deterministic attribute ordering on the root element.
 */
export function serializeXml(node: Node): string {
  let xml = new XMLSerializer().serializeToString(node);

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
