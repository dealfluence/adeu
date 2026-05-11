import { DOMParser, XMLSerializer } from '@xmldom/xmldom';

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
    if (child.nodeType === 1 /* ELEMENT_NODE */ && (child as Element).tagName === tagName) {
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
export function findAllDescendants(element: Element, tagName: string): Element[] {
  return Array.from(element.getElementsByTagName(tagName));
}

/**
 * Parses raw XML strings into xmldom Documents.
 */
export function parseXml(xmlString: string): Document {
  return new DOMParser().parseFromString(xmlString, 'text/xml');
}

/**
 * Serializes an xmldom Document or Element back to a string.
 */
export function serializeXml(node: Node): string {
  return new XMLSerializer().serializeToString(node);
}