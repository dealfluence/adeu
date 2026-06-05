// FILE: node/packages/mcp-server/src/tools/email.ts
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { DesktopAuthManager, getCloudAuthToken } from "../desktop-auth.js";
import { BACKEND_URL } from "../shared.js";
import { ToolResult } from "../response-builders.js";
import { createHash } from "node:crypto";

function isTimeoutError(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const name = (err as { name?: string }).name;
  return name === "TimeoutError" || name === "AbortError";
}

const CACHE_FILE = join(homedir(), ".adeu", "mcp_id_cache.json");
const MAX_CACHE_SIZE = 1000;

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "unknown size";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function loadIdCache(): Record<string, string> {
  if (existsSync(CACHE_FILE)) {
    try {
      return JSON.parse(readFileSync(CACHE_FILE, "utf-8"));
    } catch {
      return {};
    }
  }
  return {};
}

function saveIdCache(cache: Record<string, string>): void {
  try {
    mkdirSync(join(homedir(), ".adeu"), { recursive: true });
    const keys = Object.keys(cache);
    if (keys.length > MAX_CACHE_SIZE) {
      const trimmed: Record<string, string> = {};
      keys.slice(-MAX_CACHE_SIZE).forEach((k) => (trimmed[k] = cache[k]));
      cache = trimmed;
    }
    writeFileSync(CACHE_FILE, JSON.stringify(cache));
  } catch {
    /* ignore */
  }
}

function minifyEmailId(realId: string, cache: Record<string, string>): string {
  if (!realId) return realId;
  const hash = createHash("md5").update(realId).digest("hex").slice(0, 6);
  const shortId = `msg_${hash}`;
  cache[shortId] = realId;
  return shortId;
}

class StaleShortIdError extends Error {
  constructor(shortId: string) {
    super(
      `Short ID '${shortId}' is not in the local cache (it may have been evicted, or it came from a different machine/session). ` +
        `Short IDs only persist on the machine where they were generated. ` +
        `Re-run search_and_fetch_emails with filters (sender, subject, days_ago) to fetch fresh IDs, then use the new ID from those results.`,
    );
    this.name = "StaleShortIdError";
  }
}

function resolveEmailId(shortId: string): string {
  if (!shortId) return shortId;
  // adeu_<id> references are resolved server-side, pass through.
  if (shortId.startsWith("adeu_")) return shortId;
  const cache = loadIdCache();
  const resolved = cache[shortId];
  if (resolved) return resolved;
  // If it looks like one of our short IDs but isn't in the cache, fail loudly
  // instead of silently passing a meaningless string to the provider.
  if (shortId.startsWith("msg_")) {
    throw new StaleShortIdError(shortId);
  }
  // Otherwise treat it as a raw provider ID
  return shortId;
}

function stripTags(html: string): string {
  if (!html) return "";

  // 1. Strip suppressed blocks (style/script/head/title) — loop until stable to
  //    handle nested or malformed blocks. Matches Python MLStripper's structural
  //    suppression rather than relying on a single greedy pass.
  let text = html;
  const suppressPattern =
    /<(style|script|head|title)\b[^>]*>[\s\S]*?<\/\1\s*>/gi;
  let prev: string;
  do {
    prev = text;
    text = text.replace(suppressPattern, "");
  } while (text !== prev);

  // 2. Also strip orphan open tags for suppressed blocks (unclosed <style ...>)
  //    by killing from the open tag to end of document — safer than leaking CSS
  //    into the LLM output.
  text = text.replace(/<(style|script|head|title)\b[^>]*>[\s\S]*$/gi, "");

  // 3. Convert block-level closing tags to newlines so paragraph structure survives
  text = text.replace(
    /<\/?(p|div|br|hr|tr|li|h[1-6]|blockquote)\b[^>]*>/gi,
    "\n",
  );

  // 4. Strip all remaining tags
  text = text.replace(/<[^>]+>/g, "");

  // 5. Decode the most common HTML entities (the rest are harmless as-is)
  text = text
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&apos;/gi, "'");

  // 6. Collapse triple-or-more newlines down to a paragraph break
  return text.replace(/\n\s*\n\s*\n+/g, "\n\n").trim();
}

