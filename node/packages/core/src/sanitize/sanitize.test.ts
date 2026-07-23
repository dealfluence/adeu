import { describe, it, expect, vi } from "vitest";
import { zipSync } from "fflate";
import { DOMParser } from "@xmldom/xmldom";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { DocumentObject, Part, DocxPackage } from "../docx/bridge.js";
import * as transforms from "./transforms.js";
import { finalize_document } from "./core.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// --- Helper to build a lightweight in-memory DocumentObject ---
function createMockDoc(bodyXml: string): DocumentObject {
  const fullXml = `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body>${bodyXml}</w:body></w:document>`;
  const doc = new DOMParser().parseFromString(fullXml, "text/xml");
  const unzipped: Record<string, Uint8Array> = {};
  const pkg = new DocxPackage(unzipped);

  const part = new Part(
    "/word/document.xml",
    fullXml,
    doc.documentElement! as unknown as Element,
    "application/xml",
  );
  pkg.parts.push(part);
  pkg.mainDocumentPart = part;

  return new DocumentObject(pkg, part);
}

// --- Transforms Unit Tests ---
describe("Sanitize Transforms", () => {
  it("should strip RSID attributes and elements", () => {
    const doc = createMockDoc(`
      <w:p w:rsidR="00A21F3B" w:rsidP="00B33E21">
        <w:r><w:t>Hello</w:t></w:r>
      </w:p>
      <w:sectPr><w:rsids><w:rsidRoot w:val="00A21F3B"/></w:rsids></w:sectPr>
    `);

    const lines = transforms.strip_rsid(doc);
    const xml = doc.element.toString();

    expect(lines.length).toBeGreaterThan(0);
    expect(xml).not.toContain("w:rsidR");
    expect(xml).not.toContain("w:rsidP");
    expect(xml).not.toContain("w:rsids");
  });

  it("should strip w14:paraId and w14:textId", () => {
    const doc = createMockDoc(`
      <w:p w14:paraId="3F2A91BC" w14:textId="77777777">
        <w:r><w:t>Test</w:t></w:r>
      </w:p>
    `);

    const lines = transforms.strip_para_ids(doc);
    const xml = doc.element.toString();

    expect(lines.length).toBeGreaterThan(0);
    expect(xml).not.toContain("w14:paraId");
    expect(xml).not.toContain("w14:textId");
  });

  it("should strip hidden text runs", () => {
    const doc = createMockDoc(`
      <w:p>
        <w:r>
          <w:rPr><w:vanish/></w:rPr>
          <w:t>HiddenSecret</w:t>
        </w:r>
        <w:r>
          <w:t>VisibleText</w:t>
        </w:r>
      </w:p>
    `);

    const lines = transforms.strip_hidden_text(doc);
    const xml = doc.element.toString();

    expect(lines.length).toBeGreaterThan(0);
    expect(xml).not.toContain("HiddenSecret");
    expect(xml).toContain("VisibleText");
  });

  it("should scrub document properties", () => {
    const doc = createMockDoc("<w:p/>");

    // Mock docProps/app.xml
    const appXml =
      "<Properties><TotalTime>15</TotalTime><Template>Confidential.dotm</Template></Properties>";
    const appEl = new DOMParser().parseFromString(
      appXml,
      "text/xml",
    ).documentElement!;
    const appPart = new Part(
      "/docProps/app.xml",
      appXml,
      appEl as unknown as Element,
      "application/xml",
    );
    doc.pkg.parts.push(appPart);

    const lines = transforms.scrub_doc_properties(doc);
    const resultXml = appPart._element.toString();

    expect(lines.length).toBeGreaterThan(0);
    expect(resultXml).toContain("<TotalTime>0</TotalTime>");
    expect(resultXml).toContain("<Template/>");
    expect(resultXml).not.toContain("Confidential.dotm");
  });

  it("should strip custom XML parts and data bindings", () => {
    const doc = createMockDoc(`
      <w:p>
        <w:sdt>
          <w:sdtPr><w:dataBinding w:xpath="/test"/></w:sdtPr>
        </w:sdt>
      </w:p>
    `);

    // Mock custom XML part
    const customPart = new Part(
      "/customXml/item1.xml",
      "<t/>",
      new DOMParser().parseFromString("<t/>", "text/xml").documentElement! as unknown as Element,
      "application/xml",
    );
    doc.pkg.parts.push(customPart);

    const lines = transforms.strip_custom_xml(doc);

    expect(lines.length).toBeGreaterThan(0);
    expect(
      doc.pkg.parts.find((p) => p.partname.includes("customXml")),
    ).toBeUndefined();
    expect(doc.element.toString()).not.toContain("w:dataBinding");
  });

  it("should count and accept all tracked changes", () => {
    const doc = createMockDoc(`
      <w:p>
        <w:del w:id="1">
          <w:r><w:delText>Vendor</w:delText></w:r>
        </w:del>
        <w:ins w:id="2">
          <w:r><w:t>Supplier</w:t></w:r>
        </w:ins>
      </w:p>
    `);

    const [ins, del, fmt] = transforms.count_tracked_changes(doc);
    expect(ins).toBe(1);
    expect(del).toBe(1);

    const lines = transforms.accept_all_tracked_changes(doc);
    const xml = doc.element.toString();

    expect(lines.length).toBeGreaterThan(0);
    expect(xml).not.toContain("w:del");
    expect(xml).not.toContain("w:ins");
    expect(xml).not.toContain("Vendor"); // Deletion was removed
    expect(xml).toContain("Supplier"); // Insertion was unwrapped
  });
});

