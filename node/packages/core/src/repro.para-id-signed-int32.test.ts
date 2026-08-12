// FILE: node/packages/core/src/repro.para-id-signed-int32.test.ts
import { describe, it, expect, afterEach } from "vitest";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
  findOutOfRangeLongHexNumbers,
  outOfRangeIdReport,
} from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { CommentsManager, extract_comments_data } from "./comments.js";
import { RedlineEngine } from "./engine.js";
import { resolve_cell_anchor } from "./docx/cell-anchor.js";
import {
  ST_LONG_HEX_NUMBER_MAX,
  ST_LONG_HEX_NUMBER_MIN,
  isWordReadableLongHexNumber,
  toLongHexNumber,
} from "./docx/long-hex-number.js";
import { _extractTextFromDoc } from "./ingest.js";

/**
 * Node mirror of BUG_paraId_signed_int32_thread_collapse.md (B5, 2026-08-12,
 * Adeu 2.2.0 / 0db3cc2). Word-verified against Word 16.0 through COM; the
 * oracle and the measurements live in
 * python/tests/test_live_word_para_id_signed_int32.py, because Word runs there.
 *
 * `_generateHexId()` drew from the full 32-bit range and fed BOTH `w14:paraId`
 * and `w:rsid*`. Word parses every `ST_LongHexNumber` as a SIGNED 32-bit
 * integer, and ECMA-376 requires the value to be greater than `0x00000000` and
 * less than `0x80000000`; out-of-range values are silently discarded and
 * regenerated on load, dangling every `w15:paraIdParent` that pointed at them.
 * Roughly half of every id Adeu minted was invalid.
 *
 * Node had a SECOND, worse instance the report did not know about:
 * `docx/cell-anchor.ts` derives `{#cell:<paraId>}` fallback anchors with FNV-1a
 * over the full UNSIGNED range and STAMPS them into `word/document.xml`. That
 * one is deterministic, not a coin flip — 139 of the first 200 paragraph
 * indices produce a high-bit id, and indices 0-7 are all invalid, i.e. exactly
 * the first tables in a document. Word discards them on load, so the anchors
 * agents are handed do not survive a round-trip. Worse, a single out-of-range
 * paraId makes Word renumber EVERY paraId in the part (Word-verified: 32/32
 * preserved with no bad id, 0/32 with one), so one bad anchor invalidates all
 * of them.
 *
 * Test-first: every assertion here fails on the pre-fix engine.
 */

/**
 * ECMA-376: the value "shall be greater than 0x00000000 and less than
 * 0x80000000". Restated as LITERALS on purpose — the engine and the package
 * scanner both derive their bounds from docx/long-hex-number.ts, so a wrong
 * constant there would agree with itself and pass. "the rule itself" below is
 * what stops that.
 */
const LEGAL_MIN = 0x00000001;
const LEGAL_MAX = 0x7fffffff;

/** 256 samples: a full-range generator fails this with probability 1 - 2^-256. */
const SAMPLES = 256;

const CT = {
  COMMENTS:
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
  EXTENDED:
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
};

/** The predicate the ENGINE mints against, so the guard cannot drift away from
 * the generator. That it matches the spec is pinned separately, from literals. */
const isLegal = isWordReadableLongHexNumber;

function expectWordReadableIds(pkg: Buffer, context: string): void {
  const offenders = findOutOfRangeLongHexNumbers(pkg);
  expect(offenders, outOfRangeIdReport(offenders, context)).toEqual([]);
}

const REAL_MATH_RANDOM = Math.random;
afterEach(() => {
  Math.random = REAL_MATH_RANDOM;
});

/**
 * Deterministic stand-in for `Math.random` that always sits at one END of
 * [0, 1), stepping inward per call so successive ids stay distinct. Every
 * value it returns is one `Math.random` could have returned, so this pins the
 * GENERATOR'S RANGE rather than sampling its luck: an unmasked generator then
 * mints `FFFFFF..`/`000000..` and a masked one `7FFFFF..`/`000001..`, every
 * time instead of half the time.
 */
function pinMathRandom(edge: "high" | "low", step = 1e-9): void {
  let n = 0;
  Math.random = () => {
    const offset = n++ * step;
    return edge === "high" ? Math.max(0, 1 - Number.EPSILON - offset) : offset;
  };
}

