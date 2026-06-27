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
  extractTextFromBuffer,
  _extractTextFromDoc,
  DocumentObject,
  RedlineEngine,
  BatchValidationError,
  create_word_patch_diff,
  finalize_document,
} from "@adeu/core";

import {
  build_paginated_response,
  build_outline_response,
  build_appendix_response,
  build_search_response,
} from "./response-builders.js";

import { login_to_adeu_cloud, logout_of_adeu_cloud } from "./tools/auth.js";
import {
  search_and_fetch_emails,
  create_email_draft,
  list_available_mailboxes,
} from "./tools/email.js";
import { MARKDOWN_UI_URI, EMAIL_UI_URI } from "./shared.js";

function readFileBytesOrThrow(filePath: string): Buffer {
  try {
    return readFileSync(filePath);
  } catch (err: any) {
    if (err.code === "ENOENT") {
      throw new Error(
        `File not found: ${filePath}.\n` +
          `If you are running in a sandboxed/containerized environment (such as Claude Desktop or another containerized client), ` +
          `the host application or MCP server may not have direct access to your local workspace files.\n` +
          `You can resolve this by installing and running the local 'adeu' CLI tool directly within your environment.\n` +
          `Here is how the MCP tools map to their CLI equivalents:\n` +
          `- read_docx               -> adeu extract ${filePath}\n` +
          `- process_document_batch  -> adeu apply ${filePath}\n` +
          `- diff_docx_files         -> adeu diff ${filePath} <modified_path>\n` +
          `- accept_all_changes      -> adeu accept-all ${filePath}\n\n` +
          `To run the local tool, install it via:\n` +
          `  uv tool install adeu\n` +
          `and run the mapped CLI command directly in your terminal.`,
      );
    }
    throw err;
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
const READ_DOCX_TAIL =
  "Modes:\n- 'full' (default): paginated body content. Use page=N to navigate.\n- 'outline': heading map only — start here for large docs to plan targeted reads. Defaults to L1-L2 headings; pass outline_max_level=3-6 to see deeper structure.\n- 'appendix': defined terms, anchors, and cross-reference targets. Consult before editing legal/technical docs to avoid breaking references.";

const PROCESS_BATCH_COMMON_DESC =
  "Applies a batch of edits and review actions to a DOCX.\n\nAll changes evaluate against the ORIGINAL document state — do not chain dependent edits within one batch (e.g. rename X to Y, then modify Y). Apply the rename first, then send a second batch.\n\n";
const PROCESS_BATCH_OPERATIONS_DESC =
  "Each item in `changes` must specify a `type`:\n1. 'modify': Search-and-replace. By default `target_text` must match uniquely (`match_mode`:'strict') — add surrounding context to disambiguate, or set `match_mode`:'first'/'all' to edit the first or every occurrence. Set `regex`:true to treat `target_text` as a regular expression (capture groups available in `new_text` as $1, $2…). `new_text` supports Markdown: '# Heading 1' through '###### Heading 6', '**bold**', '_italic_', and '\\n\\n' to split into multiple paragraphs. Empty `new_text` deletes. Do NOT write CriticMarkup tags ({++, {--, {>>) manually — use the `comment` parameter for comments.\n2. 'accept' / 'reject': Finalize or revert a tracked change by `target_id` (e.g. 'Chg:12').\n3. 'reply': Reply to a comment by `target_id` (e.g. 'Com:5') with `text`.\n4. 'insert_row' / 'delete_row': Table edits. Disk mode only — not supported on Live Word canvas.\n\nID VOLATILITY: 'Chg:N' and 'Com:N' shift between document states. Always call `read_docx` immediately before any accept/reject/reply — do not reuse IDs from earlier in the conversation.\n\n`author_name` is used for attribution on all tracked changes and comments, in both disk and Live Word modes.";

const DIFF_DOCX_DESC =
  "Compares two DOCX files and returns a compact `@@ Word Patch @@` diff — Adeu's token-level, sub-word patch format — of their text content. Useful for analyzing differences between versions before editing.";

const gitSha = process.env.GIT_SHA || "unknown";
const packageVersion = process.env.PACKAGE_VERSION || "unknown";
const buildTag = ` [Adeu v${packageVersion}+${gitSha}]`;

// --- Scope Configuration ---
const args = process.argv.slice(2);
const scopeIdx = args.indexOf("--scope");
const requestedScope = (
  scopeIdx !== -1 ? args[scopeIdx + 1] : "all"
).toLowerCase();
const isDocxOnly = requestedScope === "docx";

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

registerAppResource(
  server,
  EMAIL_UI_URI,
  EMAIL_UI_URI,
  { mimeType: RESOURCE_MIME_TYPE, description: "Adeu Email Viewer UI" },
  async () => {
    let html = getAssetContent(
      "templates",
      "email_ui.html",
      "<html><body>UI Template Not Found</body></html>",
    );
    const svg = getAssetContent("assets", "adeu.svg", "");

    html = html.replace("[[ adeu_svg_code ]]", svg);

    return {
      contents: [
        {
          uri: EMAIL_UI_URI,
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
      page: z
        .union([z.number(), z.string()])
        .optional()
        .describe(
          "Without `search_query`: 1-indexed document page to display (defaults to 1). With `search_query`: restricts matches to that document page (defaults to searching all pages; pass `page='all'` to be explicit).",
        ),
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
      const buf = readFileBytesOrThrow(file_path);

      if (mode === "outline") {
        const doc = await DocumentObject.load(buf);
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

      const text = await extractTextFromBuffer(buf, clean_view);
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
      // In non-search mode, `page` defaults to 1 (show document page 1).
      const resolvedPage =
        page === undefined || page === null
          ? 1
          : typeof page === "number"
            ? page
            : parseInt(String(page), 10) || 1;
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
      .describe(
        "Change kind: 'modify' (search-and-replace), 'accept'/'reject' (resolve a tracked change by id), 'reply' (reply to a comment by id), 'insert_row'/'delete_row' (table edits; disk mode only).",
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

server.registerTool(
  "process_document_batch",
  {
    description: PROCESS_BATCH_COMMON_DESC + PROCESS_BATCH_OPERATIONS_DESC,
    inputSchema: {
      original_docx_path: z
        .string()
        .describe("Absolute path to the source file."),
      author_name: z
        .string()
        .describe("Name to appear in Track Changes (e.g., 'Reviewer AI')."),
      changes: z
        .array(z.union([z.string(), CHANGE_ITEM_SCHEMA]))
        .describe(
          "Ordered list of changes to apply. Each item is an object carrying a `type` discriminator plus that type's fields (see the per-field docs and the tool description). All items evaluate against the ORIGINAL document state.",
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
    original_docx_path,
    author_name,
    changes,
    output_path,
    dry_run,
  }) => {
    try {
      if (!author_name || !author_name.trim())
        return {
          content: [
            { type: "text", text: "Error: author_name cannot be empty." },
          ],
        };
      if (!changes || changes.length === 0)
        return {
          content: [{ type: "text", text: "Error: No changes provided." }],
        };

      // Defensive sanitization at the MCP boundary: some LLM clients
      // "double-serialize" nested arrays, delivering each element of `changes`
      // as a JSON string instead of an object. The core engine also guards
      // against this, but we normalize here too so the tool layer never hands
      // raw string primitives downstream regardless of the engine version
      // bundled. Genuine objects and unparseable strings pass through
      // untouched so validation surfaces a clear error rather than crashing.
      const sanitizedChanges = changes.map((item: any) => {
        if (typeof item === "string") {
          try {
            const parsed = JSON.parse(item);
            if (parsed !== null && typeof parsed === "object") {
              return parsed;
            }
            return item;
          } catch {
            return item;
          }
        }
        return item;
      });

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
      const doc = await DocumentObject.load(buf);
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

      if (!dry_run) {
        const outBuf = await doc.save();
        fs.writeFileSync(outPath, outBuf);
      }

      const res = formatBatchResult(stats, outPath, !!dry_run);
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
      docx_path: z.string().describe("Absolute path to the DOCX file."),
      output_path: z.string().optional().describe("Optional output path."),
    },
  },
  async ({ docx_path, output_path }) => {
    try {
      let outPath = output_path;
      if (!outPath) {
        const ext = extname(docx_path);
        const base = basename(docx_path, ext);
        const dir = dirname(docx_path);
        outPath = resolve(dir, `${base}_clean${ext}`);
      }

      const buf = readFileBytesOrThrow(docx_path);
      const doc = await DocumentObject.load(buf);
      const engine = new RedlineEngine(doc);

      engine.accept_all_revisions();

      const outBuf = await doc.save();

      fs.writeFileSync(outPath, outBuf);

      return {
        content: [
          { type: "text", text: `Accepted all changes. Saved to: ${outPath}` },
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
  "diff_docx_files",
  {
    description: DIFF_DOCX_DESC,
    inputSchema: {
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
  async ({ original_path, modified_path, compare_clean }) => {
    try {
      const origBuf = readFileBytesOrThrow(original_path);
      const modBuf = readFileBytesOrThrow(modified_path);

      const origText = await extractTextFromBuffer(origBuf, compare_clean);
      const modText = await extractTextFromBuffer(modBuf, compare_clean);

      const diff = create_word_patch_diff(
        origText,
        modText,
        basename(original_path),
        basename(modified_path),
      );

      return {
        content: [{ type: "text", text: diff || "No differences found." }],
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
    file_path,
    output_path,
    sanitize_mode,
    accept_all,
    protection_mode,
    author,
    export_pdf,
  }) => {
    try {
      let outPath = output_path;
      if (!outPath) {
        const ext = extname(file_path);
        const base = basename(file_path, ext);
        const dir = dirname(file_path);
        outPath = resolve(dir, `${base}_final${ext}`);
      }

      const buf = readFileBytesOrThrow(file_path);
      const doc = await DocumentObject.load(buf);

      const result = await finalize_document(doc, {
        filename: basename(file_path),
        sanitize_mode: (sanitize_mode as any) || "full",
        accept_all: accept_all as boolean,
        protection_mode: protection_mode as any,
        author: author as string,
        export_pdf: export_pdf as boolean,
      });

      fs.writeFileSync(outPath, result.outBuffer!);

      return {
        content: [
          {
            type: "text",
            text: `Saved to: ${outPath}\n\n${result.reportText}`,
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

if (!isDocxOnly) {
  registerAppTool(
    server,
    "search_and_fetch_emails",
    {
      title: "Search & Fetch Emails",
      description:
        "Searches the user's live email inbox via the Adeu cloud backend.\n\n" +
        "TWO MODES:\n" +
        "1. Search mode (no `email_id`): returns up to `limit` lightweight previews. Use filters (`sender`, `subject`, `is_unread`, `days_ago`, `folder`, `has_attachments`, `attachment_name`) to narrow down.\n" +
        "2. Fetch mode (with `email_id`): returns the full email body, thread history, and downloads attachments under `max_attachment_size_mb` to the local disk.\n\n" +
        "AUTO-ESCALATION: If a search returns exactly one preview, the backend automatically fetches the full email in the same call. Plan around the response shape — check the `type` field (`previews` vs `full_email`) before assuming.\n\n" +
        "EMAIL ID FORMATS (`email_id` parameter accepts any of):\n" +
        "- `msg_<6 chars>` — short ID returned by previews on THIS machine. NOT portable across machines or sessions; the local cache holds the most recent 1000. If you reference one that's been evicted, the tool returns a StaleShortIdError telling you to re-search.\n" +
        "- `adeu_<numeric>` — server-side reference for emails Adeu has previously processed. Portable across machines and sessions for the same authenticated user.\n" +
        "- Raw provider ID (Gmail/Outlook native ID) — works if you have it, but you usually won't.\n\n" +
        "FOLDER DEFAULT: omitting `folder` searches the Inbox only (matching what the user sees in their mail client). Use `folder='sent'` for sent items, `folder='all'` to include Deleted Items, Drafts, and other folders.\n\n" +
        "ATTACHMENTS: attachments larger than `max_attachment_size_mb` (default 10) are listed in the response but NOT downloaded — raise the cap if you need them. Always set `working_directory` when calling from a project so attachments land alongside the user's other files.",
      inputSchema: z.object({
        sender: z.string().optional(),
        subject: z.string().optional(),
        has_attachments: z.boolean().optional(),
        attachment_name: z.string().optional(),
        is_unread: z.boolean().optional(),
        days_ago: z.coerce.number().optional(),
        folder: z.enum(["inbox", "sent", "all"]).optional(),
        limit: z.coerce.number().default(10),
        offset: z.coerce.number().default(0),
        email_id: z.string().optional(),
        working_directory: z.string().optional(),
        mailbox_address: z
          .string()
          .optional()
          .describe("Optional target mailbox email address to search within."),
        task_id: z
          .string()
          .optional()
          .describe("If resuming a pending check, provide the task ID here."),
        max_attachment_size_mb: z.coerce
          .number()
          .optional()
          .describe(
            "Maximum attachment size in MB to download (default 10). Attachments larger than this are listed in the response but not downloaded. Raise this to fetch large files.",
          ),
      }),
      _meta: { ui: { resourceUri: EMAIL_UI_URI } },
    },
    async (args) => {
      try {
        return (await search_and_fetch_emails(args)) as any;
      } catch (e: any) {
        return {
          isError: true,
          content: [{ type: "text", text: e.message }],
        };
      }
    },
  );

  server.registerTool(
    "login_to_adeu_cloud",
    {
      description:
        "Logs the user into Adeu Cloud. Opens a browser window for SSO authentication.\n\n" +
        "IMPORTANT — login is user-level, not account-level:\n" +
        "- An Adeu user can have multiple linked provider accounts (Microsoft, Google) and multiple mailboxes (personal + shared/delegated). One linked account is marked primary.\n" +
        "- Signing in through ANY of the user's linked accounts authenticates the same Adeu user. Once logged in, the session can read from and draft in ALL of that user's linked accounts and ALL of their mailboxes — not just the one used to sign in.\n" +
        "- The choice of which provider account to sign in through is purely an SSO mechanism; it does not select a 'current account' for the session.\n\n" +
        "When the user asks which accounts or mailboxes are available, call `list_available_mailboxes` rather than naming a single account from the login response.",
    },
    async () => {
      try {
        return (await login_to_adeu_cloud()) as any;
      } catch (e: any) {
        return { isError: true, content: [{ type: "text", text: e.message }] };
      }
    },
  );

  server.registerTool(
    "logout_of_adeu_cloud",
    { description: "Logs out of the Adeu Cloud backend." },
    async () => {
      try {
        return (await logout_of_adeu_cloud()) as any;
      } catch (e: any) {
        return { isError: true, content: [{ type: "text", text: e.message }] };
      }
    },
  );
  server.registerTool(
    "create_email_draft",
    {
      description:
        "Creates an email draft in the user's native draft box (Outlook Drafts or Gmail Drafts).\n\n" +
        "TWO MODES:\n" +
        "1. Reply mode: pass `reply_to_email_id` to create a threaded reply. The draft inherits subject, recipients, and threading headers from the original — do NOT pass `subject` or `to_recipients`.\n" +
        "2. New email mode: omit `reply_to_email_id` and pass BOTH `subject` and `to_recipients`.\n\n" +
        "`reply_to_email_id` accepts the same ID formats as search_and_fetch_emails (`msg_*` short IDs, `adeu_*` references, or raw provider IDs). Short IDs are validated against the local cache before the call; stale ones fail fast with a clear error telling you to re-search.\n\n" +
        "`body_markdown` is converted server-side to styled HTML with inlined CSS for email-client compatibility. Write the body in plain Markdown — do not pre-render HTML.\n\n" +
        "`attachment_paths` takes absolute file paths on the user's local disk and uploads them with the draft. Useful right after search_and_fetch_emails downloaded attachments — those local paths can be passed directly here.",
      inputSchema: {
        body_markdown: z.string(),
        reply_to_email_id: z.string().optional(),
        subject: z.string().optional(),
        to_recipients: z.array(z.string()).optional(),
        attachment_paths: z.array(z.string()).optional(),
        mailbox_address: z
          .string()
          .optional()
          .describe(
            "Optional target mailbox email address to create the draft in.",
          ),
      },
    },
    async (args) => {
      try {
        return (await create_email_draft(args)) as any;
      } catch (e: any) {
        return { isError: true, content: [{ type: "text", text: e.message }] };
      }
    },
  );
  server.registerTool(
    "list_available_mailboxes",
    {
      description:
        "Lists all personal and shared/delegated mailboxes the authenticated Adeu user has access to, across ALL of their linked provider accounts. Returns each mailbox's `email_address`, `display_name`, auto-processing settings, and write-back preference.\n\n" +
        "This is the right tool to answer 'which accounts/mailboxes am I logged into?' — Adeu login is user-level, so a single MCP session can see every mailbox listed here regardless of which provider account was used for SSO.\n\n" +
        "Call this FIRST when the user names a specific mailbox or shared inbox, to resolve the canonical `email_address`. Then pass that address as `mailbox_address` to `search_and_fetch_emails` or `create_email_draft` to scope the operation. Omitting `mailbox_address` on those tools targets the user's primary personal mailbox.",
      inputSchema: {},
    },
    async () => {
      try {
        return (await list_available_mailboxes()) as any;
      } catch (e: any) {
        return { isError: true, content: [{ type: "text", text: e.message }] };
      }
    },
  );
}

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

  res += `Actions: ${stats.actions_applied} applied, ${stats.actions_skipped} skipped.\n`;
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
  const transport = new StdioServerTransport();
  await server.connect(transport);
  const gitSha = process.env.GIT_SHA || "unknown";
  const buildTs = process.env.BUILD_TIMESTAMP || "unknown";
  console.error(
    `Adeu MCP Server (Node.js Engine: ${identifyEngine()}) running on stdio build=${gitSha}@${buildTs}`,
  );
}

main().catch(console.error);
