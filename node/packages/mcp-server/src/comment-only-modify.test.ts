import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DocumentObject, extract_comments_data } from "@adeu/core";
import { coerceChangeItemInPlace } from "./index.js";
import { startTestServer, TestServer } from "./test-rpc.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

describe("B3: comment-only modify boundary normalisation", () => {
  describe("coerceChangeItemInPlace unit tests", () => {
    it("1. populates new_text = target_text when type is modify, new_text is missing, and non-empty comment is present", () => {
      const item: any = { type: "modify", target_text: "X", comment: "why" };
      coerceChangeItemInPlace(item);
      expect(item).toEqual({
        type: "modify",
        target_text: "X",
        comment: "why",
        new_text: "X",
      });
    });

    it("2. leaves explicit new_text: '' untouched (empty string means delete)", () => {
      const item: any = { type: "modify", target_text: "X", new_text: "", comment: "why" };
      coerceChangeItemInPlace(item);
      expect(item.new_text).toBe("");
    });

    it("3. leaves new_text absent when comment is absent or whitespace-only", () => {
      const item1: any = { type: "modify", target_text: "X" };
      coerceChangeItemInPlace(item1);
      expect(item1.new_text).toBeUndefined();

      const item2: any = { type: "modify", target_text: "X", comment: "   " };
      coerceChangeItemInPlace(item2);
      expect(item2.new_text).toBeUndefined();
    });

    it("4. does NOT infer type when type is absent with target_text + comment", () => {
      const item: any = { target_text: "X", comment: "why" };
      coerceChangeItemInPlace(item);
      expect(item.type).toBeUndefined();
      expect(item.new_text).toBeUndefined();
    });
  });

  describe("process_document_batch integration tests over RPC", () => {
    let server: TestServer;

    beforeAll(async () => {
      server = await startTestServer("comment_only_modify");
    }, 30000);

    afterAll(() => {
      server?.stop();
    });

    it("4. rejects typeless edit carrying target_text + comment per-index via typeErrors guard", async () => {
      const docPath = await server.buildDoc(["Some paragraph text."]);
      const res = await server.callTool("process_document_batch", {
        reasoning: "test typeless comment",
        original_docx_path: docPath,
        author_name: "Tester",
        changes: [{ target_text: "Some paragraph text.", comment: "why" }],
        output_path: server.tempOut("typeless"),
      });

      expect(res.isError).toBe(true);
      expect(res.content[0].text).toContain('missing or unrecognized "type"');
    });

    it("5 & 6. heading parity and no w:del regression for comment-only modify", async () => {
      // Build a fixture document with a Heading 2 paragraph ("Term") and body text
      const initialPath = resolve(__dirname, "../../../../shared/fixtures/initial.docx");
      const doc = await DocumentObject.load(readFileSync(initialPath));
      const body = doc.element;
      while (body.firstChild) body.removeChild(body.firstChild);
      const xmlDoc = body.ownerDocument!;

      // Heading 2: "Term"
      const p1 = xmlDoc.createElement("w:p");
      const pPr1 = xmlDoc.createElement("w:pPr");
      const pStyle1 = xmlDoc.createElement("w:pStyle");
      pStyle1.setAttribute("w:val", "Heading2");
      pPr1.appendChild(pStyle1);
      p1.appendChild(pPr1);
      const r1 = xmlDoc.createElement("w:r");
      const t1 = xmlDoc.createElement("w:t");
      t1.textContent = "Term";
      r1.appendChild(t1);
      p1.appendChild(r1);
      body.appendChild(p1);

      // Paragraph: "Normal paragraph text."
      const p2 = xmlDoc.createElement("w:p");
      const r2 = xmlDoc.createElement("w:r");
      const t2 = xmlDoc.createElement("w:t");
      t2.textContent = "Normal paragraph text.";
      r2.appendChild(t2);
      p2.appendChild(r2);
      body.appendChild(p2);

      const headingDocPath = server.tempOut("heading_doc.docx");
      writeFileSync(headingDocPath, await doc.save());

      const outPath = server.tempOut("heading_out.docx");

      // Agent sends { type: "modify", target_text: "## Term", comment: "heading comment" }
      const res = await server.callTool("process_document_batch", {
        reasoning: "annotate heading without text changes",
        original_docx_path: headingDocPath,
        author_name: "Reviewer AI",
        changes: [
          {
            type: "modify",
            target_text: "## Term",
            comment: "heading comment",
          },
        ],
        output_path: outPath,
      });

      expect(res.isError).toBeFalsy();
      expect(res.content[0].text).toContain("Edits: 1 applied");

      // Verify resulting document:
      const savedBuf = readFileSync(outPath);
      const savedDoc = await DocumentObject.load(savedBuf);

      // 1. Comment is present
      const commentsData = extract_comments_data(savedDoc.pkg);
      expect(commentsData).not.toBeNull();
      const commentsList = Object.values(commentsData || {});
      expect(commentsList.some((c: any) => c.text.includes("heading comment"))).toBe(true);

      // 2. XML carries no w:del tags
      const dels = savedDoc.element.getElementsByTagName("w:del");
      expect(dels.length).toBe(0);
    });
  });
});
