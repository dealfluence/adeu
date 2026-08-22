/**
 * A4 — `set_field` (CC-5).
 *
 * Twin of `python/tests/test_cc_set_field.py`. The XML assertions read the
 * SAVED package, not the in-memory tree: the whole point of a fill is what
 * Word opens, and the two have diverged before.
 */
import { describe, it, expect } from "vitest";
import {
  ccFixtureBytes,
  extractCcFixtureText,
  loadCcFixtureDocAndText,
  CC_SHAREPOINT_BOUND_BODY,
  CC_SHAREPOINT_PREFIX_MAPPINGS,
  CC_SHAREPOINT_STORE,
  CC_SHAREPOINT_STORE_AMBIGUOUS,
  CC_SHAREPOINT_STORE_DEFAULT_NS,
} from "./test-utils.js";
import { parsePrefixMappings } from "./utils/field-write.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { collectFields, resolveField, FieldResolutionError, type FieldEntry } from "./fields.js";
import { parseXml, findAllDescendants } from "./docx/dom.js";
import { unzipSync, strFromU8 } from "fflate";

const W14 = "http://schemas.microsoft.com/office/word/2010/wordml";

async function entriesOf(): Promise<FieldEntry[]> {
  const { doc, text } = await loadCcFixtureDocAndText();
  return collectFields(doc, text);
}

/** Run one `set_field` through the real batch pipeline; return saved bytes. */
async function fill(
  field: string,
  value: string,
  opts: { matchMode?: string; bodyXml?: string; customXml?: string } = {},
) {
  const buf = Buffer.from(ccFixtureBytes(undefined, opts.bodyXml, "1", opts.customXml));
  const doc = await DocumentObject.load(buf);
  const engine = new RedlineEngine(doc, "Test Author");
  const change: any = { type: "set_field", field, value };
  if (opts.matchMode) change.match_mode = opts.matchMode;
  const result = engine.process_batch([change]);
  const saved = await doc.save();
  return { saved, result };
}

/** The `w:sdt` element with the given CC ordinal, read from SAVED bytes. */
function savedSdt(saved: Buffer, ordinal: number): Element {
  const files = unzipSync(new Uint8Array(saved));
  const xml = strFromU8(files["word/document.xml"]);
  const dom = parseXml(xml);
  return findAllDescendants(dom.documentElement, "w:sdt")[ordinal - 1];
}

function textIn(el: Element, tag: string, inner = "w:t"): string {
  let out = "";
  for (const node of findAllDescendants(el, tag)) {
    for (const t of findAllDescendants(node, inner)) out += t.textContent || "";
  }
  return out;
}

/** Run a set_field expected to be rejected; return the combined message. */
async function expectRefusal(field: string, value: string, opts: any = {}): Promise<string> {
  let result: any;
  try {
    ({ result } = await fill(field, value, opts));
  } catch (e: any) {
    return String(e?.message ?? e);
  }
  expect(result.edits_applied).toBe(0);
  const details = (result.skipped_details || []).join(" ");
  const err = result.edits?.[0]?.error ?? "";
  return `${details} ${err}`;
}

