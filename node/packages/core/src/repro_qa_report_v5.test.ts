// FILE: src/repro_qa_report_v5.test.ts
//
// Node-side regression tests for the 2026-07-17 exploratory QA report
// (adeu 1.21.0). The report was executed against the Python CLI; per the
// "Make Both Perfect" principle each engine-level finding is verified (and
// where the defect existed, fixed) here too:
//
//   F3  delete-comment no-op        — Node was already safe (TS lazy getter);
//                                     pinned here as a parity regression test
//   F4  core-property scrub gaps    — shared defect, fixed
//   F5  ReDoS via regex:true        — shared defect, fixed (vm time budget)
//   F6  duplicate-definition prune  — shared defect, fixed
//   F8  pinned edits skip shape
//       validation                  — Node was already safe; pinned as parity
//   F9  duplicate w:id resolution   — shared defect, fixed
//   F10 0-based [Edit:N]            — shared defect, fixed
//   F11 control characters         — shared defect, fixed (xmldom would have
//                                     silently serialized an invalid DOCX)

import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine, BatchValidationError, validate_edit_strings } from "./engine.js";
import { CommentsManager } from "./comments.js";
import { extract_all_domain_metadata } from "./domain.js";
import { apply_edits_to_markdown } from "./markup.js";
import { scrub_doc_properties, remove_all_comments } from "./sanitize/transforms.js";
import { RegexTimeoutError, userFindAllMatches } from "./utils/safe-regex.js";
import { serializeXml } from "./docx/dom.js";
import { DocumentObject } from "./docx/bridge.js";

function addTrackedInsertion(
  doc: DocumentObject,
  p: Element,
  text: string,
  wid: string,
  author: string,
): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const ins = xmlDoc.createElement("w:ins");
  ins.setAttribute("w:id", wid);
  ins.setAttribute("w:author", author);
  ins.setAttribute("w:date", "2026-01-02T10:00:00Z");
  const r = xmlDoc.createElement("w:r");
  const t = xmlDoc.createElement("w:t");
  t.setAttribute("xml:space", "preserve");
  t.textContent = text;
  r.appendChild(t);
  ins.appendChild(r);
  p.appendChild(ins);
  return ins;
}

// ---------------------------------------------------------------------------
// F3 — parity: deleteComment must work on a freshly constructed manager
// ---------------------------------------------------------------------------

describe("F3 parity: comment deletion on a fresh CommentsManager", () => {
  it("deletes the comment from the comments part (no backing-field no-op)", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "The purchase price shall be negotiated.");
    const cm = new CommentsManager(doc);
    const cid = cm.addComment("Mallory Insider", "Walk away below 9M.");

    const xmlDoc = doc.element.ownerDocument!;
    const rs = xmlDoc.createElement("w:commentRangeStart");
    rs.setAttribute("w:id", cid);
    p.insertBefore(rs, p.firstChild);
    const re = xmlDoc.createElement("w:commentRangeEnd");
    re.setAttribute("w:id", cid);
    p.appendChild(re);

    // A FRESH manager (the sanitize path) must actually delete — this is the
    // exact seam where the Python engine silently no-opped (QA F3).
    const fresh = new CommentsManager(doc);
    fresh.deleteComment(cid);

    const commentsPart = doc.pkg.parts.find((part) =>
      part.partname.includes("comments"),
    );
    const xml = commentsPart ? serializeXml(commentsPart._element) : "";
    expect(xml).not.toContain("Walk away below 9M.");
    expect(xml).not.toContain("Mallory Insider");
  });

  it("remove_all_comments leaves no comment text or author in the package", async () => {
    const doc = await createTestDocument();
    const p = addParagraph(doc, "The parties agree to negotiate in good faith.");
    const cm = new CommentsManager(doc);
    const cid = cm.addComment("Mallory Insider", "Board approved 12M ceiling.");
    const xmlDoc = doc.element.ownerDocument!;
    const rs = xmlDoc.createElement("w:commentRangeStart");
    rs.setAttribute("w:id", cid);
    p.insertBefore(rs, p.firstChild);

    const lines = remove_all_comments(doc);
    expect(lines.join("\n")).toContain("Comments removed: 1");

    for (const part of doc.pkg.parts) {
      if (!part.partname.includes("comments")) continue;
      const xml = serializeXml(part._element);
      expect(xml).not.toContain("Board approved 12M ceiling.");
      expect(xml).not.toContain("Mallory Insider");
    }
  });
});

// ---------------------------------------------------------------------------
// F4 — sanitize must scrub (or report) all leaky core properties
// ---------------------------------------------------------------------------

