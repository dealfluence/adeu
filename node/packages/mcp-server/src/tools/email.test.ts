// FILE: node/packages/mcp-server/src/tools/email.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { search_and_fetch_emails, list_available_mailboxes } from "./email.js";

// Mock the Auth module so tests bypass active browser logins
vi.mock("../desktop-auth.js", () => {
  return {
    getCloudAuthToken: vi.fn().mockResolvedValue("mock_token_abc"),
    DesktopAuthManager: {
      getApiKey: vi.fn().mockReturnValue("mock_token_abc"),
      clearApiKey: vi.fn(),
    },
  };
});

describe("Node Email Tools Finding #2 and Finding #6 tests", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("Finding #6: Correctly handles stale msg_ short IDs inside tool boundary", async () => {
    const result = await search_and_fetch_emails({
      email_id: "msg_stale99",
    });

    expect(result.isError).toBe(true);
    const text = result.content[0].text;
    expect(text).toContain("is not in the local cache");
    expect(text).toContain("evicted, or it came from a different machine");
    expect(text).toContain("Re-run search_and_fetch_emails with filters");
  });

  it("Finding #6: Maps backend Mailbox Not Found 404 error to Node boundary ToolError", async () => {
    // Stub fetch to simulate a 404 response with Mailbox error body
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      text: async () =>
        JSON.stringify({
          detail: "Mailbox 'bogus@nowhere.invalid' not found.",
        }),
    } as Response);

    await expect(
      search_and_fetch_emails({ mailbox_address: "bogus@nowhere.invalid" }),
    ).rejects.toThrowError(
      "Cloud search failed (HTTP 404): The mailbox 'bogus@nowhere.invalid' is not connected to your Adeu account. Call list_available_mailboxes to see valid mailbox addresses, then retry with one of those as `mailbox_address`.",
    );
  });

  it("Finding #6: Maps backend Email Not Found 404 error to Node boundary ToolError", async () => {
    // Stub fetch to simulate 404 for an invalid adeu_ ID (which bypasses local cache checks)
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      text: async () => JSON.stringify({ detail: "Email not found." }),
    } as Response);

    await expect(
      search_and_fetch_emails({ email_id: "adeu_9999" }),
    ).rejects.toThrowError(
      "Cloud search failed (HTTP 404): The email ID was not found. If this was a short ID (msg_*), it may have been evicted from the local cache or come from a different machine — re-run search_and_fetch_emails with filters to get a fresh ID. If it was an adeu_<numeric> or raw provider ID, verify it's correct.",
    );
  });

  it("Finding #2: Asserts correct Markdown parity and Personal Mailbox fallback on list_available_mailboxes", async () => {
    // Stub fetch to return one null-display mailbox and one alphabetical secondary mailbox
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => [
        {
          email_address: "secondary@adeu.ai",
          display_name: "Secondary Mailbox",
          auto_process_enabled: false,
          write_back_preference: "INTERNAL",
        },
        {
          email_address: "primary@adeu.ai",
          display_name: null, // Tests 'Personal Mailbox' fallback
          auto_process_enabled: true,
          write_back_preference: "DRAFT",
        },
      ],
    } as Response);

    const result = await list_available_mailboxes();
    expect(result.isError).toBeFalsy();

    const output = result.content[0].text;

    // Verify preamble parity
    expect(output).toContain("### Connected Mailboxes");
    expect(output).toContain(
      "Below is the list of connected mailboxes you have access to.",
    );

    // Verify Fallback formatting
    expect(output).toContain("**Personal Mailbox**");

    // Verify deterministic alphabetical sorting by email address (primary before secondary)
    const idxPrimary = output.indexOf("primary@adeu.ai");
    const idxSecondary = output.indexOf("secondary@adeu.ai");
    expect(idxPrimary).not.toBe(-1);
    expect(idxSecondary).not.toBe(-1);
    expect(idxPrimary).toBeLessThan(idxSecondary);

    // Verify labels formatting matches Python exactly
    expect(output).toContain("- **Email Address**:");
    expect(output).toContain("- **Auto-Processing**:");
    expect(output).toContain("- **Write-Back Mode**:");
  });

  it("Finding #6: Gracefully maps generic, unmapped server errors on search_and_fetch_emails", async () => {
    // Simulate generic HTTP 500 error
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: async () =>
        JSON.stringify({ detail: "Database connection failed." }),
    } as Response);

    await expect(search_and_fetch_emails({ limit: 10 })).rejects.toThrowError(
      "Cloud search failed (HTTP 500): Database connection failed.",
    );
  });

  it("Finding #6: Converts abort/timeout errors to actionable ToolErrors on search_and_fetch_emails", async () => {
    // Simulate a native fetch Timeout/AbortError
    const timeoutError = new Error("The operation was aborted.");
    timeoutError.name = "AbortError";
    global.fetch = vi.fn().mockRejectedValue(timeoutError);

    await expect(search_and_fetch_emails({ limit: 10 })).rejects.toThrowError(
      "Email search timed out after 45s. The mail provider (Outlook/Gmail) may be slow.",
    );
  });

  it("Findings #3, #9, and #11: Asserts preview pagination, auto-escalation note, and downstream tool suggests", async () => {
    // --- Scenario 1: Previews listing with limit met (Finding #3 pagination hint) ---
    const mockPreviewsPayload = {
      type: "previews",
      previews: [
        {
          id: "id1",
          subject: "Subject 1",
          sender_name: "Sender 1",
          sender_email: "s1@adeu.ai",
          received_datetime: "2026-01-01T12:00:00Z",
          preview_text: "Text 1",
          has_attachments: false,
          is_read: true,
        },
        {
          id: "id2",
          subject: "Subject 2",
          sender_name: "Sender 2",
          sender_email: "s2@adeu.ai",
          received_datetime: "2026-01-01T12:00:00Z",
          preview_text: "Text 2",
          has_attachments: false,
          is_read: true,
        },
      ],
    };

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => mockPreviewsPayload,
    } as Response);

    const resPreviews = await search_and_fetch_emails({
      subject: "Invoice",
      limit: 2,
      offset: 0,
    });

    const previewsText = resPreviews.content[0].text;
    expect(previewsText).toContain(
      "*(If you need to see more results, call this tool again with offset=2)*",
    );

    // --- Scenario 2: Single result auto-escalation (Finding #11 banner notice) ---
    const mockFullEmailPayload = {
      type: "full_email",
      full_email: {
        id: "adeu_12345",
        subject: "Contract Review Required",
        sender_name: "Legal",
        sender_email: "legal@adeu.ai",
        received_datetime: "2026-01-01T12:00:00Z",
        body_html: "<p>Please look at this document.</p>",
        is_thread: false,
        attachments: [],
      },
    };

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => mockFullEmailPayload,
    } as Response);

    const resEscalation = await search_and_fetch_emails({
      subject: "Contract Review Required",
    });

    const escalationText = resEscalation.content[0].text;
    expect(
      escalationText.startsWith(
        "_(Search returned exactly one result; auto-fetched full email below.)_",
      ),
    ).toBe(true);

    // --- Scenario 3: Suggestions for attachments (Finding #9 downstream hint) ---
    const mockAttachmentsPayload = {
      type: "full_email",
      full_email: {
        id: "adeu_12345",
        subject: "Contract Attachment",
        sender_name: "Legal",
        sender_email: "legal@adeu.ai",
        received_datetime: "2026-01-01T12:00:00Z",
        body_html: "<p>Please see attachment.</p>",
        is_thread: false,
        attachments: [
          {
            filename: "draft_contract.docx",
            size_bytes: 1024,
            base64_data: Buffer.from("dummy docx contents").toString("base64"),
          },
        ],
      },
    };

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => mockAttachmentsPayload,
    } as Response);

    const resAttachments = await search_and_fetch_emails({
      email_id: "adeu_12345",
    });

    const attachmentsText = resAttachments.content[0].text;
    expect(attachmentsText).toContain(
      "You can now use tools like `read_docx`, `diff_docx_files`, or `finalize_document`",
    );
  });
});
