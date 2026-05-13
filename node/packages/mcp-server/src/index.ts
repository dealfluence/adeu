import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { readFileSync } from "node:fs";
import { basename, resolve, extname, dirname } from "node:path";
import {
  identifyEngine,
  extractTextFromBuffer,
  DocumentObject,
  RedlineEngine,
  BatchValidationError,
  create_unified_diff,
  finalize_document,
} from "@adeu/core";
import {
  build_paginated_response,
  build_outline_response,
  build_appendix_response,
} from "./response-builders.js";

// --- Tool Description Constants (Parity with Python) ---
const READ_DOCX_COMMON_DESC =
  "Reads a DOCX file. Returns text with inline CriticMarkup for Tracked Changes and Comments: {++inserted++}, {--deleted--}, {==highlighted==}{>>comment<<}. Set clean_view=True for the finalized 'Accepted' text without markup.\n\n";
const READ_DOCX_TAIL =
  "Modes:\n- 'full' (default): paginated body content. Use page=N to navigate.\n- 'outline': heading map only — start here for large docs to plan targeted reads. Defaults to L1-L2 headings; pass outline_max_level=3-6 to see deeper structure.\n- 'appendix': defined terms, anchors, and cross-reference targets. Consult before editing legal/technical docs to avoid breaking references.";

const PROCESS_BATCH_COMMON_DESC =
  "Applies a batch of edits and review actions to a DOCX.\n\nAll changes evaluate against the ORIGINAL document state — do not chain dependent edits within one batch (e.g. rename X to Y, then modify Y). Apply the rename first, then send a second batch.\n\n";
const PROCESS_BATCH_OPERATIONS_DESC =
  "Each item in `changes` must specify a `type`:\n1. 'modify': Search-and-replace. `target_text` must uniquely match — include surrounding context if the phrase is ambiguous. `new_text` supports Markdown: '# Heading 1' through '###### Heading 6', '**bold**', '_italic_', and '\\n\\n' to split into multiple paragraphs. Empty `new_text` deletes. Do NOT write CriticMarkup tags ({++, {--, {>>) manually — use the `comment` parameter for comments.\n2. 'accept' / 'reject': Finalize or revert a tracked change by `target_id` (e.g. 'Chg:12').\n3. 'reply': Reply to a comment by `target_id` (e.g. 'Com:5') with `text`.\n4. 'insert_row' / 'delete_row': Table edits. Disk mode only — not supported on Live Word canvas.\n\nID VOLATILITY: 'Chg:N' and 'Com:N' shift between document states. Always call `read_docx` immediately before any accept/reject/reply — do not reuse IDs from earlier in the conversation.\n\n`author_name` is used for attribution on all tracked changes and comments, in both disk and Live Word modes.";

const DIFF_DOCX_DESC =
  "Compares two DOCX files and returns a unified diff of their text content. Useful for analyzing differences between versions before editing.";

// --- Server Setup ---
const server = new Server(
  {
    name: "adeu-redlining-service",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  },
);

