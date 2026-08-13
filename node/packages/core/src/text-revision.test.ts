// FILE: node/packages/core/src/text-revision.test.ts
//
// The whole-text revision primitive (Task 19 / spec C3): a clean-text diff
// turned into tracked changes behind four interlocks — CriticMarkup input,
// paginated extracts, major deletions, and the post-apply verification gate
// that refuses to write a document it cannot prove matches the supplied text.
//
// Ported from python/tests/test_mcp_apply_text_revision.py; the messages are
// asserted VERBATIM because they are the agent's only recovery instructions.
import { describe, it, expect } from "vitest";
import { unzipSync, strFromU8 } from "fflate";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { _extractTextFromDoc } from "./ingest.js";
import {
  TextRevisionError,
  TextRevisionVerificationError,
  apply_text_revision_core,
  check_criticmarkup,
  check_major_deletions,
  strip_page_chrome,
  verify_clean_text,
} from "./text-revision.js";

const PARAGRAPHS = [
  "This is the original paragraph one of the document.",
  "This is paragraph two, containing more text for testing purposes.",
  "And paragraph three concludes the baseline document content.",
];

const REVISED =
  "This is the revised paragraph one of the document.\n\n" +
  "This is paragraph two, containing more text for testing purposes.\n\n" +
  "And paragraph three concludes the baseline document content.";

/** A three-paragraph document, as bytes (each call needs its own DOM). */
async function sampleBytes(): Promise<Buffer> {
  const doc = await createTestDocument();
  for (const p of PARAGRAPHS) addParagraph(doc, p);
  return await doc.save();
}

/** A paragraph plus a 2x2 table — a row whose text no text edit can remove. */
async function tableBytes(): Promise<Buffer> {
  const doc = await createTestDocument();
  addParagraph(doc, "The fee schedule below governs all invoices issued.");
  const tbl = addTable(doc, 2, 2);
  setCellText(tbl, 0, 0, "Service");
  setCellText(tbl, 0, 1, "Fee");
  setCellText(tbl, 1, 0, "Audit");
  setCellText(tbl, 1, 1, "1000");
  return await doc.save();
}

async function loadDoc(bytes: Buffer | Uint8Array): Promise<DocumentObject> {
  return await DocumentObject.load(Buffer.from(bytes));
}

async function cleanTextOf(bytes: Buffer | Uint8Array): Promise<string> {
  return _extractTextFromDoc(await loadDoc(bytes), true, false) as string;
}

function documentXml(bytes: Buffer | Uint8Array): string {
  const files = unzipSync(new Uint8Array(bytes));
  return strFromU8(files["word/document.xml"]);
}

const SRC = join(tmpdir(), "adeu_text_revision_src.docx");

describe("apply_text_revision_core — happy path", () => {
  it("1. produces tracked changes whose clean text is the supplied text", async () => {
    const bytes = await sampleBytes();
    const out = join(tmpdir(), "adeu_text_revision_out.docx");

    const res = await apply_text_revision_core({
      doc: await loadDoc(bytes),
      input_path: SRC,
      revised_text: REVISED,
      output_path: out,
      author: "TestAuthor",
    });

    expect(res.output_path).toBe(out);
    expect(res.stats.verified).toBe(true);
    expect(res.stats.edits_applied).toBe(1);
    expect(res.unverified).toBeUndefined();

    expect(await cleanTextOf(res.out_bytes)).toBe(REVISED);

    const xml = documentXml(res.out_bytes);
    expect(xml).toContain("<w:ins ");
    expect(xml).toContain("<w:del ");
    expect(xml).toContain("revised");
  });
});

describe("apply_text_revision_core — CriticMarkup refusal", () => {
  const MESSAGE =
    "Revised text contains CriticMarkup tokens ({++..++}, {--..--}, {~~..~>..~~}, {==..==}, " +
    "{>>..<<}). `apply_text_revision` compares text against the document's CLEAN view, " +
    "so CriticMarkup tokens would be diffed into the document as literal prose.";

  it("2. refuses every OPEN token with Python's exact message", () => {
    for (const markup of [
      "a {++b++} c",
      "a {--b--} c",
      "a {~~b~>c~~} d",
      "a {==b==} c",
      "a {>>b<<} c",
    ]) {
      expect(() => check_criticmarkup(markup)).toThrow(TextRevisionError);
      expect(() => check_criticmarkup(markup)).toThrow(MESSAGE);
    }
  });

  it("2b. leaves arrows and bare closing tokens alone (they are prose)", () => {
    for (const prose of [
      "Payment flows A ~> B.",
      "Escalation -> resolution within 5 days.",
      "The rate++} is stated below.",
      "Ends with <<} and --} and ==} and ~~}.",
    ]) {
      expect(() => check_criticmarkup(prose)).not.toThrow();
    }
  });

  it("2c. the core refuses a CriticMarkup revision before touching the document", async () => {
    const bytes = await sampleBytes();
    await expect(
      apply_text_revision_core({
        doc: await loadDoc(bytes),
        input_path: SRC,
        revised_text: "This is original text {++with inserted text++}.",
      }),
    ).rejects.toThrow(MESSAGE);
  });
});

