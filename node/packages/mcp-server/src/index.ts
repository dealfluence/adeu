import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { readFileSync, existsSync } from "node:fs";
import { basename, resolve, extname, dirname, join } from "node:path";
import { z } from "zod";
import {
  registerAppTool as origRegisterAppTool,
  registerAppResource,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import fs from "node:fs";
import {
  identifyEngine,
  _extractTextFromDoc,
  DocumentObject,
  RedlineEngine,
  BatchValidationError,
  create_word_patch_diff,
  collect_media_difference_warnings,
  finalize_document,
} from "@adeu/core";
import { describe_illegal_control_chars } from "@adeu/core";

import {
  build_paginated_response,
  build_full_document_response,
  build_outline_response,
  build_appendix_response,
  build_search_response,
} from "./response-builders.js";

import { MARKDOWN_UI_URI, handleServerCliArgs } from "./shared.js";
// Parity with Python models.py `_infer_type_in_place` + `_coerce_match_mode_in_place`.
// The MCP boundary schema is permissive; these repairs let recoverable payloads
// (a missing `type` that's unambiguous from the key signature, or a non-canonical
// `match_mode`) succeed instead of failing the whole-array Zod parse with an
// opaque -32602. Anything still un-inferrable is caught by the handler guard
// below and reported per-index; anything that doesn't apply to the document is
// caught by the engine's validate_edits. Mirrors how Python repairs in a
// BeforeValidator ahead of its (strict) discriminated union.
const MATCH_MODE_SYNONYMS: Record<string, "strict" | "first" | "all"> = {
  strict: "strict",
  first: "first",
  all: "all",
  first_only: "first",
  firstonly: "first",
  "first-only": "first",
  all_occurrences: "all",
  alloccurrences: "all",
  "all-occurrences": "all",
  every: "all",
};

function coerceChangeItemInPlace(item: any): void {
  if (item === null || typeof item !== "object" || Array.isArray(item)) return;

  // Infer a missing `type` ONLY when exactly one variant fits unambiguously.
  // Deliberately do NOT infer from `target_id` alone (accept vs reject is a
  // semantic choice) or `target_text` alone (delete_row vs empty-new_text
  // modify). Those stay absent and are rejected with a clear message.
  if (!("type" in item) || item.type === undefined || item.type === null) {
    if ("cells" in item) item.type = "insert_row";
    else if ("text" in item && "target_id" in item) item.type = "reply";
    else if ("target_text" in item && "new_text" in item) item.type = "modify";
  }

  // Normalize match_mode: canonical passes through, synonyms map, anything else
  // (help-string echo "strict, first, or all", empty, non-string) is dropped so
  // the engine's "strict" default applies. Never coerce junk to "all" — that
  // would silently mass-edit; defaulting to strict fails safe with an
  // ambiguity error instead.
  if ("match_mode" in item) {
    const raw = item.match_mode;
    if (typeof raw !== "string") {
      delete item.match_mode;
    } else {
      const mapped = MATCH_MODE_SYNONYMS[raw.trim().toLowerCase()];
      if (mapped === undefined) delete item.match_mode;
      else item.match_mode = mapped;
    }
  }
}
function readFileBytesOrThrow(filePath: string): Buffer {
  try {
    return readFileSync(filePath);
  } catch (err: any) {
    if (err.code === "ENOENT") {
      // Lean, agent-appropriate error: list sibling .docx files so the model
      // can self-correct a wrong filename (e.g. a guessed `-processed` suffix)
      // in one turn, instead of being handed CLI install instructions that are
      // irrelevant inside an agent loop and pure token waste.
      let available = "";
      try {
        const dir = dirname(filePath);
        const docs = fs
          .readdirSync(dir)
          .filter((f) => f.toLowerCase().endsWith(".docx"));
        available = docs.length
          ? ` available files: [${docs.join(", ")}]`
          : ` (no .docx files found in ${dir})`;
      } catch {
        // Directory unreadable — omit the listing rather than fail.
      }
      // Echo the path AS GIVEN — dropping the directory ("file not found:
      // alice_copy.docx" for qa_sandbox/alice_copy.docx) hid which path was
      // actually tried, and relative paths resolve against the server's cwd,
      // not the caller's (QA 2026-07-23 F16).
      throw new Error(
        `file not found: ${filePath};${available} Provide an absolute path — the server cannot resolve relative paths.`,
      );
    }
    throw err;
  }
}

/**
 * Overwrite disclosure for every tool that writes a document (QA 2026-07-23
 * F17): repeated default-named runs clobber <name>_processed.docx and
 * output_path == input silently replaces the source. `existedBefore` must be
 * captured BEFORE the write. Returns "" when nothing pre-existed.
 */
function overwriteNote(
  outPath: string,
  inputPath: string,
  existedBefore: boolean,
): string {
  if (!existedBefore) return "";
  if (resolve(outPath) === resolve(inputPath)) {
    return `\nNote: the source document at ${outPath} was overwritten in place.`;
  }
  return `\nNote: replaced existing file ${outPath}.`;
}

/**
 * Loads a DOCX, translating low-level container errors (fflate's bare
 * "invalid zip data" for a text file named .docx, truncated archives, XML
 * soup) into a diagnosis the caller can act on (QA 2026-07-23 F19).
 */
async function loadDocxOrThrow(
  buf: Buffer,
  filePath: string,
): Promise<DocumentObject> {
  try {
    return await DocumentObject.load(buf);
  } catch (err: any) {
    throw new Error(
      `'${filePath}' is not a valid .docx (Word) document: ${err?.message ?? err}`,
    );
  }
}

// --- Asset Loaders for UI ---
const DIST_DIR = import.meta.dirname;

function getAssetContent(
  folder: "templates" | "assets",
  filename: string,
  fallbackMessage: string,
): string {
  const filePath = join(DIST_DIR, folder, filename);
  if (existsSync(filePath)) {
    return readFileSync(filePath, "utf-8");
  }
  return fallbackMessage;
}

// --- Tool Description Constants ---
const READ_DOCX_COMMON_DESC =
  "Reads a DOCX file. Returns text with inline CriticMarkup for Tracked Changes and Comments: {++inserted++}, {--deleted--}, {==highlighted==}{>>comment<<}. Set clean_view=True for the finalized 'Accepted' text without markup.\n\n";
// `page` guidance lives HERE, not only on the parameter: real MCP clients
// drop optional-parameter descriptions in transit, so the tool description is
// the only channel guaranteed to reach the model (QA 2026-07-23 client-compat).
const READ_DOCX_TAIL =
  "Modes:\n- 'full' (default): paginated body content. Use page=N to navigate.\n- 'outline': heading map only — start here for large docs to plan targeted reads. Defaults to L1-L2 headings; pass outline_max_level=3-6 to see deeper structure.\n- 'appendix': defined terms, anchors, and cross-reference targets. Consult before editing legal/technical docs to avoid breaking references.\n\n`page`: a positive integer (1-indexed, default 1) or 'all'. Pages are synthetic length-based chunks sized for LLM consumption, NOT printed Word pages. In mode='full', page='all' returns the whole body with no page chrome. With `search_query`, `page` instead restricts matches to that page (default: search all pages).";

// BUDGET: real MCP clients truncate tool descriptions at ~2048 chars — the
// tail (wherever it falls) is invisible to the model. COMMON + OPERATIONS +
// the appended build tag must stay under that ceiling (QA 2026-07-23
// client-compat test 1). The {#cell:} stability claim must be qualified in
// the SAME paragraph (finalize/sanitize regenerates the anchors, QA F9), and
// the row-op fields (`cells` etc.) must be named in prose because clients
// strip the typed item schema to {} in transit (QA F10).
const PROCESS_BATCH_COMMON_DESC =
  "Applies a batch of edits and review actions to a DOCX.\n\nBatches apply SEQUENTIALLY: each change is validated and applied against the document state produced by the changes before it, so a later change may target text an earlier one introduced. Any validation failure rejects the whole batch transactionally — nothing is applied.\n\n";
const PROCESS_BATCH_OPERATIONS_DESC =
  "Each item in `changes` needs a `type`:\n1. 'modify': search-and-replace. `target_text` must match uniquely (`match_mode`:'strict', the default) — add surrounding context, or set `match_mode`:'first'/'all'. Set `regex`:true to treat `target_text` as a regex (capture groups in `new_text` as $1, $2…). `new_text` supports Markdown: '#'–'######' headings, '**bold**', '_italic_', '\\n\\n' paragraph split; empty `new_text` deletes. Never write CriticMarkup ({++, {--, {>>) manually — use the `comment` field.\n   • EMPTY CELLS: a blank table cell has no text to match; `read_docx` renders every cell with a trailing `{#cell:<id>}` anchor — set `target_text` to that exact anchor and put the value in `new_text`. The pipes are display separators, not editable text.\n2. 'accept'/'reject': finalize or revert a tracked change by `target_id` (e.g. 'Chg:12').\n3. 'reply': reply to a comment by `target_id` (e.g. 'Com:5') with `text`.\n4. 'insert_row': add a table row — `target_text` anchors on an existing row's text, `cells` holds the new row's cell values (strings, left to right), `position` is 'above'/'below' (default below). 'delete_row': remove the row matching `target_text`. Disk mode only.\n\nID VOLATILITY: 'Chg:N'/'Com:N' ids shift between document states — always call `read_docx` immediately before accept/reject/reply; never reuse ids from earlier turns. `{#cell:<id>}` anchors are stable across reads and edits, but finalize_document/sanitize regenerates them — re-read after finalizing.\n\n`author_name` sets Track Changes attribution; it defaults to 'Adeu AI (TS)' when omitted.";

const DIFF_DOCX_DESC =
  "Compares two DOCX files and returns a compact `@@ Word Patch @@` diff — Adeu's token-level, sub-word patch format — of their text content. Useful for analyzing differences between versions before editing.";

const gitSha = process.env.GIT_SHA || "unknown";
const packageVersion = process.env.PACKAGE_VERSION || "unknown";
const buildTag = ` [Adeu v${packageVersion}+${gitSha}]`;

// --- Server Setup ---
const server = new McpServer({
  name: "adeu-redlining-service",
  version: packageVersion,
});

// Wrap server.registerTool to inject buildTag into descriptions
const originalRegisterTool = server.registerTool.bind(server);
server.registerTool = (name: string, schema: any, handler?: any) => {
  if (schema && typeof schema === "object") {
    // Idempotent: UI tools route through BOTH this wrapper and the
    // registerAppTool wrapper, so guard against stamping the tag twice.
    if (schema.description && !schema.description.includes(buildTag.trim())) {
      schema.description = schema.description.trim() + buildTag;
    }
  }
  return originalRegisterTool(name, schema, handler);
};

// Wrap registerAppTool to inject buildTag into descriptions
const registerAppTool: typeof origRegisterAppTool = (
  mcpServer,
  name,
  schema,
  handler,
) => {
  if (schema && typeof schema === "object") {
    if (schema.description) {
      schema.description = schema.description.trim() + buildTag;
    }
  }
  return origRegisterAppTool(mcpServer, name, schema, handler);
};

// Common CSP allowing Google Fonts used by Adeu UI templates
const UI_CSP = {
  connectDomains: ["https://fonts.googleapis.com", "https://fonts.gstatic.com"],
  resourceDomains: [
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
  ],
};

// ==========================================
// 1. UI RESOURCES
// ==========================================

registerAppResource(
  server,
  MARKDOWN_UI_URI,
  MARKDOWN_UI_URI,
  { mimeType: RESOURCE_MIME_TYPE, description: "Adeu Markdown Viewer UI" },
  async () => {
    let html = getAssetContent(
      "templates",
      "markdown_ui.html",
      "<html><body>UI Template Not Found</body></html>",
    );
    const markedJs = getAssetContent(
      "assets",
      "marked.min.js",
      "window.__MARKED_ERROR = 'marked.min.js not found';",
    );
    const svg = getAssetContent("assets", "adeu.svg", "");

    html = html
      .replace("[[marked_js_code | safe]]", markedJs)
      .replace("[[ adeu_svg_code ]]", svg);

    return {
      contents: [
        {
          uri: MARKDOWN_UI_URI,
          mimeType: RESOURCE_MIME_TYPE,
          text: html,
          _meta: { ui: { csp: UI_CSP } },
        },
      ],
    };
  },
);

// ==========================================
// 2. UI-ENABLED TOOLS
// ==========================================
registerAppTool(
  server,
  "read_docx",
  {
    title: "Read DOCX",
    description: READ_DOCX_COMMON_DESC + READ_DOCX_TAIL,
    inputSchema: z.object({
      reasoning: z
        .string()
        .describe(
          "Why do I need to read this docx document? State this reason before any other parameter.",
        ),
      file_path: z.string().describe("Absolute path to the DOCX file."),
      clean_view: z
        .boolean()
        .default(false)
        .describe(
          "If False (default), returns the 'Raw' text with inline CriticMarkup. If True, returns 'Accepted' text.",
        ),
      mode: z
        .enum(["full", "outline", "appendix"])
        .default("full")
        .describe(
          "'full' returns body content. 'outline' returns a structural heading map. 'appendix' returns defined terms.",
        ),
      // ONE published JSON type (string) — real MCP clients strip
      // property-level anyOf/oneOf to {}, losing the type and docs entirely
      // (QA 2026-07-23 client-compat test 2). Numbers still arrive at
      // runtime and are coerced to their string form before validation; the
      // operative guidance lives in the tool description because clients
      // also drop optional-parameter descriptions.
      page: z
        .preprocess(
          (v) =>
            typeof v === "number" && Number.isFinite(v) ? String(v) : v,
          z
            .string()
            .describe(
              "Positive integer (1-indexed, defaults to 1) or 'all'. See the tool description for the full behavior per mode.",
            ),
        )
        .optional(),
      outline_max_level: z.coerce
        .number()
        .default(2)
        .describe("For mode='outline' only: cap on heading depth."),
      outline_verbose: z
        .boolean()
        .default(false)
        .describe("For mode='outline' only: includes metadata."),
      search_query: z
        .string()
        .optional()
        .describe(
          "The substring or regex pattern to search for. When provided, filters results to matching paragraphs.",
        ),
      search_regex: z
        .boolean()
        .default(false)
        .describe(
          "Set to true to interpret search_query as a regular expression.",
        ),
      search_case_sensitive: z
        .boolean()
        .default(true)
        .describe("Set to false to perform case-insensitive matching."),
    }),
    _meta: { ui: { resourceUri: MARKDOWN_UI_URI } },
  },
  async ({
    reasoning,
    file_path,
    clean_view,
    mode,
    page,
    outline_max_level,
    outline_verbose,
    search_query,
    search_regex,
    search_case_sensitive,
  }) => {
    try {
      void reasoning;
      const buf = readFileBytesOrThrow(file_path);

      if (mode === "outline") {
        const doc = await loadDocxOrThrow(buf, file_path);
        const extract_res = _extractTextFromDoc(
          doc,
          clean_view,
          true,
          true,
        ) as {
          text: string;
          paragraph_offsets: Map<any, [number, number]>;
        };
        const res = build_outline_response(
          doc,
          extract_res.text,
          file_path,
          outline_max_level,
          outline_verbose,
          extract_res.paragraph_offsets,
        );
        return res as any;
      }

      const readDoc = await loadDocxOrThrow(buf, file_path);
      const text = _extractTextFromDoc(readDoc, clean_view) as string;
      if (search_query !== undefined && search_query !== null) {
        // In search mode, undefined `page` means "search all document pages".
        const res = build_search_response(
          text,
          search_query,
          search_regex,
          search_case_sensitive,
          page,
          file_path,
        );
        return res as any;
      }
      // In full mode, page='all' returns the entire document without page
      // chrome — the round-trip artifact for text-based apply/diff
      // (QA 2026-07-17 F1 parity with the Python CLI's --page all).
      if (
        mode === "full" &&
        page !== undefined &&
        page !== null &&
        String(page).trim().toLowerCase() === "all"
      ) {
        const res = build_full_document_response(text, file_path);
        return res as any;
      }
      // In non-search mode, `page` defaults to 1 (show document page 1).
      // Non-numeric values must error, not silently fall back to page 1
      // (QA L1 parity with the Python CLI).
      let resolvedPage = 1;
      if (page !== undefined && page !== null) {
        const parsed =
          typeof page === "number" ? page : parseInt(String(page).trim(), 10);
        if (!Number.isFinite(parsed)) {
          return {
            isError: true,
            content: [
              {
                type: "text",
                text:
                  `Invalid page value: '${page}'. Provide a positive integer ` +
                  `(pages are 1-indexed; 'all' is valid for mode='full' and together with search_query).`,
              },
            ],
          };
        }
        resolvedPage = parsed;
      }
      if (mode === "appendix") {
        const res = build_appendix_response(text, resolvedPage, file_path);
        return res as any;
      }
      const res = build_paginated_response(text, resolvedPage, file_path);
      return res as any;
    } catch (e: any) {
      return {
        isError: true,
        content: [
          {
            type: "text",
            text: `Error executing tool read_docx: ${e.message}`,
          },
        ],
      };
    }
  },
);

// ==========================================
// 3. HEADLESS TOOLS (No UI)
// ==========================================

// Typed shape for a single `process_document_batch` change. This makes the six
// DocumentChange variants — and the modify-only `match_mode`/`regex` options —
// discoverable from the tool schema itself, instead of prose alone. A bare
// string is still accepted (and normalized in-handler) so double-serialized
// payloads from some LLM clients keep working; only `type` is required, all
// other fields are optional, and unknown keys pass through untouched.
const CHANGE_ITEM_SCHEMA = z
  .object({
    type: z
      .enum(["modify", "accept", "reject", "reply", "insert_row", "delete_row"])
      .optional()
      .describe(
        "Change kind: 'modify' (search-and-replace), 'accept'/'reject' (resolve a tracked change by id), 'reply' (reply to a comment by id), 'insert_row'/'delete_row' (table edits; disk mode only). If omitted it is inferred when unambiguous from the other fields.",
      ),
    target_text: z
      .string()
      .optional()
      .describe(
        "modify / insert_row / delete_row: the existing text to locate (interpreted as a regex when regex=true).",
      ),
    new_text: z
      .string()
      .optional()
      .describe(
        "modify: replacement text. Supports Markdown (headings, **bold**, _italic_, '\\n\\n' paragraph splits); empty string deletes. Regex capture groups are available as $1, $2…",
      ),
    target_id: z
      .string()
      .optional()
      .describe(
        "accept / reject / reply: the 'Chg:N' or 'Com:N' id taken from a fresh read_docx.",
      ),
    text: z.string().optional().describe("reply: the reply body."),
    comment: z
      .string()
      .optional()
      .describe(
        "modify / accept / reject: attach a margin comment to the change (no manual CriticMarkup).",
      ),
    match_mode: z
      .enum(["strict", "first", "all"])
      .optional()
      .describe(
        "modify only: 'strict' (default — target must match uniquely), 'first' (first occurrence), or 'all' (every occurrence).",
      ),
    regex: z
      .boolean()
      .optional()
      .describe(
        "modify only: treat target_text as a regular expression (default false).",
      ),
    position: z
      .enum(["above", "below"])
      .optional()
      .describe(
        "insert_row: place the new row above or below the matched row.",
      ),
    cells: z
      .array(z.string())
      .optional()
      .describe("insert_row: the cell values for the new row, left to right."),
  })
  .passthrough();

// Per-item repair at the schema boundary (AI_CONTEXT §6 "ONE changes
// Parameter"): a stringified ITEM (the double-serialize client quirk) is
// parsed back to its object, and recoverable payloads (missing `type`,
// match_mode synonyms) are coerced BEFORE the typed item schema validates —
// so repairs that used to happen only in the handler now survive the typed
// enum checks too. UNPARSEABLE strings are smuggled through the object schema
// under a marker key and unwrapped back to the raw string in the handler, so
// the ENGINE remains the single authority for their error ("Invalid change
// format… received a primitive string"). z.preprocess at the ITEM level
// publishes the inner OBJECT schema — no anyOf, which real clients strip to
// {} (QA 2026-07-23 client-compat test 2) — while `changes` itself stays a
// plain REQUIRED array: a WHOLLY stringified payload still fails with a
// retryable "expected array, received string".
const RAW_STRING_ITEM_KEY = "__adeu_unparseable_item";
const CHANGE_ITEM_WITH_REPAIR = z.preprocess((item) => {
  let obj: any = item;
  if (typeof item === "string") {
    try {
      const parsed = JSON.parse(item);
      obj =
        parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
          ? parsed
          : { [RAW_STRING_ITEM_KEY]: item };
    } catch {
      obj = { [RAW_STRING_ITEM_KEY]: item };
    }
  }
  if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) {
    coerceChangeItemInPlace(obj);
  }
  return obj;
}, CHANGE_ITEM_SCHEMA);

