/**
 * A2.3 / A2.4 — the fields ledger at the MCP surface (CC-2).
 *
 * The engine-level renderer is pinned in `@adeu/core`'s cc_fields_ledger
 * suite. What this file pins is the SURFACE: that `mode="fields"` reaches the
 * renderer, that `fields_offset` paginates it, and that the appendix carries
 * the summary without the detail lines.
 */
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ccFixtureBytes } from "../../core/src/test-utils.js";
import { DocumentObject, extractTextFromBuffer } from "@adeu/core";
import { build_fields_response } from "./ledger.js";
import { build_appendix_response } from "./response-builders.js";

let workDir: string;
let fixturePath: string;

function manyControls(n: number): string {
  let out = "";
  for (let i = 1; i <= n; i++) {
    out +=
      `<w:p><w:sdt><w:sdtPr><w:tag w:val="f${i}"/><w:text/></w:sdtPr>` +
      `<w:sdtContent><w:r><w:t>V${i}</w:t></w:r></w:sdtContent></w:sdt></w:p>`;
  }
  return out;
}

async function fieldsMarkdown(
  bytes: Uint8Array,
  path: string,
  offset = 0,
): Promise<string> {
  const buf = Buffer.from(bytes);
  const doc = await DocumentObject.load(buf);
  const raw = await extractTextFromBuffer(buf, false, false);
  const res: any = build_fields_response(doc, raw, path, { offset });
  return res.structuredContent.markdown;
}

beforeAll(() => {
  workDir = mkdtempSync(join(tmpdir(), "adeu-cc2-"));
  fixturePath = join(workDir, "cc_fixture.docx");
  writeFileSync(fixturePath, Buffer.from(ccFixtureBytes()));
});

afterAll(() => {
  if (workDir) rmSync(workDir, { recursive: true, force: true });
});

describe("mode='fields' response shape", () => {
  it("returns the ledger as both text content and structured markdown", async () => {
    const buf = Buffer.from(ccFixtureBytes());
    const doc = await DocumentObject.load(buf);
    const raw = await extractTextFromBuffer(buf, false, false);
    const res: any = build_fields_response(doc, raw, fixturePath, {});

    expect(res.content[0].type).toBe("text");
    // The File Path line is LLM-only chrome, exactly as every other mode.
    expect(res.content[0].text).toContain("> **File Path:**");
    expect(res.content[0].text).toContain("# Fields: cc_fixture.docx");
    expect(res.structuredContent.markdown.startsWith("# Fields:")).toBe(true);
    expect(res.structuredContent.title).toBe("cc_fixture.docx");
  });

  it("omits chrome under no_chrome", async () => {
    const buf = Buffer.from(ccFixtureBytes());
    const doc = await DocumentObject.load(buf);
    const raw = await extractTextFromBuffer(buf, false, false);
    const res: any = build_fields_response(doc, raw, fixturePath, {
      no_chrome: true,
    });
    expect(res.content[0].text).not.toContain("> **File Path:**");
  });
});

describe("A2.3 — fields_offset pagination at the surface", () => {
  const bytes = () => ccFixtureBytes(undefined, manyControls(250));

  it("returns CC:1-100 and points at offset 100", async () => {
    const md = await fieldsMarkdown(bytes(), fixturePath, 0);
    const cc = md.split("\n").filter((l) => l.startsWith("CC:"));
    expect(cc.length).toBe(100);
    expect(cc[0].startsWith("CC:1 ")).toBe(true);
    expect(cc[99].startsWith("CC:100 ")).toBe(true);
    expect(md.endsWith("\u2026 150 more \u2014 pass fields_offset=100 to continue.")).toBe(
      true,
    );
  });

  it("returns CC:101-200 and points at offset 200", async () => {
    const md = await fieldsMarkdown(bytes(), fixturePath, 100);
    const cc = md.split("\n").filter((l) => l.startsWith("CC:"));
    expect(cc[0].startsWith("CC:101 ")).toBe(true);
    expect(cc[99].startsWith("CC:200 ")).toBe(true);
    expect(md.endsWith("\u2026 50 more \u2014 pass fields_offset=200 to continue.")).toBe(
      true,
    );
  });

  it("returns CC:201-250 with no continuation", async () => {
    const md = await fieldsMarkdown(bytes(), fixturePath, 200);
    const cc = md.split("\n").filter((l) => l.startsWith("CC:"));
    expect(cc.length).toBe(50);
    expect(cc[49].startsWith("CC:250 ")).toBe(true);
    expect(md).not.toContain("pass fields_offset");
  });
});

describe("A2.4 — appendix carries the summary, never the detail", () => {
  it("has a Content Controls section with the header lines only", async () => {
    const buf = Buffer.from(ccFixtureBytes());
    const raw = await extractTextFromBuffer(buf, false, true);
    const res: any = build_appendix_response(raw, 1, fixturePath);
    const md: string = res.structuredContent.markdown;

    expect(md).toContain("## Content Controls");
    expect(md).toContain(
      "Protection: none \u00b7 16 content controls \u2014 1 empty \u00b7 2 locked \u00b7 1 bound",
    );
    expect(md).toContain('Read with mode="fields" for the full field ledger.');

    // The bounded-appendix rule: FedRAMP rev4 would put 5,007 lines here.
    expect(md).not.toMatch(/^CC:\d/m);
  });

  it("omits the section entirely for a plain document", async () => {
    const plain = "<w:p><w:r><w:t>Plain paragraph.</w:t></w:r></w:p>";
    const buf = Buffer.from(ccFixtureBytes(undefined, plain));
    const raw = await extractTextFromBuffer(buf, false, true);
    // No controls and no protection: a plain document gains zero noise.
    expect(raw).not.toContain("## Content Controls");
  });
});