describe("A4.2 — field resolution order and ambiguity", () => {
  it("resolves by CC ordinal", async () => {
    expect(resolveField(await entriesOf(), "CC:2").map((e) => e.ordinal)).toEqual([2]);
  });

  it("resolves by exact tag", async () => {
    const entries = await entriesOf();
    const tagged = entries.find((e) => e.tag)!;
    expect(resolveField(entries, tagged.tag!).map((e) => e.ordinal)).toContain(tagged.ordinal);
  });

  it("resolves by exact alias", async () => {
    const entries = await entriesOf();
    const tags = new Set(entries.filter((e) => e.tag).map((e) => e.tag));
    const aliased = entries.find((e) => e.alias && !tags.has(e.alias))!;
    expect(resolveField(entries, aliased.alias!).map((e) => e.ordinal)).toEqual([aliased.ordinal]);
  });

  it("lets the published id win over a tag that looks like one", async () => {
    // A document may legally tag a control `CC:2`. Otherwise the addressing
    // scheme this engine advertises could be shadowed by the document it is
    // addressing.
    const entries = await entriesOf();
    const decoy = { ...entries[entries.length - 1], tag: "CC:2" };
    expect(resolveField([...entries, decoy], "CC:2").map((e) => e.ordinal)).toEqual([2]);
  });

  it("prefers a tag over an alias of the same string", async () => {
    const entries = await entriesOf();
    const tagged = { ...entries[0], ordinal: 201, tag: "shared_name", alias: null };
    const aliased = { ...entries[0], ordinal: 202, tag: null, alias: "shared_name" };
    expect(resolveField([aliased, tagged], "shared_name").map((e) => e.ordinal)).toEqual([201]);
  });

  it("matches case-sensitively", async () => {
    const entries = await entriesOf();
    const tagged = entries.find((e) => e.tag && e.tag !== e.tag.toUpperCase())!;
    expect(() => resolveField(entries, tagged.tag!.toUpperCase())).toThrow(FieldResolutionError);
  });

  it("teaches the alternatives when the field does not exist", async () => {
    const entries = await entriesOf();
    try {
      resolveField(entries, "nonexistent");
      throw new Error("expected a refusal");
    } catch (e: any) {
      expect(e.message).toContain("nonexistent");
      expect(e.message).toContain("mode='fields'");
      expect(entries.some((x) => x.tag && e.message.includes(x.tag))).toBe(true);
    }
  });

  it("names the id when an ordinal does not exist", async () => {
    expect(() => resolveField([], "CC:9999")).toThrow(/CC:9999/);
  });

  it("gives a clean error for an empty field", async () => {
    // Clients drop primitive `required[]` entries, so this arrives empty.
    expect(() => resolveField([], "")).toThrow(/requires 'field'/);
  });

  describe("a tag shared by several controls", () => {
    const dupes = async () => {
      const entries = await entriesOf();
      return [
        ...entries,
        { ...entries[0], ordinal: 101, tag: "item_name" },
        { ...entries[0], ordinal: 102, tag: "item_name" },
      ];
    };

    it("strict rejects listing the candidates", async () => {
      try {
        resolveField(await dupes(), "item_name");
        throw new Error("expected a refusal");
      } catch (e: any) {
        expect(e.message).toContain("CC:101");
        expect(e.message).toContain("CC:102");
        expect(e.message).toContain("match_mode");
      }
    });

    it("first takes document order", async () => {
      expect(resolveField(await dupes(), "item_name", "first").map((e) => e.ordinal)).toEqual([101]);
    });

    it("all fans out", async () => {
      expect(resolveField(await dupes(), "item_name", "all").map((e) => e.ordinal)).toEqual([
        101, 102,
      ]);
    });
  });
});

describe("A4.1 — fill an empty text field by tag", () => {
  const filled = () => fill("client_name", "Acme Legal Services Ltd.");

  it("applies", async () => {
    const { result } = await filled();
    expect(result.edits_applied).toBe(1);
  });

  it("removes showingPlcHdr", async () => {
    const sdt = savedSdt((await filled()).saved, 2);
    expect(findAllDescendants(sdt, "w:showingPlcHdr").length).toBe(0);
  });

  it("leaves no run carrying the placeholder style", async () => {
    // CC-6(a): Word's own fill carries no rStyle PlaceholderText at all.
    const sdt = savedSdt((await filled()).saved, 2);
    const styles = findAllDescendants(sdt, "w:rStyle").map((s) => s.getAttribute("w:val"));
    expect(styles).not.toContain("PlaceholderText");
  });

  it("removes the ghost run WITHOUT a deletion", async () => {
    // CONFIRMED CC-6(a): filling an empty control makes ONE revision. A
    // w:del here would strike through prompt text the author never wrote.
    const sdt = savedSdt((await filled()).saved, 2);
    expect(findAllDescendants(sdt, "w:del").length).toBe(0);
  });

  it("lands the value inside a tracked insertion", async () => {
    const sdt = savedSdt((await filled()).saved, 2);
    expect(textIn(sdt, "w:ins")).toBe("Acme Legal Services Ltd.");
  });

  it("attributes the insertion to the acting author", async () => {
    const sdt = savedSdt((await filled()).saved, 2);
    const authors = new Set(
      findAllDescendants(sdt, "w:ins").map((i) => i.getAttribute("w:author")),
    );
    expect([...authors]).toEqual(["Test Author"]);
  });

  it("names the field in the report", async () => {
    const { result } = await filled();
    expect(result.edits[0].field).toBe('CC:2 "Client Name" (tag: client_name)');
  });

  it("shows the insertion inside the anchor pair in the raw view", async () => {
    const { saved } = await filled();
    expect(await extractCcFixtureText(saved, false)).toContain("{#cc:2}{++Acme Legal Services Ltd.++}");
  });

  it("shows the value as settled text in the clean view", async () => {
    const { saved } = await filled();
    expect(await extractCcFixtureText(saved, true)).toContain("{#cc:2}Acme Legal Services Ltd.{#/cc:2}");
  });
});

