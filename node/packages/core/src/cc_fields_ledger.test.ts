/**
 * A2 — the fields ledger, at engine level (CC-2).
 *
 * Twin of `python/tests/test_cc_fields_ledger.py`. The line format is an
 * output contract (spec-fields-ledger.md §7 tells callers to parse it), so
 * both engines are compared against the same frozen GOLDEN-LEDGER character
 * for character rather than asserting "contains".
 */
import { describe, it, expect } from "vitest";
import { ccFixtureBytes, ccGolden } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { extractTextFromBuffer } from "./ingest.js";
import {
  collectFields,
  readDocumentProtection,
  renderBanner,
  renderLedger,
  summaryCounts,
  protectionLabel,
} from "./fields.js";

const PLAIN_BODY = "<w:p><w:r><w:t>Plain paragraph.</w:t></w:r></w:p>";

async function load(protection?: any, bodyXml?: string) {
  const buf = Buffer.from(ccFixtureBytes(protection, bodyXml));
  const doc = await DocumentObject.load(buf);
  const text = await extractTextFromBuffer(buf, false, false);
  return { doc, text };
}

async function ledger(
  opts: {
    protection?: any;
    bodyXml?: string;
    offset?: number;
    name?: string;
  } = {},
): Promise<string> {
  const { doc, text } = await load(opts.protection, opts.bodyXml);
  const entries = collectFields(doc, text, null);
  return renderLedger(
    opts.name ?? "cc_fixture.docx",
    entries,
    readDocumentProtection(doc),
    opts.offset ?? 0,
  );
}

const lineFor = (out: string, n: number) =>
  out.split("\n").find((l) => l.startsWith(`CC:${n} `))!;

function manyControls(n: number): string {
  let out = "";
  for (let i = 1; i <= n; i++) {
    out +=
      `<w:p><w:sdt><w:sdtPr><w:tag w:val="f${i}"/><w:text/></w:sdtPr>` +
      `<w:sdtContent><w:r><w:t>V${i}</w:t></w:r></w:sdtContent></w:sdt></w:p>`;
  }
  return out;
}

describe("A2.1 — ledger golden", () => {
  it("renders GOLDEN-LEDGER exactly", async () => {
    expect(await ledger()).toBe(ccGolden("GOLDEN-LEDGER"));
  });

  it("renders GOLDEN-BANNER exactly", async () => {
    const { doc, text } = await load();
    const entries = collectFields(doc, text, null);
    expect(renderBanner(entries, readDocumentProtection(doc))).toBe(
      ccGolden("GOLDEN-BANNER"),
    );
  });

  it("counts what the document contains", async () => {
    const { doc, text } = await load();
    // 16 controls, CC:2 empty, CC:7 content-locked + CC:8 group, CC:10 bound.
    expect(summaryCounts(collectFields(doc, text, null))).toEqual([16, 1, 2, 1]);
  });
});

describe("A2.2 — protection line on a zero-control document", () => {
  it("reports protection with no controls", async () => {
    expect(
      await ledger({ protection: "forms", bodyXml: PLAIN_BODY, name: "juries.docx" }),
    ).toBe(
      "# Fields: juries.docx\n" +
        "Protection: fill-in-forms only (enforced) \u00b7 no content controls\n" +
        "\n" +
        "No content controls.",
    );
  });

  it("still renders a header when unprotected", async () => {
    const out = await ledger({ bodyXml: PLAIN_BODY, name: "plain.docx" });
    expect(out.split("\n")[1]).toBe("Protection: none \u00b7 no content controls");
  });

  it("emits NO banner for a plain document", async () => {
    // spec-projection §7: zero controls AND no protection => no banner at all.
    // A plain document must gain zero noise from this feature.
    const { doc, text } = await load(undefined, PLAIN_BODY);
    const entries = collectFields(doc, text, null);
    expect(renderBanner(entries, readDocumentProtection(doc))).toBeNull();
  });

  it("emits a banner for protection alone", async () => {
    const { doc, text } = await load("forms", PLAIN_BODY);
    const entries = collectFields(doc, text, null);
    expect(renderBanner(entries, readDocumentProtection(doc))).toBe(
      "> **Protection:** fill-in-forms only (enforced) \u00b7 " +
        "**Fields:** no content controls",
    );
  });

  it.each([
    ["readOnly", "read-only"],
    ["forms", "fill-in-forms only"],
    ["comments", "comments only"],
    ["trackedChanges", "tracked-changes only"],
  ])("gives %s the word %s", async (edit, word) => {
    const { doc } = await load(edit as any, PLAIN_BODY);
    const prot = readDocumentProtection(doc);
    expect(prot.mode).toBe(word);
    expect(protectionLabel(prot)).toBe(`${word} (enforced)`);
  });
});

describe("A2.5 — anonymous controls render without fabricated names", () => {
  it("renders a bare line for a control with neither alias nor tag", async () => {
    expect(lineFor(await ledger(), 12)).toBe(
      "CC:12  item \u2014 p1 \u2014 in CC:11 \u2014 wraps 1 block",
    );
  });

  it("never emits empty quotes or an empty tag", async () => {
    const out = await ledger();
    expect(out).not.toContain('""');
    expect(out).not.toContain("(tag: )");
  });
});

