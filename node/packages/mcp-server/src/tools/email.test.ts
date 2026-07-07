import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  mkdtempSync,
  mkdirSync,
  rmSync,
  writeFileSync,
  existsSync,
  readFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { homedir, tmpdir } from "node:os";
import {
  search_and_fetch_emails,
  list_available_mailboxes,
  create_email_draft,
} from "./email.js";

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

  describe("Stateful Polling symmetry with Validation", () => {
    const sleepMock = vi.spyOn(global, "setTimeout");

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("should handle async task initiation upon searching and resolve when the task completes successfully", async () => {
      let callCount = 0;
      global.fetch = vi.fn().mockImplementation(async () => {
        callCount++;
        if (callCount === 1) {
          return {
            ok: true,
            status: 202,
            json: async () => ({
              status: "pending",
              task_id: "email_task_typescript_123",
              message: "Queued",
            }),
          } as Response;
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "COMPLETED",
            type: "previews",
            previews: [],
          }),
        } as Response;
      });

      const promise = search_and_fetch_emails({ subject: "heavy search" });
      promise.catch(() => {});
      await vi.runAllTimersAsync();

      const result = await promise;

      expect(callCount).toBe(2);
      expect(result.content[0].text).toContain("No emails found matching your search criteria.");
    });

    it("should handle async task initiation upon searching and return pending status on polling timeout (50s)", async () => {
      let callCount = 0;
      global.fetch = vi.fn().mockImplementation(async () => {
        callCount++;
        if (callCount === 1) {
          return {
            ok: true,
            status: 202,
            json: async () => ({
              status: "pending",
              task_id: "email_task_typescript_123",
              message: "Queued",
            }),
          } as Response;
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: "PENDING" }),
        } as Response;
      });

      const promise = search_and_fetch_emails({ subject: "heavy search" });
      promise.catch(() => {});

      for (let i = 0; i < 10; i++) {
        await vi.advanceTimersByTimeAsync(5000);
      }
      await vi.runAllTimersAsync();

      const result = await promise;

      expect(callCount).toBe(11);
      expect(result.content[0].text).toContain("is still processing");
      expect(result.content[0].text).toContain("task_id=email_task_typescript_123");
      expect(result.structuredContent?.status).toBe("pending");
      expect(result.structuredContent?.task_id).toBe("email_task_typescript_123");
    });

    it("should poll and resolve when a task completes successfully", async () => {
      let callCount = 0;
      global.fetch = vi.fn().mockImplementation(async () => {
        callCount++;
        if (callCount === 1) {
          return {
            ok: true,
            status: 200,
            json: async () => ({ status: "PENDING" }),
          } as Response;
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({
            status: "COMPLETED",
            type: "previews",
            previews: [],
          }),
        } as Response;
      });

      const promise = search_and_fetch_emails({ task_id: "email_task_typescript_123" });
      promise.catch(() => {}); // Suppress Vitest's unhandled rejection warnings during timer advance
      await vi.runAllTimersAsync();
      
      const result = await promise;

      expect(callCount).toBe(2);
      expect(result.content[0].text).toContain("No emails found matching your search criteria.");
    });

    it("should throw standard ToolError on task failure", async () => {
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          status: "FAILED",
          error: "API authorization revoked.",
        }),
      } as Response);

      const promise = search_and_fetch_emails({ task_id: "email_task_typescript_123" });
      const caughtPromise = promise.catch((err) => err);
      await vi.runAllTimersAsync();

      await expect(promise).rejects.toThrowError(
        "Validation task failed on the server: API authorization revoked."
      );
    });

    it("should gracefully return a pending status on polling timeout (50s)", async () => {
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ status: "PENDING" }),
      } as Response);

      const promise = search_and_fetch_emails({ task_id: "email_task_typescript_123" });

      // Advance all 10 polling intervals
      for (let i = 0; i < 10; i++) {
        await vi.advanceTimersByTimeAsync(5000);
      }
      await vi.runAllTimersAsync();

      const result = await promise;
      expect(result.content[0].text).toContain("is still processing");
      expect(result.content[0].text).toContain("task_id=email_task_typescript_123");
      expect(result.structuredContent?.status).toBe("pending");
      expect(result.structuredContent?.task_id).toBe("email_task_typescript_123");
    });
  });
});