server.registerTool(
  "process_document_batch",
  {
    description: PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC,
    inputSchema: {
      reasoning: z
        .string()
        .describe(
          "Why do I need to apply these changes to the document? State this reason before any other parameter.",
        ),
      original_docx_path: z
        .string()
        .describe("Absolute path to the source file."),
      // Defaulted, not required: real MCP clients drop primitive-typed
      // entries from required[] anyway, so schema-following models
      // legitimately omit author_name — the call must succeed cleanly
      // instead of dumping raw Zod issues (QA 2026-07-23 F3/client-compat).
      // The default matches the engine's own.
      author_name: z
        .string()
        .default("Adeu AI (TS)")
        .describe(
          "Name to appear in Track Changes (e.g., 'Reviewer AI'). Defaults to 'Adeu AI (TS)' when omitted.",
        ),
      // Deliberately a plain REQUIRED array of typed items. Wrapping the
      // ARRAY in z.preprocess (to also accept the whole array as one JSON
      // string) drops it out of the schema's `required` list, and a z.union
      // publishes an anyOf that real clients strip to {} — so per-item
      // tolerance lives inside CHANGE_ITEM_WITH_REPAIR's item-level
      // preprocess instead. A wholly stringified payload gets a clear
      // "expected array, received string" it can retry from.
      changes: z
        .array(CHANGE_ITEM_WITH_REPAIR)
        .describe(
          "Ordered list of changes to apply. Each item is an object carrying a `type` discriminator plus that type's fields (see the per-field docs and the tool description). Items apply SEQUENTIALLY: each one evaluates against the document state produced by the items before it, so later items may target text an earlier item introduced.",
        ),
      output_path: z.string().optional().describe("Optional output path."),
      dry_run: z
        .boolean()
        .optional()
        .default(false)
        .describe(
          "If True, simulates the changes and returns a detailed preview report without modifying any files.",
        ),
    },
  },
  async ({
    reasoning,
    original_docx_path,
    author_name,
    changes,
    output_path,
    dry_run,
  }) => {
    try {
      void reasoning;
      if (!author_name || !author_name.trim())
        return {
          content: [
            { type: "text", text: "Error: author_name cannot be empty." },
          ],
        };
      const author_ctrl = describe_illegal_control_chars(author_name);
      if (author_ctrl)
        return {
          content: [
            {
              type: "text",
              text:
                `Error: author_name contains control character(s) (${author_ctrl}) ` +
                `that cannot be stored in a DOCX. Remove them and retry.`,
            },
          ],
        };

      if (!changes || changes.length === 0)
        return {
          content: [{ type: "text", text: "Error: No changes provided." }],
        };

      // Defensive sanitization at the MCP boundary: some LLM clients
      // "double-serialize" nested arrays, delivering each element of `changes`
      // as a JSON string instead of an object. CHANGE_ITEM_WITH_REPAIR's
      // preprocess already repaired what it could; this second pass keeps the
      // tool layer safe regardless of the schema wiring, and unwraps the
      // marker objects the preprocess used to smuggle UNPARSEABLE strings
      // through the typed item schema — the engine's validate_edits stays the
      // single authority for their error message.
      const sanitizedChanges = changes.map((item: any) => {
        let obj: any = item;
        if (
          obj !== null &&
          typeof obj === "object" &&
          !Array.isArray(obj) &&
          RAW_STRING_ITEM_KEY in obj
        ) {
          return obj[RAW_STRING_ITEM_KEY];
        }
        if (typeof item === "string") {
          try {
            const parsed = JSON.parse(item);
            obj = parsed !== null && typeof parsed === "object" ? parsed : item;
          } catch {
            obj = item;
          }
        }
        // Repair recoverable payloads (infer type, normalize match_mode) the
        // same way Python does before its union validation.
        if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) {
          coerceChangeItemInPlace(obj);
        }
        return obj;
      });

      // Boundary guard, scoped narrowly: after inference, reject only an OBJECT
      // that still carries no resolvable `type`. Strings, nulls, and non-objects
      // are intentionally left for the engine's validate_edits to report
      // ("Invalid change format… received a primitive"), keeping the engine the
      // single authority for those and avoiding a competing error surface.
      // A typeless object is the one case the engine can't cleanly reject (with
      // `type` now optional it would fall into the edits bucket as a no-op), so
      // it is caught here with an actionable, per-index message.
      const VALID_TYPES = new Set([
        "modify",
        "accept",
        "reject",
        "reply",
        "insert_row",
        "delete_row",
      ]);
      const typeErrors: string[] = [];
      sanitizedChanges.forEach((c: any, i: number) => {
        if (
          c !== null &&
          typeof c === "object" &&
          !Array.isArray(c) &&
          (!c.type || !VALID_TYPES.has(c.type))
        ) {
          typeErrors.push(
            `- Change ${i + 1}: missing or unrecognized "type". Use one of: modify (needs target_text + new_text), accept/reject (needs target_id like "Chg:12"), reply (needs target_id like "Com:5" + text), insert_row (needs target_text + cells), delete_row (needs target_text). Received keys: [${Object.keys(c).join(", ")}].`,
          );
        }
      });
      if (typeErrors.length > 0) {
        return {
          isError: true,
          content: [
            {
              type: "text",
              text: `Batch rejected. Some changes are malformed:\n\n${typeErrors.join("\n")}`,
            },
          ],
        };
      }

      let outPath = output_path;
      if (!outPath) {
        const ext = extname(original_docx_path);
        const base = basename(original_docx_path, ext);
        const dir = dirname(original_docx_path);
        // Idempotency guard (parity with Python document.py): if the input is
        // already a processed artifact, write back to it instead of compounding
        // the suffix into contract_processed_processed.docx, which fragments the
        // agent's document state across files.
        if (base.endsWith("_processed") || base.endsWith("_redlined")) {
          outPath = resolve(dir, `${base}${ext}`);
        } else {
          outPath = resolve(dir, `${base}_processed${ext}`);
        }
      }

      const buf = readFileBytesOrThrow(original_docx_path);
      const doc = await loadDocxOrThrow(buf, original_docx_path);
      const engine = new RedlineEngine(doc, author_name);

      let stats;
      try {
        stats = engine.process_batch(sanitizedChanges, dry_run);
      } catch (e: any) {
        if (e instanceof BatchValidationError) {
          return {
            isError: true,
            content: [
              {
                type: "text",
                text: `Batch rejected. Some edits failed validation:\n\n${e.errors.join("\n\n")}`,
              },
            ],
          };
        }
        throw e;
      }

      let overwrite_note = "";
      if (!dry_run) {
        const existedBefore = fs.existsSync(outPath);
        const outBuf = await doc.save();
        try {
          fs.writeFileSync(outPath, outBuf);
        } catch (e: any) {
          // Filesystem failures (name too long, missing directory, perms)
          // must surface as a clear, actionable error (QA H3 parity).
          return {
            isError: true,
            content: [
              {
                type: "text",
                text: `Could not write output file '${outPath}': ${e.message}`,
              },
            ],
          };
        }
        overwrite_note = overwriteNote(outPath, original_docx_path, existedBefore);
      }

      let res = formatBatchResult(stats, outPath, !!dry_run) + overwrite_note;
      if (sanitizedChanges.length === 0) {
        res =
          `⚠️ 0 changes provided — nothing to do. The output is an unmodified copy of the original.\n\n` +
          res;
      }
      return { content: [{ type: "text", text: res }] };
    } catch (e: any) {
      return {
        isError: true,
        content: [{ type: "text", text: `Error: ${e.message}` }],
      };
    }
  },
);