function removeNestedQuotes(text: string): string {
  if (!text) return "";

  // Localized "From:" header tokens from Outlook in major European locales.
  // Order matters only for readability; matching is anchored independently.
  const fromTokens = [
    "From", // English
    "Lähettäjä", // Finnish
    "Från", // Swedish
    "Von", // German
    "De", // French / Spanish / Portuguese
    "Da", // Italian
    "Van", // Dutch
    "Fra", // Norwegian / Danish
    "Mittente", // Italian (alt)
  ];

  // Localized "Sent:" tokens (paired with From: in Outlook quote blocks)
  const sentTokens = [
    "Sent",
    "Lähetetty",
    "Skickat",
    "Gesendet",
    "Envoyé",
    "Enviado",
    "Inviato",
    "Verzonden",
    "Sendt",
  ];

  // Localized "On ... wrote:" / "X wrote on Y:" patterns from Gmail-style clients
  const wrotePatterns = [
    /On .{1,200}? wrote:/, // English
    /Le .{1,200}? a écrit\s*:/i, // French
    /Am .{1,200}? schrieb .{1,100}?:/i, // German
    /El .{1,200}? escribió\s*:/i, // Spanish
    /Il .{1,200}? ha scritto\s*:/i, // Italian
    /Op .{1,200}? schreef .{1,100}?:/i, // Dutch
    /Den .{1,200}? skrev .{1,100}?:/i, // Swedish/Norwegian/Danish
    /Em .{1,200}? escreveu\s*:/i, // Portuguese
    /Em\b.{1,200}?, .{1,200}? escreveu\s*:/i, // Portuguese (date prefix)
    new RegExp(
      `^(${fromTokens.join("|")})\\s*:.*?\\n(?:.*\\n){0,5}?(${sentTokens.join("|")})\\s*:`,
      "m",
    ),
  ];

  const dividerPatterns = [
    /_{10,}/m,
    /-----\s*(Original Message|Alkuperäinen viesti|Ursprüngliches Nachricht|Message d'origine|Mensaje original|Messaggio originale|Oorspronkelijk bericht|Original meddelande)\s*-----/im,
    /^(Original Message|Alkuperäinen viesti|Ursprüngliches Nachricht|Message d'origine|Mensaje original|Messaggio originale|Oorspronkelijk bericht)$/im,
  ];

  const allPatterns = [...wrotePatterns, ...dividerPatterns];

  let earliestCut = text.length;
  for (const pattern of allPatterns) {
    const match = pattern.exec(text);
    if (match && match.index < earliestCut) {
      earliestCut = match.index;
    }
  }
  return text.substring(0, earliestCut).trim();
}

function getUniqueFilepath(saveDir: string, filename: string): string {
  let filepath = join(saveDir, filename);
  let counter = 1;
  const parts = filename.split(".");
  const ext = parts.length > 1 ? `.${parts.pop()}` : "";
  const stem = parts.join(".");

  while (existsSync(filepath)) {
    filepath = join(saveDir, `${stem}_${counter}${ext}`);
    counter++;
  }
  return filepath;
}

export async function search_and_fetch_emails(args: any): Promise<ToolResult> {
  const apiKey = await getCloudAuthToken();
  const maxAttachmentSizeMb: number =
    typeof args.max_attachment_size_mb === "number" &&
    args.max_attachment_size_mb > 0
      ? args.max_attachment_size_mb
      : 10;
  let realEmailId: string | undefined;
  try {
    realEmailId = args.email_id ? resolveEmailId(args.email_id) : undefined;
  } catch (err) {
    if (err instanceof StaleShortIdError) {
      return {
        isError: true,
        content: [{ type: "text", text: err.message }],
      };
    }
    throw err;
  }

  const payload = {
    email_id: realEmailId,
    sender: args.sender,
    subject: args.subject,
    has_attachments: args.has_attachments,
    attachment_name: args.attachment_name,
    is_unread: args.is_unread,
    days_ago: args.days_ago,
    folder: args.folder,
    limit: args.limit ?? 10,
    offset: args.offset ?? 0,
    mailbox_address: args.mailbox_address,
  };

  // Remove undefined fields
  Object.keys(payload).forEach(
    (k) => (payload as any)[k] === undefined && delete (payload as any)[k],
  );

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/api/v1/emails/search`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(45_000),
    });
  } catch (err) {
    if (isTimeoutError(err)) {
      throw new Error(
        "Email search timed out after 45s. The mail provider (Outlook/Gmail) may be slow. Try narrowing the search with more filters (sender, subject, days_ago), or retry shortly.",
      );
    }
    throw err;
  }

  if (res.status === 401) {
    DesktopAuthManager.clearApiKey();
    throw new Error(
      "Authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.",
    );
  }
  if (!res.ok) throw new Error(`Cloud search failed: ${await res.text()}`);

  const data: any = await res.json();
  const cache = loadIdCache();

  if (data.type === "previews") {
    const previews = data.previews || [];
    if (!previews.length)
      return {
        content: [
          {
            type: "text",
            text: "No emails found matching your search criteria.",
          },
        ],
      };

    const lines = [
      `Found ${previews.length} email(s). Here are the previews:`,
      "",
    ];
    for (const p of previews) {
      const shortId = minifyEmailId(p.id, cache);
      const attFlag = p.has_attachments ? "📎 (Has Attachments)" : "";
      const unreadFlag = p.is_read === false ? "🟢 [UNREAD]" : "";
      lines.push(
        `- **ID**: \`${shortId}\`\n  **Subject**: ${p.subject} ${attFlag} ${unreadFlag}\n  **From**: ${p.sender_name} <${p.sender_email}>\n  **Date**: ${p.received_datetime}\n  **Preview**: ${p.preview_text}\n`,
      );
    }
    saveIdCache(cache);
    lines.push(
      "⚠️ **ACTION REQUIRED**: To read the full body of an email and download its attachments, call this tool again and provide the exact `email_id`.",
    );
    return {
      content: [{ type: "text", text: lines.join("\n") }],
      structuredContent: data,
    };
  }

  if (data.type === "full_email") {
    const full = data.full_email || {};
    const shortTargetId = minifyEmailId(full.id || "unknown_id", cache);

    saveIdCache(cache);

    const baseDir =
      args.working_directory && existsSync(args.working_directory)
        ? args.working_directory
        : tmpdir();
    const saveDir = join(
      baseDir,
      args.working_directory ? "adeu_attachments" : "adeu_downloads",
      shortTargetId,
    );
    mkdirSync(saveDir, { recursive: true });

    interface SkippedAttachment {
      filename: string;
      size_bytes: number | null;
      reason: string;
    }

    async function processAttachments(
      msg: any,
    ): Promise<{ localFiles: string[]; skipped: SkippedAttachment[] }> {
      const localFiles: string[] = [];
      const skipped: SkippedAttachment[] = [];
      const maxBytes = maxAttachmentSizeMb * 1024 * 1024;

      for (const att of msg.attachments || []) {
        const filename = att.filename || "unnamed_file";
        const size: number | null =
          typeof att.size_bytes === "number" ? att.size_bytes : null;

        // Size cap: skip download but record it so the agent knows the file exists
        if (size != null && size > maxBytes) {
          skipped.push({
            filename,
            size_bytes: size,
            reason: `exceeds ${maxAttachmentSizeMb} MB cap`,
          });
          delete att.base64_data; // Drop payload from structured response too
          continue;
        }

        if (att.base64_data) {
          try {
            const filepath = getUniqueFilepath(saveDir, filename);
            writeFileSync(filepath, Buffer.from(att.base64_data, "base64"));
            localFiles.push(filepath);
            att.local_path = filepath; // For UI rendering (matches Python parity)
            delete att.base64_data; // Free memory
          } catch (e) {
            console.error(`Failed to save attachment ${filename}`, e);
            skipped.push({
              filename,
              size_bytes: size,
              reason: `download failed: ${(e as Error).message}`,
            });
          }
        }
      }
      return { localFiles, skipped };
    }

    const { localFiles: targetFiles, skipped: targetSkipped } =
      await processAttachments(full);
    const lines = [
      `# Email Thread: ${full.subject}`,
      "",
      "## Target Message (Newest):",
      `**From**: ${full.sender_name} <${full.sender_email}>`,
      `**Date**: ${full.received_datetime}`,
    ];

    if (targetFiles.length) {
      lines.push("**Attachments Saved Locally**:");
      targetFiles.forEach((f) => lines.push(`- 📎 \`${f}\``));
    }

    if (targetSkipped.length) {
      lines.push(
        `**Attachments Skipped (not downloaded)** — pass \`max_attachment_size_mb\` to raise the ${maxAttachmentSizeMb} MB cap:`,
      );
      targetSkipped.forEach((s) =>
        lines.push(
          `- ⚠️ \`${s.filename}\` (${formatBytes(s.size_bytes)}, ${s.reason})`,
        ),
      );
    }

    const cleanBody = removeNestedQuotes(stripTags(full.body_html || ""));
    lines.push(`**Body**:\n\`\`\`\n${cleanBody}\n\`\`\`\n`);

    if (full.is_thread && full.messages?.length) {
      lines.push("## Previous Messages in Thread (Historical Context):");
      for (let i = 0; i < full.messages.length; i++) {
        const histMsg = full.messages[i];
        const { localFiles: histFiles, skipped: histSkipped } =
          await processAttachments(histMsg);
        lines.push(
          `### Message -${i + 1} (Older)\n**From**: ${histMsg.sender_name} <${histMsg.sender_email}>\n**Date**: ${histMsg.received_datetime}`,
        );
        if (histFiles.length) {
          lines.push("**Attachments Saved Locally**:");
          histFiles.forEach((f) => lines.push(`- 📎 \`${f}\``));
        }
        if (histSkipped.length) {
          lines.push(
            `**Attachments Skipped (not downloaded)** — pass \`max_attachment_size_mb\` to raise the ${maxAttachmentSizeMb} MB cap:`,
          );
          histSkipped.forEach((s) =>
            lines.push(
              `- ⚠️ \`${s.filename}\` (${formatBytes(s.size_bytes)}, ${s.reason})`,
            ),
          );
        }
        lines.push(
          `**Body**:\n\`\`\`\n${removeNestedQuotes(stripTags(histMsg.body_html || ""))}\n\`\`\`\n`,
        );
      }
    }
    return {
      content: [{ type: "text", text: lines.join("\n") }],
      structuredContent: data,
    };
  }

  return {
    isError: true,
    content: [{ type: "text", text: "Unknown response format from backend." }],
  };
}

