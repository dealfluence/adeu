// FILE: node/packages/mcp-server/src/index.ts
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { readFileSync, existsSync } from "node:fs";
import { basename, resolve, extname, dirname, join } from "node:path";
import { z } from "zod";
import {
  registerAppTool,
  registerAppResource,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import fs from "node:fs";
import {
  identifyEngine,
  extractTextFromBuffer,
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
        `File not found: ${filePath}. Note: If you are running in a sandboxed/containerized environment, ` +
        `the host application or MCP server may not have access to your local workspace files. ` +
        `You can resolve this by installing Adeu directly inside your sandboxed environment using ` +
        `'uv tool install adeu' and executing the commands via the CLI.`
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
  "Each item in `changes` must specify a `type`:\n1. 'modify': Search-and-replace. `target_text` must uniquely match — include surrounding context if the phrase is ambiguous. `new_text` supports Markdown: '# Heading 1' through '###### Heading 6', '**bold**', '_italic_', and '\\n\\n' to split into multiple paragraphs. Empty `new_text` deletes. Do NOT write CriticMarkup tags ({++, {--, {>>) manually — use the `comment` parameter for comments.\n2. 'accept' / 'reject': Finalize or revert a tracked change by `target_id` (e.g. 'Chg:12').\n3. 'reply': Reply to a comment by `target_id` (e.g. 'Com:5') with `text`.\n4. 'insert_row' / 'delete_row': Table edits. Disk mode only — not supported on Live Word canvas.\n\nID VOLATILITY: 'Chg:N' and 'Com:N' shift between document states. Always call `read_docx` immediately before any accept/reject/reply — do not reuse IDs from earlier in the conversation.\n\n`author_name` is used for attribution on all tracked changes and comments, in both disk and Live Word modes.";

const DIFF_DOCX_DESC =
  "Compares two DOCX files and returns a unified diff of their text content. Useful for analyzing differences between versions before editing.";

// --- Server Setup ---
const server = new McpServer({
  name: "adeu-redlining-service",
  version: "1.0.0",
});

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
        .number()
        .default(1)
        .describe("Page number (1-indexed) for mode='full'. Defaults to 1."),
      outline_max_level: z
        .number()
        .default(2)
        .describe("For mode='outline' only: cap on heading depth."),
      outline_verbose: z
        .boolean()
        .default(false)
        .describe("For mode='outline' only: includes metadata."),
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
  }) => {
    try {
      const buf = readFileBytesOrThrow(file_path);
      const text = await extractTextFromBuffer(buf, clean_view);

      if (mode === "outline") {
        const doc = await DocumentObject.load(buf);
        return build_outline_response(
          doc,
          text,
          file_path,
          outline_max_level,
          outline_verbose,
        ) as any;
      }
      if (mode === "appendix") {
        return build_appendix_response(text, page, file_path) as any;
      }
      return build_paginated_response(text, page, file_path) as any;
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
      days_ago: z.number().optional(),
      folder: z.enum(["inbox", "sent", "all"]).optional(),
      limit: z.number().default(10),
      offset: z.number().default(0),
      email_id: z.string().optional(),
      working_directory: z.string().optional(),
      mailbox_address: z
        .string()
        .optional()
        .describe("Optional target mailbox email address to search within."),
      max_attachment_size_mb: z
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

// ==========================================
// 3. HEADLESS TOOLS (No UI)
// ==========================================

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
        .array(z.any())
        .describe("List of changes to apply. Each change must specify 'type'."),
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

      let outPath = output_path;
      if (!outPath) {
        const ext = extname(original_docx_path);
        const base = basename(original_docx_path, ext);
        const dir = dirname(original_docx_path);
        outPath = resolve(dir, `${base}_processed${ext}`);
      }

      const buf = readFileBytesOrThrow(original_docx_path);
      const doc = await DocumentObject.load(buf);
      const engine = new RedlineEngine(doc, author_name);

      let stats;
      try {
        stats = engine.process_batch(changes, dry_run);
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
      "Prepares a document for external distribution or e-signature.",
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
        .describe("Native OOXML document locking."),
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
  res += `Actions: ${stats.actions_applied} applied, ${stats.actions_skipped} skipped.\n`;
  res += `Edits: ${stats.edits_applied} applied, ${stats.edits_skipped} skipped.\n`;

  if (stats.edits && stats.edits.length > 0) {
    res += "\nDetailed Edit Reports:\n";
    for (let i = 0; i < stats.edits.length; i++) {
      const report = stats.edits[i];
      const status_indicator =
        report.status === "applied" ? "✅ [applied]" : "❌ [failed]";
      res += `Edit ${i + 1} ${status_indicator}:\n`;
      res += `  Target: '${report.target_text}'\n`;
      res += `  New text: '${report.new_text}'\n`;
      if (report.warning) {
        res += `  Warning: ${report.warning}\n`;
      }
      if (report.error) {
        res += `  Error: ${report.error}\n`;
      }
      if (report.critic_markup) {
        res += `  Preview (CriticMarkup): ${report.critic_markup}\n`;
      }
      if (report.clean_text) {
        res += `  Clean text preview: ${report.clean_text}\n`;
      }
    }
  }

  if (stats.skipped_details && stats.skipped_details.length > 0) {
    res += `\n\nSkipped Details:\n${stats.skipped_details.join("\n")}`;
  }
  return res;
}

// --- Startup ---
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(
    `Adeu MCP Server (Node.js Engine: ${identifyEngine()}) running on stdio`,
  );
}

main().catch(console.error);