describe("apply_text_revision_core — paginated extract refusal", () => {
  it("3. refuses page 2 of 5 of a paginated extract", async () => {
    const bytes = await sampleBytes();
    const pageTwo =
      "> **Page 2 of 5** (synthetic page — a length-based chunk, not a printed Word page)\n\n" +
      "---\n\n" +
      "This is paragraph two, containing more text for testing purposes.";

    await expect(
      apply_text_revision_core({
        doc: await loadDoc(bytes),
        input_path: SRC,
        revised_text: pageTwo,
      }),
    ).rejects.toThrow(
      "Text revision looks like page 2 of 5 of a paginated extract — it contains only part " +
        "of the document, and applying it would delete every page not present. Re-extract " +
        "the ENTIRE document first with --page all --clean-view.",
    );
  });
});

describe("strip_page_chrome", () => {
  const CHROMED =
    "> **File Path:** `C:\\docs\\sample.docx`\n\n" +
    "> **Page 1 of 1** (synthetic page — a length-based chunk, not a printed Word page)\n\n" +
    "---\n\n" +
    REVISED +
    "\n\n---\n\n" +
    "> **Appendix available.** This document has structural metadata. Call `read_docx` with `mode='appendix'`.";

  it("4. strips the File-Path header, the page banner and the appendix pointer", () => {
    const { text, page, total } = strip_page_chrome(CHROMED);
    expect(text).toBe(REVISED);
    expect(page).toBe(1);
    expect(total).toBe(1);
  });

  it("4b. reads page/total off a continuation footer when there is no banner", () => {
    const { text, page, total } = strip_page_chrome(
      "Body text.\n\n---\n\n> **Continues on page 3 of 7.**",
    );
    expect(text).toBe("Body text.");
    expect(page).toBe(2);
    expect(total).toBe(7);
  });

  it("4c. leaves ordinary text untouched", () => {
    const { text, page, total } = strip_page_chrome(REVISED);
    expect(text).toBe(REVISED);
    expect(page).toBeNull();
    expect(total).toBeNull();
  });

  it("4d. a single-page extract with all its chrome is accepted and applied", async () => {
    const bytes = await sampleBytes();
    const res = await apply_text_revision_core({
      doc: await loadDoc(bytes),
      input_path: SRC,
      revised_text: CHROMED,
      output_path: join(tmpdir(), "adeu_text_revision_chrome.docx"),
    });
    expect(res.stats.verified).toBe(true);
    const clean = await cleanTextOf(res.out_bytes);
    expect(clean).toBe(REVISED);
    expect(clean).not.toContain("File Path");
    expect(clean).not.toContain("Appendix available");
  });
});

