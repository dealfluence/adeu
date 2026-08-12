#!/usr/bin/env node
// FILE: shared/conformance/build_fixtures.mjs
//
// Builds the six conformance fixtures both engines read (spec §8.3). Run from
// the repo root:
//
//     node shared/conformance/build_fixtures.mjs
//
// Everything here is deterministic BY CONSTRUCTION so regenerating does not
// churn the committed bytes (and therefore does not churn the goldens):
//
//   • Text comes from literals and index arithmetic — never a clock, never a
//     random draw.
//   • `Date` and `Math.random` are frozen before `@adeu/core` is imported.
//     The engine stamps `w:date`/`w16du:dateUtc` from `new Date()` and mints
//     `w14:paraId`/`w:rsid*`/`w16cid:durableId` from `Math.random()`, and
//     fflate writes each zip entry's DOS timestamp from `Date.now()`.
//   • TZ is pinned to UTC because fflate converts that timestamp to a LOCAL
//     DOS date — without this the same run in another timezone writes
//     different bytes.
//
// Only `@adeu/core` is used: no Word, no benchmark corpus. `@adeu/core` does
// not export its test-utils, so the four document-construction helpers below
// are the same shapes as node/packages/core/src/test-utils.ts.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// --- determinism freeze (must precede the @adeu/core import) ---------------
process.env.TZ = "UTC";

const FROZEN_ISO = "2026-01-01T00:00:00Z";
const FROZEN_MS = Date.parse(FROZEN_ISO);
const RealDate = Date;
class FrozenDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(FROZEN_MS);
    else super(...args);
  }
  static now() {
    return FROZEN_MS;
  }
}
globalThis.Date = FrozenDate;

// mulberry32 — a seeded stand-in for Math.random. Every value it returns is a
// value Math.random could have returned, so the engine's id generators keep
// behaving exactly as they do in production (including the ST_LongHexNumber
// range fold).
let _seed = 0x2f6e2b1;
Math.random = () => {
  _seed = (_seed + 0x6d2b79f5) | 0;
  let t = _seed;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};

// --- paths ----------------------------------------------------------------
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "../..");
const FIXTURE_DIR = join(HERE, "fixtures");
const INITIAL_DOCX = join(REPO_ROOT, "shared/fixtures/initial.docx");
const CORE_DIST = join(REPO_ROOT, "node/packages/core/dist/index.js");

let core;
try {
  core = await import(pathToFileURL(CORE_DIST).href);
} catch (err) {
  console.error(
    `Cannot load @adeu/core from ${CORE_DIST}.\n` +
      "Run `cd node && npm ci && npm run build` first.\n" +
      String(err),
  );
  process.exit(1);
}
const { DocumentObject, RedlineEngine, _extractTextFromDoc, paginate, split_structural_appendix, extract_comments_data } = core;

// --- document construction helpers ----------------------------------------

/** Loads the pristine empty fixture and clears its body (python-docx style). */
async function createDoc() {
  const doc = await DocumentObject.load(readFileSync(INITIAL_DOCX));
  const body = doc.element;
  while (body.firstChild) body.removeChild(body.firstChild);
  return doc;
}

/**
 * Appends a paragraph. `outlineLevel` (0-based, as in ECMA-376) writes
 * `w:outlineLvl`, which is the STRUCTURAL heading signal both engines read
 * first — so headings work without depending on the fixture's styles.xml.
 */
function addParagraph(doc, text, outlineLevel = null) {
  const xmlDoc = doc.element.ownerDocument;
  const p = xmlDoc.createElement("w:p");
  if (outlineLevel !== null) {
    const pPr = xmlDoc.createElement("w:pPr");
    const oLvl = xmlDoc.createElement("w:outlineLvl");
    oLvl.setAttribute("w:val", String(outlineLevel));
    pPr.appendChild(oLvl);
    p.appendChild(pPr);
  }
  const r = xmlDoc.createElement("w:r");
  const t = xmlDoc.createElement("w:t");
  t.textContent = text;
  if (/\s/.test(text)) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  p.appendChild(r);
  doc.element.appendChild(p);
  return p;
}