describe("A4.11 — classes that hold no single value", () => {
  // Writing to these is destructive: a group's "content" is other controls,
  // so replacing it with a string would delete every field inside it.
  it("refuses a group and names the class", async () => {
    const msg = await expectRefusal("std_terms", "anything");
    expect(msg).toContain("not a value-bearing field");
    expect(msg).toContain("group");
  });

  it("refuses a repeating section and names the class", async () => {
    const msg = await expectRefusal("deliverables", "x");
    expect(msg).toContain("not a value-bearing field");
    expect(msg).toContain("repeating");
  });

  it("points the group refusal at the nested fields", async () => {
    const msg = await expectRefusal("std_terms", "anything");
    expect(msg.toLowerCase()).toMatch(/nested|inside/);
  });
});

describe("A4.7 — a plain-text control cannot hold structure", () => {
  it("refuses paragraphs", async () => {
    const msg = await expectRefusal("counterparty", "Line1\n\nLine2");
    expect(msg.toLowerCase()).toContain("paragraph");
  });

  it("refuses a line break without multiLine", async () => {
    const msg = await expectRefusal("counterparty", "Line1\nLine2");
    expect(msg.toLowerCase()).toMatch(/line break|multiline/);
  });

  it("accepts paragraphs in a rich-text control", async () => {
    const { saved, result } = await fill("indemnity", "Line1\n\nLine2");
    expect(result.edits_applied).toBe(1);
    const inserted = textIn(savedSdt(saved, 1), "w:ins");
    expect(inserted).toContain("Line1");
    expect(inserted).toContain("Line2");
  });
});