async function threadedPackage(replyCount = 2): Promise<Buffer> {
  const doc = await createTestDocument();
  addParagraph(
    doc,
    "The parties shall confer in good faith before moving to compel production.",
  );
  const engine = new RedlineEngine(doc, "Sarah Chen");
  engine.apply_edits([
    {
      type: "modify",
      target_text: "confer in good faith",
      new_text: "confer in good faith",
      comment: "Root note.",
    } as any,
  ]);
  const rootId = Object.keys(extract_comments_data(doc.pkg))[0];
  (engine as any).author = "Adeu AI (TS)";
  for (let i = 0; i < replyCount; i++) {
    engine.apply_review_actions([
      { type: "reply", target_id: `Com:${rootId}`, text: `Reply ${i}.` } as any,
    ]);
  }
  return await doc.save();
}

// ---------------------------------------------------------------------------
// The rule itself
// ---------------------------------------------------------------------------

describe("B5: the ST_LongHexNumber rule", () => {
  // docx/long-hex-number.ts is the single definition of "an id Word will
  // keep": the generators mint against it and the scanner audits against it.
  // That is the right structure and it has one failure mode — a wrong constant
  // would be consistent with itself everywhere. Everything here checks it
  // against literals from the spec text, not against itself.

  it("uses the bounds ECMA-376 states", () => {
    expect([ST_LONG_HEX_NUMBER_MIN, ST_LONG_HEX_NUMBER_MAX]).toEqual([
      LEGAL_MIN,
      LEGAL_MAX,
    ]);
  });

  it.each([
    ["00000000", false, "forbidden: Word rejects the whole package"],
    ["00000001", true, "smallest legal value"],
    ["12345678", true, "ordinary value"],
    ["7FFFFFFF", true, "largest legal value"],
    ["80000000", false, "smallest illegal value — exactly one greater"],
    ["D2AEAE20", false, "the id from the field report that collapsed the thread"],
    ["FFFFFFFF", false, "all bits set"],
    ["", false, "not a value at all"],
    ["1234567", true, "short forms are legal; Word writes them padded"],
    ["123456789", false, "too long for xsd:hexBinary length 4"],
    ["ZZZZZZZZ", false, "not hexadecimal"],
  ])("classifies %s as readable=%s (%s)", (value, readable, why) => {
    expect(isWordReadableLongHexNumber(value as string), why as string).toBe(
      readable,
    );
  });

  it.each([
    [0x00000000, "00000001"], // 0 is forbidden, so it cannot be the identity
    [0x00000001, "00000001"],
    [0x7fffffff, "7FFFFFFF"],
    [0x80000000, "00000001"], // masks to 0, which is forbidden -> MIN
    [0x80000001, "00000001"],
    [0xffffffff, "7FFFFFFF"],
    [0xfc855cfc, "7C855CFC"], // the first id the cell anchor used to derive
  ])("folds %s into the legal range", (value, folded) => {
    expect(toLongHexNumber(value as number)).toBe(folded);
    expect(isWordReadableLongHexNumber(toLongHexNumber(value as number))).toBe(
      true,
    );
  });

  it("folds every 32-bit value into the legal range", () => {
    // The derived-id path has no second chance: whatever the hash produces has
    // to be usable, including the two inputs that map to the forbidden zero.
    for (let i = 0; i < 4096; i++) {
      const value = (Math.imul(i, 2654435761) >>> 0) ^ (i << 3);
      const folded = toLongHexNumber(value);
      expect(folded, `folding ${value} produced ${folded}`).toMatch(
        /^[0-9A-F]{8}$/,
      );
      expect(isWordReadableLongHexNumber(folded), `folding ${value}`).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// The generators
// ---------------------------------------------------------------------------

describe("B5: every ST_LongHexNumber Adeu mints is a positive signed int32", () => {
  const generators = ["_generateHexId", "_generateDurableId"];

  it.each(generators)("%s never leaves the legal range", async (name) => {
    const mgr = (await createTestDocument().then(
      (d) => new CommentsManager(d),
    )) as any;
    if (!mgr[name]) return; // collapsed into the shared generator — nothing to check
    const bad: string[] = [];
    for (let i = 0; i < SAMPLES; i++) {
      const value = mgr[name]();
      expect(value).toMatch(/^[0-9A-F]{8}$/);
      if (!isLegal(value)) bad.push(value);
    }
    expect(
      bad.length,
      `${bad.length}/${SAMPLES} values from ${name} are outside (0x00000000, 0x80000000) ` +
        `(e.g. ${bad.slice(0, 4).join(", ")}). Word discards them on load.`,
    ).toBe(0);
  });

  it.each(generators)("%s can never return 00000000", async (name) => {
    // Forbidden as explicitly as the high half, and the one value Word does
    // NOT paper over: a comment paragraph with w14:paraId="00000000" makes
    // Word declare the file corrupted and refuse to open it (Word-verified).
    pinMathRandom("low", 0); // the smallest value this generator can emit
    const mgr = (await createTestDocument().then(
      (d) => new CommentsManager(d),
    )) as any;
    if (!mgr[name]) return;
    expect(
      mgr[name](),
      `${name} can mint 00000000; Word rejects the entire package`,
    ).not.toBe("00000000");
  });

  it.each([
    ["low", "00000001"],
    ["high", "7FFFFFFF"],
  ] as const)("every generator shares its %s bound", async (edge, expected) => {
    // The class-level guard. B3 was fixed by giving ONE attribute its own
    // masked generator and recording that the others did not need it — which
    // is exactly how B5 shipped. Pinning that they agree makes "narrow one,
    // leave the rest" fail on the next attempt.
    pinMathRandom(edge, 0);
    const mgr = (await createTestDocument().then(
      (d) => new CommentsManager(d),
    )) as any;
    const bounds = generators
      .filter((name) => mgr[name])
      .map((name) => [name, mgr[name]()] as const);
    expect(
      bounds.map(([, v]) => v),
      `the generators do not share a bound: ${JSON.stringify(bounds)}`,
    ).toEqual(bounds.map(() => expected));
  });
});

// ---------------------------------------------------------------------------
// The general guard — what would have caught all three instances
// ---------------------------------------------------------------------------

describe("B5: a saved package carries no out-of-range ST_LongHexNumber", () => {
  it("holds for a threaded document written with the real RNG", async () => {
    expectWordReadableIds(await threadedPackage(), "threaded document");
  });

  it.each(["high", "low"] as const)(
    "holds when the RNG sits at the %s end of every range",
    async (edge) => {
      pinMathRandom(edge);
      expectWordReadableIds(
        await threadedPackage(3),
        `${edge}-edge RNG — no call site may reach an unmasked generator`,
      );
    },
  );

  it("holds for a table document whose cell anchors are derived", async () => {
    // The Node-only instance: resolve_cell_anchor STAMPS its derived paraId
    // into word/document.xml, so a bad derivation ships in the body of every
    // document with an empty unlabeled cell — no RNG involved, and no comment
    // needed to trigger it.
    const doc = await createTestDocument();
    addParagraph(doc, "Intro.");
    const table = addTable(doc, 4, 3);
    setCellText(table, 0, 0, "Filled");
    _extractTextFromDoc(doc, false, false);
    expectWordReadableIds(await doc.save(), "derived cell anchors");
  });

  it("detects a bad id when there is one", async () => {
    // Guards the guard: a scanner that matched nothing would make every
    // assertion above vacuous.
    const doc = await createTestDocument();
    const p = addParagraph(doc, "Probe.");
    p.setAttribute("w14:paraId", "80000000");
    expect(
      findOutOfRangeLongHexNumbers(await doc.save()).map(([, a, v]) => [a, v]),
    ).toEqual([["w14:paraId", "80000000"]]);
  });
});

// ---------------------------------------------------------------------------
// Threading — the consumer-visible symptom
// ---------------------------------------------------------------------------

describe("B5: a reply points at a paraId Word will still recognise", () => {
  it("resolves the parent paraId across 64 independent threads", async () => {
    // B1 already guarantees the reply CARRIES w15:paraIdParent. What this adds
    // is that the value it points AT survives Word's load. A reply can be
    // perfectly parented in the XML and still not thread.
    const dangling: string[] = [];
    for (let i = 0; i < 64; i++) {
      const doc = await DocumentObject.load(await threadedPackage(1));
      const xml = doc.pkg.parts
        .find((p) => p.contentType === CT.EXTENDED)!
        ._element.toString();
      const entries = Array.from(
        xml.matchAll(
          /w15:paraId="([0-9A-Fa-f]{8})"(?:\s+w15:paraIdParent="([0-9A-Fa-f]{8})")?/g,
        ),
      );
      const roots = new Set(
        entries.filter(([, , parent]) => !parent).map(([, id]) => id),
      );
      for (const [, id, parent] of entries) {
        if (!parent) continue;
        expect(roots.has(parent), `reply ${id} points at unknown ${parent}`).toBe(
          true,
        );
        if (!isLegal(parent)) dangling.push(parent);
        if (!isLegal(id)) dangling.push(id);
      }
    }
    expect(
      dangling.length,
      `${dangling.length} of 64 threads reference a paraId Word will discard ` +
        `(${dangling.slice(0, 4).join(", ")}). process_batch reports success and B1's ` +
        `CommentThreadingError correctly does not fire — the reply simply stops being a ` +
        `reply the moment Word opens the file.`,
    ).toBe(0);
  });

  it("mints an in-range paraId when adopting a legacy parent", async () => {
    // The second mint site: a parent with no w14:paraId gets one so the reply
    // can thread. That id becomes the thread ROOT — the worst place for a bad
    // one, because an out-of-range root takes every reply down with it.
    pinMathRandom("high");
    const doc = await createTestDocument();
    addParagraph(doc, "Discovery shall proceed under the model order.");
    const engine = new RedlineEngine(doc, "Sarah Chen");
    engine.apply_edits([
      {
        type: "modify",
        target_text: "the model order",
        new_text: "the model order",
        comment: "Which model order?",
      } as any,
    ]);

    const commentsPart = doc.pkg.parts.find(
      (p) => p.contentType === CT.COMMENTS,
    )!;
    let stripped = 0;
    for (const c of Array.from(
      commentsPart._element.getElementsByTagName("w:p"),
    ) as Element[]) {
      if (c.getAttribute("w14:paraId")) {
        c.removeAttribute("w14:paraId");
        stripped++;
      }
    }
    expect(stripped, "fixture precondition: nothing to strip").toBeGreaterThan(0);

    const parentId = Object.keys(extract_comments_data(doc.pkg))[0];
    const [applied, skipped] = engine.apply_review_actions([
      { type: "reply", target_id: `Com:${parentId}`, text: "The WAWD one." } as any,
    ]);
    expect([applied, skipped]).toEqual([1, 0]);

    expectWordReadableIds(await doc.save(), "adopted legacy parent");
  });
});

// ---------------------------------------------------------------------------
// The Node-only instance: derived {#cell:paraId} anchors
// ---------------------------------------------------------------------------

describe("B5: derived cell anchors are ids Word will keep", () => {
  /** Paragraph indices 0..127, covering the run of low indices that the FNV
   * derivation maps into the high half — i.e. the first tables in a document,
   * which is where real cell anchors live. */
  const INDICES = Array.from({ length: 128 }, (_, i) => i);

  async function derivedAnchorAt(index: number): Promise<string> {
    const doc = await createTestDocument();
    for (let i = 0; i < index; i++) addParagraph(doc, `filler ${i}`);
    const table = addTable(doc, 1, 1);
    const cell = table.getElementsByTagName("w:tc")[0] as Element;
    const { paraId } = resolve_cell_anchor(cell, true);
    return paraId!;
  }

  it("derives only legal ids across the first 128 paragraph indices", async () => {
    const bad: [number, string][] = [];
    for (const index of INDICES) {
      const paraId = await derivedAnchorAt(index);
      expect(paraId).toMatch(/^[0-9A-F]{8}$/);
      if (!isLegal(paraId)) bad.push([index, paraId]);
    }
    expect(
      bad.length,
      `${bad.length}/${INDICES.length} derived cell anchors are outside ` +
        `(0x00000000, 0x80000000) — e.g. ${JSON.stringify(bad.slice(0, 4))}. This is not a coin ` +
        `flip: the derivation is deterministic, so these documents fail every time. Word ` +
        `discards the id on load, so the {#cell:...} anchor an agent was handed no longer ` +
        `addresses anything — and it renumbers the rest of the part with it.`,
    ).toBe(0);
  });

  it("keeps the anchor stamped on the paragraph equal to the one it returns", async () => {
    // The returned anchor is what agents address; the stamped attribute is
    // what a re-read resolves. Masking the derivation must move both.
    const doc = await createTestDocument();
    const table = addTable(doc, 1, 1);
    const cell = table.getElementsByTagName("w:tc")[0] as Element;
    const { paraId, firstP } = resolve_cell_anchor(cell, true);
    expect(firstP!.getAttribute("w14:paraId")).toBe(paraId);
  });

  it("stays deterministic: the same index derives the same id", async () => {
    expect(await derivedAnchorAt(7)).toBe(await derivedAnchorAt(7));
    expect(await derivedAnchorAt(7)).not.toBe(await derivedAnchorAt(8));
  });

  it("derives distinct ids for distinct indices", async () => {
    // Masking discards a bit, so collisions are the thing to check for: two
    // cells sharing an anchor would make {#cell:...} ambiguous.
    const ids = await Promise.all(INDICES.map((i) => derivedAnchorAt(i)));
    expect(new Set(ids).size, `derived anchors collide: ${ids.length - new Set(ids).size} duplicates`).toBe(
      ids.length,
    );
  });
});