describe("Working directory resolution (silent /tmp fallback fix)", () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
  });

  function mockFullEmailFetch(emailId: string) {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        type: "full_email",
        full_email: {
          id: emailId,
          subject: "Attachment Delivery",
          sender_name: "Legal",
          sender_email: "legal@adeu.ai",
          received_datetime: "2026-01-01T12:00:00Z",
          body_html: "<p>See attachment.</p>",
          is_thread: false,
          attachments: [
            {
              filename: "questionnaire.docx",
              size_bytes: 128,
              base64_data: Buffer.from("questionnaire contents").toString(
                "base64",
              ),
            },
          ],
        },
      }),
    } as Response);
  }

  it("creates a missing working_directory recursively and saves attachments inside it", async () => {
    const root = mkdtempSync(join(tmpdir(), "adeu-wd-test-"));
    const requestedDir = join(root, "questionnaires", "nested");
    try {
      mockFullEmailFetch("adeu_777");

      const result = await search_and_fetch_emails({
        email_id: "adeu_777",
        working_directory: requestedDir,
      });

      const text = result.content[0].text;
      expect(existsSync(requestedDir)).toBe(true);
      expect(text).not.toContain("Attachment location notice");

      const savedPathMatch = text.match(/📎 `([^`]+)`/);
      expect(savedPathMatch).not.toBeNull();
      const savedPath = savedPathMatch![1];
      expect(savedPath.startsWith(join(requestedDir, "adeu_attachments"))).toBe(
        true,
      );
      expect(readFileSync(savedPath, "utf-8")).toBe("questionnaire contents");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("falls back to the temp dir WITH an explicit notice when working_directory cannot be created", async () => {
    const root = mkdtempSync(join(tmpdir(), "adeu-wd-test-"));
    const blockerFile = join(root, "blocker.txt");
    writeFileSync(blockerFile, "not a directory");
    try {
      mockFullEmailFetch("adeu_778");

      const result = await search_and_fetch_emails({
        email_id: "adeu_778",
        working_directory: join(blockerFile, "sub"),
      });

      const text = result.content[0].text;
      expect(text).toContain("Attachment location notice");
      expect(text).toContain("do NOT re-run the search");

      const savedPathMatch = text.match(/📎 `([^`]+)`/);
      expect(savedPathMatch).not.toBeNull();
      const savedPath = savedPathMatch![1];
      expect(savedPath.startsWith(join(tmpdir(), "adeu_downloads"))).toBe(true);
      expect(existsSync(savedPath)).toBe(true);

      rmSync(dirname(savedPath), { recursive: true, force: true });
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("returns structuredContent for empty preview results so the UI can dismiss its skeleton", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ type: "previews", previews: [] }),
    } as Response);

    const result = await search_and_fetch_emails({ subject: "nothing matches" });
    expect(result.content[0].text).toContain(
      "No emails found matching your search criteria.",
    );
    expect(result.structuredContent).toEqual({ type: "previews", previews: [] });
  });
});

