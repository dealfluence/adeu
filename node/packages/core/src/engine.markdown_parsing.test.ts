import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer, _extractTextFromDoc } from "./ingest.js";
import { RedlineEngine } from "./engine.js";
import { ModifyText } from "./models.js";

describe("Inline Markdown Parsing & Underscore Runs (Node.js Parity)", () => {
  function parseAndCheck(
    engine: RedlineEngine,
    text: string,
    expectedTokens: [string, any][],
  ) {
    const tokens = (engine as any)._parse_inline_markdown(text);
    expect(tokens).toEqual(expectedTokens);
  }

  it("preserves underscore runs as literal text", async () => {
    const doc = await createTestDocument();
    const engine = new RedlineEngine(doc);

    parseAndCheck(engine, "__", [["__", {}]]);
    parseAndCheck(engine, "____", [["____", {}]]);
    parseAndCheck(engine, "__________", [["__________", {}]]);
    parseAndCheck(engine, "___", [["___", {}]]);
    parseAndCheck(engine, "_____", [["_____", {}]]);
  });

  it("handles intra-word underscores correctly", async () => {
    const doc = await createTestDocument();
    const engine = new RedlineEngine(doc);

    parseAndCheck(engine, "foo_bar_baz", [["foo_bar_baz", {}]]);
    parseAndCheck(engine, "foo__bar", [["foo__bar", {}]]);
    parseAndCheck(engine, "_foo_bar_", [["foo_bar", { italic: true }]]);
  });

  it("preserves empty delimiters as literal text", async () => {
    const doc = await createTestDocument();
    const engine = new RedlineEngine(doc);

    parseAndCheck(engine, "****", [["****", {}]]);
    parseAndCheck(engine, "__", [["__", {}]]);
  });

  it("supports backslash escaping for markdown syntax", async () => {
    const doc = await createTestDocument();
    const engine = new RedlineEngine(doc);

    parseAndCheck(engine, "\\_", [["_", {}]]);
    parseAndCheck(engine, "\\_\\_", [["__", {}]]);
    parseAndCheck(engine, "foo\\_bar", [["foo_bar", {}]]);
    parseAndCheck(engine, "\\*", [["*", {}]]);
    parseAndCheck(engine, "\\*\\*", [["**", {}]]);
    parseAndCheck(engine, "\\**", [["**", {}]]);
    parseAndCheck(engine, "\\_italic_", [["_italic_", {}]]);
    parseAndCheck(engine, "_foo\\_bar_", [["foo_bar", { italic: true }]]);
  });

  it("parses fill-in blanks with surrounding text and formatting", async () => {
    const doc = await createTestDocument();
    const engine = new RedlineEngine(doc);

    parseAndCheck(engine, "Name: __________", [["Name: __________", {}]]);
    parseAndCheck(engine, "Fill _name_: __________", [
      ["Fill ", {}],
      ["name", { italic: true }],
      [": __________", {}],
    ]);
    parseAndCheck(
      engine,
      "This is a line with __________ blank and **bold** and _italic_ and \\_escaped\\_ and foo_bar_baz",
      [
        ["This is a line with __________ blank and ", {}],
        ["bold", { bold: true }],
        [" and ", {}],
        ["italic", { italic: true }],
        [" and _escaped_ and foo_bar_baz", {}],
      ],
    );
  });

  it("preserves existing nested styles and sequential tags", async () => {
    const doc = await createTestDocument();
    const engine = new RedlineEngine(doc);

    parseAndCheck(engine, "A _B **C** B_ A", [
      ["A ", {}],
      ["B ", { italic: true }],
      ["C", { italic: true, bold: true }],
      [" B", { italic: true }],
      [" A", {}],
    ]);

    parseAndCheck(engine, "_Start **Bold** End_", [
      ["Start ", { italic: true }],
      ["Bold", { italic: true, bold: true }],
      [" End", { italic: true }],
    ]);

    parseAndCheck(engine, "**Bold**_Italic_", [
      ["Bold", { bold: true }],
      ["Italic", { italic: true }],
    ]);
  });

  it("tracked insert of underscore run generates w:ins and survives into document", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Name: [blank]");

    const engine = new RedlineEngine(doc);
    const edits: ModifyText[] = [
      {
        type: "modify",
        target_text: "Name: [blank]",
        new_text: "Name: __________",
      },
    ];

    engine.apply_edits(edits);

    const redlinedBuf = await doc.save();
    const criticText = await extractTextFromBuffer(redlinedBuf);
    expect(criticText).toContain("{++__________++}");

    const cleanText = _extractTextFromDoc(doc, true, false) as string;
    expect(cleanText).toContain("Name: __________");
  });

  it("tracked insert of escaped characters generates unescaped literal text", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "Original: placeholder");

    const engine = new RedlineEngine(doc);
    const edits: ModifyText[] = [
      {
        type: "modify",
        target_text: "Original: placeholder",
        new_text: "Original: foo\\_bar and \\*not bold\\*",
      },
    ];

    engine.apply_edits(edits);

    const redlinedBuf = await doc.save();
    const criticText = await extractTextFromBuffer(redlinedBuf);
    expect(criticText).toContain("{++foo_bar and *not bold*++}");

    const cleanText = _extractTextFromDoc(doc, true, false) as string;
    expect(cleanText).toContain("Original: foo_bar and *not bold*");
  });
});