describe("check_major_deletions", () => {
  it("5. refuses a 60% cut of a document at or above 2000 characters (50% budget)", () => {
    const original = "x".repeat(2500);
    const revised = "y".repeat(1000);
    expect(() => check_major_deletions(original, revised)).toThrow(
      "The revised text is ~60% shorter than the document's clean text " +
        "(1,000 vs 2,500 characters, threshold is >50% deletion). " +
        "Applying it would delete the majority of the document as tracked deletions.\n" +
        "   If the text is a partial extract, re-extract the ENTIRE document with " +
        "`--page all --clean-view` and edit that.\n" +
        "   If the mass deletion is intentional, re-run with --allow-major-deletions " +
        "(over MCP: allow_major_deletions=True).",
    );
  });

  it("5b. names the revised text's source when there is one", () => {
    expect(() =>
      check_major_deletions("x".repeat(2500), "y".repeat(1000), false, "revised.txt"),
    ).toThrow("'revised.txt' is ~60% shorter than the document's clean text");
  });

  it("5c. allows the same 60% cut below 2000 characters (75% floor)", () => {
    expect(() =>
      check_major_deletions("x".repeat(1000), "y".repeat(400)),
    ).not.toThrow();
  });

  it("5d. arms below 2000 characters too, at the higher floor", () => {
    expect(() => check_major_deletions("x".repeat(1000), "y".repeat(100))).toThrow(
      "threshold is >75% deletion",
    );
  });

  it("5e. allow_major_deletions bypasses both budgets", () => {
    expect(() =>
      check_major_deletions("x".repeat(2500), "y".repeat(10), true),
    ).not.toThrow();
    expect(() =>
      check_major_deletions("x".repeat(1000), "y".repeat(10), true),
    ).not.toThrow();
  });

  it("5f. counts characters, not paragraphs", () => {
    const para = (i: number) => `Clause ${i}: this paragraph states an obligation.`;
    const original = Array.from({ length: 200 }, (_, i) => para(i)).join("\n\n");
    const revised = Array.from({ length: 140 }, (_, i) => para(i)).join("\n\n");
    expect(original.length).toBeGreaterThanOrEqual(2000);
    expect(() => check_major_deletions(original, revised)).not.toThrow();
  });

  it("5g. the core refuses a major deletion and honours the override", async () => {
    const bytes = await sampleBytes();
    await expect(
      apply_text_revision_core({
        doc: await loadDoc(bytes),
        input_path: SRC,
        revised_text: "This is short.",
      }),
    ).rejects.toThrow("threshold is >75% deletion");

    const res = await apply_text_revision_core({
      doc: await loadDoc(bytes),
      input_path: SRC,
      revised_text: "This is short.",
      allow_major_deletions: true,
    });
    expect(res.stats.verified).toBe(true);
  });
});

describe("apply_text_revision_core — verification gate", () => {
  it("6. keeps a diagnostic copy and writes nothing to the target path", async () => {
    const bytes = await tableBytes();
    const original = await cleanTextOf(bytes);
    // A table ROW's text cannot be removed by text replacement: the tracked
    // deletion empties the cells and the row itself survives as " | ".
    const revised = original
      .split("\n")
      .filter((line) => !line.includes("Audit"))
      .join("\n");
    const out = join(tmpdir(), "adeu_text_revision_verify.docx");
    const unverified = join(tmpdir(), "adeu_text_revision_verify.unverified.docx");

    let err: TextRevisionVerificationError | null = null;
    try {
      await apply_text_revision_core({
        doc: await loadDoc(bytes),
        input_path: SRC,
        revised_text: revised,
        output_path: out,
      });
    } catch (e) {
      err = e as TextRevisionVerificationError;
    }

    expect(err).toBeInstanceOf(TextRevisionVerificationError);
    expect(err).toBeInstanceOf(TextRevisionError);
    err = err!;

    expect(err.message).toContain(
      "Post-apply verification failed: the applied document's clean text does not match the supplied text",
    );
    expect(err.message).toContain(
      "The document structure could not fully realize the requested text " +
        "(e.g. headings or table cells cannot be deleted via text replacement).",
    );
    // The quoted excerpt is repr()-escaped: no raw newline leaks into the
    // single-line failure message (the emptied row reads "\n |").
    expect(err.message).toContain("applied reads '\\n |'");
    expect(err.message).toContain(`Nothing was written to '${out}'`);
    expect(err.message).toContain(
      `a diagnostic copy was kept at '${unverified}' — it is NOT the requested document.`,
    );

    expect(err.output_path).toBe(out);
    expect(err.unverified_path).toBe(unverified);
    expect(err.unverified_bytes.byteLength).toBeGreaterThan(0);
    // The kept copy is the document the engine actually produced — the one the
    // gate refused — so a human can see what went wrong.
    expect(await cleanTextOf(err.unverified_bytes)).not.toBe(revised);

    expect(err.stats.verified).toBe(false);
    expect(err.stats.error).toBe("verification_failed");
    expect(err.stats.verification_error).toBe(err.message);
    expect(err.stats.edits_applied).toBe(0);
    expect(err.stats.edits_skipped).toBeGreaterThan(0);
    expect(err.stats.actions_applied).toBe(0);
    expect(err.stats.output_path).toBeNull();
    expect(err.stats.unverified_output_path).toBe(unverified);
    expect(err.stats.edits.length).toBeGreaterThan(0);
    for (const report of err.stats.edits) {
      expect(report.status).toBe("failed");
      expect(report.error).toBe("Not applied: post-apply verification failed.");
      expect(report.critic_markup).toBeNull();
      expect(report.clean_text).toBeNull();
    }
  });

  it("6b. verify_clean_text normalizes Markdown heading chrome", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Plain body paragraph.");
    const loaded = await loadDoc(await doc.save());
    expect(verify_clean_text(loaded, "Plain body paragraph.")).toEqual([true, null]);
    const [ok, msg] = verify_clean_text(loaded, "Different body paragraph.");
    expect(ok).toBe(false);
    expect(msg).toContain("first divergence at character 0");
  });

  it("6c. quotes the diverging text the way Python's repr() does", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Plain body paragraph.");
    const loaded = await loadDoc(await doc.save());
    const [, msg] = verify_clean_text(loaded, "Different body paragraph.");
    // repr('Plain body paragraph.') is single-quoted, not JSON's "…".
    expect(msg).toContain("applied reads 'Plain body paragraph.'");
    expect(msg).toContain("supplied text reads 'Different body paragraph.'");
  });

  it("6d. matches repr()'s quote choice and escapes for awkward text", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Don't stop.");
    const loaded = await loadDoc(await doc.save());
    // repr() switches to double quotes when the text holds ' but no ".
    const [, msg] = verify_clean_text(loaded, "Won't stop.");
    expect(msg).toContain(`applied reads "Don't stop."`);
    expect(msg).toContain(`supplied text reads "Won't stop."`);
  });
});