describe("A4.6 — the checkbox toggle", () => {
  const unchecked = () => fill("confidential", "false");

  it("applies", async () => {
    expect((await unchecked()).result.edits_applied).toBe(1);
  });

  it("flips the state attribute", async () => {
    const sdt = savedSdt((await unchecked()).saved, 6);
    const checked = findAllDescendants(sdt, "w14:checked")[0];
    expect(checked.getAttribute("w14:val")).toBe("0");
  });

  it("takes no revision for the attribute flip", async () => {
    // URL_RETARGET precedent: one act, one redline.
    const sdt = savedSdt((await unchecked()).saved, 6);
    expect(findAllDescendants(sdt, "w:ins").length).toBe(1);
    expect(findAllDescendants(sdt, "w:del").length).toBe(1);
  });

  it("puts the insertion BEFORE the deletion", async () => {
    // CC-6(b): Word's order, and visible - the projection reads document
    // order, so the reverse renders the toggle backwards.
    const sdt = savedSdt((await unchecked()).saved, 6);
    const seen: string[] = [];
    const walk = (el: any) => {
      for (const child of Array.from(el.childNodes ?? []) as any[]) {
        if (child.nodeType !== 1) continue;
        if (child.tagName === "w:ins" || child.tagName === "w:del") seen.push(child.tagName);
        walk(child);
      }
    };
    walk(sdt);
    expect(seen).toEqual(["w:ins", "w:del"]);
  });

  it("swaps to the control's own unchecked character", async () => {
    const sdt = savedSdt((await unchecked()).saved, 6);
    expect(textIn(sdt, "w:ins")).toBe("\u2610");
  });

  it("refuses a value naming neither state", async () => {
    const msg = await expectRefusal("confidential", "maybe");
    expect(msg).toContain("checkbox");
    expect(msg).toContain("true/false");
  });

  for (const value of ["true", "x", "[x]", "1", "yes", "checked"]) {
    it(`treats ${value} as checked`, async () => {
      const { saved, result } = await fill("confidential", value);
      expect(result.edits_applied).toBe(1);
      const sdt = savedSdt(saved, 6);
      expect(findAllDescendants(sdt, "w14:checked")[0].getAttribute("w14:val")).toBe("1");
    });
  }

  const rawLine = async () => {
    const { saved } = await unchecked();
    const s = await extractCcFixtureText(saved, false);
    return s.split("\n").find((l: string) => l.includes("Confidentiality"))! as string;
  };

  it("shows the pending toggle in the raw view", async () => {
    // CC-19 restated this. It used to assert `{++ ++}` and `{--x--}`, which
    // the brackets sat OUTSIDE of - so one checkbox rendered as two bracket
    // pairs, `[{++ ++}][{--x--}]`, because the chrome is emitted per glyph run
    // and a toggle has two of them.
    const line = await rawLine();
    expect(line).toContain("{++[ ]++}");
    expect(line).toContain("{--[x]--}");
  });

  it("renders one box per state and no more", async () => {
    // Counted over the text BEFORE the annotation only: the bubble's own
    // `[Chg:1 insert]` labels are brackets too, and counting the whole line
    // measures the annotation rather than the checkbox.
    const boxes = (await rawLine()).split("{>>")[0];
    expect((boxes.match(/\[/g) || []).length).toBe(2);
    expect((boxes.match(/\]/g) || []).length).toBe(2);
  });

  it("keeps the change annotation outside the box", async () => {
    // The bubble used to open after `{--x--}`, run for two lines, and only
    // then let the closing `]` arrive - separating a box's bracket from its
    // content by an unrelated multi-line comment.
    const boxes = (await rawLine()).split("{>>")[0];
    expect(boxes.endsWith("{--[x]--}")).toBe(true);
    expect((boxes.match(/\[/g) || []).length).toBe((boxes.match(/\]/g) || []).length);
  });

  it("shows exactly one checkbox in the clean view", async () => {
    // The deleted half must not leave a second, permanently empty box.
    const { saved } = await unchecked();
    const s = await extractCcFixtureText(saved, true);
    const line = s.split("\n").find((l: string) => l.includes("Confidentiality"))!;
    expect((line.match(/\[/g) || []).length).toBe(1);
    expect((line.match(/\]/g) || []).length).toBe(1);
    expect(line.endsWith("[ ]")).toBe(true);
  });
});

describe("A4.3 / A4.4 — dropdown and combobox (G10)", () => {
  it("replaces the selection with a listed display text", async () => {
    const { saved, result } = await fill("governing_law", "British Columbia");
    expect(result.edits_applied).toBe(1);
    const sdt = savedSdt(saved, 4);
    expect(textIn(sdt, "w:ins")).toBe("British Columbia");
    expect(textIn(sdt, "w:del", "w:delText")).toBe("Ontario");
  });

  it("follows the selection with w:lastValue", async () => {
    const sdt = savedSdt((await fill("governing_law", "British Columbia")).saved, 4);
    expect(findAllDescendants(sdt, "w:dropDownList")[0].getAttribute("w:lastValue")).toBe(
      "British Columbia",
    );
  });

  it("resolves a machine value to its display text", async () => {
    // The document must read like every other row, not like a database.
    const { saved, result } = await fill("governing_law", "BC");
    expect(result.edits_applied).toBe(1);
    expect(textIn(savedSdt(saved, 4), "w:ins")).toBe("British Columbia");
  });

  it("refuses an unlisted option with the list", async () => {
    const msg = await expectRefusal("governing_law", "Manitoba");
    expect(msg).toContain("Ontario");
    expect(msg).toContain("British Columbia");
    expect(msg).toContain("Federal");
  });
});

