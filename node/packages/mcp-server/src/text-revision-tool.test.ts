// FILE: node/packages/mcp-server/src/text-revision-tool.test.ts
//
// `apply_text_revision` over the LIVE stdio boundary (Task 19 / spec C3): the
// tool an agent uses to hand back a whole revised document. Pinned live because
// the integrity-critical halves are the boundary's own — what the schema
// advertises, and what the server does to the FILESYSTEM when the post-apply
// verification gate refuses a document (nothing at output_path, a diagnostic
// sibling next to it) or when the deletion budget refuses one (nothing at all).
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DocumentObject, _extractTextFromDoc } from "@adeu/core";
import { startTestServer, TestServer } from "./test-rpc.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

const PARAGRAPHS = [
  "This is the original paragraph one of the document.",
  "This is paragraph two, containing more text for testing purposes.",
  "And paragraph three concludes the baseline document content.",
];

const REVISED =
  "This is the revised paragraph one of the document.\n\n" +
  "This is paragraph two, containing more text for testing purposes.\n\n" +
  "And paragraph three concludes the baseline document content.";

/**
 * A document whose body is one paragraph plus a 2x2 table, written to `path`.
 * A table ROW's text cannot be removed by text replacement — the tracked
 * deletion empties the cells and the row survives — which is exactly what the
 * verification gate exists to catch.
 */
async function writeTableDoc(path: string): Promise<void> {
  const initial = resolve(__dirname, "../../../../shared/fixtures/initial.docx");
  const doc = await DocumentObject.load(readFileSync(initial));
  const body = doc.element;
  while (body.firstChild) body.removeChild(body.firstChild);
  const xml = body.ownerDocument!;

  const para = xml.createElement("w:p");
  const run = xml.createElement("w:r");
  const t = xml.createElement("w:t");
  t.textContent = "The fee schedule below governs all invoices issued.";
  t.setAttribute("xml:space", "preserve");
  run.appendChild(t);
  para.appendChild(run);
  body.appendChild(para);

  const tbl = xml.createElement("w:tbl");
  const grid = xml.createElement("w:tblGrid");
  grid.appendChild(xml.createElement("w:gridCol"));
  grid.appendChild(xml.createElement("w:gridCol"));
  tbl.appendChild(grid);
  for (const cells of [
    ["Service", "Fee"],
    ["Audit", "1000"],
  ]) {
    const tr = xml.createElement("w:tr");
    for (const value of cells) {
      const tc = xml.createElement("w:tc");
      const p = xml.createElement("w:p");
      const r = xml.createElement("w:r");
      const cellText = xml.createElement("w:t");
      cellText.textContent = value;
      r.appendChild(cellText);
      p.appendChild(r);
      tc.appendChild(p);
      tr.appendChild(tc);
    }
    tbl.appendChild(tr);
  }
  body.appendChild(tbl);

  writeFileSync(path, await doc.save());
}

/** The `.unverified.docx` sibling of an output path. */
function unverifiedSibling(outPath: string): string {
  return outPath.replace(/\.docx$/, ".unverified.docx");
}

