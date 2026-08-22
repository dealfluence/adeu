// FILE: node/packages/core/src/cc_gate_surfaces.test.ts
/**
 * CC-4: the override parameters exist on every surface, with the right default.
 *
 * spec-gates.md §1 lists three parameters and the surfaces each must appear
 * on. A gate whose override is unreachable from the caller's surface is a gate
 * the caller cannot legitimately get past, which turns a safety rail into a
 * wall — so the surface list is part of the contract, not packaging.
 *
 * The default matters as much as the presence. §1: "Booleans, schema default
 * `false` (truthy defaults survive client stripping)". A default of true would
 * mean the gate silently does not exist for any client that strips defaults.
 *
 * Python twin: `python/tests/test_cc_gate_surfaces.py`. Node has no CLI — the
 * Python package owns it — so the CLI half lives only on that side.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ccFixtureBytes } from "./test-utils.js";
import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import {
  ALLOW_UNTRACKED_WRITES,
  IGNORE_CONTROL_LOCKS,
  IGNORE_DOCUMENT_PROTECTION,
} from "./gates.js";

const OVERRIDES = [
  IGNORE_CONTROL_LOCKS,
  IGNORE_DOCUMENT_PROTECTION,
  ALLOW_UNTRACKED_WRITES,
];

/** Read a sibling package's source. CRLF-normalised: git checks these out
 *  with CRLF on Windows and readFileSync does NOT translate, unlike Python's
 *  read_text — the trap that made the CC-1c golden helper Windows-only. */
function sourceOf(...parts: string[]): string {
  return readFileSync(join(__dirname, "..", "..", ...parts), "utf-8").replace(
    /\r\n/g,
    "\n",
  );
}

describe("CC-4 override surfaces", () => {
  it.each(OVERRIDES)("the engine accepts %s, defaulting off", async (name) => {
    const doc = await DocumentObject.load(Buffer.from(ccFixtureBytes()));
    const eng = new RedlineEngine(doc, "Surface Test");
    expect(eng.gate_overrides).toHaveProperty(name);
    expect((eng.gate_overrides as any)[name]).toBe(false);
  });

  it.each(OVERRIDES)("the engine honours %s when passed", async (name) => {
    const doc = await DocumentObject.load(Buffer.from(ccFixtureBytes()));
    const eng = new RedlineEngine(doc, "Surface Test", { [name]: true });
    expect((eng.gate_overrides as any)[name]).toBe(true);
  });

  it.each(OVERRIDES)("the MCP server declares %s with default false", (name) => {
    const src = sourceOf("mcp-server", "src", "index.ts");
    expect(src, `${name} missing from the MCP tool schema`).toContain(`${name}: z`);
    // The declaration, its .default(false) and its .describe() must travel
    // together; a bare z.boolean() would land in required[] and be dropped by
    // clients that strip primitives.
    const decl = src.slice(src.indexOf(`${name}: z`), src.indexOf(`${name}: z`) + 400);
    expect(decl, `${name} must default to false`).toContain(".default(false)");
    expect(decl).toContain(".describe(");
  });

  it.each(OVERRIDES)("the MCP handler forwards %s to the engine", (name) => {
    const src = sourceOf("mcp-server", "src", "index.ts");
    // Destructured from the tool arguments AND passed into the constructor.
    // Declaring it in the schema without forwarding it is the silent failure
    // this asserts against: the caller sets the flag and nothing happens.
    const handler = src.slice(src.indexOf("async ({\n    reasoning,"));
    expect(handler).toContain(`    ${name},`);
    expect(handler).toContain(`        ${name},`);
  });

  it.each(OVERRIDES)("the n8n node exposes %s", (name) => {
    const src = sourceOf(
      "n8n-nodes-adeu",
      "nodes",
      "Adeu",
      "descriptions",
      "applyEdits.operation.ts",
    );
    const camel = name.replace(/_([a-z])/g, (_m, c) => c.toUpperCase());
    expect(src, `${camel} missing from the n8n node properties`).toContain(
      `name: "${camel}"`,
    );
    expect(src).toContain(`${name}: ${camel}`);
  });

  it("records protection state at load", async () => {
    // spec-gates §3: read once at load, not per gate, so the gates, the
    // banner and the ledger cannot report different states.
    const protectedDoc = await DocumentObject.load(
      Buffer.from(ccFixtureBytes("forms")),
    );
    const eng = new RedlineEngine(protectedDoc, "Surface Test");
    expect(eng.protection.edit).toBe("forms");
    expect(eng.protection.enforced).toBe(true);

    const plainDoc = await DocumentObject.load(Buffer.from(ccFixtureBytes()));
    expect(new RedlineEngine(plainDoc, "Surface Test").protection.edit).toBeNull();
  });
});