describe("A4.5 — date handling (G12)", () => {
  it("writes a canonical date as a tracked change", async () => {
    const { saved, result } = await fill("effective_date", "2026-03-01");
    expect(result.edits_applied).toBe(1);
    expect(textIn(savedSdt(saved, 5), "w:ins")).toBe("2026-03-01");
  });

  it("syncs fullDate with no revision of its own", async () => {
    const sdt = savedSdt((await fill("effective_date", "2026-03-01")).saved, 5);
    expect(findAllDescendants(sdt, "w:date")[0].getAttribute("w:fullDate")).toBe(
      "2026-03-01T00:00:00Z",
    );
    expect(findAllDescendants(sdt, "w:ins").length).toBe(1);
  });

  it("refuses a non-canonical date naming the format", async () => {
    expect(await expectRefusal("effective_date", "01.03.2026")).toContain("YYYY-MM-DD");
  });

  it("refuses an impossible date", async () => {
    // A regex-only check would accept 2026-02-30 and write it.
    expect((await expectRefusal("effective_date", "2026-02-30")).toLowerCase()).toContain("date");
  });
});

describe("A4.8 — bound controls", () => {
  const STORE = "<root><matter>M-2026-001</matter></root>";

  it("updates the store silently when the binding resolves", async () => {
    // CC-6(e): the store WINS ON OPEN. A content-only write to a bound
    // control is destroyed the next time anyone opens the document.
    const { saved, result } = await fill("matter_number", "M-2026-002", { customXml: STORE });
    expect(result.edits_applied).toBe(1);
    const files = unzipSync(new Uint8Array(saved));
    const store = strFromU8(files["customXml/item1.xml"]);
    expect(store).toContain("M-2026-002");
    expect(store).not.toContain("M-2026-001");
  });

  it("still tracks the content change", async () => {
    const { saved } = await fill("matter_number", "M-2026-002", { customXml: STORE });
    expect(textIn(savedSdt(saved, 10), "w:ins")).toBe("M-2026-002");
  });

  it("discloses the store write in the report", async () => {
    const { result } = await fill("matter_number", "M-2026-002", { customXml: STORE });
    const note = result.edits[0].warning || "";
    expect(note).toContain("bound store");
    expect(note).toContain("/root[1]/matter[1]");
  });

  it("applies content-only with a warning when the binding dangles", async () => {
    // Dangling bindings exist in the wild - sanitize's scrub is one producer
    // - so refusing would be worse than disclosing.
    const { result } = await fill("matter_number", "M-2026-002");
    expect(result.edits_applied).toBe(1);
    const note = result.edits[0].warning || "";
    expect(note).toContain("WARNING");
    expect(note).toContain("could not be resolved");
  });

  it("says what will happen later in the dangling warning", async () => {
    const { result } = await fill("matter_number", "M-2026-002");
    expect((result.edits[0].warning || "").toLowerCase()).toContain("overwrite");
  });
});

const LOCKED_CHECKBOX_BODY =
  '<w:p><w:r><w:t xml:space="preserve">Locked box: </w:t></w:r>' +
  "<w:sdt><w:sdtPr>" +
  '<w:alias w:val="Locked Box"/><w:tag w:val="locked_box"/><w:id w:val="401"/>' +
  '<w:lock w:val="sdtContentLocked"/>' +
  '<w14:checkbox><w14:checked w14:val="1"/>' +
  '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>' +
  '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/></w14:checkbox>' +
  "</w:sdtPr><w:sdtContent>" +
  '<w:r><w:rPr><w:rFonts w:ascii="MS Gothic"/></w:rPr><w:t>\u2612</w:t></w:r>' +
  "</w:sdtContent></w:sdt></w:p>";