describe("F4: core property scrub covers title/category/keywords/subject/status", () => {
  async function docWithCoreProps(): Promise<DocumentObject> {
    const doc = await createTestDocument();
    addParagraph(doc, "Body text.");
    const corePart = doc.pkg.getPartByPath("docProps/core.xml");
    expect(corePart).toBeTruthy();
    const coreDoc = corePart!._element.ownerDocument!;
    const root = corePart!._element;
    const add = (tag: string, value: string) => {
      const el = coreDoc.createElement(tag);
      el.textContent = value;
      root.appendChild(el);
    };
    add("dc:title", "Secret Merger Agreement");
    add("cp:category", "Project Falcon");
    add("cp:keywords", "confidential,merger,project-falcon");
    add("dc:subject", "Acquisition of TargetCo");
    add("cp:contentStatus", "Draft - privileged");
    add("dc:description", "Internal: do not circulate");
    return doc;
  }

  it("strips category/keywords/subject/contentStatus/description and reports them", async () => {
    const doc = await docWithCoreProps();
    const lines = scrub_doc_properties(doc);
    const report = lines.join("\n");

    const coreXml = serializeXml(doc.pkg.getPartByPath("docProps/core.xml")!._element);
    expect(coreXml).not.toContain("Project Falcon");
    expect(coreXml).not.toContain("project-falcon");
    expect(coreXml).not.toContain("Acquisition of TargetCo");
    expect(coreXml).not.toContain("Draft - privileged");
    expect(coreXml).not.toContain("do not circulate");
    expect(report).toContain("Category");
    expect(report).toContain("Keywords");
  });

  it("reports the title as kept instead of silently ignoring it", async () => {
    const doc = await docWithCoreProps();
    const report = scrub_doc_properties(doc).join("\n");
    const coreXml = serializeXml(doc.pkg.getPartByPath("docProps/core.xml")!._element);
    expect(coreXml).toContain("Secret Merger Agreement"); // kept by design
    expect(report).toContain("Secret Merger Agreement"); // ...but visibly
  });
});

// ---------------------------------------------------------------------------
// F5 — LLM-controlled regex needs a wall-clock budget
// ---------------------------------------------------------------------------

describe("F5: user regex runs under a time budget", () => {
  const REDOS_PATTERN = "(a|a)*$";
  const HAYSTACK = "x" + "a".repeat(40) + "!";

  it(
    "userFindAllMatches interrupts catastrophic backtracking",
    () => {
      const t0 = Date.now();
      expect(() => userFindAllMatches(REDOS_PATTERN, HAYSTACK)).toThrow(RegexTimeoutError);
      expect(Date.now() - t0).toBeLessThan(10_000);
    },
    15_000,
  );

  it(
    "a ReDoS edit fails as a clean per-edit validation error",
    async () => {
      const doc = await createTestDocument();
      addParagraph(doc, HAYSTACK);
      const engine = new RedlineEngine(doc);

      let errors: string[] = [];
      try {
        engine.process_batch([
          { type: "modify", target_text: REDOS_PATTERN, new_text: "X", regex: true, match_mode: "first" } as any,
        ]);
      } catch (e) {
        expect(e).toBeInstanceOf(BatchValidationError);
        errors = (e as BatchValidationError).errors;
      }
      const joined = errors.join("\n");
      expect(joined).toContain("Edit 1 Failed");
      expect(joined.toLowerCase()).toContain("time");
    },
    15_000,
  );

  it("a valid user regex still matches normally", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "The fee is 12,500 euros.");
    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([
      { type: "modify", target_text: "\\d{2},\\d{3}", new_text: "13,000", regex: true, match_mode: "first" } as any,
    ]);
    expect(res.edits_applied).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// F6 — duplicate-definition diagnostics must survive the unused-term prune
// ---------------------------------------------------------------------------

describe("F6: duplicate-definition diagnostics survive the unused prune", () => {
  it("reports a duplicate that is never used", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, '"Gadget" means a mechanical device.');
    addParagraph(doc, '"Gadget" means an electronic device.');
    addParagraph(doc, "Nothing else references that term at all.");
    const base_text = [
      '"Gadget" means a mechanical device.',
      '"Gadget" means an electronic device.',
      "Nothing else references that term at all.",
    ].join("\n\n");

    const [, diagnostics] = extract_all_domain_metadata(doc, base_text);
    expect(
      diagnostics.some((d) => d.includes("Duplicate Definition") && d.includes("Gadget")),
    ).toBe(true);
  });

  it("still reports a duplicate that IS used (control)", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, '"Widget" means a mechanical device.');
    addParagraph(doc, '"Widget" means an electronic device.');
    addParagraph(doc, "The Widget shall be delivered on time.");
    const base_text = [
      '"Widget" means a mechanical device.',
      '"Widget" means an electronic device.',
      "The Widget shall be delivered on time.",
    ].join("\n\n");

    const [, diagnostics] = extract_all_domain_metadata(doc, base_text);
    expect(
      diagnostics.some((d) => d.includes("Duplicate Definition") && d.includes("Widget")),
    ).toBe(true);
  });

  it("surfaces an orphan definition as an Unused Definition warning", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, '"Orphan Term" means something noone mentions again.');
    addParagraph(doc, "The rest of the document is unrelated.");
    const base_text = [
      '"Orphan Term" means something noone mentions again.',
      "The rest of the document is unrelated.",
    ].join("\n\n");

    const [, diagnostics] = extract_all_domain_metadata(doc, base_text);
    expect(
      diagnostics.some((d) => d.includes("Unused Definition") && d.includes("Orphan Term")),
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// F8 — parity: pinned edits pass the same string-shape validation
// ---------------------------------------------------------------------------

describe("F8 parity: pinned edits are shape-validated", () => {
  it.each([
    ["{++New York++}", "CriticMarkup"],
    ["####### Deep heading", "Heading level 7"],
  ])("rejects a pinned edit whose new_text is %s", async (bad_new_text, expected) => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is a simple contract paragraph for testing.");
    const engine = new RedlineEngine(doc);

    const edit: any = {
      type: "modify",
      target_text: "simple",
      new_text: bad_new_text,
      _match_start_index: engine.mapper.full_text.indexOf("simple"),
    };

    expect(() => engine.process_batch([edit])).toThrow(BatchValidationError);
    try {
      engine.process_batch([edit]);
    } catch (e) {
      expect((e as BatchValidationError).errors.join("\n").toLowerCase()).toContain(
        expected.toLowerCase(),
      );
    }
  });
});

