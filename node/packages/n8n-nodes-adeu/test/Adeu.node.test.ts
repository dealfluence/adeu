// FILE: node/packages/n8n-nodes-adeu/test/Adeu.node.test.ts
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
    helpers: {
      prepareBinaryData: vi.fn().mockResolvedValue({
        data: "mock-base64-string",
        mimeType:
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        fileName: "output.docx",
      }),
      getBinaryDataBuffer: vi.fn(),
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
  });

  describe("Operation: Apply Edits", () => {
    let uniqueTarget: string;

    beforeAll(async () => {
      // Discover a unique substring from golden.docx so the test is independent
      // of the fixture's content. Picks the first non-heading line of moderate
      // length that appears exactly once.
      const markdown = await extractTextFromBuffer(goldenBuffer, true);
      const candidates = markdown
        .split("\n")
        .map((l) => l.trim())
        .filter(
          (l) =>
            l.length >= 15 &&
            l.length <= 80 &&
            /[a-zA-Z]/.test(l) &&
            !l.startsWith("#") &&
            !l.startsWith("<!--"),
        );

      for (const candidate of candidates) {
        const escaped = candidate.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const matches = markdown.match(new RegExp(escaped, "g"));
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
      ).mockImplementation((paramName: string) => {
        if (paramName === "resource") return "document";
        if (paramName === "operation") return "applyEdits";
        if (paramName === "binaryPropertyName") return "data";
        if (paramName === "outputBinaryPropertyName") return "data";
        if (paramName === "author") return "n8n AI";
        if (paramName === "editsSource") return "fromInputJson";
        if (paramName === "editsJsonPath") return "changes";
        return undefined;
      });
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
});
