// FILE: node/packages/core/src/cc_run_control_identity.test.ts
/**
 * CC-4 — every run knows which content controls enclose it.
 *
 * The write gates are all the same question in different clothes: "does this
 * edit's text sit inside control X, and what does X permit?". Answering it
 * needs a run-to-control mapping, and before CC-4 nothing in either engine had
 * one - `TextSpan` carries `part_index` for the OPC wall but no control
 * identity.
 *
 * `Run.sdtStack` (python: `ProjectedRun.sdt_stack`) is that mapping,
 * maintained by the traversal that already walks into every `w:sdt`. The
 * python twin is `python/tests/test_cc_run_control_identity.py`.
 *
 * The property these tests exist to defend is the non-obvious one: the stack
 * tracks **every** control, not only the anchored ones that project `{#cc:N}`
 * tokens. Anchoring answers "does a token appear in the text"; enclosure
 * answers "which gates apply". A `sdtContentLocked` picture control projects
 * no token and is still locked, and a gate that consulted only anchor events
 * would let edits straight through it.
 */

import { describe, it, expect } from "vitest";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { Run } from "./docx/primitives.js";
import { iter_block_items, iter_paragraph_content } from "./utils/docx.js";
import { Paragraph } from "./docx/primitives.js";
import { assignOrdinals } from "./utils/content-controls.js";

const runXml = (t: string) => `<w:r><w:t xml:space="preserve">${t}</w:t></w:r>`;

/** A picture control: UNANCHORED, so it emits no `sdt_start`/`sdt_end`. */
const PICTURE = "<w:picture/>";
/** A group: anchored, and the wrapper G3 gates on. */
const GROUP = "<w:group/>";
/** A plain text control: anchored. */
const TEXT = "<w:text/>";

function sdt(
  inner: string,
  opts: { clsXml: string; lock?: string; sdtId: number },
): string {
  const lockXml = opts.lock ? `<w:lock w:val="${opts.lock}"/>` : "";
  return (
    `<w:sdt><w:sdtPr><w:id w:val="${opts.sdtId}"/><w:tag w:val="t${opts.sdtId}"/>` +
    `${lockXml}${opts.clsXml}</w:sdtPr><w:sdtContent>${inner}</w:sdtContent></w:sdt>`
  );
}

/**
 * Every Run the traversal emits for a one-paragraph body.
 * `withInfos=false` omits the ordinal map, as outline and sanitize do.
 */
async function runsFor(body: string, withInfos = true): Promise<Run[]> {
  const doc = await DocumentObject.load(
    Buffer.from(ccFixtureBytes(undefined, body)),
  );
  const infos = withInfos ? assignOrdinals([doc.element]) : undefined;
  const out: Run[] = [];
  for (const block of iter_block_items(doc)) {
    if (!(block instanceof Paragraph)) continue;
    for (const item of iter_paragraph_content(block, infos)) {
      if (item instanceof Run) out.push(item);
    }
  }
  return out;
}

const ids = (r: Run) => r.sdtStack.map((i: any) => i.sdtId);

describe("run control identity (CC-4)", () => {
  it("a run outside every control has an empty stack", async () => {
    const [only] = await runsFor(`<w:p>${runXml("Plain body text.")}</w:p>`);
    expect(only.sdtStack).toEqual([]);
  });

  it("a run inside an anchored control names it", async () => {
    const body = `<w:p>${sdt(runXml("Inside."), { clsXml: TEXT, sdtId: 901 })}</w:p>`;
    const [only] = await runsFor(body);
    expect(ids(only)).toEqual(["901"]);
  });

  it("the stack tracks UNANCHORED controls too", async () => {
    // The point of the whole design. A picture control never emits
    // `sdt_start`/`sdt_end` - it is unanchored, so the traversal descends
    // through it transparently and no token is projected. A gate driven by
    // anchor events would therefore be blind to its lock. The stack is not,
    // because it is maintained structurally rather than from the projection.
    const body = `<w:p>${sdt(runXml("Caption text."), {
      clsXml: PICTURE,
      lock: "sdtContentLocked",
      sdtId: 902,
    })}</w:p>`;
    const [only] = await runsFor(body);
    expect(ids(only)).toEqual(["902"]);
    expect(only.sdtStack[0].contentLocked).toBe(true);
  });

  it("nesting is outermost first", async () => {
    // G1 says "control (or ancestor)" and G3 needs the group/leaf
    // distinction, so the ORDER carries meaning: index 0 is the outermost.
    const inner = sdt(runXml("Nested."), { clsXml: TEXT, sdtId: 904 });
    const body = `<w:p>${sdt(inner, {
      clsXml: GROUP,
      lock: "sdtContentLocked",
      sdtId: 903,
    })}</w:p>`;
    const [only] = await runsFor(body);
    expect(ids(only)).toEqual(["903", "904"]);
    expect(only.sdtStack[0].cls).toBe("group");
  });

  it("the stack is popped on the way out", async () => {
    // A snapshot, not a shared reference. If the stack leaked, the trailing
    // run would claim to be inside a control it left.
    const body =
      `<w:p>${runXml("Before ")}` +
      `${sdt(runXml("inside"), { clsXml: TEXT, sdtId: 905 })}` +
      `${runXml(" after.")}</w:p>`;
    const [before, inside, after] = await runsFor(body);
    expect(before.sdtStack).toEqual([]);
    expect(ids(inside)).toEqual(["905"]);
    expect(after.sdtStack).toEqual([]);
  });

  it("omitting the ordinal map leaves the stack empty", async () => {
    // Callers that opt out of control awareness (outline, sanitize) must see
    // exactly the historical behaviour, stack included.
    const body = `<w:p>${sdt(runXml("Inside."), { clsXml: TEXT, sdtId: 906 })}</w:p>`;
    const runs = await runsFor(body, false);
    expect(runs.map((r) => r.sdtStack)).toEqual([[]]);
  });
});
