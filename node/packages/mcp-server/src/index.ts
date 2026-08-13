import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { readFileSync, existsSync } from "node:fs";
import { basename, resolve, extname, dirname, isAbsolute, join } from "node:path";
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
  failure_envelope,
  create_word_patch_diff,
  collect_media_difference_warnings,
  finalize_document,
  parse_page_arg,
  PageArgKind,
  extract_comments_data,
  response_budget_limit,
  has_fused_json_marker,
  FUSED_JSON_HINT,
  apply_text_revision_core,
  TextRevisionError,
  TextRevisionVerificationError,
} from "@adeu/core";
import { describe_illegal_control_chars } from "@adeu/core";

import {
  build_paginated_response,
  build_page_range_response,
  build_full_document_response,
  build_budget_guard_message,
  build_outline_response,
  build_appendix_response,
  build_search_response,
  render_outline_response,
  build_changes_response,
} from "./response-builders.js";
import { docCache } from "./doc-cache.js";
import type { ProgressFn } from "./doc-cache.js";

import { MARKDOWN_UI_URI, handleServerCliArgs } from "./shared.js";
import { attachProtocolAdapter } from "./protocol-adapter.js";
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

export function coerceChangeItemInPlace(item: any): void {
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

  // MCP-boundary tolerance (parity with Python models.py:427): the published
  // schema makes new_text optional, so a schema-following model that only
  // wants to annotate sends target_text + comment. The lossless reading is
  // the pure-comment form (new_text == target_text) — never a bounce, and
  // never a tracked deletion. An explicit "" is left alone: empty means
  // delete, and delete-with-explanation is a distinct intent.
  if (
    item.type === "modify" &&
    (item.new_text === undefined || item.new_text === null)
  ) {
    const target = item.target_text;
    const comment = item.comment;
    if (
      typeof target === "string" &&
      target &&
      typeof comment === "string" &&
      comment.trim()
    ) {
      item.new_text = target;
    }
  }
}
// At most this many sibling filenames are suggested in a file-not-found
// error. Dumping a crowded directory's full listing (~300 names in the QA
// workspace) is thousands of tokens of noise; the closest few names are what
// enable one-turn self-correction (QA round 3, finding 3.3).
const NOT_FOUND_SUGGESTION_CAP = 10;

/** Rank candidates by similarity to the requested filename (longest common
 *  prefix first, then shared-substring length) and return the closest few. */
function closestFilenames(requested: string, candidates: string[]): string[] {
  const req = requested.toLowerCase();
  const score = (name: string): number => {
    const cand = name.toLowerCase();
    let prefix = 0;
    while (
      prefix < req.length &&
      prefix < cand.length &&
      req[prefix] === cand[prefix]
    ) {
      prefix++;
    }
    const stem = req.replace(/\.docx$/, "");
    return prefix * 100 + (stem && cand.includes(stem) ? 50 : 0);
  };
  return [...candidates]
    .sort((a, b) => score(b) - score(a) || a.localeCompare(b))
    .slice(0, NOT_FOUND_SUGGESTION_CAP);
}

