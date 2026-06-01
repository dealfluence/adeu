// FILE: node/packages/mcp-server/src/tools/email.ts
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { DesktopAuthManager, getCloudAuthToken } from "../desktop-auth.js";
import { BACKEND_URL } from "../shared.js";
import { ToolResult } from "../response-builders.js";
import { createHash } from "node:crypto";

const CACHE_FILE = join(homedir(), ".adeu", "mcp_id_cache.json");
const MAX_CACHE_SIZE = 1000;

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

function resolveEmailId(shortId: string): string {
  if (!shortId) return shortId;
  const cache = loadIdCache();
  return cache[shortId] || shortId;
}

function stripTags(html: string): string {
  if (!html) return "";
  let text = html.replace(/<(style|script|head)[^>]*>[\s\S]*?<\/\1>/gi, "");
  text = text.replace(
    /<\/?(p|div|br|hr|tr|li|h[1-6]|blockquote)\b[^>]*>/gi,
    "\n",
  );
  text = text.replace(/<[^>]+>/g, "");
  return text.replace(/\n\s*\n\s*\n+/g, "\n\n").trim();
}

function removeNestedQuotes(text: string): string {
  if (!text) return "";
  const patterns = [
    /_{10,}/m,
    /^From:\s.*?\n(?:.*\n){0,5}?Sent:\s/m,
    /-----Original Message-----/m,
    /On .{1,200}? wrote:/m,
    /^Original Message$/m,
  ];
  let earliestCut = text.length;
  for (const pattern of patterns) {
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
  const realEmailId = args.email_id ? resolveEmailId(args.email_id) : undefined;

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

  const res = await fetch(`${BACKEND_URL}/api/v1/emails/search`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

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

    async function processAttachments(msg: any): Promise<string[]> {
      const localFiles: string[] = [];
      for (const att of msg.attachments || []) {
        if (att.base64_data) {
          try {
            const filepath = getUniqueFilepath(
              saveDir,
              att.filename || "unnamed_file",
            );
            writeFileSync(filepath, Buffer.from(att.base64_data, "base64"));
            localFiles.push(filepath);
            delete att.base64_data; // Free memory
          } catch (e) {
            console.error(`Failed to save attachment ${att.filename}`, e);
          }
        }
      }
      return localFiles;
    }

    const targetFiles = await processAttachments(full);
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

    const cleanBody = removeNestedQuotes(stripTags(full.body_html || ""));
    lines.push(`**Body**:\n\`\`\`\n${cleanBody}\n\`\`\`\n`);

    if (full.is_thread && full.messages?.length) {
      lines.push("## Previous Messages in Thread (Historical Context):");
      for (let i = 0; i < full.messages.length; i++) {
        const histMsg = full.messages[i];
        const histFiles = await processAttachments(histMsg);
        lines.push(
          `### Message -${i + 1} (Older)\n**From**: ${histMsg.sender_name} <${histMsg.sender_email}>\n**Date**: ${histMsg.received_datetime}`,
        );
        if (histFiles.length) {
          lines.push("**Attachments Saved Locally**:");
          histFiles.forEach((f) => lines.push(`- 📎 \`${f}\``));
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
    formData.append(
      "reply_to_email_id",
      resolveEmailId(args.reply_to_email_id),
    );
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

  const res = await fetch(`${BACKEND_URL}/api/v1/emails/drafts/new`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" },
    body: formData as any,
  });

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

  const res = await fetch(`${BACKEND_URL}/api/v1/users/me/shared-mailboxes`, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      Accept: "application/json",
    },
  });

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
