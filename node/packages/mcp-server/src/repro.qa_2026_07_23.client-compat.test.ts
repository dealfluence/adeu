// FILE: node/packages/mcp-server/src/repro.qa_2026_07_23.client-compat.test.ts
//
// Client-compatibility constraints for the schemas the Node MCP server
// publishes, derived from ADEU-MCP-QA-REPORT.md (2026-07-23) follow-up
// analysis: the raw tools/list schema was compared property-by-property
// against what Claude Code's MCP client actually presents to the model
// (measured live on v1.29.0+4bb70f9, 2026-07-23). The client transforms
// schemas in transit, and those transformations are REAL-WORLD constraints
// the published schemas must be designed within:
//
//   1. TOOL DESCRIPTIONS ARE TRUNCATED at ~2048 chars (measured: a 2128-char
//      description was cut mid-word after 2046 chars + an ellipsis marker).
//      Everything past the cut — currently the `author_name` attribution
//      guidance — is invisible to the model.
//   2. PROPERTY-LEVEL TYPE UNIONS (anyOf/oneOf) ARE STRIPPED TO {}: the
//      client dropped `read_docx.page` (anyOf number|string) and
//      `process_document_batch.changes.items` (anyOf string|object) to empty
//      schemas, losing the type, the item field documentation AND the
//      property description. This is the true mechanism behind QA F10/F22
//      ("untyped {}"). A property must publish exactly one JSON type;
//      runtime tolerance (per-item stringified-JSON repair,
//      coerceChangeItemInPlace) can stay runtime-only — the schema need not
//      advertise it.
//   3. REQUIRED[] IS REWRITTEN: primitive-typed (string) entries are dropped
//      from required[] client-side — of ["reasoning","original_docx_path",
//      "author_name","changes"] only the array-typed "changes" survived, and
//      tools whose required params are all strings lose required[] entirely.
//      The model therefore legitimately omits author_name (QA F3) and the
//      server must degrade GRACEFULLY (default it, or answer with a clean
//      self-service error) instead of the SDK's raw Zod issue dump.
//   4. (Not directly testable server-side, documented for schema authors:)
//      descriptions of OPTIONAL properties are dropped entirely — only
//      required-property descriptions survive — and falsy defaults
//      (`default: false`) are dropped while truthy ones survive. Operative
//      guidance for optional params must therefore live in the tool
//      description, inside the budget of rule 1.
//
// Every test in this file is written test-first: it fails on current main
// and passes once the published schemas/runtime comply.

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  readFileSync,
  writeFileSync,
  existsSync,
  rmSync,
  mkdtempSync,
} from "node:fs";
import { DocumentObject } from "@adeu/core";
import { createTestDocument, addParagraph } from "../../core/src/test-utils.js";
import { startTestServer, type TestServer } from "./test-rpc.js";

const CLIENT_DESCRIPTION_BUDGET = 2048;