server.registerTool(
  "accept_all_changes",
  {
    description:
      "Accepts all tracked changes and removes all comments in a single operation.",
    inputSchema: {
      reasoning: z
        .string()
        .describe(
          "Why do I need to accept all changes in this document? State this reason before any other parameter.",
        ),
      docx_path: z.string().describe("Absolute path to the DOCX file."),
      output_path: z.string().optional().describe("Optional output path."),
    },
  },
  async ({ reasoning, docx_path, output_path }) => {
    try {
      void reasoning;
      let outPath = output_path;
      if (!outPath) {
        const ext = extname(docx_path);
        const base = basename(docx_path, ext);
        const dir = dirname(docx_path);
        outPath = resolve(dir, `${base}_clean${ext}`);
      }

      const buf = readFileBytesOrThrow(docx_path);
      const doc = await loadDocxOrThrow(buf, docx_path);
      const engine = new RedlineEngine(doc);

      // Revision-mark counts straight from the engine (AI_CONTEXT
      // "Accept-All Counts Are Revision MARKS"): a no-op must say so instead
      // of claiming "Accepted all changes" over an already-clean document
      // (QA 2026-07-23 F18).
      const counts = engine.accept_all_revisions();
      const total =
        counts.accepted_insertions +
        counts.accepted_deletions +
        counts.accepted_formatting +
        counts.removed_comments;

      const existedBefore = fs.existsSync(outPath);
      const outBuf = await doc.save();

      fs.writeFileSync(outPath, outBuf);

      let text: string;
      if (total === 0) {
        text = `No tracked changes or comments to accept — the document is already clean. Saved to: ${outPath}`;
      } else {
        text =
          `Accepted all changes. Saved to: ${outPath}\n` +
          `Accepted: ${counts.accepted_insertions} insertion(s), ` +
          `${counts.accepted_deletions} deletion(s), ` +
          `${counts.accepted_formatting} formatting change(s).`;
        if (counts.removed_comments > 0) {
          text += `\nComments removed: ${counts.removed_comments}.`;
        }
      }
      text += overwriteNote(outPath, docx_path, existedBefore);

      return {
        content: [{ type: "text", text }],
      };
    } catch (e: any) {
      return {
        isError: true,
        content: [{ type: "text", text: `Error: ${e.message}` }],
      };
    }
  },
);

