// FILE: node/packages/n8n-nodes-adeu/test/examples.test.ts
//
// The example workflows can only be *executed* by importing them into a live
// n8n instance. This suite is the substitute: it validates every example JSON
// against the node's own description object, so a renamed parameter, a stale
// operation value, or a hand-edited node `type` fails the build instead of
// failing silently in a user's n8n.
//
// Test findings on baseline example workflows:
// - Sequential_workflow.json passes all 5 assertions.
// - AI_Agent_workflow.json fails on:
//   1. Unrecognized `CUSTOM.adeuTool` / `CUSTOM.adeu` types (expected `n8n-nodes-adeu.adeu` / `n8n-nodes-adeu.adeuTool`).
//   2. Missing explicit `operation` on `extract_markdown`.
//   3. Port wiring mismatch (which triggers because nodes with `CUSTOM.adeuTool` default to expecting main ports in the assertion until the type is fixed).
//   Note: `descriptionType`/`toolDescription` checks will execute once types are corrected in Task 3.

import { describe, it, expect, vi } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

vi.mock("n8n-workflow", () => {
  class NodeOperationError extends Error {
    description?: string;
    itemIndex?: number;
    constructor(_node: unknown, message: unknown, options?: any) {
      super(
        typeof message === "string"
          ? message
          : ((message as Error)?.message ?? "NodeOperationError"),
      );
      this.name = "NodeOperationError";
      this.description = options?.description;
      this.itemIndex = options?.itemIndex;
    }
  }

  class NodeApiError extends Error {
    description?: string;
    constructor(_node: unknown, _error: unknown, options?: any) {
      super(options?.message ?? "NodeApiError");
      this.name = "NodeApiError";
      this.description = options?.description;
    }
  }

  return {
    NodeConnectionTypes: {
      Main: "main",
      AiLanguageModel: "ai_languageModel",
      AiMemory: "ai_memory",
      AiTool: "ai_tool",
      AiDocument: "ai_document",
      AiTextSplitter: "ai_textSplitter",
      AiVectorStore: "ai_vectorStore",
      AiEmbedding: "ai_embedding",
      AiChain: "ai_chain",
      AiAgent: "ai_agent",
      AiRetriever: "ai_retriever",
      AiOutputParser: "ai_outputParser",
    },
    NodeOperationError,
    NodeApiError,
  };
});

// Node import MUST come after vi.mock() in source order.
import { Adeu } from "../nodes/Adeu/Adeu.node";

const EXAMPLES_DIR = resolve(__dirname, "../examples");

const BASE_TYPE = "n8n-nodes-adeu.adeu";
const TOOL_TYPE = "n8n-nodes-adeu.adeuTool";

/**
 * Properties n8n's own `convertNodeToAiTool` splices onto the tool variant of a
 * node that declares both `resource` and `operation`. They exist only on the
 * generated tool node, never in this package's source.
 */
const TOOL_ONLY_PARAMS = new Set(["descriptionType", "toolDescription"]);

const FROM_AI_KEY = /^[a-zA-Z0-9_-]{1,64}$/;
const FROM_AI_CALL = /\$fromAI\(\s*'([^']*)'/g;

const description = new Adeu().description;
const KNOWN_PARAMS = new Set(description.properties.map((p) => p.name));
const OPERATIONS = new Set(
  (
    (description.properties.find((p) => p.name === "operation")?.options ??
      []) as Array<{ value: string }>
  ).map((o) => o.value),
);

interface WfNode {
  name: string;
  type: string;
  typeVersion: number;
  parameters: Record<string, unknown>;
}
interface Workflow {
  name: string;
  nodes: WfNode[];
  connections: Record<string, Record<string, Array<Array<{ node: string }>>>>;
}

const exampleFiles = readdirSync(EXAMPLES_DIR).filter((f) =>
  f.endsWith(".json"),
);

describe("example workflows", () => {
  it("both documented examples are present", () => {
    expect(exampleFiles.sort()).toEqual([
      "AI_Agent_workflow.json",
      "Sequential_workflow.json",
    ]);
  });

  for (const file of exampleFiles) {
    describe(file, () => {
      const wf = JSON.parse(
        readFileSync(resolve(EXAMPLES_DIR, file), "utf8"),
      ) as Workflow;

      const adeuNodes = () =>
        wf.nodes.filter((n) => n.type.includes("adeu") || n.type.includes("Adeu"));

      it("has uniquely named nodes and a resolvable connection graph", () => {
        const names = wf.nodes.map((n) => n.name);
        expect(new Set(names).size).toBe(names.length);
        for (const [source, outputs] of Object.entries(wf.connections)) {
          expect(names, `connection source "${source}"`).toContain(source);
          for (const branches of Object.values(outputs)) {
            for (const targets of branches) {
              for (const t of targets) {
                expect(names, `connection target "${t.node}"`).toContain(t.node);
              }
            }
          }
        }
      });

      it("uses only the two node types n8n exposes for this package", () => {
        for (const n of adeuNodes()) {
          expect([BASE_TYPE, TOOL_TYPE], `node "${n.name}"`).toContain(n.type);
          expect(n.typeVersion, `node "${n.name}"`).toBe(1);
        }
      });

      it("sets a real operation and only real parameters on every Adeu node", () => {
        for (const n of adeuNodes()) {
          expect(
            n.parameters.operation,
            `node "${n.name}" must set operation explicitly`,
          ).toBeTypeOf("string");
          expect(OPERATIONS, `node "${n.name}"`).toContain(
            n.parameters.operation,
          );
          for (const key of Object.keys(n.parameters)) {
            if (TOOL_ONLY_PARAMS.has(key)) {
              expect(n.type, `"${key}" on node "${n.name}"`).toBe(TOOL_TYPE);
              continue;
            }
            expect(KNOWN_PARAMS, `parameter on node "${n.name}"`).toContain(key);
          }
        }
      });

      it("wires tool nodes on ai_tool and regular nodes on main", () => {
        for (const n of adeuNodes()) {
          const outputs = wf.connections[n.name];
          if (!outputs) continue;
          const ports = Object.keys(outputs);
          if (n.type === TOOL_TYPE) {
            expect(ports, `node "${n.name}"`).toEqual(["ai_tool"]);
          } else {
            expect(ports, `node "${n.name}"`).toEqual(["main"]);
          }
        }
      });

      it("gives every tool node a manual description and valid $fromAI keys", () => {
        for (const n of adeuNodes().filter((x) => x.type === TOOL_TYPE)) {
          expect(n.parameters.descriptionType, `node "${n.name}"`).toBe(
            "manual",
          );
          expect(
            String(n.parameters.toolDescription ?? "").trim().length,
            `node "${n.name}" toolDescription`,
          ).toBeGreaterThan(0);

          const serialized = JSON.stringify(n.parameters);
          const keys = [...serialized.matchAll(FROM_AI_CALL)].map((m) => m[1]);
          // Google Gemini rejects a tool with zero dynamic parameters.
          expect(keys.length, `node "${n.name}" $fromAI bindings`).toBeGreaterThan(
            0,
          );
          expect(new Set(keys).size, `node "${n.name}" duplicate $fromAI key`).toBe(
            keys.length,
          );
          for (const k of keys) {
            expect(k, `$fromAI key on node "${n.name}"`).toMatch(FROM_AI_KEY);
          }
        }
      });
    });
  }
});