describe("apply_text_revision MCP tool (C3)", () => {
  let server: TestServer;
  let docPath: string;
  const strays: string[] = [];

  beforeAll(async () => {
    server = await startTestServer("text_revision");
    docPath = await server.buildDoc(PARAGRAPHS);
  }, 30000);

  afterAll(() => {
    for (const p of strays) {
      if (existsSync(p)) {
        try {
          unlinkSync(p);
        } catch {
          // best-effort cleanup
        }
      }
    }
    server?.stop();
  });

  it("9. tools/list advertises the tool, its six parameters and both contracts", async () => {
    const list = await server.rpc("tools/list", {});
    const tool = (list.result.tools ?? []).find(
      (t: any) => t.name === "apply_text_revision",
    );
    expect(tool, "apply_text_revision is not advertised").toBeTruthy();

    const props = tool.inputSchema.properties ?? {};
    for (const name of [
      "file_path",
      "revised_text",
      "output_path",
      "author",
      "allow_major_deletions",
      "reasoning",
    ]) {
      expect(Object.keys(props), `missing parameter ${name}`).toContain(name);
    }
    expect(tool.inputSchema.required).toEqual(
      expect.arrayContaining(["file_path", "revised_text"]),
    );
    for (const optional of ["output_path", "author", "reasoning"]) {
      expect(tool.inputSchema.required ?? []).not.toContain(optional);
    }

    // The input contract (a COMPLETE clean view, not CriticMarkup, not one
    // page) and the deletion interlock are the two things an agent cannot
    // infer from the parameter names.
    expect(tool.description).toContain("clean_view");
    expect(tool.description).toContain("complete");
    expect(tool.description).toContain("allow_major_deletions");
    expect(props.allow_major_deletions.description).toContain(
      ">50% of characters (>75% for documents under 2000 characters)",
    );
    // Real MCP clients truncate tool descriptions at ~2048 chars, build tag
    // included — whatever falls past that is invisible to the model
    // (QA 2026-07-23 client-compat).
    expect(tool.description.length).toBeLessThan(2048);
  });

  it("10. a successful call reports the saved path and the applied/skipped counters", async () => {
    const outPath = server.tempOut("ok");
    const res = await server.callTool("apply_text_revision", {
      reasoning: "Applying the counterparty's clean redraft.",
      file_path: docPath,
      revised_text: REVISED,
      output_path: outPath,
      author: "TestAuthor",
    });

    const text = res.content[0].text as string;
    expect(res.isError, text).toBeFalsy();
    expect(text).toContain(`Saved to: ${outPath}`);
    expect(text).toContain("Edits: 1 applied, 0 skipped.");
    expect(text).toContain("Actions: 0 applied, 0 skipped.");
    expect(existsSync(outPath)).toBe(true);

    // The saved document reads as the supplied text once accepted, and carries
    // the change as a tracked one rather than a silent rewrite.
    const saved = await DocumentObject.load(readFileSync(outPath));
    expect(_extractTextFromDoc(saved, true, false)).toBe(REVISED);
    expect(_extractTextFromDoc(saved, false, false)).toContain(
      "{--original--}{++revised++}",
    );
  });

  it("11. a verification failure writes the .unverified.docx sibling and nothing else", async () => {
    const srcPath = server.tempOut("table_src");
    await writeTableDoc(srcPath);
    const outPath = server.tempOut("verify");
    const sibling = unverifiedSibling(outPath);
    strays.push(sibling);

    // The revised text drops the "Audit | 1000" row — a change the document's
    // structure cannot realize through text replacement.
    const originalText = _extractTextFromDoc(
      await DocumentObject.load(readFileSync(srcPath)),
      true,
      false,
    ) as string;
    expect(originalText).toContain("Audit | 1000");
    const revised = originalText
      .split("\n")
      .filter((line) => !line.includes("Audit"))
      .join("\n");

    const res = await server.callTool("apply_text_revision", {
      file_path: srcPath,
      revised_text: revised,
      output_path: outPath,
    });

    const text = res.content[0].text as string;
    expect(res.isError, text).toBe(true);
    expect(text).toContain("Post-apply verification failed");
    expect(text).toContain(`Nothing was written to '${outPath}'`);
    expect(text).toContain(sibling);
    expect(text).toContain("it is NOT the requested document");

    expect(existsSync(outPath)).toBe(false);
    expect(existsSync(sibling)).toBe(true);
  });

  it("12. a major-deletion refusal names the threshold and writes no file", async () => {
    const outPath = server.tempOut("refused");
    const res = await server.callTool("apply_text_revision", {
      file_path: docPath,
      revised_text: "This is short.",
      output_path: outPath,
    });

    const text = res.content[0].text as string;
    expect(res.isError, text).toBe(true);
    expect(text).toContain("shorter than the document's clean text");
    expect(text).toContain("threshold is >75% deletion");
    expect(text).toContain("allow_major_deletions=True");
    expect(existsSync(outPath)).toBe(false);
    expect(existsSync(unverifiedSibling(outPath))).toBe(false);
  });
});