describe("CC-20 — bindings to the package core properties", () => {
  // Word exposes docProps/core.xml through the data store under a well-known
  // item id, so this is a LIVE binding even though no customXml item carries
  // that id. Measured on Word 16.0: IsMapped is true, the store still wins on
  // open, and Word dual-writes the part. Three corpus documents drive their
  // cover-page title fields this way.
  const CORE_STORE_ID = "{6C3C8BC8-F283-45AE-878A-BAB7291924A1}";
  const CORE_BODY =
    '<w:p><w:r><w:t xml:space="preserve">Title: </w:t></w:r>' +
    "<w:sdt><w:sdtPr>" +
    '<w:tag w:val="doc_title"/><w:id w:val="120"/>' +
    "<w:dataBinding w:prefixMappings=\"xmlns:ns0='http://purl.org/dc/elements/1.1/' " +
    "xmlns:ns1='http://schemas.openxmlformats.org/package/2006/metadata/core-properties'\" " +
    'w:xpath="/ns1:coreProperties[1]/ns0:title[1]" ' +
    `w:storeItemID="${CORE_STORE_ID}"/>` +
    "<w:text/></w:sdtPr><w:sdtContent>" +
    "<w:r><w:t>T-OLD</w:t></w:r>" +
    "</w:sdtContent></w:sdt></w:p>";

  it("resolves the store, writes it, and does not warn", async () => {
    const buf = Buffer.from(
      ccFixtureBytes(undefined, CORE_BODY, "1", undefined, { title: "T-OLD" }),
    );
    const doc = await DocumentObject.load(buf);
    const eng = new RedlineEngine(doc, "Test Author");
    const res = eng.process_batch([
      { type: "set_field", field: "doc_title", value: "T-NEW" } as any,
    ]);

    expect(res.edits_applied).toBe(1);
    expect(JSON.stringify(res)).not.toMatch(/could not be resolved/);

    // The bytes, not the return value. `writeBoundValue` returning true is
    // exactly what the python side reported while writing nothing (the part
    // served `.blob` from a re-serialized element, so the assignment went to an
    // attribute nothing read). Assert the store actually moved.
    const out = await doc.save();
    const core = strFromU8(unzipSync(new Uint8Array(out))["docProps/core.xml"]);
    expect(core).toContain("T-NEW");
    expect(core).not.toContain("T-OLD");
  });

  it("still reports a genuinely absent well-known part as dangling", async () => {
    // The reserved id with no such part in the package is dangling for real,
    // and must keep warning — the fix must not turn the id itself into proof.
    const buf = Buffer.from(ccFixtureBytes(undefined, CORE_BODY, "1"));
    const doc = await DocumentObject.load(buf);
    const eng = new RedlineEngine(doc, "Test Author");
    const res = eng.process_batch([
      { type: "set_field", field: "doc_title", value: "T-NEW" } as any,
    ]);
    expect(res.edits_applied).toBe(1);
    expect(JSON.stringify(res)).toMatch(/could not be resolved/);
  });
});

describe("CC-21 — a checkbox set_field is gated like every other write", () => {
  // A checkbox carries no `{#cc:N}` anchor pair (CC-1 dropped anchors for it:
  // 3,800+ per document), so `_cc_content_range` returns null. The validation
  // loop read that as "no content span" and skipped the control gates
  // entirely, which is a different claim. Locks, read-only protection, forms
  // protection and the untracked-write gate were ALL bypassed, silently.
  const toggle = async (opts: any, engineOpts: any = {}) => {
    const buf = Buffer.from(
      ccFixtureBytes(opts.protection, opts.bodyXml, "1", undefined),
    );
    const doc = await DocumentObject.load(buf);
    const eng = new RedlineEngine(doc, "Test Author", engineOpts);
    return eng.process_batch([
      { type: "set_field", field: opts.field, value: "false" } as any,
    ]);
  };

  it("refuses a toggle inside a content-locked checkbox", async () => {
    await expect(
      toggle({ field: "locked_box", bodyXml: LOCKED_CHECKBOX_BODY }),
    ).rejects.toThrow(/content-locked[\s\S]*ignore_control_locks/);
  });

  it("still honours the lock override", async () => {
    // Fail-closed must not become fail-shut: the override is the point.
    const res = await toggle(
      { field: "locked_box", bodyXml: LOCKED_CHECKBOX_BODY },
      { ignore_control_locks: true },
    );
    expect(res.edits_applied).toBe(1);
  });

  for (const [mode, override] of [
    ["readOnly", "ignore_document_protection"],
    ["comments", "ignore_document_protection"],
    ["forms", "allow_untracked_writes"],
  ] as const) {
    it(`refuses the toggle under ${mode} protection`, async () => {
      await expect(
        toggle({ field: "confidential", protection: mode }),
      ).rejects.toThrow(new RegExp(override));
    });

    it(`lets it through under ${mode} with ${override}`, async () => {
      const res = await toggle(
        { field: "confidential", protection: mode },
        { [override]: true },
      );
      expect(res.edits_applied).toBe(1);
    });
  }

  it("leaves an unprotected toggle alone", async () => {
    // G11 sees the probe's target/new pair, so handing it the raw value would
    // refuse every ordinary fill.
    const res = await toggle({ field: "confidential" });
    expect(res.edits_applied).toBe(1);
  });
});