server.registerTool(
  "diff_docx_files",
  {
    description: DIFF_DOCX_DESC,
    inputSchema: {
      reasoning: z
        .string()
        .describe(
          "Why do I need to diff these two documents? State this reason before any other parameter.",
        ),
      original_path: z
        .string()
        .describe("Absolute path to the baseline DOCX file."),
      modified_path: z
        .string()
        .describe("Absolute path to the modified DOCX file."),
      compare_clean: z
        .boolean()
        .default(true)
        .describe(
          "If True, compares 'Accepted' state. If False, compares raw text.",
        ),
    },
  },
  async ({ reasoning, original_path, modified_path, compare_clean }) => {
    try {
      void reasoning;
      const origBuf = readFileBytesOrThrow(original_path);
      const modBuf = readFileBytesOrThrow(modified_path);

      // includeAppendix=false: the generated appendix ("used N times",
      // diagnostics) is not document content — diffing it produces phantom
      // changes no apply can consume (QA 2026-07-18 H1).
      const origDoc = await loadDocxOrThrow(origBuf, original_path);
      const modDoc = await loadDocxOrThrow(modBuf, modified_path);
      const origText = _extractTextFromDoc(origDoc, compare_clean, false) as string;
      const modText = _extractTextFromDoc(modDoc, compare_clean, false) as string;

      let diff = create_word_patch_diff(
        origText,
        modText,
        basename(original_path),
        basename(modified_path),
      );

      // Identical documents used to yield ONLY the two header lines, leaving
      // the caller to infer that nothing differs (QA 2026-07-23 F14). Mirrors
      // the Python MCP wording.
      if (!diff.includes("@@ Word Patch @@")) {
        diff =
          `--- ${basename(original_path)}\n+++ ${basename(modified_path)}\n\n` +
          "No textual differences found between the documents.";
      }

      // A text diff cannot see image bytes: when embedded media differ, an
      // empty diff must never read as "the documents are identical"
      // (QA 2026-07-19 F-04).
      const media_warnings = collect_media_difference_warnings(
        new Uint8Array(origBuf),
        new Uint8Array(modBuf),
      );
      const warning_text = media_warnings.length
        ? media_warnings.map((w) => `⚠️  ${w}`).join("\n") + "\n\n"
        : "";

      return {
        content: [
          {
            type: "text",
            text: warning_text + diff,
          },
        ],
      };
    } catch (e: any) {
      return {
        isError: true,
        content: [{ type: "text", text: `Error: ${e.message}` }],
      };
    }
  },
);

