import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { DocumentObject } from "@adeu/core";
import { startTestServer, TestServer } from "./test-rpc.js";

const _dirname = dirname(fileURLToPath(import.meta.url));

async function createSampleDoc(path: string, paragraphs: string[]): Promise<void> {
  const fixturePath = resolve(_dirname, "../../../../shared/fixtures/initial.docx");
  const doc = await DocumentObject.load(readFileSync(fixturePath));
  while (doc.element.firstChild) {
    doc.element.removeChild(doc.element.firstChild);
  }
  const xmlDoc = doc.element.ownerDocument!;
  for (const text of paragraphs) {
    const p = xmlDoc.createElement("w:p");
    const r = xmlDoc.createElement("w:r");
    const t = xmlDoc.createElement("w:t");
    t.setAttribute("xml:space", "preserve");
    t.textContent = text;
    r.appendChild(t);
    p.appendChild(r);
    doc.element.appendChild(p);
  }
  writeFileSync(path, await doc.save());
}

describe("Task 10: derivative filename in-place detection and enriched batch receipts", () => {
  let srv: TestServer;

  beforeAll(async () => {
    srv = await startTestServer("batch-ergonomics");
  });

  afterAll(() => {
    srv.stop();
  });

  it("updates derivative redline filenames in place when output_path is omitted", async () => {
    const dir = mkdtempSync(join(tmpdir(), "adeu-t10-"));

    for (const filename of [
      "master-supply-agreement.redline.docx",
      "contract-redline.docx",
      "agreement_markup.docx",
      "terms.marked.docx",
    ]) {
      const docPath = join(dir, filename);
      await createSampleDoc(docPath, [
        "The Supplier shall deliver the goods within 30 days.",
      ]);

      const res = await srv.callTool("process_document_batch", {
        original_docx_path: docPath,
        author_name: "Tester",
        changes: [
          {
            type: "modify",
            target_text: "30 days",
            new_text: "45 days",
          },
        ],
      });
      const text = res.content?.[0]?.text ?? "";
      expect(text).toContain(`Saved to: ${docPath}`);
      const stem = filename.replace(/\.docx$/i, "");
      expect(existsSync(join(dir, `${stem}_processed.docx`))).toBe(false);
    }
  });

  it("includes minted IDs, action receipts, and Document State in batch output", async () => {
    const dir = mkdtempSync(join(tmpdir(), "adeu-t10-receipts-"));
    const docPath = join(dir, "contract_redlined.docx");
    await createSampleDoc(docPath, [
      "The Supplier shall pay 30 days net.",
      "Governing law shall be the State of Delaware.",
    ]);

    const res1 = await srv.callTool("process_document_batch", {
      original_docx_path: docPath,
      author_name: "Reviewer",
      changes: [
        {
          type: "modify",
          target_text: "30 days",
          new_text: "45 days",
          comment: "Extend payment term.",
        },
        {
          type: "modify",
          target_text: "State of Delaware",
          new_text: "State of New York",
        },
      ],
    });
    const text1 = res1.content?.[0]?.text ?? "";
    expect(text1).toContain("**IDs:**");
    expect(text1).toContain("`Chg:1`");
    expect(text1).toContain("`Com:1`");
    expect(text1).toContain("Document State:");
    expect(text1).toContain("comment(s) remaining");

    const res2 = await srv.callTool("process_document_batch", {
      original_docx_path: docPath,
      author_name: "Reviewer",
      changes: [
        {
          type: "accept",
          target_id: "Chg:1",
        },
        {
          type: "reply",
          target_id: "Com:1",
          text: "Agreed on 45 days.",
        },
      ],
    });
    const text2 = res2.content?.[0]?.text ?? "";
    expect(text2).toContain("Applied Actions:");
    expect(text2).toContain("`Chg:1`");
    expect(text2).toContain("`Com:1`");
    expect(text2).toContain("Document State:");
  });
});