describe("QA 2026-07-23 — client-compat constraints (real server over stdio)", () => {
  let server: TestServer;
  let workDir: string;
  let allTools: any[] = [];
  let plainDocPath: string;

  function rpc(method: string, params: any): Promise<any> {
    return server.rpc(method, params);
  }

  async function buildDoc(paragraphs: string[]): Promise<Buffer> {
    const doc = await createTestDocument();
    for (const text of paragraphs) addParagraph(doc, text);
    return doc.save();
  }

  beforeAll(async () => {
    workDir = mkdtempSync(join(tmpdir(), "adeu_client_compat_"));
    plainDocPath = join(workDir, "compat_input.docx");
    writeFileSync(
      plainDocPath,
      await buildDoc(["The quick brown fox jumps over the lazy dog."]),
    );

    server = await startTestServer("client-compat-tests");
    const listed = await server.rpc("tools/list", {});
    allTools = listed.result.tools;
    expect(allTools.length).toBeGreaterThan(0);
  });

  afterAll(() => {
    server?.stop();
    if (workDir && existsSync(workDir))
      rmSync(workDir, { recursive: true, force: true });
  });

  it("every tool description fits the measured ~2048-char client budget", () => {
    // On current main process_document_batch publishes 2128 chars, so the
    // client cuts it mid-sentence — the tail (author_name guidance and the
    // build tag) never reaches the model.
    const over = allTools
      .filter((t) => (t.description || "").length > CLIENT_DESCRIPTION_BUDGET)
      .map((t) => `${t.name} (${t.description.length} chars)`);
    expect(
      over,
      `these tool descriptions exceed the client truncation budget of ` +
        `${CLIENT_DESCRIPTION_BUDGET} chars, so their tail guidance is ` +
        `invisible to the model: ${over.join(", ")}`,
    ).toEqual([]);
  });

  it("the fields ledger surface survives client stripping (A2.7)", () => {
    // The generic walkers below already cover every property, but CC-2 added a
    // mode and an offset to the most heavily used tool in the server, so the
    // acceptance criterion is pinned by name rather than left implicit.
    const readDocx = allTools.find((t) => t.name === "read_docx")!;
    const props: any = readDocx.inputSchema.properties;

    expect(props.mode.enum).toContain("fields");
    expect(props.mode.type).toBe("string");
    expect(props.mode.anyOf ?? props.mode.oneOf).toBeUndefined();
    expect(props.fields_offset).toBeDefined();
    expect(props.fields_offset.anyOf ?? props.fields_offset.oneOf).toBeUndefined();

    // Documented, or the model cannot reach it: the description is the only
    // channel guaranteed to survive to the client.
    expect(readDocx.description).toContain("mode='fields'");
    expect(readDocx.description.length).toBeLessThanOrEqual(
      CLIENT_DESCRIPTION_BUDGET,
    );
  });

  it("no property schema publishes a type union — clients strip anyOf/oneOf to {}", () => {
    // Walk every property schema (including array item schemas). A schema
    // node "publishes a union" if it carries anyOf/oneOf, and it is "bare"
    // if it has neither a type nor an enum — both arrive as {} client-side.
    const offenders: string[] = [];
    const walk = (schema: any, path: string) => {
      if (!schema || typeof schema !== "object") return;
      if (schema.anyOf || schema.oneOf) {
        offenders.push(`${path} (anyOf/oneOf)`);
        return; // children of the union are lost anyway
      }
      if (schema.type === "array") walk(schema.items, `${path}.items`);
      for (const [key, prop] of Object.entries(schema.properties || {})) {
        walk(prop, `${path}.${key}`);
      }
    };
    for (const tool of allTools) {
      for (const [key, prop] of Object.entries(
        tool.inputSchema?.properties || {},
      )) {
        walk(prop, `${tool.name}.${key}`);
      }
    }
    // On current main: read_docx.page (anyOf number|string) and
    // process_document_batch.changes.items (anyOf string|object — the
    // stringified-item repair tolerance leaking into the published schema).
    expect(
      offenders,
      `these property schemas publish unions that real MCP clients strip ` +
        `to {} (type, docs and item fields all lost): ${offenders.join(", ")}`,
    ).toEqual([]);
  });

  it("omitting author_name (shown as optional by real clients) degrades gracefully, not as a raw Zod dump", async () => {
    // The client drops primitive entries from required[], so a
    // schema-following model legitimately omits author_name. Acceptable
    // server behaviors: default it (call succeeds), or reply with a clean
    // one-sentence error naming author_name. NOT acceptable: the SDK's
    // serialized Zod issue dump ("expected"/"received"/"path" JSON).
    const resp = await rpc("tools/call", {
      name: "process_document_batch",
      arguments: {
        reasoning: "client-compat repro: schema-following call",
        original_docx_path: plainDocPath,
        output_path: join(workDir, "compat_output.docx"),
        changes: [
          { type: "modify", target_text: "lazy dog", new_text: "sleepy cat" },
        ],
      },
    });

    // The SDK reports the validation failure as an isError tool result
    // (verified 2026-07-23): content[0].text = 'MCP error -32602: Input
    // validation error: ... [{"expected":"string","code":"invalid_type",
    // "path":["author_name"],...}]'. A protocol-level error would carry it
    // in resp.error.message instead — accept either envelope.
    const isError = Boolean(resp.error) || Boolean(resp.result?.isError);
    if (isError) {
      const msg: string =
        resp.error?.message || resp.result?.content?.[0]?.text || "";
      expect(msg, "setup: expected a non-empty error message").toBeTruthy();
      expect(
        msg,
        "a missing author_name must produce a clean self-service error, " +
          `not a raw Zod issue dump. Got: ${msg}`,
      ).not.toMatch(/"expected"|"received"|"path"|invalid_type/);
      expect(msg).toMatch(/author_name/);
    }
    // Defaulting author_name (a clean success) is the other legitimate fix —
    // nothing to assert in that case.
  });
});