function readFileBytesOrThrow(filePath: string): Buffer {
  try {
    return readFileSync(filePath);
  } catch (err: any) {
    if (err.code === "ENOENT") {
      // Lean, agent-appropriate error: suggest the CLOSEST sibling .docx
      // files (capped) so the model can self-correct a wrong filename (e.g.
      // a guessed `-processed` suffix) in one turn — never the whole
      // directory listing, and never CLI install instructions.
      let available = "";
      try {
        const dir = dirname(filePath);
        const docs = fs
          .readdirSync(dir)
          .filter((f) => f.toLowerCase().endsWith(".docx"));
        if (docs.length) {
          const shown = closestFilenames(basename(filePath), docs);
          available = ` available files: [${shown.join(", ")}]`;
          if (docs.length > shown.length) {
            available += ` (+${docs.length - shown.length} more in ${dir})`;
          }
        } else {
          available = ` (no .docx files found in ${dir})`;
        }
      } catch {
        // Directory unreadable — omit the listing rather than fail.
      }
      // Echo the path AS GIVEN — dropping the directory ("file not found:
      // alice_copy.docx" for qa_sandbox/alice_copy.docx) hid which path was
      // actually tried, and relative paths resolve against the server's cwd,
      // not the caller's (QA 2026-07-23 F16). The absolute-path hint applies
      // only when the caller actually passed a relative path (QA round 3,
      // finding 3.3).
      const hint = isAbsolute(filePath)
        ? ""
        : " Provide an absolute path — the server cannot resolve relative paths.";
      throw new Error(`file not found: ${filePath};${available}${hint}`);
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
  opts?: Parameters<typeof DocumentObject.load>[1],
): Promise<DocumentObject> {
  try {
    return await DocumentObject.load(buf, opts);
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
  "Modes:\n- 'full' (default): paginated body content. Use page=N to navigate.\n- 'outline': heading map only — start here for large docs to plan targeted reads. Defaults to L1-L2 headings; pass outline_max_level=3-6 to see deeper structure.\n- 'appendix': defined terms, anchors, and cross-reference targets. Consult before editing legal/technical docs to avoid breaking references.\n- 'changes' (mode='changes'): a ledger of every tracked change and comment (id, type, author, page, snippet) — start here for review work instead of reading pages. Filter with changes_author, page, and changes_offset.\n\n`page`: a positive integer (1-indexed, default 1), a page RANGE like '2-6' (returns up to 8 pages in one call, then names the next range), or 'all'. Pages are synthetic length-based chunks sized for LLM consumption, NOT printed Word pages. In mode='full', page='all' returns the whole body with no page chrome; oversized documents are refused with an outline and a bounded-read recipe unless force=true. With `search_query`, `page` instead restricts matches to that page (default: search all pages).";

// BUDGET: real MCP clients truncate tool descriptions at ~2048 chars — the
// tail (wherever it falls) is invisible to the model. COMMON + OPERATIONS +
// the appended build tag must stay under that ceiling (QA 2026-07-23
// client-compat test 1). The {#cell:} stability claim must be qualified in
// the SAME paragraph (finalize/sanitize regenerates the anchors, QA F9), and
// the row-op fields (`cells` etc.) must be named in prose because clients
// strip the typed item schema to {} in transit (QA F10).
const PROCESS_BATCH_COMMON_DESC =
  "Applies a batch of edits and review actions to a DOCX.\n\nBatches apply SEQUENTIALLY: each change validates against state from prior changes; later edits may target text an earlier edit introduced. Valid changes apply when others fail (salvage default): response LEADS with `PARTIAL: applied K of N` listing unapplied changes — resubmit only those. Pass partial=false for all-or-nothing.\n\n";
const PROCESS_BATCH_OPERATIONS_DESC =
  "Each item in `changes` needs a `type`:\n1. 'modify': search-and-replace. `target_text` must match uniquely (`match_mode`:'strict', default) — add context or set `match_mode`:'first'/'all'. Set `regex`:true for regex matching (groups in `new_text` as $1, $2…). `new_text` supports Markdown: '#'–'######' headings, '**bold**', '_italic_', '\\n\\n' paragraph split. Omit it (with a comment) to annotate without changing the text; an explicit empty string deletes. Never write CriticMarkup ({++, {--, {>>) manually — use the `comment` field.\n   • EMPTY CELLS: blank cells carry `{#cell:<id>}` anchors — set `target_text` to the anchor and value in `new_text`. Pipes are display separators, not text.\n2. 'accept'/'reject': finalize or revert a tracked change by `target_id` (e.g. 'Chg:12').\n3. 'reply': reply to a comment by `target_id` (e.g. 'Com:5') with `text`.\n4. 'insert_row': add a table row — `target_text` anchors on an existing row's text, `cells` holds cell values (left to right), `position` is 'above'/'below' (default below). 'delete_row': remove row matching `target_text`. Disk mode only.\n\nID VOLATILITY: 'Chg:N'/'Com:N' ids shift between states — call `read_docx` before accept/reject/reply; never reuse ids from earlier turns. `{#cell:<id>}` anchors are stable across edits, but finalize/sanitize regenerates them.\n\n`author_name` sets Track Changes attribution; defaults to 'Adeu AI (TS)' when omitted.";

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

// read_docx must DECLARE this, not merely populate structuredContent. The MCP
// Apps host only forwards `structuredContent` to the UI app when the tool
// advertises an outputSchema; without one it hands the app a result carrying
// `content` alone and the markdown viewer has nothing to render (observed in
// Claude Desktop 2026-07-27: `params=content,isError`). Mirrors
// READ_DOCX_OUTPUT_SCHEMA in python/src/adeu/mcp_components/tools/document.py.
// Only `markdown` is required, and the object stays loose: clients validate
// the payload against this schema and reject the whole call on a mismatch, so
// an edge path that omits `title` must not fail the read.
const READ_DOCX_OUTPUT_SCHEMA = z
  .object({
    markdown: z.string().describe("Document content as Markdown, for display."),
    title: z
      .string()
      .optional()
      .describe("Display title (the file name, or 'Search: <file name>')."),
    file_path: z
      .string()
      .optional()
      .describe("Absolute path of the document that was read."),
  })
  .loose();

registerAppTool(
  server,
  "read_docx",
  {
    title: "Read DOCX",
    description: READ_DOCX_COMMON_DESC + READ_DOCX_TAIL,
    inputSchema: z.object({
      reasoning: z
        .string()
        .optional()
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
        .enum(["full", "outline", "appendix", "changes"])
        .default("full")
        .describe(
          "'full' returns body content. 'outline' returns a structural heading map. 'appendix' returns defined terms. 'changes' returns tracked changes and comments ledger.",
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
      force: z
        .boolean()
        .default(false)
        .describe(
          "For mode='full' with page='all': read the whole document even when it exceeds the response budget.",
        ),
      outline_max_level: z.coerce
        .number()
        .default(2)
        .describe("For mode='outline' only: cap on heading depth."),
      outline_verbose: z
        .boolean()
        .default(false)
        .describe("For mode='outline' only: includes metadata."),
      changes_author: z
        .string()
        .optional()
        .describe(
          "For mode='changes' only: filter tracked changes ledger by author name.",
        ),
      changes_offset: z.coerce
        .number()
        .int()
        .default(0)
        .describe(
          "For mode='changes' only: entry offset for paginating tracked changes ledger.",
        ),
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
      max_matches: z.coerce
        .number()
        .int()
        .default(20)
        .describe(
          "For search queries: maximum number of search matches to return (default 20).",
        ),
      match_offset: z.coerce
        .number()
        .int()
        .default(0)
        .describe(
          "For search queries: 0-based match offset to start search results from for pagination (default 0).",
        ),
      full_paragraph: z
        .boolean()
        .default(false)
        .describe(
          "For search queries: return full paragraph for search matches instead of clamping snippets to ±120 chars.",
        ),
    }),
    outputSchema: READ_DOCX_OUTPUT_SCHEMA,
    _meta: { ui: { resourceUri: MARKDOWN_UI_URI } },
  },
  async (
    {
      reasoning,
      file_path,
      clean_view,
      mode,
      page,
      force,
      outline_max_level,
      outline_verbose,
      search_query,
      search_regex,
      search_case_sensitive,
      max_matches,
      match_offset,
      full_paragraph,
      changes_author,
      changes_offset,
    },
    extra?: any,
  ) => {
    try {
      void reasoning;
      const readBytes = () => readFileBytesOrThrow(file_path);

      // Progress relay: only when the client supplied a progressToken, and
      // never allowed to fail the read. Cold ingests of huge documents use
      // it so the first read shows live work instead of a silent stall.
      const progressToken = extra?._meta?.progressToken;
      const onProgress: ProgressFn | undefined =
        progressToken !== undefined && extra?.sendNotification
          ? async (message, progress, total) => {
              try {
                await extra.sendNotification({
                  method: "notifications/progress",
                  params: { progressToken, progress, total, message },
                });
              } catch {
                /* progress is best-effort */
              }
            }
          : undefined;

      // The cache delegates byte-reading and document-loading back to the
      // boundary helpers so error shapes stay identical to the uncached
      // handler (lean file-not-found with sibling listing; container errors
      // diagnosed as invalid .docx per QA 2026-07-23 F19).
      const loadDoc = (buf: Buffer, opts?: any) =>
        loadDocxOrThrow(buf, file_path, opts);
      const getEntry = () =>
        docCache.get(file_path, readBytes, loadDoc, onProgress);

      if (mode === "outline") {
        if (!clean_view) {
          const entry = await getEntry();
          const res = render_outline_response(
            entry.outline_nodes,
            entry.raw_bundle.pagination.total_pages,
            file_path,
            outline_max_level,
            outline_verbose,
          );
          return res as any;
        }
        // clean_view outline: rare combination, needs clean-text offsets the
        // cache does not keep — served by the historical uncached path.
        const buf = readBytes();
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

      const entry = await getEntry();
      const text = clean_view
        ? await docCache.ensureCleanText(entry, readBytes, loadDoc)
        : entry.raw_text;
      const bundle = clean_view
        ? await docCache.ensureCleanBundle(entry, readBytes, loadDoc)
        : entry.raw_bundle;

      if (search_query !== undefined && search_query !== null) {
        // In search mode, undefined `page` means "search all document pages".
        const res = build_search_response(
          text,
          search_query,
          search_regex,
          search_case_sensitive,
          page,
          file_path,
          bundle,
          { max_matches, match_offset, full_paragraph },
        );
        return res as any;
      }

      if (mode === "changes") {
        if (clean_view) {
          return {
            isError: true,
            content: [
              {
                type: "text",
                text: "Error executing tool read_docx: --clean-view cannot be used with mode='changes'.",
              },
            ],
          };
        }
        const entry2 = await getEntry();
        let comments_data: Record<string, any> | null = null;
        let existing_change_ids: string[] | null = null;
        try {
          const buf = readBytes();
          const doc = await loadDocxOrThrow(buf, file_path);
          comments_data = extract_comments_data(doc.pkg);
          existing_change_ids = new RedlineEngine(doc).existing_change_ids();
        } catch {
          // Best-effort enrichment, exactly as Python (document.py:436-451):
          // a ledger without comment authors still beats no ledger.
        }
        const res = build_changes_response(entry2.raw_text, file_path, {
          comments_data,
          author_filter: changes_author ?? null,
          page: page ?? null,
          offset: changes_offset,
          bundle: entry2.raw_bundle,
          existing_change_ids,
        });
        return res as any;
      }

      let pageKind: PageArgKind = "single";
      let pageVal: number | [number, number] | null = 1;
      try {
        [pageKind, pageVal] = parse_page_arg(page);
      } catch (e: any) {
        return {
          isError: true,
          content: [
            {
              type: "text",
              text: e.message,
            },
          ],
        };
      }

      if (mode === "appendix") {
        if (pageKind === "range") {
          return {
            isError: true,
            content: [
              {
                type: "text",
                text: "Page range pagination is only supported in 'full' mode, not 'appendix' mode.",
              },
            ],
          };
        }
        if (pageKind === "all") {
          return {
            isError: true,
            content: [
              {
                type: "text",
                text: `Invalid page parameter: '${page}'. Provide a positive integer.`,
              },
            ],
          };
        }
        const resolvedPage = pageVal as number;
        const res = build_appendix_response(
          text,
          resolvedPage,
          file_path,
          bundle,
        );
        return res as any;
      }

      if (mode === "full") {
        if (pageKind === "all") {
          // A3: an UNBOUNDED whole-document read is the one path that can
          // return an arbitrarily large payload. Refuse it over the budget
          // with the page count, the L1 outline and the bounded-read recipe;
          // `force` is the documented opt-out. Mirrors Python's
          // tools/document.py:512-529, which asks its cache for the outline of
          // the view it is refusing — so clean_view gets the CLEAN heading map,
          // not the raw one (whose page numbers and CriticMarkup-bearing
          // heading text describe a different projection).
          //
          // Measured on the string this path RETURNS — bundle.body, what
          // build_full_document_response emits — not on `text`, which on the
          // raw view carries the structural appendix nobody asked for here.
          // Python's mode='full' text is projected with include_appendix=False
          // (doc_cache.py:159-164), so measuring `text` refused documents whose
          // body fits the budget while Python served them.
          if (!force && bundle.body.length > response_budget_limit()) {
            const nodes = clean_view
              ? await docCache.ensureCleanOutline(entry, readBytes, loadDoc)
              : entry.outline_nodes;
            return {
              isError: true,
              content: [
                {
                  type: "text",
                  text: build_budget_guard_message(
                    bundle.body,
                    file_path,
                    nodes,
                    bundle,
                  ),
                },
              ],
            } as any;
          }
          const res = build_full_document_response(text, file_path, bundle);
          return res as any;
        }
        if (pageKind === "range") {
          const [startP, endP] = pageVal as [number, number];
          const res = build_page_range_response(
            text,
            startP,
            endP,
            file_path,
            bundle,
          );
          return res as any;
        }
      }

      const resolvedPage = pageVal as number;
      const res = build_paginated_response(
        text,
        resolvedPage,
        file_path,
        bundle,
      );
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
export const CHANGE_ITEM_SCHEMA = z
  .object({
    type: z
      .string()
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
        "modify: replacement text. Supports Markdown (headings, **bold**, _italic_, '\\n\\n' paragraph splits); empty string deletes. Regex capture groups are available as $1, $2… Omit it (with a comment) to annotate without changing the text; an explicit empty string deletes.",
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
        "modify: attach a margin comment to the edited text. accept / reject: record the rationale as a margin comment anchored where the change was resolved (reported as Com:N).",
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
        .optional()
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
      // Salvage is the default (B5, parity with tools/document.py:1511-1514):
      // losing the changes that were right because one was wrong costs the
      // agent a whole round trip. The response leads with what did not land.
      partial: z
        .boolean()
        .default(true)
        .describe(
          "Whether to apply valid edits when some fail (salvage mode). Defaults to true.",
        ),
    },
  },
  async ({
    reasoning,
    original_docx_path,
    author_name,
    changes,
    output_path,
    partial,
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
      // (0-based index in the caller's `changes`, reason) — the machine-readable
      // half of the same rejection (B9). The human line stays 1-based.
      const typeFailed: [number, string][] = [];
      sanitizedChanges.forEach((c: any, i: number) => {
        if (
          c !== null &&
          typeof c === "object" &&
          !Array.isArray(c) &&
          (!c.type || !VALID_TYPES.has(c.type))
        ) {
          const fused = has_fused_json_marker(c.type) ? ` ${FUSED_JSON_HINT}` : "";
          const reason = `missing or unrecognized "type". Use one of: modify (needs target_text + new_text), accept/reject (needs target_id like "Chg:12"), reply (needs target_id like "Com:5" + text), insert_row (needs target_text + cells), delete_row (needs target_text). Received keys: [${Object.keys(c).join(", ")}].${fused}`;
          typeErrors.push(`- Change ${i + 1}: ${reason}`);
          typeFailed.push([i, reason]);
        }
      });
      if (typeErrors.length > 0) {
        const env = failure_envelope(
          "invalid_changes_file",
          typeFailed,
          "Batch rejected. Some changes are malformed.",
          typeErrors,
        );
        return {
          isError: true,
          content: [
            {
              type: "text",
              text:
                `Batch rejected. Some changes are malformed:\n\n${typeErrors.join("\n")}` +
                `\n\n\`\`\`json\n${JSON.stringify(env)}\n\`\`\``,
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

      // Hot-DOM reuse (docs/PERFORMANCE.md §5): a read_docx of this same
      // file version usually preceded this call — take its parse instead of
      // re-parsing from disk. Consume-on-take: the batch mutates the DOM.
      let doc = await docCache.takeHotDoc(original_docx_path);
      if (!doc) {
        const buf = readFileBytesOrThrow(original_docx_path);
        doc = await loadDocxOrThrow(buf, original_docx_path);
      }
      const engine = new RedlineEngine(doc, author_name);

      let stats;
      try {
        stats = engine.process_batch(sanitizedChanges, undefined, partial);
      } catch (e: any) {
        if (e instanceof BatchValidationError) {
          // Pin the DOM back for the retry that typically follows a rejected
          // batch — but ONLY once the engine has verified that its rollback
          // restored the exact on-disk state. It re-pinned unconditionally
          // before, and a batch's review actions used to survive its own
          // rollback: every rejected attempt handed the retry a document
          // carrying the previous attempt's reply, so one reviewer comment
          // ended up with three identical replies (BUG 2026-08-12). An
          // unverified DOM is simply dropped; the retry re-parses from disk.
          if (engine.rollback_verified) {
            docCache.restoreHotDoc(original_docx_path, doc);
          }
          // Prose for the human reader, envelope for the machine one: the
          // indices name positions in the caller's own `changes` array
          // (B9; python/src/adeu/mcp_components/tools/document.py:710-717).
          const env = failure_envelope(
            "batch_validation_failed",
            e.failed,
            "Batch rejected. Some edits failed validation.",
            e.errors,
          );
          return {
            isError: true,
            content: [
              {
                type: "text",
                text:
                  `Batch rejected. Some edits failed validation:\n\n${e.errors.join("\n\n")}` +
                  `\n\n\`\`\`json\n${JSON.stringify(env)}\n\`\`\``,
              },
            ],
          };
        }
        throw e;
      }

      // Salvage that salvaged nothing is a REJECTION, not a partial success:
      // checked before the save so no output file is produced and no response
      // claims one (tools/document.py:645-661). The hot doc is deliberately
      // NOT restored here — unlike the transactional path above, salvage takes
      // no snapshot, so this DOM may carry a failed edit's partial mutations.
      const applied_count =
        (stats.edits_applied || 0) + (stats.actions_applied || 0);
      const engine_failed: Array<{ index: number; reason: string }> =
        stats.failed || [];
      if (applied_count === 0 && engine_failed.length > 0) {
        const env = failure_envelope(
          "batch_validation_failed",
          engine_failed.map((f) => [f.index, f.reason] as [number, string]),
          "Batch rejected. Some edits failed validation.",
          engine_failed.map((f) => f.reason),
        );
        return {
          isError: true,
          content: [
            {
              type: "text",
              text:
                `Batch rejected. Some edits failed validation:\n\n${engine_failed
                  .map((f) => f.reason)
                  .join("\n\n")}` +
                `\n\n\`\`\`json\n${JSON.stringify(env)}\n\`\`\``,
            },
          ],
        };
      }

      let overwrite_note = "";
      const existedBefore = fs.existsSync(outPath);
      const outBuf = await doc.save();
      try {
        fs.mkdirSync(dirname(outPath), { recursive: true });
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
      // The in-memory document IS the state of the file just written:
      // adopt it as the output's cache (text products built in the
      // background; DOM pinned for a chained edit). The agent's
      // read-after-edit then skips the full re-parse.
      docCache.primeFromDoc(outPath, doc);

      // A partial success is a SUCCESS response with the failures hoisted to
      // the top — never a failure envelope, whose recovery protocol ("Nothing
      // was written") would contradict the saved path in the same response
      // (tools/document.py:739-766).
      let partial_header = "";
      if (partial && engine_failed.length > 0 && applied_count > 0) {
        const fails = [...engine_failed].sort((a, b) => a.index - b.index);
        const max_idx = fails.reduce((m, f) => Math.max(m, f.index), 0);
        const total_n = Math.max(max_idx + 1, sanitizedChanges.length);
        partial_header = `PARTIAL: applied ${applied_count} of ${total_n} changes. ${fails.length} failed validation:\n\n`;
        for (const f of fails) {
          partial_header += `- Change #${f.index + 1} Failed: ${f.reason}\n`;
        }
        partial_header += "\n";
      }

      let res =
        partial_header + formatBatchResult(stats, outPath) + overwrite_note;
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
      "Accepts every tracked change in the document, producing a finalized clean document.\n\n" +
      "remove_comments (boolean, DEFAULT TRUE): also delete every comment. The default is " +
      "TRUE because this tool's purpose is a distributable clean document, and comments are " +
      "internal review notes that must not travel to a counterparty. Pass " +
      "remove_comments=false to accept the tracked changes while KEEPING the comments — use " +
      "that when the review conversation is still live. Either way the response reports how " +
      "many comments were deleted and names each one with its author, and comments whose " +
      "anchored text an accepted deletion consumes are removed regardless, exactly as Word does.",
    inputSchema: {
      reasoning: z
        .string()
        .optional()
        .describe(
          "Why do I need to accept all changes in this document? State this reason before any other parameter.",
        ),
      docx_path: z.string().describe("Absolute path to the DOCX file."),
      output_path: z.string().optional().describe("Optional output path."),
      remove_comments: z
        .boolean()
        .default(true)
        .describe(
          "Also delete every comment in the document. Defaults to true (finalized clean " +
            "document); pass false to keep comments while accepting the tracked changes.",
        ),
    },
  },
  async ({ reasoning, docx_path, output_path, remove_comments }) => {
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
      //
      // This surface DELIBERATELY defaults to true while the library API
      // defaults to false: accept_all_changes exists to produce a distributable
      // clean document, and shipping a counterparty a file that still carries
      // internal review notes is the more expensive failure
      // (QA_ISSUES_DISCOVERED #10, "Confidentiality risk"). What
      // BUG_comment_threading_anchoring_and_typography.md B2 correctly objected
      // to was that the inversion was SILENT and unavoidable — the caller now
      // has an explicit parameter, the published description states the
      // default, and every deleted comment is named with its author.
      const counts = engine.accept_all_revisions(remove_comments !== false);
      const removedCommentNotes = [...engine.removed_comment_notes];
      const total =
        counts.accepted_insertions +
        counts.accepted_deletions +
        counts.accepted_formatting +
        counts.removed_comments;

      const existedBefore = fs.existsSync(outPath);
      const outBuf = await doc.save();

      fs.mkdirSync(dirname(outPath), { recursive: true });
      fs.writeFileSync(outPath, outBuf);
      // This tool rewrites a document the cache may already hold products
      // for (output_path defaults next to the input, and may BE the input).
      // Not priming from `doc` here is deliberate: the prime path's
      // byte-equality gate is only covered for the batch pipeline, so the
      // correct-by-construction choice is to make the next read re-parse.
      docCache.invalidate(outPath);

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
          // Deleting review content is disclosed WITH attribution: a comment the
          // caller did not write is somebody else's work product (B2).
          text += `\nComments removed: ${counts.removed_comments}.`;
          if (removedCommentNotes.length > 0) {
            text += `\nComments deleted: ${removedCommentNotes.join(", ")}`;
            if (remove_comments === false) {
              text +=
                `\nNote: these comments were anchored to text an accepted deletion ` +
                `consumed, so Word removes them too. Nothing else was deleted.`;
            }
          }
        } else if (remove_comments === false) {
          text += `\nComments kept (remove_comments=false).`;
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
        .optional()
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
        .optional()
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
        fs.mkdirSync(dirname(outPath), { recursive: true });
        fs.writeFileSync(outPath, result.outBuffer);
        // Sanitize/finalize rewrites the package; drop any cached products
        // for this path so a later read cannot serve the pre-finalize text.
        docCache.invalidate(outPath);
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

server.registerTool(
  "apply_text_revision",
  {
    description:
      "Applies whole-text revised text to a DOCX document by computing a diff and generating " +
      "tracked changes. Includes a clean-text verification gate to ensure the applied document " +
      "matches the supplied text.\n\n" +
      "`revised_text` must be the complete CLEAN view of the document: read it with `read_docx` " +
      "(`clean_view=true`, `page='all'`), edit that text, and send ALL of it back. Never " +
      "CriticMarkup ({++, {--, {>>) — this tool diffs against the clean view, so markup tokens " +
      "would land in the document as literal prose. Never one page of a paginated extract — " +
      "everything absent from the text is applied as a tracked deletion.\n\n" +
      "INTERLOCK: a revision that drops >50% of the characters (>75% for documents under 2000 " +
      "characters) is refused unless you pass allow_major_deletions=true.\n\n" +
      "If the applied document's clean text does not then match `revised_text`, NOTHING is " +
      "written to output_path: a diagnostic copy is kept at <name>.unverified.docx and the call " +
      "fails. output_path defaults to <name>_redlined.docx; an existing _redlined/_processed " +
      "artifact is revised in place.",
    inputSchema: {
      reasoning: z
        .string()
        .optional()
        .describe(
          "Why do I need to apply this text revision? State this reason before any other parameter.",
        ),
      file_path: z.string().describe("Absolute path to the source DOCX file."),
      revised_text: z
        .string()
        .describe("The complete revised clean text of the document."),
      output_path: z
        .string()
        .optional()
        .describe("Optional output path for the modified DOCX."),
      author: z.string().optional().describe("Author name for Track Changes."),
      allow_major_deletions: z
        .boolean()
        .default(false)
        .describe(
          "Allow deleting >50% of characters (>75% for documents under 2000 characters).",
        ),
    },
  },
  async ({
    reasoning,
    file_path,
    revised_text,
    output_path,
    author,
    allow_major_deletions,
  }) => {
    try {
      void reasoning;
      const buf = readFileBytesOrThrow(file_path);
      const doc = await loadDocxOrThrow(buf, file_path);

      const result = await apply_text_revision_core({
        doc,
        input_path: file_path,
        revised_text,
        output_path,
        author,
        allow_major_deletions,
      });
      const outPath = result.output_path;

      const existedBefore = fs.existsSync(outPath);
      fs.mkdirSync(dirname(outPath), { recursive: true });
      fs.writeFileSync(outPath, result.out_bytes);
      // Never primeFromDoc here: only the batch pipeline's byte-equality gate
      // is covered (see the comment on the batch tool's prime call), so the
      // correct-by-construction choice is to make the next read re-parse.
      docCache.invalidate(outPath);

      return {
        content: [
          {
            type: "text",
            text:
              formatBatchResult(result.stats, outPath) +
              overwriteNote(outPath, file_path, existedBefore),
          },
        ],
      };
    } catch (e: any) {
      if (e instanceof TextRevisionVerificationError) {
        // The gate refused the document: keep the copy it refused, next to the
        // path the caller asked for, so a human can see what could not be
        // realized — and say so in the SAME message that reports the failure.
        let note = "";
        try {
          fs.mkdirSync(dirname(e.unverified_path), { recursive: true });
          fs.writeFileSync(e.unverified_path, e.unverified_bytes);
        } catch (werr: any) {
          note = ` (the diagnostic copy could not be written: ${werr.message})`;
        }
        return {
          isError: true,
          content: [{ type: "text", text: e.message + note }],
        };
      }
      // A guard refusal is the agent's recovery instruction verbatim (parity
      // with Python's ToolError(str(e))); anything else keeps the "Error: "
      // shape the other tools use.
      return {
        isError: true,
        content: [
          {
            type: "text",
            text:
              e instanceof TextRevisionError ? e.message : `Error: ${e.message}`,
          },
        ],
      };
    }
  },
);

// --- Formatter for process_document_batch ---
export function formatBatchResult(stats: any, outPath: string): string {
  // Rendered markdown is the minimal form for MCP tool output; see payloads.ts for structured consumers.
  let res = `Batch complete. Saved to: ${outPath}\n`;
  if (stats.author_impersonation_warning) {
    res += `\n*Warning:* ${stats.author_impersonation_warning}\n`;
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

      const occ = report.occurrences_modified ?? 0;
      res += `**Mode:** \`${report.match_mode || "strict"}\` (${occ} occurrence${occ !== 1 ? "s" : ""} modified)\n`;

      if (report.comment) {
        res += `**Comment:** "${report.comment}"\n`;
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
      res += "\n";
    }
  }

  if (stats.skipped_details && stats.skipped_details.length > 0) {
    // Purely informational notes ("… the action itself succeeded") must not
    // be filed under "Skipped Details" — that header claims work was skipped
    // when it wasn't (QA round 3, finding 3.4).
    const allNotes = stats.skipped_details.every((d: string) =>
      d.trimStart().startsWith("- Note:"),
    );
    const header = allNotes ? "Notes:" : "Skipped Details:";
    res += `\n\n${header}\n${stats.skipped_details.join("\n")}`;
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
  // Attach AFTER connect: the SDK's Protocol.connect chains any pre-existing
  // onmessage handler *inside* its own (sdk/dist/esm/shared/protocol.js:230),
  // so a pre-connect wrapper cannot stop the SDK from also answering
  // server/discover (-32601) or a request we already rejected. See task_plan §2.4.
  await server.connect(transport);
  attachProtocolAdapter(server, transport, "adeu-redlining-service", packageVersion);
  const gitSha = process.env.GIT_SHA || "unknown";
  const buildTs = process.env.BUILD_TIMESTAMP || "unknown";
  console.error(
    `Adeu MCP Server (Node.js Engine: ${identifyEngine()}) running on stdio build=${gitSha}@${buildTs}`,
  );
}

main().catch(console.error);