// --- Tool Registration ---
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "read_docx",
        description: READ_DOCX_COMMON_DESC + READ_DOCX_TAIL,
        inputSchema: {
          type: "object",
          properties: {
            file_path: {
              type: "string",
              description: "Absolute path to the DOCX file.",
            },
            clean_view: {
              type: "boolean",
              description:
                "If False (default), returns the 'Raw' text with inline CriticMarkup. If True, returns 'Accepted' text.",
              default: false,
            },
            mode: {
              type: "string",
              enum: ["full", "outline", "appendix"],
              description:
                "'full' returns body content. 'outline' returns a structural heading map. 'appendix' returns defined terms.",
              default: "full",
            },
            page: {
              type: "number",
              description:
                "Page number (1-indexed) for mode='full'. Defaults to 1.",
              default: 1,
            },
            outline_max_level: {
              type: "number",
              description: "For mode='outline' only: cap on heading depth.",
              default: 2,
            },
            outline_verbose: {
              type: "boolean",
              description: "For mode='outline' only: includes metadata.",
              default: false,
            },
          },
          required: ["file_path"],
        },
      },
      {
        name: "process_document_batch",
        description: PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC,
        inputSchema: {
          type: "object",
          properties: {
            original_docx_path: {
              type: "string",
              description: "Absolute path to the source file.",
            },
            author_name: {
              type: "string",
              description:
                "Name to appear in Track Changes (e.g., 'Reviewer AI').",
            },
            changes: {
              type: "array",
              description:
                "List of changes to apply. Each change must specify 'type'.",
              items: { type: "object" },
            },
            output_path: {
              type: "string",
              description: "Optional output path.",
            },
          },
          required: ["original_docx_path", "author_name", "changes"],
        },
      },
      {
        name: "accept_all_changes",
        description:
          "Accepts all tracked changes and removes all comments in a single operation, producing a finalized clean document. Use this when a document review is entirely complete and you want to clear all redlines.",
        inputSchema: {
          type: "object",
          properties: {
            docx_path: {
              type: "string",
              description: "Absolute path to the DOCX file.",
            },
            output_path: {
              type: "string",
              description: "Optional output path.",
            },
          },
          required: ["docx_path"],
        },
      },
      {
        name: "diff_docx_files",
        description: DIFF_DOCX_DESC,
        inputSchema: {
          type: "object",
          properties: {
            original_path: {
              type: "string",
              description: "Absolute path to the baseline DOCX file.",
            },
            modified_path: {
              type: "string",
              description: "Absolute path to the modified DOCX file.",
            },
            compare_clean: {
              type: "boolean",
              description:
                "If True, compares 'Accepted' state. If False, compares raw text.",
              default: true,
            },
          },
          required: ["original_path", "modified_path"],
        },
      },
      {
        name: "finalize_document",
        description:
          "Prepares a document for external distribution or e-signature. This tool combines metadata sanitization, document locking (protection), and markup resolution into a single step. NOTE: PDF export and AES encryption are disabled in this environment.",
        inputSchema: {
          type: "object",
          properties: {
            file_path: {
              type: "string",
              description: "Absolute path to the DOCX file.",
            },
            output_path: {
              type: "string",
              description: "Optional output path.",
            },
            sanitize_mode: {
              type: "string",
              enum: ["full", "keep-markup"],
              description:
                "full removes all markup, keep-markup redacts metadata but keeps comments/redlines.",
            },
            accept_all: {
              type: "boolean",
              description:
                "If true, auto-accepts all unresolved track changes before finalizing.",
            },
            protection_mode: {
              type: "string",
              enum: ["read_only", "encrypt"],
              description:
                "Native OOXML document locking. encrypt falls back to read_only in this environment.",
            },
            password: {
              type: "string",
              description: "Ignored in this environment.",
            },
            author: {
              type: "string",
              description:
                "Replace all remaining markup authorship with this name.",
            },
            export_pdf: {
              type: "boolean",
              description: "Ignored in this environment.",
            },
          },
          required: ["file_path"],
        },
      },
    ],
  };
});