server.registerTool(
  "finalize_document",
  {
    description:
      "Prepares a document for external distribution or e-signature. Note: in this zero-dependency environment, protection_mode='encrypt' is unsupported and falls back to a native read-only lock; export_pdf and password are ignored.",
    inputSchema: {
      reasoning: z
        .string()
        .describe(
          "Why do I need to finalize this document? State this reason before any other parameter.",
        ),
      file_path: z.string().describe("Absolute path to the DOCX file."),
      output_path: z.string().optional().describe("Optional output path."),
      sanitize_mode: z
        .enum(["full", "keep-markup"])
        .optional()
        .describe("full removes all markup, keep-markup redacts metadata."),
      accept_all: z
        .boolean()
        .optional()
        .describe(
          "If true, auto-accepts all unresolved track changes before finalizing.",
        ),
      protection_mode: z
        .enum(["read_only", "encrypt"])
        .optional()
        .describe(
          "Native OOXML document locking. Note: 'encrypt' is unsupported in this zero-dependency build and falls back to 'read_only'.",
        ),
      password: z.string().optional().describe("Ignored in this environment."),
      author: z
        .string()
        .optional()
        .describe("Replace all remaining markup authorship with this name."),
      export_pdf: z
        .boolean()
        .optional()
        .describe("Ignored in this environment."),
    },
  },
  async ({
    reasoning,
    file_path,
    output_path,
    sanitize_mode,
    accept_all,
    protection_mode,
    author,
    export_pdf,
  }) => {
    try {
      void reasoning;
      let outPath = output_path;
      if (!outPath) {
        const ext = extname(file_path);
        const base = basename(file_path, ext);
        const dir = dirname(file_path);
        outPath = resolve(dir, `${base}_final${ext}`);
      }

      const buf = readFileBytesOrThrow(file_path);
      const doc = await loadDocxOrThrow(buf, file_path);

      const result = await finalize_document(doc, {
        filename: basename(file_path),
        sanitize_mode: (sanitize_mode as any) || "full",
        accept_all: accept_all as boolean,
        protection_mode: protection_mode as any,
        author: author as string,
        export_pdf: export_pdf as boolean,
      });

      if (result.outBuffer) {
        const existedBefore = fs.existsSync(outPath);
        fs.writeFileSync(outPath, result.outBuffer);
        const note = overwriteNote(outPath, file_path, existedBefore);
        return {
          content: [
            {
              type: "text",
              text: `Saved to: ${outPath}${note}\n\n${result.reportText}`,
            },
          ],
        };
      } else {
        return {
          content: [
            {
              type: "text",
              text: result.reportText,
            },
          ],
        };
      }
    } catch (e: any) {
      return {
        isError: true,
        content: [{ type: "text", text: `Error: ${e.message}` }],
      };
    }
  },
);

