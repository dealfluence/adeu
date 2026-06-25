import { describe, it, expect } from "vitest";
import { RedlineEngine } from "./engine.js";
import { createTestDocument, addParagraph } from "./test-utils.js";

describe("QA Report V2: Engine Logic", () => {
  it("F6: Breadcrumb resolver does not dump paragraph bodies", async () => {
    const doc = await createTestDocument();
    
    // Setup a heading
    const p = addParagraph(doc, "2. Confidentiality");
    const docEl = p.ownerDocument!;
    const pPr = docEl.createElement("w:pPr");
    const pStyle = docEl.createElement("w:pStyle");
    pStyle.setAttribute("w:val", "Heading1");
    pPr.appendChild(pStyle);
    p.insertBefore(pPr, p.firstChild);

    // Add a massive paragraph that could erroneously get caught by a greedy regex
    addParagraph(doc, "This is a massive body paragraph ".repeat(20));
    addParagraph(doc, "Target phrase is here.");

    const engine = new RedlineEngine(doc);
    engine.mapper["_build_map"]();
    
    const text = engine.mapper.full_text;
    const start_idx = text.indexOf("Target phrase");
    
    const [path, page] = (engine as any)._get_heading_path_and_page(start_idx, text, [0, 1000]);
    
    expect(path).toContain("2. Confidentiality");
    expect(path).not.toContain("This is a massive body paragraph");
  });

  it("F9: Empty insertion CriticMarkup artifact {++++} is suppressed", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "the Board of Directors");
    const engine = new RedlineEngine(doc);

    // Mock an edit that acts as a strict deletion/shrink (new_text is empty)
    const edit = {
      type: "modify",
      target_text: " of Directors",
      new_text: "",
      _resolved_start_idx: "the Board".length,
      _active_mapper_ref: engine.mapper
    };
    
    engine.mapper["_build_map"]();
    
    const [critic_markup, clean_text] = (engine as any)._build_edit_context_previews(edit);
    
    expect(critic_markup).toContain("{-- of Directors--}");
    // The {++++} artifact shouldn't exist if new_text is empty
    expect(critic_markup).not.toContain("{++++}");
    expect(clean_text).not.toContain("{++++}");
  });

  it("R2 & R3: all-mode page list is ascending and breadcrumb anchors on first occurrence", async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;
    
    const p1 = addParagraph(doc, "Heading one");
    const pPr1 = xmlDoc.createElement("w:pPr");
    const pStyle1 = xmlDoc.createElement("w:pStyle");
    pStyle1.setAttribute("w:val", "Heading1");
    pPr1.appendChild(pStyle1);
    p1.insertBefore(pPr1, p1.firstChild);

    addParagraph(doc, "Occurrence one.");

    const p3 = addParagraph(doc, "Heading two");
    const pPr2 = xmlDoc.createElement("w:pPr");
    const pStyle2 = xmlDoc.createElement("w:pStyle");
    pStyle2.setAttribute("w:val", "Heading2");
    pPr2.appendChild(pStyle2);
    p3.insertBefore(pPr2, p3.firstChild);

    addParagraph(doc, "Occurrence two.");

    const engine = new RedlineEngine(doc);
    const edit = {
      type: "modify",
      target_text: "Occurrence",
      new_text: "Match",
      match_mode: "all"
    };
    const stats = engine.process_batch([edit], false);
    
    const report = stats.edits[0];
    expect(report.heading_path).toContain("Heading one");
    expect(report.heading_path).not.toContain("Heading two");

    if (report.pages && report.pages.length > 1) {
      expect(report.pages[0]).toBeLessThanOrEqual(report.pages[1]);
    }
  });

  it("§5.3.2 Transactional rollback for foreign author", async () => {
    const doc = await createTestDocument();
    const xmlDoc = doc.element.ownerDocument!;
    
    const p = addParagraph(doc, "Target ");
    const ins = xmlDoc.createElement("w:ins");
    ins.setAttribute("w:id", "1");
    ins.setAttribute("w:author", "Other User");
    const r = xmlDoc.createElement("w:r");
    const t = xmlDoc.createElement("w:t");
    t.textContent = "word";
    r.appendChild(t);
    ins.appendChild(r);
    p.appendChild(ins);

    const engine = new RedlineEngine(doc);
    const edit = {
      type: "modify",
      target_text: "Target word",
      new_text: "Replaced",
      match_mode: "all"
    };

    expect(() => engine.process_batch([edit], false)).toThrow(/targets an active insertion from another author/);
  });

  it("§5.3.3 Double-sided paragraph merge regex rejection", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "First part.");
    addParagraph(doc, "Second part.");

    const engine = new RedlineEngine(doc);
    const edit = {
      type: "modify",
      target_text: "part.\\n\\nSecond",
      new_text: "merged",
      regex: true
    };

    expect(() => engine.process_batch([edit], false)).toThrow(/spans a paragraph boundary with body text on both sides/);
  });

  it("S1: Transactional rollback blocks all-mode edit overlapping foreign COMMENT_ONLY edit", async () => {
    const doc = await createTestDocument();
    addParagraph(doc, "This is constituting the Board of Directors today.");

    const engineAlice = new RedlineEngine(doc, "Alice");
    engineAlice.process_batch([{
      type: "modify",
      target_text: "constituting the Board of Directors",
      new_text: "constituting the Board of Directors",
      comment: "Alice touches this clause"
    }]);

    const engineBob = new RedlineEngine(doc, "Bob");
    const editBob = {
      type: "modify",
      target_text: "the Board of Directors",
      new_text: "the Supervisory Board",
      match_mode: "all"
    };

    expect(() => engineBob.process_batch([editBob], false)).toThrow(/another author/i);
  });
});