// --- Tool Execution ---
server.setRequestHandler(
  CallToolRequestSchema,
  async (request): Promise<any> => {
    const { name, arguments: args } = request.params;

    try {
      if (name === "read_docx") {
        const filePath = args?.file_path as string;
        const cleanView = (args?.clean_view as boolean) ?? false;
        const mode = (args?.mode as string) ?? "full";
        const page = (args?.page as number) ?? 1;
        const outline_max_level = (args?.outline_max_level as number) ?? 2;
        const outline_verbose = (args?.outline_verbose as boolean) ?? false;

        const buf = readFileSync(filePath);
        const text = await extractTextFromBuffer(buf, cleanView);

        if (mode === "outline") {
          const doc = await DocumentObject.load(buf);
          return build_outline_response(
            doc,
            text,
            filePath,
            outline_max_level,
            outline_verbose,
          );
        }
        if (mode === "appendix") {
          return build_appendix_response(text, page, filePath);
        }
        return build_paginated_response(text, page, filePath);
      }
      if (name === "process_document_batch") {
        const origPath = args?.original_docx_path as string;
        const authorName = args?.author_name as string;
        const changes = args?.changes as any[];
        let outPath = args?.output_path as string;

        if (!authorName || !authorName.trim()) {
          return {
            content: [
              { type: "text", text: "Error: author_name cannot be empty." },
            ],
          };
        }

        if (!changes || changes.length === 0) {
          return {
            content: [{ type: "text", text: "Error: No changes provided." }],
          };
        }
        if (!outPath) {
          const ext = extname(origPath);
          const base = basename(origPath, ext);
          const dir = dirname(origPath);
          outPath = resolve(dir, `${base}_processed${ext}`);
        }

        const buf = readFileSync(origPath);
        const doc = await DocumentObject.load(buf);
        const engine = new RedlineEngine(doc, authorName);

        let stats;
        try {
          stats = engine.process_batch(changes);
        } catch (e) {
          if (e instanceof BatchValidationError) {
            return {
              content: [
                {
                  type: "text",
                  text: `Batch rejected. Some edits failed validation:\n\n${(e as BatchValidationError).errors.join("\n\n")}`,
                },
              ],
              isError: true,
            };
          }
          throw e;
        }

        const outBuf = await doc.save();
        // Using dynamic import of fs/promises or just sync write
        const fs = await import("node:fs");
        fs.writeFileSync(outPath, outBuf);

        let res = `Batch complete. Saved to: ${outPath}\nActions: ${stats.actions_applied} applied, ${stats.actions_skipped} skipped.\nEdits: ${stats.edits_applied} applied, ${stats.edits_skipped} skipped.`;
        if (stats.skipped_details?.length > 0) {
          res += `\n\nSkipped Details:\n${stats.skipped_details.join("\n")}`;
        }

        return {
          content: [{ type: "text", text: res }],
        };
      }

      if (name === "accept_all_changes") {
        const docxPath = args?.docx_path as string;
        let outPath = args?.output_path as string;

        if (!outPath) {
          const ext = extname(docxPath);
          const base = basename(docxPath, ext);
          const dir = dirname(docxPath);
          outPath = resolve(dir, `${base}_clean${ext}`);
        }

        const buf = readFileSync(docxPath);
        const doc = await DocumentObject.load(buf);
        const engine = new RedlineEngine(doc);

        // We implement the public facing accept_all wrapper from python
        engine.accept_all_revisions();

        const outBuf = await doc.save();
        const fs = await import("node:fs");
        fs.writeFileSync(outPath, outBuf);

        return {
          content: [
            {
              type: "text",
              text: `Accepted all changes. Saved to: ${outPath}`,
            },
          ],
        };
      }
      if (name === "diff_docx_files") {
        const origPath = args?.original_path as string;
        const modPath = args?.modified_path as string;
        const compareClean = (args?.compare_clean as boolean) ?? true;
        const origBuf = readFileSync(origPath);
        const modBuf = readFileSync(modPath);

        // Pass compareClean flag into extraction
        const origText = await extractTextFromBuffer(origBuf, compareClean);
        const modText = await extractTextFromBuffer(modBuf, compareClean);

        const diff = create_unified_diff(origText, modText);

        return {
          content: [{ type: "text", text: diff || "No differences found." }],
        };
      }

      if (name === "finalize_document") {
        const filePath = args?.file_path as string;
        let outPath = args?.output_path as string;

        if (!outPath) {
          const ext = extname(filePath);
          const base = basename(filePath, ext);
          const dir = dirname(filePath);
          outPath = resolve(dir, `${base}_final${ext}`);
        }

        const buf = readFileSync(filePath);
        const doc = await DocumentObject.load(buf);

        const result = await finalize_document(doc, {
          filename: basename(filePath),
          sanitize_mode: (args?.sanitize_mode as any) || "full",
          accept_all: args?.accept_all as boolean,
          protection_mode: args?.protection_mode as any,
          author: args?.author as string,
          export_pdf: args?.export_pdf as boolean,
        });

        const fs = await import("node:fs");
        fs.writeFileSync(outPath, result.outBuffer!);

        return {
          content: [
            {
              type: "text",
              text: `Saved to: ${outPath}\n\n${result.reportText}`,
            },
          ],
        };
      }

      throw new Error(`Unknown tool: ${name}`);
    } catch (error: any) {
      return {
        content: [
          {
            type: "text",
            text: `Error executing tool ${name}: ${error.message}`,
          },
        ],
        isError: true,
      };
    }
  },
);

// --- Startup ---
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(
    `Adeu MCP Server (Node.js Engine: ${identifyEngine()}) running on stdio`,
  );
}

main().catch(console.error);