// --- Formatter for process_document_batch ---
export function formatBatchResult(
  stats: any,
  outPath: string,
  dry_run: boolean,
): string {
  let res = "";
  if (dry_run) {
    res = `Dry-run simulation complete.\n`;
  } else {
    res = `Batch complete. Saved to: ${outPath}\n`;
  }
  const total_occurrences = stats.edits
    ? stats.edits.reduce(
        (acc: number, e: any) =>
          acc + (e.status === "applied" ? e.occurrences_modified || 1 : 0),
        0,
      )
    : 0;
  const occ_text =
    total_occurrences > stats.edits_applied
      ? ` (${total_occurrences} occurrences)`
      : "";

  const already = stats.actions_already_resolved || 0;
  const already_text = already
    ? `, ${already} already resolved (no effect)`
    : "";
  res += `Actions: ${stats.actions_applied} applied, ${stats.actions_skipped} skipped${already_text}.\n`;
  res += `Edits: ${stats.edits_applied} applied${occ_text}, ${stats.edits_skipped} skipped.\n`;

  if (stats.edits && stats.edits.length > 0) {
    res += "\nDetailed Edit Reports:\n";
    for (let i = 0; i < stats.edits.length; i++) {
      const report = stats.edits[i];
      const status_indicator =
        report.status === "applied" ? "✅ [applied]" : "❌ [failed]";

      const pagesStr =
        report.pages && report.pages.length > 0
          ? ` (p${report.pages.join(", p")})`
          : "";

      res += `### Edit ${i + 1} ${status_indicator}${pagesStr}\n`;

      if (report.heading_path) {
        res += `**Path:** \`${report.heading_path}\`\n`;
      }

      if (report.match_mode) {
        const occ =
          report.occurrences_modified || (report.status === "applied" ? 1 : 0);
        res += `**Mode:** \`${report.match_mode}\` (${occ} occurrence${occ !== 1 ? "s" : ""} modified)\n`;
      }

      if (report.error) {
        res += `*Error:* ${report.error}\n`;
      }
      if (report.warning) {
        res += `*Warning:* ${report.warning}\n`;
      }

      if (report.critic_markup) {
        res += `*Preview (CriticMarkup):*\n> ${report.critic_markup.split("\\n").join("\\n> ")}\n`;
      }
      if (report.clean_text) {
        res += `*Preview (Clean):*\n> ${report.clean_text.split("\\n").join("\\n> ")}\n`;
      }
      res += "\n";
    }
  }

  if (stats.skipped_details && stats.skipped_details.length > 0) {
    res += `Skipped Details:\n${stats.skipped_details.join("\n")}`;
  }
  return res.trim();
}

// --- Startup ---
async function main() {
  const cliOutput = handleServerCliArgs(process.argv.slice(2), packageVersion);
  if (cliOutput !== null) {
    // stdout is safe here: the stdio transport was never started.
    process.stdout.write(cliOutput + "\n");
    return;
  }
  const transport = new StdioServerTransport();
  await server.connect(transport);
  const gitSha = process.env.GIT_SHA || "unknown";
  const buildTs = process.env.BUILD_TIMESTAMP || "unknown";
  console.error(
    `Adeu MCP Server (Node.js Engine: ${identifyEngine()}) running on stdio build=${gitSha}@${buildTs}`,
  );
}

main().catch(console.error);
