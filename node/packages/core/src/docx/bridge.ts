import JSZip from 'jszip';
import { parseXml, findChild, findAllDescendants, serializeXml } from './dom.js';

export class Relationship {
  constructor(
    public id: string,
    public type: string,
    public target: string,
    public isExternal: boolean
  ) {}
}

export class Part {
  public rels: Map<string, Relationship> = new Map();
  public _element: Element;

  constructor(
    public partname: string,
    public blob: string,
    element: Element,
    public contentType: string
  ) {
    this._element = element;
  }

  public addRelationship(id: string, type: string, target: string, isExternal: boolean = false) {
    this.rels.set(id, new Relationship(id, type, target, isExternal));
    
    // Directly append the relationship element to the document structure
    if (this.partname.endsWith('.rels')) {
      const doc = this._element.ownerDocument;
      if (doc) {
        // Use strict namespace to ensure it parses successfully on reload
        const relEl = doc.createElementNS('http://schemas.openxmlformats.org/package/2006/relationships', 'Relationship');
        relEl.setAttribute('Id', id);
        relEl.setAttribute('Type', type);
        relEl.setAttribute('Target', target);
        if (isExternal) relEl.setAttribute('TargetMode', 'External');
        this._element.appendChild(relEl);
      }
    }
  }
}

export class DocxPackage {
  public parts: Part[] = [];
  public mainDocumentPart!: Part;

  constructor(public zip: JSZip) {}

  public getPartByPath(path: string): Part | undefined {
    // Strip leading slash for jszip compat
    const searchPath = path.startsWith('/') ? path.substring(1) : path;
    return this.parts.find((p) => p.partname === searchPath || p.partname === '/' + searchPath);
  }

  public nextPartname(pattern: string): string {
    let i = 1;
    while (true) {
      const candidate = pattern.replace('%d', i === 1 ? '' : i.toString());
      if (!this.getPartByPath(candidate)) return candidate;
      i++;
    }
  }

  public addPart(partname: string, contentType: string, xmlString: string): Part {
    const doc = parseXml(xmlString);
    const part = new Part(partname, xmlString, doc.documentElement, contentType);
    this.parts.push(part);

    // Update [Content_Types].xml
    const ctPart = this.getPartByPath('[Content_Types].xml');
    if (ctPart) {
      const docCT = ctPart._element.ownerDocument;
      if (docCT) {
        const override = docCT.createElement('Override');
        override.setAttribute('PartName', partname);
        override.setAttribute('ContentType', contentType);
        ctPart._element.appendChild(override);
      }
    }
    return part;
  }

  public getOrCreateRelsPart(sourcePartname: string): Part {
    // e.g., /word/document.xml -> /word/_rels/document.xml.rels
    const parts = sourcePartname.split('/');
    const file = parts.pop();
    const relsPath = parts.join('/') + '/_rels/' + file + '.rels';
    
    let relsPart = this.getPartByPath(relsPath);
    if (!relsPart) {
      const xml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>`;
      relsPart = this.addPart(relsPath, 'application/vnd.openxmlformats-package.relationships+xml', xml);
    }
    return relsPart;
  }
}

export class DocumentObject {
  public part: Part;
  public settings: { oddAndEvenPagesHeaderFooter: boolean } = { oddAndEvenPagesHeaderFooter: false };
  // Simplification for the TS port: sections hold header/footer refs
  public sections: any[] = []; 

  constructor(public pkg: DocxPackage, part: Part) {
    this.part = part;
  }

  public get element(): Element {
    return findChild(this.part._element, 'w:body') || this.part._element;
  }

  /**
   * Main entrypoint for loading a DOCX buffer into the DOM wrapper.
   */
  public static async load(buffer: Buffer | ArrayBuffer): Promise<DocumentObject> {
    const zip = await JSZip.loadAsync(buffer);
    const pkg = new DocxPackage(zip);

    // 1. Load Content Types
    const ctFile = zip.file('[Content_Types].xml');
    let contentTypes: Record<string, string> = {};
    if (ctFile) {
      const ctXml = parseXml(await ctFile.async('text'));
      const overrides = findAllDescendants(ctXml.documentElement, 'Override');
      for (const override of overrides) {
        contentTypes[override.getAttribute('PartName') || ''] = override.getAttribute('ContentType') || '';
      }
    }

    // 2. Pre-load all XML parts to allow synchronous traversal later
    for (const [path, file] of Object.entries(zip.files)) {
      if (!file.dir && (path.endsWith('.xml') || path.endsWith('.rels'))) {
        const text = await file.async('text');
        const doc = parseXml(text);
        const cType = contentTypes['/' + path] || 'application/xml';
        const part = new Part('/' + path, text, doc.documentElement, cType);
        pkg.parts.push(part);
      }
    }

    // 3. Resolve Relationships for the main document
    const mainPart = pkg.getPartByPath('word/document.xml');
    if (!mainPart) throw new Error('Invalid DOCX: Missing word/document.xml');
    pkg.mainDocumentPart = mainPart;

    const relsPart = pkg.getPartByPath('word/_rels/document.xml.rels');
    if (relsPart) {
      const relElements = findAllDescendants(relsPart._element, 'Relationship');
      for (const rel of relElements) {
        const rId = rel.getAttribute('Id');
        const target = rel.getAttribute('Target');
        const type = rel.getAttribute('Type');
        const targetMode = rel.getAttribute('TargetMode');
        
        if (rId && target && type) {
          mainPart.rels.set(rId, new Relationship(rId, type, target, targetMode === 'External'));
        }
      }
    }

    return new DocumentObject(pkg, mainPart);
  }

  public relateTo(part: Part, relType: string) {
    let rId = 1;
    while (this.part.rels.has(`rId${rId}`)) rId++;
    const id = `rId${rId}`;
    
    // In DOCX, targets in .rels are relative to the source part's directory.
    // /word/document.xml relating to /word/comments.xml -> target is "comments.xml"
    const target = part.partname.split('/').pop()!;
    
    this.part.rels.set(id, new Relationship(id, relType, target, false));
    const relsPart = this.pkg.getOrCreateRelsPart(this.part.partname);
    relsPart.addRelationship(id, relType, target, false);
  }

  public relateToExternal(target: string, relType: string): string {
    let rId = 1;
    while (this.part.rels.has(`rId${rId}`)) rId++;
    const id = `rId${rId}`;
    
    this.part.rels.set(id, new Relationship(id, relType, target, true));
    const relsPart = this.pkg.getOrCreateRelsPart(this.part.partname);
    relsPart.addRelationship(id, relType, target, true);
    return id;
  }

  public async save(): Promise<Buffer> {
    for (const part of this.pkg.parts) {
      let xmlStr = serializeXml(part._element.ownerDocument || part._element);
      if (!xmlStr.startsWith('<?xml')) {
        xmlStr = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xmlStr;
      }
      this.pkg.zip.file(part.partname.substring(1), xmlStr); // Strip leading slash for JSZip
    }
    return this.pkg.zip.generateAsync({ type: 'nodebuffer' });
  }
}