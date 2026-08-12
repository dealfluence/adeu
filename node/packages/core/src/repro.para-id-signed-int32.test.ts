// FILE: node/packages/core/src/repro.para-id-signed-int32.test.ts
import { describe, it, expect, afterEach } from "vitest";
import { unzipSync, zipSync, strFromU8, strToU8 } from "fflate";
import {
  createTestDocument,
  addParagraph,
  addTable,
  setCellText,
  findDuplicateParaIds,
  findOutOfRangeLongHexNumbers,
  findTextIdsWithoutParaId,
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

// ---------------------------------------------------------------------------
// B6: the id Adeu did not mint
// ---------------------------------------------------------------------------
//
// B5 masked every id Adeu MINTS, and the two threading tests above prove it -
// but both build their parent with the fixed engine, so the thread root is
// always already legal. Neither covers a parent that ARRIVES with an id Word
// will throw away.
//
// That is the case that shipped, through this engine: the western-district
// demo ran the Node MCP (desktop-extension/Adeu.mcpb). Sarah Chen's comment
// came in carrying `w14:paraId="D2AEAE20"`, `_adoptIntoModernComments` found a
// paraId and reused it verbatim, and the reply's `w15:paraIdParent` was written
// to point at a value Word discards on load. Word COM on the output: two
// comments, both `Ancestor = NONE`, zero replies.

/** The value the demo actually shipped with: an ordinary paraId whose top bit
 *  happens to be set. */
const POISON = "D2AEAE20";

/** What folding POISON into range produces. Word-verified: patching the demo
 *  document's root paraId to exactly this made the reply thread. */
const POISON_FOLDED = "52AEAE20";

function repack(pkg: Buffer, rewrite: (name: string, xml: string) => string): Buffer {
  const unzipped = unzipSync(new Uint8Array(pkg));
  const out: Record<string, Uint8Array> = {};
  for (const [name, bytes] of Object.entries(unzipped)) {
    out[name] = name.endsWith(".xml")
      ? strToU8(rewrite(name, strFromU8(bytes)))
      : bytes;
  }
  return Buffer.from(zipSync(out));
}

/**
 * Rewrite ST_LongHexNumber values across every part, references included.
 *
 * This is exactly what produced the demo document: a fixture builder that
 * rewrote the ids in a Word-authored package, masked `w16cid:durableId` because
 * B3 had taught it to, and left `w14:paraId` alone because the note B3 left
 * behind said paraId was unconstrained. The result validates against the
 * schema, opens without complaint, and silently loses its thread.
 */
function poison(pkg: Buffer, replacements: Record<string, string>): Buffer {
  return repack(pkg, (_name, xml) => {
    for (const [from, to] of Object.entries(replacements)) {
      xml = xml.split(`"${from}"`).join(`"${to}"`);
    }
    return xml;
  });
}

/** The text of `word/<stem>.xml`, tolerating the numbered part names. */
function partText(pkg: Buffer, stem: string): string {
  const unzipped = unzipSync(new Uint8Array(pkg));
  const pattern = new RegExp(`^word/${stem}\\d*\\.xml$`);
  const name = Object.keys(unzipped).find((n) => pattern.test(n));
  expect(name, `no word/${stem}.xml part in ${Object.keys(unzipped)}`).toBeDefined();
  return strFromU8(unzipped[name!]);
}

/** `[paraId, paraIdParent | undefined]` from commentsExtended, in order. */
function commentExEntries(pkg: Buffer): [string, string | undefined][] {
  return Array.from(
    partText(pkg, "commentsExtended").matchAll(
      /w15:paraId="([0-9A-Fa-f]{1,8})"(?:\s+w15:paraIdParent="([0-9A-Fa-f]{1,8})")?/g,
    ),
  ).map(([, id, parent]) => [id, parent]);
}

function rootParaId(pkg: Buffer): string {
  const roots = commentExEntries(pkg).filter(([, p]) => !p).map(([id]) => id);
  expect(roots, "fixture precondition: expected exactly one thread root").toHaveLength(1);
  return roots[0];
}

function paraIdsIn(pkg: Buffer, stem = "comments"): string[] {
  return Array.from(partText(pkg, stem).matchAll(/w14:paraId="([0-9A-Fa-f]{1,8})"/g)).map(
    ([, id]) => id,
  );
}

/** Reply to whichever comment owns the thread root, and save. */
async function replyToRoot(pkg: Buffer, text = "Addressed."): Promise<Buffer> {
  const doc = await DocumentObject.load(pkg);
  const engine = new RedlineEngine(doc, "Adeu AI (TS)");
  const parentId = Object.keys(extract_comments_data(doc.pkg))[0];
  const [applied, skipped] = engine.apply_review_actions([
    { type: "reply", target_id: `Com:${parentId}`, text } as any,
  ]);
  expect([applied, skipped], "the reply did not apply at all").toEqual([1, 0]);
  return await doc.save();
}

describe("B6: an inherited out-of-range id is repaired, not propagated", () => {
  it("threads a reply onto a parent whose paraId Word would discard", async () => {
    // The demo, reduced: one inherited high-bit paraId, one reply.
    const base = await threadedPackage(0);
    const pkg = poison(base, { [rootParaId(base)]: POISON });
    expect(partText(pkg, "comments"), "fixture precondition").toContain(POISON);

    const saved = await replyToRoot(pkg);

    expectWordReadableIds(saved, "inherited parent paraId");
    const entries = new Map(commentExEntries(saved));
    const replies = [...entries].filter(([, parent]) => parent);
    expect(replies, `expected one reply, got ${JSON.stringify([...entries])}`).toHaveLength(1);
    const parent = replies[0][1]!;
    expect(
      entries.has(parent) && !entries.get(parent),
      `the reply points at ${parent}, which is not a thread root in ` +
        `${JSON.stringify([...entries])}. Word renders it as a second top-level comment: ` +
        `right author, right text, wrong place.`,
    ).toBe(true);
  });

  it("keeps the replies that already pointed at the root it repaired", async () => {
    // The repair's own failure mode. Rewriting the root's paraId without
    // rewriting the w15:paraIdParents that referenced it turns one broken
    // thread into N broken threads - strictly worse than doing nothing.
    const base = await threadedPackage(2);
    const pkg = poison(base, { [rootParaId(base)]: POISON });

    const entries = new Map(commentExEntries(await replyToRoot(pkg, "Third reply.")));
    const roots = [...entries].filter(([, p]) => !p).map(([id]) => id);
    const parents = new Set([...entries.values()].filter(Boolean));
    expect(roots, `the repair split the thread: ${JSON.stringify([...entries])}`).toHaveLength(1);
    expect([...parents]).toEqual(roots);
    expect(entries.size, "expected root + 3 replies").toBe(4);
  });

  it("writes the repaired paraId identically into all three comment parts", async () => {
    // Word consults comments.xml, commentsExtended AND commentsIds. A paraId
    // repaired in one and left stale in another drops the comment out of the
    // modern-comments path entirely - the same end state as not repairing it.
    const base = await threadedPackage(1);
    const saved = await replyToRoot(poison(base, { [rootParaId(base)]: POISON }));

    for (const stem of ["comments", "commentsExtended", "commentsIds"]) {
      expect(partText(saved, stem), `${stem} still carries ${POISON}`).not.toContain(POISON);
    }
    const root = rootParaId(saved);
    expect(partText(saved, "comments")).toContain(`w14:paraId="${root}"`);
    expect(partText(saved, "commentsIds")).toContain(`w16cid:paraId="${root}"`);
  });

  it("folds rather than reinvents, so a re-run is a no-op", async () => {
    // D2AEAE20 -> 52AEAE20: clear the top bit, which is what Word does to the
    // value anyway. Pinned because it is the exact substitution verified in
    // Word against the demo document.
    const base = await threadedPackage(0);
    const saved = await replyToRoot(poison(base, { [rootParaId(base)]: POISON }));
    expect(rootParaId(saved)).toBe(POISON_FOLDED);
  });

  it("repairs inherited out-of-range rsids in the comments part too", async () => {
    // The demo document carried w:rsidR/@w:rsidRDefault/@w:rsidP = AD3412F6
    // alongside the bad paraId. Word renumbers a PART, not an attribute, so a
    // bad rsid left in comments.xml re-arms the pass that de-threads the reply.
    const base = await threadedPackage(0);
    const rsids = Array.from(
      partText(base, "comments").matchAll(/w:rsidR="([0-9A-Fa-f]{1,8})"/g),
    ).map(([, v]) => v);
    const pkg = poison(base, {
      [rootParaId(base)]: POISON,
      ...Object.fromEntries(rsids.map((r) => [r, "AD3412F6"])),
    });
    expect(partText(pkg, "comments"), "fixture precondition").toContain("AD3412F6");

    const saved = await replyToRoot(pkg);

    expectWordReadableIds(saved, "inherited rsid");
    expect(partText(saved, "comments")).not.toContain("AD3412F6");
  });

  it("leaves a package whose ids are already legal alone", async () => {
    // The repair must be a no-op on a healthy document. A pass that re-mints
    // unconditionally would churn every paraId on every save and invalidate
    // every {#cell:paraId} anchor the caller is holding - the exact damage it
    // exists to prevent.
    const base = await threadedPackage(1);
    const before = paraIdsIn(base);
    const after = new Set(paraIdsIn(await replyToRoot(base)));
    expect(
      before.filter((id) => !after.has(id)),
      "the repair re-minted ids that were already legal",
    ).toEqual([]);
  });

  it("flattens a reply-to-a-reply onto the repaired root", async () => {
    // Modern Word has no nesting: a reply to a reply points at the thread ROOT.
    // That lookup runs through the poisoned value, so it has to see the
    // repaired one.
    const base = await threadedPackage(1);
    const doc = await DocumentObject.load(poison(base, { [rootParaId(base)]: POISON }));
    const engine = new RedlineEngine(doc, "Adeu AI (TS)");
    const ids = Object.keys(extract_comments_data(doc.pkg));
    const [applied, skipped] = engine.apply_review_actions([
      { type: "reply", target_id: `Com:${ids[ids.length - 1]}`, text: "Reply to the reply." } as any,
    ]);
    expect([applied, skipped]).toEqual([1, 0]);

    const entries = new Map(commentExEntries(await doc.save()));
    const roots = [...entries].filter(([, p]) => !p).map(([id]) => id);
    expect(roots, `reply-to-reply created a second root: ${JSON.stringify([...entries])}`).toHaveLength(1);
    for (const parent of [...entries.values()].filter(Boolean)) {
      expect(parent).toBe(roots[0]);
    }
  });

  it("repairs what the demo document actually shipped", async () => {
    // End to end on the real shape: parent paraId AND its registrations in
    // commentsExtended / commentsIds all poisoned together, which is how a
    // fixture builder that rewrites ids leaves a document.
    const base = await threadedPackage(0);
    const pkg = poison(base, { [rootParaId(base)]: POISON });
    expect(
      findOutOfRangeLongHexNumbers(pkg).map(([, attr, value]) => [attr, value]),
      "fixture precondition: not the demo's shape",
    ).toEqual([
      ["w14:paraId", POISON],
      ["w15:paraId", POISON],
      ["w16cid:paraId", POISON],
    ]);

    expect(findOutOfRangeLongHexNumbers(await replyToRoot(pkg))).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The other two MUSTs the schema does not state
// ---------------------------------------------------------------------------

describe("[MS-DOCX] 2.6.2.4: paraId is unique within the part", () => {
  // Like the range rule, this is prose rather than schema, so nothing rejects a
  // duplicate. It becomes load-bearing the moment ids are repaired rather than
  // minted: folding clears the top bit, and D2AEAE20 and 52AEAE20 fold to the
  // SAME value. A repair that ignores uniqueness turns a dangling paraIdParent
  // into an ambiguous one, which is not an improvement.

  it("holds for a freshly written thread", async () => {
    expect(findDuplicateParaIds(await threadedPackage(3))).toEqual([]);
  });

  it("holds when a repair folds onto an id the part already uses", async () => {
    // The collision is constructed, not hoped for: the document already
    // contains 52AEAE20, which is exactly what D2AEAE20 folds to.
    const doc = await createTestDocument();
    addParagraph(doc, "The parties shall confer in good faith before moving to compel production.");
    const engine = new RedlineEngine(doc, "Sarah Chen");
    engine.apply_edits([
      { type: "modify", target_text: "confer in good faith", new_text: "confer in good faith", comment: "Root note." } as any,
      { type: "modify", target_text: "production", new_text: "production", comment: "Second thread." } as any,
    ]);
    const base = await doc.save();
    const [first, second] = paraIdsIn(base);
    const pkg = poison(base, { [first]: POISON, [second]: POISON_FOLDED });
    expect(partText(pkg, "comments"), "fixture precondition: no collision to hit").toContain(
      POISON_FOLDED,
    );

    const saved = await replyToRoot(pkg);

    expect(
      findDuplicateParaIds(saved),
      "the repair folded onto a paraId the part was already using; [MS-DOCX] requires " +
        "paraId to be unique within the part",
    ).toEqual([]);
    expectWordReadableIds(saved, "collision repair");
  });

  it("detects a collision when there is one", async () => {
    // Guards the guard: a scanner that never fires makes the above vacuous.
    const base = await threadedPackage(1);
    const ids = paraIdsIn(base);
    const collided = poison(base, { [ids[1]]: ids[0] });
    expect(findDuplicateParaIds(collided).map(([, attr, value]) => [attr, value])).toEqual([
      ["w14:paraId", ids[0]],
    ]);
  });
});

describe("[MS-DOCX] 2.6.2.6: textId travels with paraId", () => {
  // textId carries the same range rule AND "any element having this attribute
  // MUST also have the paraId attribute". Adeu writes w14:textId on every
  // comment paragraph it creates, so the pairing is a real obligation, and it
  // constrains the repair: renaming a paraId while leaving its textId behind
  // produces an element that violates the spec even though every id is in range.

  it("holds for a freshly written thread", async () => {
    expect(findTextIdsWithoutParaId(await threadedPackage(2))).toEqual([]);
  });

  it("survives repairing an inherited paraId", async () => {
    const base = await threadedPackage(1);
    const saved = await replyToRoot(poison(base, { [rootParaId(base)]: POISON }));
    expect(findTextIdsWithoutParaId(saved)).toEqual([]);
  });

  it("writes a textId that is itself in range", async () => {
    // w14:textId is an ST_LongHexNumber like every other id here. Stated
    // separately because it is the one Adeu writes as a literal rather than
    // from the generator.
    const xml = partText(await threadedPackage(1), "comments");
    for (const [, value] of xml.matchAll(/w14:textId="([0-9A-Fa-f]{1,8})"/g)) {
      expect(isLegal(value), `textId ${value} is outside the legal range`).toBe(true);
    }
  });

  it("detects a lone textId when there is one", async () => {
    const base = await threadedPackage(0);
    const orphaned = repack(base, (_n, xml) =>
      xml.replace(/\s*w14:paraId="[0-9A-Fa-f]{1,8}"/, ""),
    );
    expect(findTextIdsWithoutParaId(orphaned).length).toBeGreaterThan(0);
  });
});
