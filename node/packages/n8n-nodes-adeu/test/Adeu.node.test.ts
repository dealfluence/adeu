// FILE: test/Adeu.node.test.ts

import { describe, beforeAll, beforeEach, it, expect, vi } from "vitest";
import type { IExecuteFunctions, INode } from "n8n-workflow";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Static mock — `vi.mock("n8n-workflow", async (importOriginal) => …)` fails
// because n8n-workflow's package.json `exports` field is incompatible with
// Vitest's module resolver. We reconstruct only what our node and
// GenericFunctions actually consume at runtime.
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

// Node import MUST come after vi.mock() in source order. Vitest hoists
// vi.mock() calls to the top of the file, so this ordering is safe.
import { Adeu } from "../nodes/Adeu/Adeu.node";
import { extractTextFromBuffer } from "@adeu/core";

const GOLDEN_FIXTURE = resolve(process.env.ADEU_FIXTURES!, "golden.docx");

function createMockExecuteFunctions(): IExecuteFunctions {
  return {
    getNode: vi.fn().mockReturnValue({
      name: "Adeu",
      type: "n8n-nodes-adeu.adeu",
      typeVersion: 1,
    } as INode),
    continueOnFail: vi.fn().mockReturnValue(false),
    getInputData: vi.fn(),
    getNodeParameter: vi.fn(),
    evaluateExpression: vi.fn(),
    getWorkflowStaticData: vi.fn(),
    helpers: {
      prepareBinaryData: vi.fn().mockResolvedValue({
        data: "mock-base64-string",
        mimeType:
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        fileName: "output.docx",
      }),
      getBinaryDataBuffer: vi.fn(),
      getBinaryStream: vi.fn(),
      binaryToBuffer: vi.fn(),
    },
  } as unknown as IExecuteFunctions;
}
describe("Test Adeu n8n Node", () => {
  let node: Adeu;
  let mockExecuteFunctions: ReturnType<typeof createMockExecuteFunctions>;
  const goldenBuffer = readFileSync(GOLDEN_FIXTURE);

  beforeEach(() => {
    node = new Adeu();
    mockExecuteFunctions = createMockExecuteFunctions();
  });

  describe("Operation: Extract Markdown", () => {
    beforeEach(() => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([
        { json: {}, binary: { data: { fileName: "input.docx" } } },
      ]);
      (
        mockExecuteFunctions.helpers.getBinaryDataBuffer as ReturnType<
          typeof vi.fn
        >
      ).mockResolvedValue(goldenBuffer);
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        return undefined;
      });
    });

    it("should successfully extract markdown and place it in the JSON output", async () => {
      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);

      const item = result[0][0];
      expect(item.json).toHaveProperty("fileName", "input.docx");
      expect(item.json).toHaveProperty("markdown");
      expect(typeof item.json.markdown).toBe("string");
      expect(item.json.markdown).toContain("golden");
    });

    it("should return paginated output with pagination metadata when Page >= 1", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        if (paramName === "page") return 1;
        return fallback;
      });

      const result = await node.execute.call(mockExecuteFunctions);
      const item = result[0][0];

      expect(item.json).toHaveProperty("markdown");
      expect(item.json).toHaveProperty("page", 1);
      expect(item.json).toHaveProperty("total_pages");
      expect(item.json).toHaveProperty("has_next");
      expect(item.json).toHaveProperty("has_prev", false);
      expect(item.json).toHaveProperty("tracked_change_count");
      expect(typeof item.json.total_pages).toBe("number");
      expect((item.json.total_pages as number) >= 1).toBe(true);
    });
    it("should throw when Page exceeds total_pages", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        if (paramName === "page") return 999;
        return fallback;
      });

      await expect(node.execute.call(mockExecuteFunctions)).rejects.toThrow(
        /exceeds total_pages/i,
      );
    });

    it("should omit the structural appendix when includeAppendix is false", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        if (paramName === "includeAppendix") return false;
        return fallback;
      });

      const result = await node.execute.call(mockExecuteFunctions);
      const item = result[0][0];

      expect(item.json).toHaveProperty("markdown");
      expect(typeof item.json.markdown).toBe("string");
      expect(item.json.markdown).not.toContain("READONLY_BOUNDARY_START");
    });
  });
  describe("Operation: Extract Outline", () => {
    beforeEach(() => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([
        { json: {}, binary: { data: { fileName: "input.docx" } } },
      ]);
      (
        mockExecuteFunctions.helpers.getBinaryDataBuffer as ReturnType<
          typeof vi.fn
        >
      ).mockResolvedValue(goldenBuffer);
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractOutline";
        if (paramName === "binaryPropertyName") return "data";
        return fallback;
      });
    });

    it("should return an outline array and total_pages", async () => {
      const result = await node.execute.call(mockExecuteFunctions);
      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);

      const item = result[0][0];
      expect(item.json).toHaveProperty("fileName", "input.docx");
      expect(item.json).toHaveProperty("total_pages");
      expect(item.json).toHaveProperty("outline");
      expect(Array.isArray(item.json.outline)).toBe(true);

      // Every entry conforms to the OutlineNode shape (golden.docx may or may
      // not have headings, but the shape contract holds regardless).
      for (const node of item.json.outline as Array<Record<string, unknown>>) {
        expect(node).toHaveProperty("level");
        expect(node).toHaveProperty("text");
        expect(node).toHaveProperty("page");
        expect(node).toHaveProperty("style");
        expect(node).toHaveProperty("has_table");
        expect(node).toHaveProperty("footnote_ids");
        expect(typeof node.level).toBe("number");
        expect(typeof node.text).toBe("string");
        expect(typeof node.page).toBe("number");
        expect(typeof node.has_table).toBe("boolean");
        expect(Array.isArray(node.footnote_ids)).toBe(true);
      }
    });
  });
  describe("Operation: Apply Edits", () => {
    let uniqueTarget: string;

    beforeAll(async () => {
      // Discover a unique substring from golden.docx so the test is independent
      // of the fixture's content. Picks the first non-heading line of moderate
      // length that appears exactly once, and has no pending tracked changes/comments on it.
      const rawMarkdown = await extractTextFromBuffer(goldenBuffer, false);
      const cleanMarkdown = await extractTextFromBuffer(goldenBuffer, true);

      const cleanChunks = rawMarkdown
        .split(
          /\{--[\s\S]*?--\}|\{\+\+[\s\S]*?\+\+\}|\{>>[\s\S]*?<<\}|\{==[\s\S]*?==\}/g,
        )
        .map((chunk) => chunk.trim())
        .filter((chunk) => chunk.length >= 3 && /[a-zA-Z]/.test(chunk));

      for (const candidate of cleanChunks) {
        const escaped = candidate.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const matches = cleanMarkdown.match(new RegExp(escaped, "g"));
        if (matches && matches.length === 1) {
          uniqueTarget = candidate;
          break;
        }
      }

      if (!uniqueTarget) {
        throw new Error(
          "Could not find a unique target string in golden.docx for testing",
        );
      }
    });

    beforeEach(() => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([
        {
          json: {
            changes: [
              {
                type: "modify",
                target_text: uniqueTarget,
                new_text: "Replaced",
                comment: "Test comment",
              },
            ],
          },
          binary: { data: { fileName: "contract.docx" } },
        },
      ]);
      (
        mockExecuteFunctions.helpers.getBinaryDataBuffer as ReturnType<
          typeof vi.fn
        >
      ).mockResolvedValue(goldenBuffer);
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "applyEdits";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "outputBinaryPropertyName") return "data";
        if (paramName === "author") return "n8n AI";
        if (paramName === "editsSource") return "fromInputJson";
        if (paramName === "editsJsonPath") return "changes";
        return fallback;
      });
    });

    it("should echo reasoning into the output JSON when supplied", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "applyEdits";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "outputBinaryPropertyName") return "data";
        if (paramName === "author") return "n8n AI";
        if (paramName === "editsSource") return "fromInputJson";
        if (paramName === "editsJsonPath") return "changes";
        if (paramName === "reasoning")
          return "Standardizing governing law per playbook.";
        return fallback;
      });

      const result = await node.execute.call(mockExecuteFunctions);
      const item = result[0][0];

      expect(item.json).toHaveProperty(
        "reasoning",
        "Standardizing governing law per playbook.",
      );
    });

    it("should omit reasoning from output JSON when left empty", async () => {
      const result = await node.execute.call(mockExecuteFunctions);
      const item = result[0][0];

      // The block's default getNodeParameter mock returns undefined for
      // "reasoning"; the operation coerces that to "" via its fallback and
      // omits the key.
      expect(item.json).not.toHaveProperty("reasoning");
    });

    it("should successfully apply edits and output binary data", async () => {
      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);

      const item = result[0][0];
      expect(item.json).toHaveProperty("author", "n8n AI");
      expect(item.json).toHaveProperty("stats");
      expect(
        mockExecuteFunctions.helpers.prepareBinaryData as ReturnType<
          typeof vi.fn
        >,
      ).toHaveBeenCalledWith(
        expect.any(Buffer),
        "contract_redlined.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      );
      expect(item.binary).toHaveProperty("data");
    });
    it("should pass match_mode='all' and regex=true through to the engine and return enriched edit report", async () => {
      // Build a changes array exercising match_mode + regex. We use a regex
      // matching any single word followed by 'document' (the golden fixture's
      // body contains 'golden document' after change acceptance, so this is
      // safe and verifies the JSON pass-through reaches the engine.)
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([
        {
          json: {
            changes: [
              {
                type: "modify",
                target_text: "\\bdocument\\b",
                new_text: "doc",
                match_mode: "all",
                regex: true,
              },
            ],
          },
          binary: { data: { fileName: "contract.docx" } },
        },
      ]);

      const result = await node.execute.call(mockExecuteFunctions);
      const item = result[0][0];

      expect(item.json).toHaveProperty("stats");
      const stats = item.json.stats as Record<string, unknown>;
      expect(stats).toHaveProperty("edits");
      const edits = stats.edits as Array<Record<string, unknown>>;
      expect(edits.length).toBe(1);
      // Enriched report must include occurrences_modified and match_mode
      // surfaced from the engine.
      expect(edits[0]).toHaveProperty("status", "applied");
      expect(edits[0]).toHaveProperty("match_mode", "all");
      expect(edits[0]).toHaveProperty("occurrences_modified");
      expect((edits[0].occurrences_modified as number) >= 1).toBe(true);
    });

    it("should run in dry-run mode without producing a redlined binary or stashing static data", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "applyEdits";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "outputBinaryPropertyName") return "data";
        if (paramName === "author") return "n8n AI";
        if (paramName === "editsSource") return "fromInputJson";
        if (paramName === "editsJsonPath") return "changes";
        if (paramName === "returnMarkdown") return false;
        if (paramName === "dryRun") return true;
        return fallback;
      });

      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);

      const item = result[0][0];

      // Dry-run-specific output shape
      expect(item.json).toHaveProperty("dryRun", true);
      expect(item.json).toHaveProperty("stats");
      expect(item.json).not.toHaveProperty("redlinedBinaryId");

      // Stats must contain the per-edit report shape produced by the engine
      const stats = item.json.stats as Record<string, unknown>;
      expect(stats).toHaveProperty("edits");
      expect(Array.isArray(stats.edits)).toBe(true);

      // Critically: no prepareBinaryData call (no redlined binary produced)
      expect(
        mockExecuteFunctions.helpers.prepareBinaryData as ReturnType<
          typeof vi.fn
        >,
      ).not.toHaveBeenCalled();

      // No outgoing binary attached to the new property (the dry-run path
      // passes through the incoming binary bag unchanged; the input fixture
      // sets `data` as a fileName-only stub, so we just assert no fresh
      // prepared binary landed there)
      const outputBinary = item.binary as Record<string, unknown> | undefined;
      // The incoming binary stub has `data: { fileName: "contract.docx" }` —
      // dry-run passes that through verbatim, it should NOT be replaced by
      // a `prepareBinaryData` mock result.
      expect(outputBinary?.data).toEqual({ fileName: "contract.docx" });
    });

    it("should repair changes missing type or carrying match_mode synonyms", async () => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([
        {
          json: {
            changes: [
              {
                // type missing — should be inferred as "modify"
                target_text: uniqueTarget,
                new_text: "Replaced Coerced",
                match_mode: "all-occurrences", // synonym for "all"
              },
            ],
          },
          binary: { data: { fileName: "contract.docx" } },
        },
      ]);

      const result = await node.execute.call(mockExecuteFunctions);
      const item = result[0][0];

      expect(item.json).toHaveProperty("stats");
      const stats = item.json.stats as Record<string, unknown>;
      const edits = stats.edits as Array<Record<string, unknown>>;
      expect(edits[0]).toHaveProperty("status", "applied");
      expect(edits[0]).toHaveProperty("match_mode", "all");
    });
  });

  describe("continueOnFail logic", () => {
    beforeEach(() => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([{ json: {} }]);
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data"; // Missing in binary!
        return undefined;
      });
    });

    it("should throw NodeOperationError if binary data is missing and continueOnFail is false", async () => {
      (
        mockExecuteFunctions.continueOnFail as ReturnType<typeof vi.fn>
      ).mockReturnValue(false);

      await expect(node.execute.call(mockExecuteFunctions)).rejects.toThrow(
        /no binary data found/i,
      );
    });

    it("should continue execution and return error data when continueOnFail is true", async () => {
      (
        mockExecuteFunctions.continueOnFail as ReturnType<typeof vi.fn>
      ).mockReturnValue(true);

      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);
      expect(result[0][0].json).toHaveProperty("error");
      expect((result[0][0].json.error as string).toLowerCase()).toContain(
        "no binary data found",
      );
    });
  });

  describe("Document Source: fromNode (AI Agent tool path)", () => {
    beforeEach(() => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([{ json: {} }]);

      // Mock evaluateExpression to return the leaf IBinaryData object directly
      // (matching what `{{ $('Node').first().binary.data }}` returns under
      // n8n's leaf-property proxy semantics — not the parent .binary bag).
      (
        mockExecuteFunctions.evaluateExpression as ReturnType<typeof vi.fn>
      ).mockReturnValue({
        data: goldenBuffer.toString("base64"),
        mimeType:
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        fileName: "from-trigger.docx",
      });

      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        if (paramName === "documentSource") return "fromNode";
        if (paramName === "sourceNodeName") return "Trigger";
        return fallback;
      });
    });

    it("should resolve binary from a sibling node and extract markdown successfully", async () => {
      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);
      expect(result[0][0].json).toHaveProperty("fileName", "from-trigger.docx");
      expect(result[0][0].json).toHaveProperty("markdown");
      expect(typeof result[0][0].json.markdown).toBe("string");
      expect(
        mockExecuteFunctions.evaluateExpression as ReturnType<typeof vi.fn>,
      ).toHaveBeenCalledWith(
        "{{ $('Trigger').first().binary.data }}",
        expect.any(Number),
      );
    });

    it("should throw a clear NodeApiError when the source node has no output", async () => {
      // Both the binary leaf probe and the `.json` disambiguation probe
      // return undefined — i.e., the source node truly did not execute.
      (
        mockExecuteFunctions.evaluateExpression as ReturnType<typeof vi.fn>
      ).mockReturnValue(undefined);

      await expect(node.execute.call(mockExecuteFunctions)).rejects.toThrow(
        /Source node 'Trigger' has no output/i,
      );
    });

    it("should throw a clear NodeApiError when the binary property is missing on the source node", async () => {
      // Model what n8n returns under leaf-property access when the requested
      // binary property is missing: the binary leaf is undefined, but the
      // node DID run, so the .json disambiguation probe returns an object.
      // The Object.keys(...) probe also returns a list of available binary
      // property names so the error message can hint at alternatives.
      (
        mockExecuteFunctions.evaluateExpression as ReturnType<typeof vi.fn>
      ).mockImplementation((expression: string) => {
        if (expression.includes(".binary.data")) {
          return undefined; // ← requested property is missing
        }
        if (expression.includes(".first().json")) {
          return {}; // ← node ran, so .json resolves to an object
        }
        if (expression.includes("Object.keys")) {
          return ["attachment_0"]; // ← list of present binary properties
        }
        return undefined;
      });

      await expect(node.execute.call(mockExecuteFunctions)).rejects.toThrow(
        /no binary on property 'data'/i,
      );
    });

    it("should throw a clear NodeApiError when Source Node Name is empty", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        if (paramName === "documentSource") return "fromNode";
        if (paramName === "sourceNodeName") return ""; // ← empty
        return fallback;
      });

      await expect(node.execute.call(mockExecuteFunctions)).rejects.toThrow(
        /Source Node Name is required/i,
      );
    });

    it("should resolve binary directly from sourceBinaryId when provided, bypassing expression evaluation", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        if (paramName === "documentSource") return "fromNode";
        if (paramName === "sourceNodeName") return "Trigger";
        if (paramName === "sourceBinaryId")
          return "filesystem-v2:test-stash-id";
        return fallback;
      });

      (
        mockExecuteFunctions.helpers.getBinaryStream as ReturnType<typeof vi.fn>
      ).mockResolvedValue("test-stream");
      (
        mockExecuteFunctions.helpers.binaryToBuffer as ReturnType<typeof vi.fn>
      ).mockResolvedValue(goldenBuffer);

      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);
      expect(result[0][0].json).toHaveProperty("fileName", "document.docx");
      expect(result[0][0].json).toHaveProperty("markdown");
      expect(typeof result[0][0].json.markdown).toBe("string");

      // Verify that evaluateExpression was NEVER called to parse a sibling node,
      // confirming the direct storage bypass is working.
      expect(mockExecuteFunctions.evaluateExpression).not.toHaveBeenCalledWith(
        expect.stringContaining(".binary."),
        expect.any(Number),
      );
      expect(mockExecuteFunctions.helpers.getBinaryStream).toHaveBeenCalledWith(
        "filesystem-v2:test-stash-id",
      );
    });

    it("should throw a clear NodeApiError when loading binary content from sourceBinaryId fails", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "extractMarkdown";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "cleanView") return false;
        if (paramName === "documentSource") return "fromNode";
        if (paramName === "sourceNodeName") return "Trigger";
        if (paramName === "sourceBinaryId") return "filesystem-v2:corrupted-id";
        return fallback;
      });

      (
        mockExecuteFunctions.helpers.getBinaryStream as ReturnType<typeof vi.fn>
      ).mockRejectedValue(new Error("Storage unavailable"));

      await expect(node.execute.call(mockExecuteFunctions)).rejects.toThrow(
        /Failed to load document from Binary ID 'filesystem-v2:corrupted-id'/i,
      );
    });
  });

  describe("Operation: Generate Diff", () => {
    beforeEach(() => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([
        {
          json: {},
          binary: {
            data: { fileName: "orig.docx" },
            data2: { fileName: "mod.docx" },
          },
        },
      ]);
      (
        mockExecuteFunctions.helpers.getBinaryDataBuffer as ReturnType<
          typeof vi.fn
        >
      ).mockResolvedValue(goldenBuffer);
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "generateDiff";
        if (paramName === "originalDocumentSource") return "fromInput";
        if (paramName === "originalBinaryPropertyName") return "data";
        if (paramName === "modifiedDocumentSource") return "fromInput";
        if (paramName === "modifiedBinaryPropertyName") return "data2";
        if (paramName === "cleanView") return true;
        return fallback;
      });
    });

    it("should generate a diff between two documents without leaking the appendix", async () => {
      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);

      const item = result[0][0];
      expect(item.json).toHaveProperty("originalFileName", "orig.docx");
      expect(item.json).toHaveProperty("modifiedFileName", "mod.docx");
      expect(item.json).toHaveProperty("diff");
      expect(typeof item.json.diff).toBe("string");
      expect(item.json.diff).not.toContain("READONLY_BOUNDARY_START");
      expect(item.json.diff).not.toContain("— used ");
    });

    it("should generate a structured changes array when diffFormat is structuredChanges", async () => {
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string, _itemIndex, fallback?) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "generateDiff";
        if (paramName === "originalDocumentSource") return "fromInput";
        if (paramName === "originalBinaryPropertyName") return "data";
        if (paramName === "modifiedDocumentSource") return "fromInput";
        if (paramName === "modifiedBinaryPropertyName") return "data2";
        if (paramName === "cleanView") return true;
        if (paramName === "diffFormat") return "structuredChanges";
        return fallback;
      });

      const result = await node.execute.call(mockExecuteFunctions);
      const item = result[0][0];

      expect(item.json).toHaveProperty("diffFormat", "structuredChanges");
      expect(item.json).toHaveProperty("changes");
      expect(Array.isArray(item.json.changes)).toBe(true);
    });
  });

  describe("Operation: Hydrate Tool Output", () => {
    beforeEach(() => {
      (
        mockExecuteFunctions.getInputData as ReturnType<typeof vi.fn>
      ).mockReturnValue([{ json: {} }]);
      (
        mockExecuteFunctions.getNodeParameter as ReturnType<typeof vi.fn>
      ).mockImplementation((paramName: string) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "hydrateToolOutput";
        if (paramName === "staticDataKey") return "adeu_last_redlined";
        if (paramName === "outputBinaryPropertyName") return "data";
        if (paramName === "onMissing") return "emit_empty";
        if (paramName === "clearAfterRead") return true;
        if (paramName === "outputPathTemplate")
          return "C:\\test\\{baseName}_{timestamp}.docx";
        return undefined;
      });

      (
        mockExecuteFunctions.getWorkflowStaticData as ReturnType<typeof vi.fn>
      ).mockReturnValue({
        adeu_last_redlined: {
          id: "stash-id-123",
          fileName: "contract.docx",
          mimeType:
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          timestamp: 123456789,
        },
      });

      (
        mockExecuteFunctions.helpers.getBinaryStream as ReturnType<typeof vi.fn>
      ).mockResolvedValue("mock-stream");
      (
        mockExecuteFunctions.helpers.binaryToBuffer as ReturnType<typeof vi.fn>
      ).mockResolvedValue(Buffer.from("dummy content"));
    });

    it("should hydrate a stashed tool output and compute the outputPath template correctly", async () => {
      const result = await node.execute.call(mockExecuteFunctions);

      expect(result).toHaveLength(1);
      expect(result[0]).toHaveLength(1);

      const item = result[0][0];
      expect(item.json).toHaveProperty("hydrated", true);
      expect(item.json).toHaveProperty("fileName", "contract.docx");
      expect(item.json).toHaveProperty("outputPath");
      expect(item.json.outputPath).toMatch(
        /^C:\\test\\contract_[0-9-T]+.docx$/,
      );
    });
  });
});