export async function create_email_draft(args: any): Promise<ToolResult> {
  const apiKey = await getCloudAuthToken();
  if (!args.reply_to_email_id && (!args.subject || !args.to_recipients)) {
    throw new Error(
      "You must provide either 'reply_to_email_id' OR both 'subject' and 'to_recipients'.",
    );
  }

  const formData = new FormData();
  formData.append("body_markdown", args.body_markdown);

  if (args.reply_to_email_id) {
    try {
      formData.append(
        "reply_to_email_id",
        resolveEmailId(args.reply_to_email_id),
      );
    } catch (err) {
      if (err instanceof StaleShortIdError) {
        return {
          isError: true,
          content: [{ type: "text", text: err.message }],
        };
      }
      throw err;
    }
  }
  if (args.subject) formData.append("subject", args.subject);
  if (args.mailbox_address) {
    formData.append("mailbox_address", args.mailbox_address);
  }

  if (args.to_recipients) {
    const recips =
      typeof args.to_recipients === "string"
        ? JSON.parse(args.to_recipients)
        : args.to_recipients;
    formData.append("to_recipients", JSON.stringify(recips));
  }

  if (args.attachment_paths) {
    const paths =
      typeof args.attachment_paths === "string"
        ? JSON.parse(args.attachment_paths)
        : args.attachment_paths;
    for (const p of paths) {
      const buf = readFileSync(p);
      const filename = p.split(/[/\\]/).pop();
      formData.append("files", new Blob([buf]), filename);
    }
  }

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/api/v1/emails/drafts/new`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        Accept: "application/json",
      },
      body: formData as any,
      signal: AbortSignal.timeout(90_000),
    });
  } catch (err) {
    if (isTimeoutError(err)) {
      throw new Error(
        "Draft creation timed out after 90s. If the draft includes large attachments, try splitting them across multiple drafts or omitting the largest files.",
      );
    }
    throw err;
  }

  if (res.status === 401) {
    DesktopAuthManager.clearApiKey();
    throw new Error(
      "Authentication expired. Please call `login_to_adeu_cloud`.",
    );
  }
  if (!res.ok)
    throw new Error(`Cloud draft creation failed: ${await res.text()}`);

  const data: any = await res.json();
  return {
    content: [
      {
        type: "text",
        text: `Successfully created email draft! Draft ID: ${data.id}`,
      },
    ],
  };
}
export async function list_available_mailboxes(): Promise<ToolResult> {
  const apiKey = await getCloudAuthToken();

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/api/v1/users/me/shared-mailboxes`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        Accept: "application/json",
      },
      signal: AbortSignal.timeout(15_000),
    });
  } catch (err) {
    if (isTimeoutError(err)) {
      throw new Error(
        "Listing mailboxes timed out after 15s. The Adeu backend may be temporarily unavailable; retry shortly.",
      );
    }
    throw err;
  }

  if (res.status === 401) {
    DesktopAuthManager.clearApiKey();
    throw new Error(
      "Authentication expired. Please call `login_to_adeu_cloud` to re-authenticate.",
    );
  }
  if (!res.ok) {
    throw new Error(`Failed to list available mailboxes: ${await res.text()}`);
  }

  const mailboxes: any[] = await res.json();
  if (!mailboxes.length) {
    return {
      content: [
        {
          type: "text",
          text: "No configured mailboxes found for your profile.",
        },
      ],
    };
  }

  const lines = [
    "### Connected Mailboxes",
    "Below is the list of connected mailboxes you have access to. Use the `email_address` as the `mailbox_address` parameter in other tools to query or draft from a specific mailbox:",
    "",
  ];

  for (const box of mailboxes) {
    lines.push(
      `- **${box.display_name || "Personal Mailbox"}**\n  - **Email Address**: \`${box.email_address}\`\n  - **Auto-Processing**: ${box.auto_process_enabled ? "Enabled" : "Disabled"}\n  - **Write-Back Mode**: \`${box.write_back_preference}\``,
    );
  }

  return {
    content: [{ type: "text", text: lines.join("\n") }],
  };
}