function addTable(doc, rows, cols) {
  const xmlDoc = doc.element.ownerDocument;
  const tbl = xmlDoc.createElement("w:tbl");
  const tblGrid = xmlDoc.createElement("w:tblGrid");
  for (let i = 0; i < cols; i++) tblGrid.appendChild(xmlDoc.createElement("w:gridCol"));
  tbl.appendChild(tblGrid);
  for (let r = 0; r < rows; r++) {
    const tr = xmlDoc.createElement("w:tr");
    for (let c = 0; c < cols; c++) {
      const tc = xmlDoc.createElement("w:tc");
      tc.appendChild(xmlDoc.createElement("w:p"));
      tr.appendChild(tc);
    }
    tbl.appendChild(tr);
  }
  doc.element.appendChild(tbl);
  return tbl;
}

function setCellText(table, rowIndex, colIndex, text) {
  const rows = Array.from(table.childNodes).filter((n) => n.tagName === "w:tr");
  const cells = Array.from(rows[rowIndex].childNodes).filter((n) => n.tagName === "w:tc");
  const cell = cells[colIndex];
  while (cell.firstChild) cell.removeChild(cell.firstChild);
  const xmlDoc = table.ownerDocument;
  const p = xmlDoc.createElement("w:p");
  const r = xmlDoc.createElement("w:r");
  const t = xmlDoc.createElement("w:t");
  t.textContent = text;
  if (/\s/.test(text)) t.setAttribute("xml:space", "preserve");
  r.appendChild(t);
  p.appendChild(r);
  cell.appendChild(p);
}

/**
 * Stamps a FORMAT-ONLY tracked change (`w:rPrChange`) on a paragraph's first
 * run. No batch action produces one — the six DocumentChange variants all
 * touch text — so the ledger's `fmt` row can only be fixtured as raw OOXML.
 * Same shape as python/tests/test_repro_qa_round3_2026_07_24.py:301-315.
 */
function addFormatOnlyChange(p, id, author) {
  const xmlDoc = p.ownerDocument;
  const r = Array.from(p.childNodes).find((n) => n.tagName === "w:r");
  let rPr = Array.from(r.childNodes).find((n) => n.tagName === "w:rPr");
  if (!rPr) {
    rPr = xmlDoc.createElement("w:rPr");
    r.insertBefore(rPr, r.firstChild);
  }
  rPr.appendChild(xmlDoc.createElement("w:b")); // the new formatting
  const chg = xmlDoc.createElement("w:rPrChange");
  chg.setAttribute("w:id", String(id));
  chg.setAttribute("w:author", author);
  chg.setAttribute("w:date", FROZEN_ISO);
  chg.appendChild(xmlDoc.createElement("w:rPr")); // the previous formatting
  rPr.appendChild(chg); // w:rPrChange is last in CT_RPr
  return p;
}

/** Round-trips through bytes, the way each review round reaches the engine. */
async function reload(doc) {
  return DocumentObject.load(await doc.save());
}

// --- reporting ------------------------------------------------------------
const written = [];