// ---------------------------------------------------------------------------
// F9 — duplicate w:id values must not let one action resolve unrelated changes
// ---------------------------------------------------------------------------

describe("F9: duplicate w:id accept/reject is refused across authors", () => {
  it("refuses to reject Chg:5 shared by Alice and Bob", async () => {
    const doc = await createTestDocument();
    const p1 = addParagraph(doc, "Clause A fee: ");
    addTrackedInsertion(doc, p1, "ALPHA", "5", "Alice");
    const p2 = addParagraph(doc, "Clause B fee: ");
    addTrackedInsertion(doc, p2, "BRAVO", "5", "Bob");

    const engine = new RedlineEngine(doc);
    let errors: string[] = [];
    try {
      engine.process_batch([{ type: "reject", target_id: "Chg:5" } as any]);
    } catch (e) {
      expect(e).toBeInstanceOf(BatchValidationError);
      errors = (e as BatchValidationError).errors;
    }
    const joined = errors.join("\n");
    expect(joined).toContain("Alice");
    expect(joined).toContain("Bob");

    // Neither revision may have been touched.
    const xml = serializeXml(doc.element);
    expect(xml).toContain("ALPHA");
    expect(xml).toContain("BRAVO");
  });

  it("same-author id reuse (the engine's own output) stays resolvable", async () => {
    const doc = await createTestDocument();
    const p1 = addParagraph(doc, "Fee clause: ");
    addTrackedInsertion(doc, p1, "FIRST", "7", "Adeu AI");
    // Second element of the SAME logical change: same id, same author.
    const p2 = addParagraph(doc, "Continued: ");
    addTrackedInsertion(doc, p2, "SECOND", "7", "Adeu AI");

    const engine = new RedlineEngine(doc);
    const res = engine.process_batch([{ type: "accept", target_id: "Chg:7" } as any]);
    expect(res.actions_applied).toBe(1);
    expect(res.actions_skipped).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// F10 — [Edit:N] indices must be 1-based to match apply's Edit N reports
// ---------------------------------------------------------------------------

describe("F10: markup [Edit:N] indices are 1-based", () => {
  it("emits [Edit:1] / [Edit:2] for a two-edit batch", () => {
    const result = apply_edits_to_markdown(
      "Alpha beta gamma.",
      [
        { type: "modify", target_text: "Alpha", new_text: "Omega" } as any,
        { type: "modify", target_text: "gamma", new_text: "delta" } as any,
      ],
      true,
    );
    expect(result).toContain("[Edit:1]");
    expect(result).toContain("[Edit:2]");
    expect(result).not.toContain("[Edit:0]");
  });
});

// ---------------------------------------------------------------------------
// F11 — control characters must fail as clean validation, not corrupt XML
// ---------------------------------------------------------------------------

describe("F11: XML-illegal control characters are rejected cleanly", () => {
  it("rejects new_text containing \\x01 with a per-edit error", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is a simple contract.");
    const engine = new RedlineEngine(doc);

    let errors: string[] = [];
    try {
      engine.process_batch([
        { type: "modify", target_text: "simple", new_text: "bad\x01value" } as any,
      ]);
    } catch (e) {
      expect(e).toBeInstanceOf(BatchValidationError);
      errors = (e as BatchValidationError).errors;
    }
    const joined = errors.join("\n");
    expect(joined).toContain("Edit 1 Failed");
    expect(joined.toLowerCase()).toContain("control character");
  });

  it("rejects a comment containing \\x00", () => {
    const errors = validate_edit_strings([
      { type: "modify", target_text: "a", new_text: "b", comment: "note\x00here" },
    ]);
    expect(errors.join("\n").toLowerCase()).toContain("control character");
  });

  it("without validation, xmldom would serialize the control char silently", async () => {
    // Documents WHY the check must live in validation on Node: unlike lxml,
    // @xmldom/xmldom writes \x01 into the XML without complaint, producing a
    // package Word cannot open — worse than Python's loud traceback.
    const doc = await createTestDocument();
    const p = addParagraph(doc, "bad\x01char");
    expect(serializeXml(p)).toContain("bad\x01char");
  });
});