describe("line format", () => {
  it("pads the ordinal column to the widest", async () => {
    const lines = (await ledger()).split("\n").filter((l) => l.startsWith("CC:"));
    const columns = new Set(lines.map((l) => l.indexOf(l.split(/\s+/)[1])));
    expect(columns).toEqual(new Set([7]));
  });

  it("puts states before the value", async () => {
    const line = lineFor(await ledger(), 7);
    expect(line.indexOf("LOCKED (contents)")).toBeLessThan(line.indexOf("value:"));
  });

  it("gives a group its extent, not a value", async () => {
    const line = lineFor(await ledger(), 8);
    expect(line.endsWith("wraps 2 blocks, 1 nested field")).toBe(true);
    expect(line).not.toContain("value:");
  });

  it("gives a checkbox its state, not a value", async () => {
    const line = lineFor(await ledger(), 6);
    expect(line.endsWith("checked")).toBe(true);
    expect(line).not.toContain("value:");
  });

  it("shows a binding's xpath", async () => {
    expect(lineFor(await ledger(), 10)).toContain("BOUND \u2192 /root[1]/matter[1]");
  });

  it("labels row- and cell-level controls", async () => {
    const out = await ledger();
    expect(lineFor(out, 14)).toContain("table cell");
    expect(lineFor(out, 15)).toContain("table row");
  });

  it("reads a row-level value from the projection, not from w:t", async () => {
    // Proof of the design choice: the value is the flattened markdown row,
    // cell separator included.
    expect(lineFor(await ledger(), 15)).toContain('value: "Approver | Jane Roe"');
  });

  it("shows a placeholder instead of a value when empty", async () => {
    const line = lineFor(await ledger(), 2);
    expect(line).toContain("EMPTY");
    expect(line).toContain('placeholder: "Click or tap here to enter text."');
    expect(line).not.toContain("value:");
  });
});

describe("preview caps", () => {
  it("truncates a value at 80 characters", async () => {
    const body =
      '<w:p><w:sdt><w:sdtPr><w:tag w:val="long"/><w:text/></w:sdtPr>' +
      `<w:sdtContent><w:r><w:t>${"A".repeat(200)}</w:t></w:r>` +
      "</w:sdtContent></w:sdt></w:p>";
    expect(lineFor(await ledger({ bodyXml: body }), 1)).toContain(
      `value: "${"A".repeat(80)}\u2026"`,
    );
  });

  it("caps options at eight with an overflow marker", async () => {
    let items = "";
    for (let i = 1; i <= 11; i++)
      items += `<w:listItem w:displayText="Opt${i}" w:value="${i}"/>`;
    const body =
      '<w:p><w:sdt><w:sdtPr><w:tag w:val="dd"/><w:dropDownList>' +
      items +
      "</w:dropDownList></w:sdtPr>" +
      "<w:sdtContent><w:r><w:t>Opt1</w:t></w:r></w:sdtContent></w:sdt></w:p>";
    expect(lineFor(await ledger({ bodyXml: body }), 1)).toContain(
      "options: Opt1 | Opt2 | Opt3 | Opt4 | Opt5 | Opt6 | Opt7 | Opt8 | \u2026 (+3 more)",
    );
  });
});

describe("w:temporary", () => {
  it("renders a TEMPORARY state token", async () => {
    const body =
      '<w:p><w:sdt><w:sdtPr><w:tag w:val="tmp"/><w:temporary/><w:text/>' +
      "</w:sdtPr><w:sdtContent><w:r><w:t>Draft</w:t></w:r>" +
      "</w:sdtContent></w:sdt></w:p>";
    expect(lineFor(await ledger({ bodyXml: body }), 1)).toContain("TEMPORARY");
  });

  it("treats w:val=0 as off", async () => {
    const body =
      '<w:p><w:sdt><w:sdtPr><w:tag w:val="tmp"/><w:temporary w:val="0"/>' +
      "<w:text/></w:sdtPr><w:sdtContent><w:r><w:t>Draft</w:t></w:r>" +
      "</w:sdtContent></w:sdt></w:p>";
    expect(lineFor(await ledger({ bodyXml: body }), 1)).not.toContain("TEMPORARY");
  });
});

describe("pagination (spec §4)", () => {
  it("caps the first page and points forward", async () => {
    const out = (await ledger({ bodyXml: manyControls(250) })).split("\n");
    const cc = out.filter((l) => l.startsWith("CC:"));
    expect(cc.length).toBe(100);
    expect(cc[0].startsWith("CC:1 ")).toBe(true);
    expect(cc[99].startsWith("CC:100 ")).toBe(true);
    expect(out[out.length - 1]).toBe(
      "\u2026 150 more \u2014 pass fields_offset=100 to continue.",
    );
  });

  it("renders the middle page", async () => {
    const out = (await ledger({ bodyXml: manyControls(250), offset: 100 })).split("\n");
    const cc = out.filter((l) => l.startsWith("CC:"));
    expect(cc[0].startsWith("CC:101 ")).toBe(true);
    expect(cc[99].startsWith("CC:200 ")).toBe(true);
    expect(out[out.length - 1]).toBe(
      "\u2026 50 more \u2014 pass fields_offset=200 to continue.",
    );
  });

  it("ends the last page without a continuation", async () => {
    const out = (await ledger({ bodyXml: manyControls(250), offset: 200 })).split("\n");
    const cc = out.filter((l) => l.startsWith("CC:"));
    expect(cc.length).toBe(50);
    expect(cc[49].startsWith("CC:250 ")).toBe(true);
    expect(out[out.length - 1]).not.toContain("more \u2014 pass fields_offset");
  });

  it("keeps header counts document-wide, not page-wide", async () => {
    // A paginated ledger still describes the whole document; reporting 100
    // would make the count depend on where the reader happened to be.
    const out = await ledger({ bodyXml: manyControls(250), offset: 100 });
    expect(out.split("\n")[1]).toContain("250 content controls");
  });
});
