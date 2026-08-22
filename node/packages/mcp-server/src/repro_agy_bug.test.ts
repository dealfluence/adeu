import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { DocumentObject, RedlineEngine } from "@adeu/core";
import { startTestServer, type TestServer } from "./test-rpc.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

describe("QA Regression Test - Finding 1: finalize_document crash on missing sanitize_mode with tracked changes", () => {
  let server: TestServer;
  let trackedDocPath: string;
  let outputDocPath: string;

  beforeAll(async () => {
    const fixturePath = resolve(
      __dirname,
      "../../../../shared/fixtures/golden.docx",
    );

    trackedDocPath = join(tmpdir(), `adeu_regression_tracked_${Date.now()}.docx`);
    outputDocPath = join(tmpdir(), `adeu_regression_output_${Date.now()}.docx`);

    const fixtureBuf = readFileSync(fixturePath);
    const doc = await DocumentObject.load(fixtureBuf);
    const engine = new RedlineEngine(doc, "Reviewer");

    engine.process_batch([
      {
        type: "modify",
        target_text: "document",
        new_text: "modified tracked document",
      },
    ]);
    writeFileSync(trackedDocPath, await doc.save());

    server = await startTestServer("agy-bug");
  });

  afterAll(() => {
    server?.stop();
    if (existsSync(trackedDocPath)) unlinkSync(trackedDocPath);
    if (existsSync(outputDocPath)) unlinkSync(outputDocPath);
  });

  it("should return a clean block report instead of crashing when sanitize_mode is omitted with tracked changes", async () => {
    const res = await server.rpc(
      "tools/call",
      {
        name: "finalize_document",
        arguments: {
          file_path: trackedDocPath,
          output_path: outputDocPath,
          reasoning: "Test finalizing with tracked changes but no sanitize_mode",
        },
      },
    );

    expect(res.error).toBeUndefined();
    expect(res.result).toBeDefined();
    expect(res.result.isError).toBeUndefined();

    const responseText = res.result.content[0].text;
    expect(responseText).not.toContain("TypeError");
    expect(responseText).not.toContain("must be of type string");
    expect(responseText.toLowerCase()).toContain("blocked");
    expect(responseText).toContain("unresolved tracked changes");
    expect(existsSync(outputDocPath)).toBe(false);
  });
});

/**
 * Creates a DOCX document containing a table with empty cells.
 */
async function writeTableWithEmptyCellDoc(path: string): Promise<void> {
  const initial = resolve(__dirname, "../../../../shared/fixtures/initial.docx");
  const doc = await DocumentObject.load(readFileSync(initial));
  const body = doc.element;
  while (body.firstChild) body.removeChild(body.firstChild);
  const xml = body.ownerDocument!;

  const para1 = xml.createElement("w:p");
  const run1 = xml.createElement("w:r");
  const t1 = xml.createElement("w:t");
  t1.textContent = "Table Test Document";
  run1.appendChild(t1);
  para1.appendChild(run1);
  body.appendChild(para1);

  const para2 = xml.createElement("w:p");
  const run2 = xml.createElement("w:r");
  const t2 = xml.createElement("w:t");
  t2.textContent = "Below is a sample pricing table:";
  run2.appendChild(t2);
  para2.appendChild(run2);
  body.appendChild(para2);

  const tbl = xml.createElement("w:tbl");
  const grid = xml.createElement("w:tblGrid");
  grid.appendChild(xml.createElement("w:gridCol"));
  grid.appendChild(xml.createElement("w:gridCol"));
  grid.appendChild(xml.createElement("w:gridCol"));
  tbl.appendChild(grid);

  const rowsData = [
    ["Item", "Quantity", "Price"],
    ["Widget A", "10", "$100.00"],
    ["Widget B", "", "$250.00"], // Middle cell is empty!
  ];

  for (const rowCells of rowsData) {
    const tr = xml.createElement("w:tr");
    for (const value of rowCells) {
      const tc = xml.createElement("w:tc");
      const p = xml.createElement("w:p");
      if (value) {
        const r = xml.createElement("w:r");
        const cellText = xml.createElement("w:t");
        cellText.textContent = value;
        r.appendChild(cellText);
        p.appendChild(r);
      }
      tc.appendChild(p);
      tr.appendChild(tc);
    }
    tbl.appendChild(tr);
  }
  body.appendChild(tbl);

  writeFileSync(path, await doc.save());
}

describe("QA Regression Test - apply_text_revision rejection of read_docx payloads with empty cell anchors", () => {
  let server: TestServer;
  let docPath: string;
  let outPath: string;

  beforeAll(async () => {
    server = await startTestServer("repro_empty_cell_anchor");
    docPath = server.tempOut("doc_table.docx");
    outPath = server.tempOut("doc_table_revised.docx");
    await writeTableWithEmptyCellDoc(docPath);
  }, 30000);

  afterAll(() => {
    server?.stop();
  });

  it("should apply text revision successfully on document with empty table cells", async () => {
    // 1. Read the document using read_docx
    const readRes = await server.callTool("read_docx", {
      reasoning: "Reading document content before applying revision.",
      file_path: docPath,
      page: "all",
    });

    expect(readRes.isError).toBeFalsy();
    const readText = readRes.content[0].text as string;

    // Verify read_docx output contains empty table cell anchor
    expect(readText).toMatch(/Widget B \| \{#cell:[0-9a-fA-F]{8}\} \| \$250\.00/);

    // 2. Prepare revised text by making a minor edit in prose
    const revisedText = readText.replace(
      "Below is a sample pricing table:",
      "Below is an updated pricing table:",
    );

    // 3. Apply text revision
    const applyRes = await server.callTool("apply_text_revision", {
      reasoning: "Updating pricing table introduction.",
      file_path: docPath,
      revised_text: revisedText,
      output_path: outPath,
    });

    // Expect apply_text_revision to succeed without batch validation error
    const resultText = applyRes.content[0].text as string;
    expect(applyRes.isError, `apply_text_revision failed with error: ${resultText}`).toBeFalsy();
    expect(resultText).toContain(`Saved to: ${outPath}`);
    expect(existsSync(outPath)).toBe(true);
  });
});
