import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  DocumentObject,
  RedlineEngine,
  extractTextFromBuffer,
  parse_page_arg,
  userFindAllMatches,
} from "@adeu/core";
import {
  build_outline_response,
  build_page_range_response,
  build_search_response,
} from "./response-builders.js";

const _dirname = dirname(fileURLToPath(import.meta.url));

async function createEmptyDoc(): Promise<DocumentObject> {
  const fixturePath = resolve(_dirname, "../../../../shared/fixtures/initial.docx");
  const doc = await DocumentObject.load(readFileSync(fixturePath));
  while (doc.element.firstChild) {
    doc.element.removeChild(doc.element.firstChild);
  }
  return doc;
}

function addPara(
  doc: DocumentObject,
  runs: Array<{ text: string; bold?: boolean }>,
): void {
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement("w:p");
  for (const rSpec of runs) {
    const r = xmlDoc.createElement("w:r");
    if (rSpec.bold) {
      const rPr = xmlDoc.createElement("w:rPr");
      rPr.appendChild(xmlDoc.createElement("w:b"));
      r.appendChild(rPr);
    }
    const t = xmlDoc.createElement("w:t");
    if (rSpec.text.includes(" ")) {
      t.setAttribute("xml:space", "preserve");
    }
    t.textContent = rSpec.text;
    r.appendChild(t);
    p.appendChild(r);
  }
  doc.element.appendChild(p);
}

function makeBodyWithPages(targetPages: number): string {
  const filler =
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.";
  const paragraphs: string[] = [];
  for (let i = 0; i < targetPages * 40; i++) {
    paragraphs.push(`Paragraph ${i + 1}: ${filler}`);
  }
  return paragraphs.join("\n\n");
}

describe("Task 9: read_docx ergonomics", () => {
  it("supports leading inline regex flags (?i), (?m), (?s) in search and engine", async () => {
    const body = "The Supplier shall indemnify the Customer.\nSecond line here.";
    const res = build_search_response(
      body,
      "(?i)supplier.*indemnify",
      true,
      true,
      undefined,
      "/tmp/doc.docx",
    );
    const text = res.content[0].text;
    expect(text).not.toContain("not a valid regular expression");
    expect(text).toContain("**Supplier shall indemnify**");

    const matches = userFindAllMatches("(?is)supplier.*second", body);
    expect(matches.length).toBe(1);

    const doc = await createEmptyDoc();
    addPara(doc, [{ text: "The Supplier shall pay 30 days." }]);
    const buf = await doc.save();
    const engDoc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(engDoc, "Tester");
    const stats = engine.process_batch([
      {
        type: "modify",
        target_text: "(?i)supplier shall pay (\\d+) days",
        new_text: "Supplier shall pay 45 days",
        regex: true,
      } as any,
    ]);
    expect(stats.edits_applied).toBe(1);
  });

  it("strips surrounding quotes in parse_page_arg and supports page range tuple in build_search_response", () => {
    expect(parse_page_arg("'all'")).toEqual(["all", null]);
    expect(parse_page_arg('"all"')).toEqual(["all", null]);
    expect(parse_page_arg("'2-6'")).toEqual(["range", [2, 6]]);
    expect(parse_page_arg('"3"')).toEqual(["single", 3]);

    const body = makeBodyWithPages(6);
    const res = build_search_response(
      body,
      "Paragraph",
      false,
      true,
      [2, 4] as any,
      "/tmp/doc.docx",
    );
    const text = res.content[0].text;
    expect(text).toContain("Search Results");
    expect(text).toContain("(p2)");
    expect(text).not.toContain("(p1)");
    expect(text).not.toContain("(p5)");
  });

  it("emits continuation footer on full 8-page window when last < total_pages", () => {
    const body = makeBodyWithPages(14);
    const res = build_page_range_response(body, 1, 8, "/tmp/doc14.docx");
    const text = res.content[0].text;
    expect(text).toContain("Continues on page 9 of ");
    expect(text).toContain('page="9-');
  });

  it("detects standalone structural bold section headers in extract_outline", async () => {
    const doc = await createEmptyDoc();
    addPara(doc, [{ text: "MASTER SERVICES AGREEMENT", bold: true }]);
    addPara(doc, [{ text: "Article I — Definitions", bold: true }]);
    addPara(doc, [{ text: "Paragraph 1 — Services and Exclusivity", bold: true }]);
    addPara(doc, [{ text: "The Service Provider shall provide the services." }]);
    addPara(doc, [{ text: "Section 2.01 — Purchase Orders", bold: true }]);
    addPara(doc, [{ text: "Each purchase order shall be in writing." }]);
    addPara(doc, [{ text: "3. Term and Termination", bold: true }]);
    addPara(doc, [
      { text: "3.1 Initial Term. ", bold: true },
      { text: "This Agreement commences on the Effective Date." },
    ]);

    const buf = await doc.save();
    const loaded = await DocumentObject.load(buf);
    const text = await extractTextFromBuffer(buf);
    const res = build_outline_response(loaded, text, "/tmp/bold.docx");
    const out = res.content[0].text;

    expect(out).toContain("# Article I — Definitions");
    expect(out).toContain("## Paragraph 1 — Services and Exclusivity");
    expect(out).toContain("## Section 2.01 — Purchase Orders");
    expect(out).toContain("## 3. Term and Termination");
    expect(out).not.toContain("Initial Term");
  });
});