describe("CC-18 — bindings shaped the way Word writes them", () => {
  // The A4.8 fixture binds `/root[1]/matter[1]`, which no Word deployment
  // produces. Node already resolved by local name and so was never broken, but
  // the shape was untested in both engines - which is exactly how python's
  // XPath version stayed green while being inert on every real document.
  const storeOf = (saved: Buffer | Uint8Array) =>
    strFromU8(unzipSync(new Uint8Array(saved))["customXml/item1.xml"]);

  it("reaches the store through a SharePoint binding", async () => {
    const { saved, result } = await fill("case_num", "2:25-cv-09999", {
      bodyXml: CC_SHAREPOINT_BOUND_BODY,
      customXml: CC_SHAREPOINT_STORE,
    });
    expect(result.edits_applied).toBe(1);
    expect(storeOf(saved)).toContain("2:25-cv-09999");
    expect(storeOf(saved)).not.toContain("2:24-cv-01234");
  });

  it("reaches it when the store carries a default namespace", async () => {
    // The shape XPath 1.0 cannot express at all: an unprefixed step means "no
    // namespace" to it, so `documentManagement[1]` misses an element in a
    // default namespace even WITH the prefix mappings supplied.
    const { saved, result } = await fill("case_num", "2:25-cv-09999", {
      bodyXml: CC_SHAREPOINT_BOUND_BODY,
      customXml: CC_SHAREPOINT_STORE_DEFAULT_NS,
    });
    expect(result.edits_applied).toBe(1);
    expect(storeOf(saved)).toContain("2:25-cv-09999");
  });

  it("discloses the store write rather than taking the dangling path", async () => {
    const { result } = await fill("case_num", "2:25-cv-09999", {
      bodyXml: CC_SHAREPOINT_BOUND_BODY,
      customXml: CC_SHAREPOINT_STORE,
    });
    const note = result.edits[0].warning || "";
    expect(note).toContain("bound store");
    expect(note).not.toContain("could not be resolved");
  });

  it("still warns when the store is genuinely absent", async () => {
    // `wawd_esi_agreement` carries three of these bindings and NO customXml
    // part, so the dangling path must stay reachable.
    const { result } = await fill("case_num", "2:25-cv-09999", {
      bodyXml: CC_SHAREPOINT_BOUND_BODY,
    });
    expect(result.edits_applied).toBe(1);
    expect(result.edits[0].warning || "").toContain("could not be resolved");
  });

  it("uses the prefix mapping to pick between same-named columns", async () => {
    const { saved } = await fill("case_num", "2:25-cv-09999", {
      bodyXml: CC_SHAREPOINT_BOUND_BODY,
      customXml: CC_SHAREPOINT_STORE_AMBIGUOUS,
    });
    const store = storeOf(saved);
    expect(store).toContain("2:25-cv-09999");
    // Writing the neighbour would be a silent corruption of unrelated data.
    expect(store).toContain("DO-NOT-TOUCH");
  });

  it("parses prefix mappings, including a bare-GUID namespace", () => {
    const got = parsePrefixMappings(CC_SHAREPOINT_PREFIX_MAPPINGS);
    expect(got.ns0).toBe("http://schemas.microsoft.com/office/2006/metadata/properties");
    expect(got.ns2).toBe("2f9f1944-3a9b-49e1-93d3-d1cb06258e09");
  });
});

const TEMPORARY_BODY =
  '<w:p><w:r><w:t xml:space="preserve">Prepared by </w:t></w:r>' +
  "<w:sdt><w:sdtPr>" +
  '<w:alias w:val="Preparer"/><w:tag w:val="preparer"/><w:id w:val="900"/>' +
  "<w:temporary/><w:showingPlcHdr/><w:text/>" +
  "</w:sdtPr><w:sdtContent>" +
  '<w:r><w:rPr><w:rStyle w:val="PlaceholderText"/></w:rPr>' +
  "<w:t>Click here to enter a name.</w:t></w:r>" +
  "</w:sdtContent></w:sdt>" +
  '<w:r><w:t xml:space="preserve">.</w:t></w:r></w:p>';

