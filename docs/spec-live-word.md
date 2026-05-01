# Feature Specification: Live MS Word Interop Engine (`LiveWordEngine`)

## 1. Objective
To enable LLM agents to interact directly with an active, open document in Microsoft Word on Windows. Unlike the core `RedlineEngine` (which operates on closed `.docx` XML files), the `LiveWordEngine` will manipulate the live Word COM (Component Object Model) application. 

This enables a "Copilot" UX where the user can watch the agent type, redline, and comment on the canvas in real-time, and allows testing the complete round-trip loop of document transformations natively within MS Word.

## 2. Architectural Paradigm
*   **Dependency**: Requires `pywin32` (`win32com.client`). Restricted via `sys.platform == 'win32'`.
*   **Interface Parity**: The engine must strictly adhere to the existing `adeu.models.DocumentChange` schema (`ModifyText`, `AcceptChange`, `RejectChange`, `ReplyComment`). The LLM should not know or care whether it is editing a file on disk or a live application.
*   **Spoofing Identity**: To ensure tracked edits appear as the Agent (and not the local user), the engine temporarily hijacks the `Word.Application.UserName` property. **Limitation**: Modern Comments (M365) are strictly tied to the logged-in Microsoft Account. While text revisions will show the Agent's name, comments will inevitably show the local user's real name. Attempts to override this via `Options.UseLocalUserInfo` cause fatal COM deadlocks/freezes.

## 3. Core Components

### 3.1 Connection & State Management
*   **COM Dispatch**: Connects to the active Word instance via `win32com.client.GetActiveObject("Word.Application")`.
*   **Threading**: Microsoft Office COM objects are strictly Single-Threaded Apartment (STA). Because FastMCP handles asynchronous tool execution (potentially dispatching across thread pools or `asyncio` event loops), **we do not manage the apartment lifecycle manually**.
    *   We deliberately **omit** `pythoncom.CoUninitialize()` in our tool `finally` blocks.
    *   We let the Windows process and `pytest` runner exit naturally. Attempting to tear down the COM apartment while the Python GC or Pytest traceback mechanism still holds references to the COM proxy objects results in fatal Windows Access Violations (`0x800401fd` or `0x800706b5`).
    *   If threading architectures change in the future, use `win32com.client.DispatchEx` with localized process isolation rather than manual `CoUninitialize` wrestling.

### 3.2 Live Extraction (`read_active_word_document`)
Extracting text with embedded CriticMarkup from a live COM object is fundamentally different from parsing XML.
*   **Mechanism**: The engine will read `ActiveDocument.Content.Text`.
*   **Revisions**: Instead of parsing `<w:ins>` tags, the engine will iterate over the `ActiveDocument.Revisions` collection. Each `Revision` object provides a `Range` (Start/End offsets). The engine will reconstruct the `{++...++}` and `{--...--}` CriticMarkup by mapping these offsets back to the plain text string.
*   **Comments**: Iterates `ActiveDocument.Comments`, extracting the `Range`, `Author`, and `Scope`, formatting them as `{==...==}{>>...<<}`.

### 3.3 Live Modification (`process_active_word_batch`)
The core challenge is translating the LLM's fuzzy text match into a strict COM `Range` replacement. Word's native `.Find.Execute()` is too brittle for LLM outputs (it fails on minor whitespace/quote differences).

**The Hybrid Matching Strategy:**
1.  **Extract**: Pull the full text from the live document.
2.  **Fuzzy Match**: Run Adeu's existing `adeu.redline.mapper` against the extracted text to find the exact absolute string indices (e.g., `Start: 1045, End: 1090`).
3.  **Map to COM Range**: Convert the string indices to a Word `Range(Start, End)` object.
4.  **Execute Edit**:
    ```python
    app.TrackRevisions = True
    original_user = app.UserName
    app.UserName = "Adeu Agent"
    
    target_range = doc.Range(Start=1045, End=1090)
    target_range.Text = new_text # Word natively generates the Redline!
    
    app.UserName = original_user
    ```

### 3.4 Review Actions (Accept/Reject/Reply)
Word COM makes this incredibly straightforward compared to XML parsing:
*   **Accept**: Find `Revision` in `doc.Revisions` -> `Revision.Accept()`
*   **Reject**: Find `Revision` in `doc.Revisions` -> `Revision.Reject()`
*   **Reply**: Find `Comment` in `doc.Comments` -> `Comment.Replies.Add(Range, Text)`

*Challenge:* Matching the LLM's `target_id` (e.g., `Chg:12`) to a live COM object. The engine must generate deterministic, stable IDs during the extraction phase (e.g., based on the Revision's index or internal COM reference) so the LLM can accurately target them in the subsequent batch call.

## 4. MCP Tool Integration

New tools will be added to `src/adeu/mcp_components/tools/live_word.py`:

1.  `read_active_word_document(ctx: Context, clean_view: bool = False)`
    *   *Description*: Reads the currently active, visible document in Microsoft Word.
2.  `process_active_word_batch(ctx: Context, changes: List[DocumentChange], author_name: str)`
    *   *Description*: Applies redlines and review actions directly to the user's active MS Word window.

*Safety Gate*: These tools will conditionally register in the FastMCP server only if `sys.platform == 'win32'`. On macOS/Linux, they will not be exposed to the LLM, preventing hallucinated crashes.

## 5. UI Recommendations for Remote Contexts

If the LLM is running remotely (e.g., Mac Studio) but the MCP server and Word are running locally (Windows), the standard **Claude Desktop + API Proxy** architecture is recommended. 
*   The Windows machine runs Claude Desktop and the Adeu MCP Server.
*   Claude Desktop points to a local proxy (like `LiteLLM`), which forwards the standardized tool-calling inference requests to the remote Mac Studio (Gemma).
*   When Gemma decides to call `process_active_word_batch`, the proxy routes the tool execution back to the Windows machine, where the MCP Server uses `pywin32` to move the mouse/type in the local Word application.
*   This architecture fully supports the MCP Apps UI `iframe` protocol, allowing rich HTML interfaces to display alongside the live Word application.