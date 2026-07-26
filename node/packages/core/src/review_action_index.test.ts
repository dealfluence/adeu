// FILE: node/packages/core/src/review_action_index.test.ts
// apply_review_actions used to re-scan the whole document for each of its six
// revision lookups — 18 getElementsByTagName calls, ~12 full walks per action —
// so a batch cost O(actions x document): measured 5.77M node visits and 50ms
// for 20 actions over 8k paragraphs, and 225ms at 32k.
//
// One index walk now answers all six. These tests pin the invariant
// deterministically (walks per action), not by wall clock, which would be flaky
// in CI. The semantics of the rewritten group closure and its ordering are
// covered by the review-action suites (engine.comment-preservation,
// repro_qa_round3_2026_07_24 findings 1.1/2.1/2.2, engine.batch) — this file
// only adds the nested-id closure case that motivated the fixpoint.
import { describe, it, expect } from "vitest";
import { createTestDocument, addParagraph } from "./test-utils.js";
import { RedlineEngine } from "./engine.js";

function countElements(node: any): number {
  let n = 1;
  const kids = node.childNodes;
  if (kids) for (const k of kids) if (k.nodeType === 1) n += countElements(k);
  return n;
}

/**
 * Total elements traversed by getElementsByTagName — the primitive every
 * whole-document scan goes through. Implementation-independent, so it fails
 * loudly on the O(actions x document) shape regardless of how the lookups are
 * expressed.
 */
function countScanTraversal(sample: any) {
  let proto: any = Object.getPrototypeOf(sample);
  while (proto && !Object.getOwnPropertyDescriptor(proto, "getElementsByTagName")) {
    proto = Object.getPrototypeOf(proto);
  }
  const orig = proto.getElementsByTagName;
  let visited = 0;
  proto.getElementsByTagName = function (this: any, tag: string) {
    visited += countElements(this);
    return orig.call(this, tag);
  };
  return {
    get visited() {
      return visited;
    },
    restore: () => {
      proto.getElementsByTagName = orig;
    },
  };
}

function countIndexBuilds() {
  const proto: any = RedlineEngine.prototype;
  const orig = proto._buildRevisionIndex;
  let builds = 0;
  proto._buildRevisionIndex = function (this: any, ...args: any[]) {
    builds++;
    return orig.apply(this, args);
  };
  return {
    get builds() {
      return builds;
    },
    restore: () => {
      proto._buildRevisionIndex = orig;
    },
  };
}