describe("A4.9 — a temporary control unwraps on fill", () => {
  const filled = () => fill("preparer", "Jane Roe", { bodyXml: TEMPORARY_BODY });

  it("removes the sdt wrapper from the saved XML", async () => {
    const { saved, result } = await filled();
    expect(result.edits_applied).toBe(1);
    const files = unzipSync(new Uint8Array(saved));
    const dom = parseXml(strFromU8(files["word/document.xml"]));
    expect(findAllDescendants(dom.documentElement, "w:sdt").length).toBe(0);
  });

  it("leaves the inserted text as a tracked insertion", async () => {
    // The revision outlives the wrapper (CC-6(c)), so the value is still
    // reviewable even though the control is gone.
    const { saved } = await filled();
    const files = unzipSync(new Uint8Array(saved));
    const dom = parseXml(strFromU8(files["word/document.xml"]));
    expect(textIn(dom.documentElement, "w:ins")).toBe("Jane Roe");
  });

  it("drops it from the ledger", async () => {
    const { saved } = await filled();
    const doc = await DocumentObject.load(saved);
    const s = await extractCcFixtureText(saved, false);
    expect(collectFields(doc, s)).toEqual([]);
  });

  it("discloses the unwrap", async () => {
    // The control vanishing is a surprise unless the report says so.
    const note = (await filled()).result.edits[0].warning || "";
    expect(note).toContain("temporary");
    expect(note).toContain("unwrapped");
  });
});

describe("A4.12 — set_field respects the gates", () => {
  // True by construction rather than by a second implementation remembering
  // to check: a fill desugars into ordinary pinned ModifyText sub-edits, so
  // the gates see a normal edit. These tests exist to keep it that way - the
  // cheapest way to break it would be a future "fast path" for fills.
  it("refuses a fill into a locked control", async () => {
    expect((await expectRefusal("fixed_clause", "Net 90")).toLowerCase()).toContain("lock");
  });

  it("names the control in the refusal", async () => {
    const msg = await expectRefusal("fixed_clause", "Net 90");
    expect(msg).toMatch(/CC:7|Payment Terms/);
  });

  it("lets the ordinary override through", async () => {
    const buf = Buffer.from(ccFixtureBytes());
    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "Test Author", {
      ignore_control_locks: true,
    } as any);
    const result = engine.process_batch([
      { type: "set_field", field: "fixed_clause", value: "Net 90" } as any,
    ]);
    expect(result.edits_applied).toBe(1);
    expect(textIn(savedSdt(await doc.save(), 7), "w:ins")).toContain("Net 90");
  });

  it("refuses an untracked fill under forms protection", async () => {
    // spec-set-field §2 says forms protection is "exactly what stays allowed"
    // for set_field. CC-6 measured otherwise and CC-4 encoded the
    // measurement: Word records fills in a forms-protected document as
    // UNTRACKED, so applying one would break Adeu's guarantee that every
    // write is tracked. G5 permits the fill; the untracked-write gate refuses
    // it. The measurement wins over the frozen sentence.
    const buf = Buffer.from(ccFixtureBytes("forms"));
    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "Test Author");
    let msg = "";
    try {
      const r = engine.process_batch([
        { type: "set_field", field: "client_name", value: "Acme Ltd." } as any,
      ]);
      msg = (r.skipped_details || []).join(" ");
      expect(r.edits_applied).toBe(0);
    } catch (e: any) {
      msg = String(e?.message ?? e);
    }
    expect(msg.toLowerCase()).toContain("untracked");
    expect(msg).toContain("allow_untracked_writes");
  });

  it("lets the untracked-write override through", async () => {
    const buf = Buffer.from(ccFixtureBytes("forms"));
    const doc = await DocumentObject.load(buf);
    const engine = new RedlineEngine(doc, "Test Author", {
      allow_untracked_writes: true,
    } as any);
    const result = engine.process_batch([
      { type: "set_field", field: "client_name", value: "Acme Ltd." } as any,
    ]);
    expect(result.edits_applied).toBe(1);
  });
});