async function writeFixture(name, doc) {
  const buf = await doc.save();
  mkdirSync(FIXTURE_DIR, { recursive: true });
  writeFileSync(join(FIXTURE_DIR, `${name}.docx`), buf);

  const reloaded = await DocumentObject.load(buf);
  const text = _extractTextFromDoc(reloaded, false, false);
  const [body] = split_structural_appendix(text);
  const pages = paginate(body, "").total_pages;
  const changes = new Set(Array.from(body.matchAll(/\[Chg:(\w+)/g)).map((m) => m[1])).size;
  const comments = Object.keys(extract_comments_data(reloaded.pkg)).length;
  written.push({ name, kb: Math.round(buf.length / 1024), chars: text.length, pages, changes, comments });
}

// --- fixture 1: multi_author ----------------------------------------------
// Three authors, two paired ins/del replacements, one edit wholly inside a
// foreign insertion, one format-only change. Nine tracked changes total.
async function buildMultiAuthor() {
  const doc = await createDoc();
  addParagraph(doc, "Master Services Agreement", 0);
  addParagraph(doc, "Invoiced charges are due net thirty (30) days from the invoice date.");
  addParagraph(doc, "The Provider shall maintain liability insurance of one million dollars.");
  addParagraph(doc, "Either party may terminate this Agreement upon ninety (90) days written notice.");
  addParagraph(doc, "This Agreement is governed by the laws of the State of New York.");

  const round1 = new RedlineEngine(doc, "Jane Doe");
  round1.process_batch([
    { type: "modify", target_text: "net thirty (30) days", new_text: "net forty-five (45) days" },
    { type: "modify", target_text: "ninety (90) days", new_text: "sixty (60) days" },
  ]);

  // Bob edits text that exists only inside Jane's insertion.
  const doc2 = await reload(doc);
  new RedlineEngine(doc2, "Bob Smith").process_batch([
    // The target is the whole of Jane's inserted run ("forty-five"): the
    // engine word-diffs a replacement, so only the changed words sit inside
    // <w:ins>. `match_mode` must be explicit — only a strict/first
    // single-occurrence edit may nest inside a foreign insertion
    // (engine.ts:3113-3170).
    { type: "modify", target_text: "forty-five", new_text: "thirty-five", match_mode: "strict" },
  ]);

  const doc3 = await reload(doc2);
  new RedlineEngine(doc3, "Acme LLP").process_batch([
    { type: "modify", target_text: "one million dollars", new_text: "two million dollars" },
  ]);

  const doc4 = await reload(doc3);
  const governing = Array.from(doc4.element.childNodes)
    .filter((n) => n.tagName === "w:p")
    .find((p) => (p.textContent || "").includes("State of New York"));
  addFormatOnlyChange(governing, 901, "Acme LLP");

  await writeFixture("multi_author", doc4);
}

// --- fixture 2: comments_threads ------------------------------------------
// Three top-level comments (comment-only modifies: target_text === new_text)
// plus two replies threaded onto the first two.
async function buildCommentsThreads() {
  const doc = await createDoc();
  addParagraph(doc, "The parties shall confer in good faith before escalating a dispute.");
  addParagraph(doc, "Discovery material shall be produced within thirty days of request.");
  addParagraph(doc, "The receiving party shall bear the reasonable cost of production.");
  addParagraph(doc, "Notices are effective upon receipt at the address on the signature page.");

  new RedlineEngine(doc, "Sarah Chen").process_batch([
    {
      type: "modify",
      target_text: "confer in good faith",
      new_text: "confer in good faith",
      comment: "Should this reference the protective order?",
    },
    {
      type: "modify",
      target_text: "within thirty days",
      new_text: "within thirty days",
      comment: "Confirm this matches the scheduling order.",
    },
    {
      type: "modify",
      target_text: "reasonable cost",
      new_text: "reasonable cost",
      comment: "Whose cost is this, really?",
    },
  ]);

  const doc2 = await reload(doc);
  const ids = Object.keys(extract_comments_data(doc2.pkg)).sort((a, b) => Number(a) - Number(b));
  new RedlineEngine(doc2, "Mikko Korpela").process_batch([
    { type: "reply", target_id: `Com:${ids[0]}`, text: "Yes — cite the protective order, section 4." },
    { type: "reply", target_id: `Com:${ids[1]}`, text: "It does not; the order says forty-five days." },
  ]);

  await writeFixture("comments_threads", await reload(doc2));
}

// --- fixture 3: tables_cells ----------------------------------------------
// A 3x3 table with one revised cell and one EMPTY cell, whose {#cell:...}
// anchor both engines derive from the cell's paragraph index.
async function buildTablesCells() {
  const doc = await createDoc();
  addParagraph(doc, "Fee Schedule", 0);
  const tbl = addTable(doc, 3, 3);
  const rows = [
    ["Service", "Unit", "Rate"],
    ["Implementation", "per project", "40,000 USD"],
    ["Support", "per month", ""], // the empty cell
  ];
  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 3; c++) {
      if (rows[r][c] !== "") setCellText(tbl, r, c, rows[r][c]);
    }
  }
  addParagraph(doc, "Rates are exclusive of taxes and out-of-pocket expenses.");

  new RedlineEngine(doc, "Jane Doe").process_batch([
    { type: "modify", target_text: "40,000 USD", new_text: "36,500 USD" },
  ]);

  await writeFixture("tables_cells", doc);
}

// --- fixture 4: unicode ---------------------------------------------------
// Smart quotes, an em dash and a non-ASCII author name: the B6 parity point
// (JS JSON.stringify does not escape non-ASCII; Python passes
// ensure_ascii=False, so emitted lengths must agree).
async function buildUnicode() {
  const doc = await createDoc();
  addParagraph(doc, "Sch\u00e9dule A \u2014 Definitions", 0);
  addParagraph(doc, "The Vendor\u2019s obligations under \u201cSchedule A\u201d survive termination.");
  addParagraph(doc, "\u201cConfidential Information\u201d means information the D\u00edsclosing Party marks as such.");
  addParagraph(doc, "Fees are quoted in \u20ac (EUR) and invoiced monthly \u2014 net thirty (30) days.");

  new RedlineEngine(doc, "\u00c5sa \u00d6berg").process_batch([
    { type: "modify", target_text: "net thirty (30) days", new_text: "net sixty (60) days" },
    {
      type: "modify",
      target_text: "survive termination",
      new_text: "survive termination",
      comment: "Add a five-year tail \u2014 see \u00a712.3.",
    },
  ]);

  await writeFixture("unicode", await reload(doc));
}