/** `nRevisions` same-author del+ins pairs, then filler paragraphs. */
async function docWithRevisions(nParas: number, nRevisions: number) {
  const doc = await createTestDocument();
  const xml = doc.element.ownerDocument!;
  const el = (name: string, attrs: Record<string, string> = {}) => {
    const e = xml.createElement(name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  };
  const stamp = { "w:author": "Indexer", "w:date": "2026-01-01T00:00:00Z" };
  for (let i = 0; i < nParas; i++) {
    if (i >= nRevisions) {
      addParagraph(doc, `filler paragraph ${i}`);
      continue;
    }
    const p = el("w:p");
    const del = el("w:del", { "w:id": `${1000 + i}`, ...stamp });
    const dr = el("w:r");
    const dt = el("w:delText");
    dt.textContent = `old text ${i}`;
    dr.appendChild(dt);
    del.appendChild(dr);
    p.appendChild(del);
    const ins = el("w:ins", { "w:id": `${5000 + i}`, ...stamp });
    const ir = el("w:r");
    const it = el("w:t");
    it.textContent = `new text ${i}`;
    ir.appendChild(it);
    ins.appendChild(ir);
    p.appendChild(ins);
    doc.element.appendChild(p);
  }
  return doc;
}

describe("apply_review_actions revision index", () => {
  it("does not scan the whole document once per lookup", async () => {
    const nActions = 20;
    const doc = await docWithRevisions(400, nActions);
    const engine = new RedlineEngine(doc, "Indexer");
    const docSize = countElements(doc.element);
    const actions = Array.from({ length: nActions }, (_, i) => ({
      type: "accept",
      target_id: `Chg:${5000 + i}`,
    }));

    const scan = countScanTraversal(doc.element);
    let applied: number;
    try {
      [applied] = engine.apply_review_actions(actions);
    } finally {
      scan.restore();
    }

    expect(applied).toBe(nActions);
    // The old shape did ~12 whole-document scans per action. Budget one
    // document's worth per action total — generous for the index approach
    // (which does not use getElementsByTagName at all) and unreachable for
    // per-lookup rescanning.
    const budget = docSize * nActions;
    expect(
      scan.visited,
      `getElementsByTagName traversed ${scan.visited} elements across ` +
        `${nActions} actions on a ${docSize}-element document ` +
        `(${(scan.visited / docSize).toFixed(1)} full-document scans) — the ` +
        "review-action lookups must come off one index, not a scan each",
    ).toBeLessThan(budget);
  });

  it("walks the document at most once per action", async () => {
    // Design invariant, measurable only through the index itself: an action
    // may invalidate the index with its own mutations, but nothing may rebuild
    // it more than once per action.
    const nActions = 20;
    const doc = await docWithRevisions(400, nActions);
    const engine = new RedlineEngine(doc, "Indexer");
    const actions = Array.from({ length: nActions }, (_, i) => ({
      type: "accept",
      target_id: `Chg:${5000 + i}`,
    }));

    const spy = countIndexBuilds();
    let applied: number;
    try {
      [applied] = engine.apply_review_actions(actions);
    } finally {
      spy.restore();
    }

    expect(applied).toBe(nActions);
    expect(spy.builds).toBeGreaterThan(0);
    expect(
      spy.builds,
      `${spy.builds} index builds for ${nActions} actions — at most one per action`,
    ).toBeLessThanOrEqual(nActions);
  });

  it("shares one index across actions that mutate nothing", async () => {
    const doc = await docWithRevisions(400, 20);
    const engine = new RedlineEngine(doc, "Indexer");
    // Unknown ids: every action is skipped, so nothing bumps the document's
    // mutation counter and the index must be built exactly once.
    const misses = Array.from({ length: 20 }, (_, i) => ({
      type: "accept",
      target_id: `Chg:${900000 + i}`,
    }));

    const spy = countIndexBuilds();
    let applied: number;
    let skipped: number;
    try {
      [applied, skipped] = engine.apply_review_actions(misses);
    } finally {
      spy.restore();
    }

    expect(applied).toBe(0);
    expect(skipped).toBe(20);
    expect(spy.builds).toBe(1);
  });

  it("still pulls a nested revision's id into the resolved group", async () => {
    // A chained edit nests a transient w:del inside a pending w:ins. Resolving
    // the host consumes the nested one, so acting on the nested id afterwards
    // is "already resolved", not "no such id" (QA round 3, finding 2.1). This
    // is the closure the index's precomputed `nested` lists replaced.
    const doc = await createTestDocument();
    const xml = doc.element.ownerDocument!;
    const el = (name: string, attrs: Record<string, string> = {}) => {
      const e = xml.createElement(name);
      for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
      return e;
    };
    const stamp = { "w:author": "Indexer", "w:date": "2026-01-01T00:00:00Z" };
    const p = el("w:p");
    const ins = el("w:ins", { "w:id": "700", ...stamp });
    const nested = el("w:del", { "w:id": "701", ...stamp });
    const nr = el("w:r");
    const nt = el("w:delText");
    nt.textContent = "superseded";
    nr.appendChild(nt);
    nested.appendChild(nr);
    ins.appendChild(nested);
    const keep = el("w:r");
    const kt = el("w:t");
    kt.textContent = "kept text";
    keep.appendChild(kt);
    ins.appendChild(keep);
    p.appendChild(ins);
    doc.element.appendChild(p);

    const engine = new RedlineEngine(doc, "Indexer");
    const [applied, skipped, already] = engine.apply_review_actions([
      { type: "accept", target_id: "Chg:700" },
      { type: "accept", target_id: "Chg:701" },
    ]);

    expect(applied).toBe(1);
    expect(skipped, engine.skipped_details.join(" | ")).toBe(0);
    expect(
      already,
      "the nested id must be recognised as resolved with its host, not reported missing",
    ).toBe(1);
  });
});