describe("Mailbox-aware short ID cache", () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("re-applies the search's mailbox_address when fetching by short ID without one", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        type: "previews",
        previews: [
          {
            id: "AAMkAD_shared_item_1",
            subject: "Questionnaire",
            sender_name: "Abo Shoten",
            sender_email: "ops@aboshoten.example",
            received_datetime: "2026-07-07T09:00:00Z",
            preview_text: "Please fill in",
            has_attachments: true,
            is_read: false,
          },
          {
            id: "AAMkAD_shared_item_2",
            subject: "Other mail",
            sender_name: "Abo Shoten",
            sender_email: "ops@aboshoten.example",
            received_datetime: "2026-07-07T09:01:00Z",
            preview_text: "Something else",
            has_attachments: false,
            is_read: true,
          },
        ],
      }),
    } as Response);

    const searchRes = await search_and_fetch_emails({
      subject: "Questionnaire",
      mailbox_address: "risto.kariranta@ahti.io",
    });
    const shortIdMatch = searchRes.content[0].text.match(/msg_[0-9a-f]{6}/);
    expect(shortIdMatch).not.toBeNull();
    const shortId = shortIdMatch![0];

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        type: "full_email",
        full_email: {
          id: "AAMkAD_shared_item_1",
          subject: "Questionnaire",
          sender_name: "Abo Shoten",
          sender_email: "ops@aboshoten.example",
          received_datetime: "2026-07-07T09:00:00Z",
          body_html: "<p>Please fill in.</p>",
          is_thread: false,
          attachments: [],
        },
      }),
    } as Response);

    await search_and_fetch_emails({ email_id: shortId });

    const fetchBody = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(fetchBody.email_id).toBe("AAMkAD_shared_item_1");
    expect(fetchBody.mailbox_address).toBe("risto.kariranta@ahti.io");
  });

  it("still resolves legacy plain-string cache entries without injecting a mailbox", async () => {
    // Seed a legacy-format entry directly into the cache file (merge, don't clobber).
    const cachePath = join(homedir(), ".adeu", "mcp_id_cache.json");
    mkdirSync(join(homedir(), ".adeu"), { recursive: true });
    let existing: Record<string, unknown> = {};
    try {
      existing = JSON.parse(readFileSync(cachePath, "utf-8"));
    } catch {
      /* no cache yet */
    }
    existing["msg_leg01"] = "raw_provider_id_123";
    writeFileSync(cachePath, JSON.stringify(existing));

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        type: "full_email",
        full_email: {
          id: "raw_provider_id_123",
          subject: "Legacy",
          sender_name: "Old Cache",
          sender_email: "old@cache.example",
          received_datetime: "2026-07-07T09:00:00Z",
          body_html: "<p>hi</p>",
          is_thread: false,
          attachments: [],
        },
      }),
    } as Response);
    global.fetch = fetchMock;

    await search_and_fetch_emails({ email_id: "msg_leg01" });

    const fetchBody = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(fetchBody.email_id).toBe("raw_provider_id_123");
    expect(fetchBody.mailbox_address).toBeUndefined();
  });

  it("maps known errors to recovery hints when an async task fails", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: "FAILED", error: "Email not found." }),
    } as Response);

    await expect(
      search_and_fetch_emails({ task_id: "email_task_777" }),
    ).rejects.toThrowError(
      /re-run search_and_fetch_emails with filters[\s\S]*mailbox_address/,
    );
  });

  it("re-applies the cached mailbox when replying to a short ID without one", async () => {
    // Seed a new-format entry directly into the cache file (merge, don't clobber).
    const cachePath = join(homedir(), ".adeu", "mcp_id_cache.json");
    mkdirSync(join(homedir(), ".adeu"), { recursive: true });
    let existing: Record<string, unknown> = {};
    try {
      existing = JSON.parse(readFileSync(cachePath, "utf-8"));
    } catch {
      /* no cache yet */
    }
    existing["msg_rep01"] = {
      id: "AAMkAD_reply_item_1",
      mailbox: "sales@ahti.io",
    };
    writeFileSync(cachePath, JSON.stringify(existing));

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: "draft_1" }),
    } as Response);
    global.fetch = fetchMock;

    await create_email_draft({
      reply_to_email_id: "msg_rep01",
      body_markdown: "Thanks, will do!",
    });

    const formData = fetchMock.mock.calls[0][1].body as FormData;
    expect(formData.get("reply_to_email_id")).toBe("AAMkAD_reply_item_1");
    expect(formData.get("mailbox_address")).toBe("sales@ahti.io");
  });
});