// --- shared prose pool ----------------------------------------------------
// Literal sentences, cycled by index. "Confidential Information" occurs in
// exactly one of them, so the search cases have a stable, countable term.
const SENTENCES = [
  "Each party shall perform its obligations under this clause with reasonable diligence and without undue delay.",
  "Confidential Information disclosed under this clause remains the property of the disclosing party at all times.",
  "The receiving party shall limit access to those employees who need it to perform the services described here.",
  "No waiver of any provision is effective unless it is made in writing and signed by an authorised officer.",
  "Any notice given under this clause takes effect upon actual receipt at the address on the signature page.",
  "The parties shall review this clause annually and record any agreed amendment in a written change order.",
  "Nothing in this clause limits a remedy available at law or in equity for a material breach of the agreement.",
  "Fees stated in this clause are exclusive of value added tax and of documented out-of-pocket expenses.",
];

/** ~700 chars of stable prose for clause `n`, seeded only from literals. */
function clauseBody(n) {
  const picks = [0, 1, 2, 3, 4, 5, 6, 7].map((k) => SENTENCES[(n + k) % SENTENCES.length]);
  return `Clause ${n}. ${picks.slice(0, 6).join(" ")}`;
}

// --- fixture 5: long_5pages -----------------------------------------------
// Five synthetic pages (>76,000 projected chars, so the A3 whole-document
// guard fires) with L1/L2 headings for the outline and guard cases.
async function buildLong5Pages() {
  const doc = await createDoc();
  const sections = ["Definitions", "Services", "Fees and Invoicing", "Confidentiality", "Term and Termination"];
  let n = 1;
  for (let s = 0; s < sections.length; s++) {
    addParagraph(doc, `Article ${s + 1} \u2014 ${sections[s]}`, 0);
    for (let sub = 1; sub <= 3; sub++) {
      addParagraph(doc, `${s + 1}.${sub} ${sections[s]} \u2014 operative provisions`, 1);
      for (let i = 0; i < 9; i++) addParagraph(doc, clauseBody(n++));
    }
  }
  await writeFixture("long_5pages", doc);
}

// --- fixture 6: dense_175 -------------------------------------------------
// 175 replacement edits — 350 tracked changes — across nine pages: the
// ledger paging (300/page), page-range cap and token-budget cases.
async function buildDense175() {
  const doc = await createDoc();
  const EDITS = 175;
  for (let i = 1; i <= EDITS; i++) {
    const tag = `ITEM-${String(i).padStart(3, "0")}`;
    // One extra sentence over clauseBody's six, so 175 clauses project past
    // the eight-page ceiling and `range_cap_1_12` can hit the cap note.
    addParagraph(doc, `${clauseBody(i)} ${SENTENCES[(i + 6) % SENTENCES.length]} Rate for ${tag} is 100 USD.`);
  }
  const engine = new RedlineEngine(doc, "Reviewer");
  engine.process_batch(
    Array.from({ length: EDITS }, (_, i) => ({
      type: "modify",
      target_text: `Rate for ITEM-${String(i + 1).padStart(3, "0")} is 100 USD`,
      new_text: `Rate for ITEM-${String(i + 1).padStart(3, "0")} is 125 USD`,
    })),
  );
  await writeFixture("dense_175", doc);
}

// --- main -----------------------------------------------------------------
await buildMultiAuthor();
await buildCommentsThreads();
await buildTablesCells();
await buildUnicode();
await buildLong5Pages();
await buildDense175();

for (const f of written) {
  console.log(
    `${f.name.padEnd(17)} ${String(f.kb).padStart(4)} KB  ` +
      `${String(f.chars).padStart(7)} chars  ${f.pages} page(s)  ` +
      `${f.changes} change(s)  ${f.comments} comment(s)`,
  );
}
console.log(`\nWrote ${written.length} fixture(s) to shared/conformance/fixtures/`);