describe("apply_text_revision_core — default output path", () => {
  it("7. x.docx becomes x_redlined.docx", async () => {
    const bytes = await sampleBytes();
    const input = join(tmpdir(), "contract.docx");
    const res = await apply_text_revision_core({
      doc: await loadDoc(bytes),
      input_path: input,
      revised_text: await cleanTextOf(bytes),
    });
    expect(res.output_path).toBe(join(tmpdir(), "contract_redlined.docx"));
  });

  it("7b. an already-suffixed artifact is written in place", async () => {
    const bytes = await sampleBytes();
    for (const stem of ["contract_redlined", "contract_processed"]) {
      const input = join(tmpdir(), `${stem}.docx`);
      const res = await apply_text_revision_core({
        doc: await loadDoc(bytes),
        input_path: input,
        revised_text: await cleanTextOf(bytes),
      });
      expect(res.output_path).toBe(input);
    }
  });

  it("7c. the default output is a .docx whatever the input's extension", async () => {
    const bytes = await sampleBytes();
    // Parity with with_name(f"{stem}_redlined.docx"): the extension is
    // replaced, never inherited — an extensionless input must not produce an
    // extensionless DOCX.
    for (const name of ["contract", "contract.DOCX", "contract.doc"]) {
      const res = await apply_text_revision_core({
        doc: await loadDoc(bytes),
        input_path: join(tmpdir(), name),
        revised_text: await cleanTextOf(bytes),
      });
      expect(res.output_path).toBe(join(tmpdir(), "contract_redlined.docx"));
    }
  });
});

describe("apply_text_revision_core — author resolution", () => {
  async function authorOf(author?: string | null): Promise<string[]> {
    const bytes = await sampleBytes();
    const res = await apply_text_revision_core({
      doc: await loadDoc(bytes),
      input_path: SRC,
      revised_text: REVISED,
      output_path: join(tmpdir(), "adeu_text_revision_author.docx"),
      author,
    });
    const xml = documentXml(res.out_bytes);
    return [...new Set([...xml.matchAll(/w:author="([^"]*)"/g)].map((m) => m[1]))];
  }

  it("8. an explicit author wins over the environment", async () => {
    const previous = process.env.ADEU_AUTHOR;
    process.env.ADEU_AUTHOR = "Env Author";
    try {
      expect(await authorOf("Explicit Author")).toEqual(["Explicit Author"]);
    } finally {
      if (previous === undefined) delete process.env.ADEU_AUTHOR;
      else process.env.ADEU_AUTHOR = previous;
    }
  });

  it("8b. ADEU_AUTHOR is used when no author is passed", async () => {
    const previous = process.env.ADEU_AUTHOR;
    process.env.ADEU_AUTHOR = "Env Author";
    try {
      expect(await authorOf(null)).toEqual(["Env Author"]);
    } finally {
      if (previous === undefined) delete process.env.ADEU_AUTHOR;
      else process.env.ADEU_AUTHOR = previous;
    }
  });

  it("8c. otherwise the Node engine default 'Adeu AI (TS)' — not Python's 'Adeu AI'", async () => {
    const previous = process.env.ADEU_AUTHOR;
    delete process.env.ADEU_AUTHOR;
    try {
      expect(await authorOf(undefined)).toEqual(["Adeu AI (TS)"]);
    } finally {
      if (previous !== undefined) process.env.ADEU_AUTHOR = previous;
    }
  });
});
