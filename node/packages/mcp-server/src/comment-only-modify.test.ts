import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DocumentObject, createTestDocument, extract_comments_data } from "@adeu/core";
import { coerceChangeItemInPlace, CHANGE_ITEM_SCHEMA } from "./index.js";
import { startTestServer, TestServer } from "./test-rpc.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

/** Concatenated `w:t` text of a paragraph, in document order. */
function paragraphText(p: Element): string {
  return Array.from(p.getElementsByTagName("w:t"))
    .map((t) => t.textContent || "")
    .join("");
}

describe("comment-only modify boundary normalization", () => {
  it("populates new_text = target_text when type is modify, new_text is missing, and non-empty comment is present (including heading syntax)", () => {
    const item: any = { type: "modify", target_text: "## Term", comment: "why" };
    coerceChangeItemInPlace(item);
    expect(item).toEqual({
      type: "modify",
      target_text: "## Term",
      comment: "why",
      new_text: "## Term",
    });

    const parsed = CHANGE_ITEM_SCHEMA.parse(item);
    expect(parsed.new_text).toBe("## Term");
  });

  it("leaves explicit new_text: '' untouched (empty string means delete)", () => {
    const item: any = { type: "modify", target_text: "X", new_text: "", comment: "why" };
    coerceChangeItemInPlace(item);
    expect(item.new_text).toBe("");

    const parsed = CHANGE_ITEM_SCHEMA.parse(item);
    expect(parsed.new_text).toBe("");
  });

  it("leaves new_text absent when comment is absent or whitespace-only", () => {
    const item1: any = { type: "modify", target_text: "X" };
    coerceChangeItemInPlace(item1);
    expect(item1.new_text).toBeUndefined();

    const item2: any = { type: "modify", target_text: "X", comment: "   " };
    coerceChangeItemInPlace(item2);
    expect(item2.new_text).toBeUndefined();
  });

  it("does NOT infer type or populate new_text when type is absent with target_text + comment", () => {
    const item: any = { target_text: "X", comment: "why" };
    coerceChangeItemInPlace(item);
    expect(item.type).toBeUndefined();
    expect(item.new_text).toBeUndefined();
  });
});

// The unit cases above prove WHERE the normalisation happens. The live cases
// below prove two different things:
//   - the heading-parity and never-a-w:del guards are downstream regression
//     guards asserted on the SAVED document, NOT proof of location: the engine
//     normalises an absent new_text too (engine.ts, "QA 2026-07-23 customer
//     C3"), so an omitted new_text still reaches the engine and is repaired
//     there even with the boundary block removed;
//   - the `new_text: null` case is boundary-exclusive: `new_text` is
//     `z.string().optional()`, so an explicit null never reaches the engine —
//     without the boundary coercion in the item preprocess the call comes back
//     as an isError result carrying "MCP error -32602: … expected string,
//     received null at changes[0].new_text", before the handler ever runs.
describe("comment-only modify through process_document_batch (live MCP server)", () => {
  let server: TestServer;
  let reportText: string;
  let savedDoc: DocumentObject;
  let headingDocPath: string;

  beforeAll(async () => {
    server = await startTestServer("comment_only_modify");

    // Fixture: a Heading 2 "Term" (which `read_docx` renders as "## Term", the
    // form an agent copies into target_text) plus one ordinary paragraph.
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;

    const heading = xmlDoc.createElement("w:p");
    const pPr = xmlDoc.createElement("w:pPr");
    const pStyle = xmlDoc.createElement("w:pStyle");
    pStyle.setAttribute("w:val", "Heading2");
    pPr.appendChild(pStyle);
    heading.appendChild(pPr);
    const hRun = xmlDoc.createElement("w:r");
    const hText = xmlDoc.createElement("w:t");
    hText.textContent = "Term";
    hRun.appendChild(hText);
    heading.appendChild(hRun);
    doc.element.appendChild(heading);

    const para = xmlDoc.createElement("w:p");
    const pRun = xmlDoc.createElement("w:r");
    const pText = xmlDoc.createElement("w:t");
    pText.textContent = "Normal paragraph text.";
    pRun.appendChild(pText);
    para.appendChild(pRun);
    doc.element.appendChild(para);

    headingDocPath = server.tempOut("heading_doc");
    writeFileSync(headingDocPath, await doc.save());
    const outPath = server.tempOut("heading_out");

    // The wire payload under test: no `new_text` at all.
    const res = await server.callTool("process_document_batch", {
      reasoning: "annotate the heading without changing its text",
      original_docx_path: headingDocPath,
      author_name: "Reviewer AI",
      changes: [
        {
          type: "modify",
          target_text: "## Term",
          comment: "defined term needs a cross-reference",
        },
      ],
      output_path: outPath,
    });

    expect(res.isError).toBeFalsy();
    reportText = res.content[0].text;
    savedDoc = await DocumentObject.load(readFileSync(outPath));
  }, 60000);

  afterAll(() => {
    server?.stop();
  });

  it("heading parity: the batch applies, the rationale lands as a comment, and the heading keeps its text and style", () => {
    expect(reportText).toContain("Edits: 1 applied");

    const comments = Object.values(extract_comments_data(savedDoc.pkg));
    expect(
      comments.some((c: any) =>
        String(c.text).includes("defined term needs a cross-reference"),
      ),
    ).toBe(true);

    // `stripMatchingHeadingHashes` only runs when new_text is present; the
    // engine's own normalisation also supplies it, so this asserts the
    // end-to-end outcome, not where the repair happened.
    const heading = savedDoc.element.getElementsByTagName("w:p")[0];
    expect(paragraphText(heading)).toBe("Term");
    expect(
      heading.getElementsByTagName("w:pStyle")[0]?.getAttribute("w:val"),
    ).toBe("Heading2");
  });

  it("regression (spec §10): a comment-only modify never produces a tracked deletion", () => {
    expect(savedDoc.element.getElementsByTagName("w:del").length).toBe(0);
    expect(savedDoc.element.getElementsByTagName("w:ins").length).toBe(0);
  });

  it(
    "boundary-exclusive: an explicit new_text: null survives schema validation " +
      "and applies as a comment-only modify",
    async () => {
      // `new_text` is `z.string().optional()`: null is NOT optional-absent, so
      // this payload only reaches the handler because the item preprocess
      // coerces it first. Remove the boundary block and this call returns
      // isError with "MCP error -32602: Input validation error: … expected
      // string, received null at changes[0].new_text" instead.
      const outPath = server.tempOut("null_new_text");
      const res = await server.callTool("process_document_batch", {
        reasoning: "annotate the paragraph without changing its text",
        original_docx_path: headingDocPath,
        author_name: "Reviewer AI",
        changes: [
          {
            type: "modify",
            target_text: "Normal paragraph text.",
            new_text: null,
            comment: "why this paragraph matters",
          },
        ],
        output_path: outPath,
      });

      expect(res.isError).toBeFalsy();
      expect(res.content[0].text).toContain("Edits: 1 applied");

      const doc = await DocumentObject.load(readFileSync(outPath));
      expect(doc.element.getElementsByTagName("w:del").length).toBe(0);
      const comments = Object.values(extract_comments_data(doc.pkg));
      expect(
        comments.some((c: any) =>
          String(c.text).includes("why this paragraph matters"),
        ),
      ).toBe(true);
      const para = doc.element.getElementsByTagName("w:p")[1];
      expect(paragraphText(para)).toBe("Normal paragraph text.");
    },
    60000,
  );
});