// --- Orchestrator Integration Tests ---
describe("Finalize Document (Core)", () => {
  it("should inject XML locking (Read-Only) into settings.xml", async () => {
    const doc = createMockDoc("<w:p/>");

    // Mock word/settings.xml
    const settingsXml =
      '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:settings>';
    const settingsEl = new DOMParser().parseFromString(
      settingsXml,
      "text/xml",
    ).documentElement!;
    const settingsPart = new Part(
      "/word/settings.xml",
      settingsXml,
      settingsEl as unknown as Element,
      "application/xml",
    );
    doc.pkg.parts.push(settingsPart);

    // Mock the doc.save buffer return
    doc.save = vi.fn().mockResolvedValue(Buffer.from(zipSync({})));

    const res = await finalize_document(doc, {
      filename: "test.docx",
      protection_mode: "read_only",
    });

    const finalSettings = settingsPart._element.toString();

    expect(res.reportText).toContain("Result: CLEAN");
    expect(res.reportText).toContain("Document locked (Read-Only");

    // Validate mathematical injection
    expect(finalSettings).toContain("w:documentProtection");
    expect(finalSettings).toContain('w:edit="readOnly"');
    expect(finalSettings).toContain('w:enforcement="1"');
  });

  it("should return a blocked status if unaccepted changes remain and accept_all is false", async () => {
    const doc = createMockDoc(`
      <w:p>
        <w:ins w:id="1"><w:r><w:t>Unresolved Edit</w:t></w:r></w:ins>
      </w:p>
    `);

    const res = await finalize_document(doc, {
      filename: "draft.docx",
      sanitize_mode: "full",
      accept_all: false, // <-- Should block
    });

    expect(res.reportText).toContain("BLOCKED:");
    expect(res.reportText).toContain("unresolved tracked changes");
    expect(res.reportText).toContain("sanitize_mode='keep-markup'");
  });

  describe("Resolved Bugs Sanitize Parity Verification", () => {
    it("BUG-FRAG-1: Coalesces adjacent identical runs after accepting tracked changes", async () => {
      const doc = createMockDoc(`
        <w:p>
          <w:r><w:t xml:space="preserve">The term shall be </w:t></w:r>
          <w:ins w:id="1"><w:r><w:t>five (5)</w:t></w:r></w:ins>
          <w:r><w:t xml:space="preserve"> years from the Effective Date.</w:t></w:r>
        </w:p>
      `);

      doc.save = vi.fn().mockResolvedValue(Buffer.from(zipSync({})));

      await finalize_document(doc, {
        filename: "test.docx",
        sanitize_mode: "full",
        accept_all: true,
      });

      const xml = doc.element.toString();
      // We should see a single coalesced string rather than fragmented <w:t> nodes
      expect(xml).toContain(
        "The term shall be five (5) years from the Effective Date.",
      );

      const runs = doc.element.getElementsByTagName("w:r");
      // If they are coalesced properly, there will be exactly 1 run instead of 3
      expect(runs.length).toBe(1);
    });

    it("BUG-NS-1: Strips unused xmlns:w16du namespace declarations during finalization", async () => {
      const doc = createMockDoc("<w:p/>");
      // Manually inject the namespace onto the absolute root as the engine does
      doc.part._element.setAttribute(
        "xmlns:w16du",
        "http://schemas.microsoft.com/office/word/2023/wordml/word16du",
      );

      doc.save = vi.fn().mockResolvedValue(Buffer.from(zipSync({})));

      await finalize_document(doc, {
        filename: "test.docx",
        sanitize_mode: "full",
      });

      // The final stringified XML of the root document should NOT contain the unused namespace
      const xml = doc.part._element.toString();
      expect(xml).not.toContain("xmlns:w16du");
    });

    it("BUG-REPRO: finalize_document leaks comment parts in full sanitize mode", async () => {
      // 1. Load the golden DOCX which we know has comments and comment parts
      const fixturePath = resolve(__dirname, "../../../../../shared/fixtures/golden.docx");
      const buf = readFileSync(fixturePath);
      const doc = await DocumentObject.load(buf);

      // Verify pre-condition: comment parts exist in the loaded package
      const original_comment_parts = doc.pkg.parts.filter(p => p.contentType.includes("comments"));
      expect(original_comment_parts.length).toBeGreaterThan(0);

      const original_xml = doc.element.toString();
      expect(original_xml).toContain("w:commentRangeStart");
      expect(original_xml).toContain("w:commentReference");

      // Mock the doc.save buffer return
      doc.save = vi.fn().mockResolvedValue(Buffer.from(zipSync({})));

      // 2. Act: Finalize the document in full sanitize mode with accept_all: true
      await finalize_document(doc, {
        filename: "golden.docx",
        sanitize_mode: "full",
        accept_all: true,
      });

      // 3. Assert: All comments and comment parts are completely removed
      const final_xml = doc.element.toString();
      
      // Assert NO in-body comment anchors survive (anchors must be completely gone)
      expect(final_xml).not.toContain("w:commentRangeStart");
      expect(final_xml).not.toContain("w:commentRangeEnd");
      expect(final_xml).not.toContain("w:commentReference");

      const final_comment_parts = doc.pkg.parts.filter(p => p.contentType.includes("comments"));
      expect(final_comment_parts.length).toBe(0);
    });
  });
});
