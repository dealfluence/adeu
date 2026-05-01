# Model Context Protocol (MCP) Apps Extension

This document outlines the underlying JSON-RPC communication protocol (Protocol Version `2025-11-21`). 

By understanding these methods, you can build zero-dependency Vanilla JS interfaces.

## 1. Architecture Overview

An MCP App is an HTML page loaded inside an `iframe` by the Host (e.g., Claude Desktop). Communication happens exclusively via the browser's `window.postMessage` API using the JSON-RPC 2.0 specification.

*   **Host**: The AI Client (Claude Desktop).
*   **App (Client)**: The sandboxed HTML UI rendered in the iframe.
*   **Request**: A JSON-RPC message containing an `id`. The receiver *must* reply with the same `id`.
*   **Notification**: A JSON-RPC message without an `id`. No response is expected.

All messages must be wrapped in the standard envelope:
```json
{
  "jsonrpc": "2.0",
  "method": "...",
  "params": {}
}
```

---

## 2. The Handshake (Initialization)

Before the Host sends any tool results to the App, a 3-step handshake must occur.

### Step 1: `ui/initialize` (Request: App ➔ Host)
The App must initiate the connection by sending its capabilities.
*   **Method**: `ui/initialize`
*   **Payload**:
    ```json
    {
      "appInfo": { "name": "App Name", "version": "1.0.0" },
      "appCapabilities": {},
      "protocolVersion": "2025-11-21"
    }
    ```

### Step 2: Initialize Response (Response: Host ➔ App)
The Host replies (matching the `id` from Step 1) with its capabilities and context.
*   **Payload**:
    ```json
    {
      "protocolVersion": "2025-11-21",
      "hostInfo": { "name": "Claude", "version": "..." },
      "hostCapabilities": { "openLinks": {}, "message": {} },
      "hostContext": { "theme": "dark", "displayMode": "inline" }
    }
    ```

### Step 3: `ui/notifications/initialized` (Notification: App ➔ Host)
The App acknowledges it is ready to receive data.
*   **Method**: `ui/notifications/initialized`
*   **Payload**: `{}`

---

## 3. Host ➔ App Communication

These are the messages the Host sends to the App's iframe.

### `ui/notifications/tool-result` (Notification)
Delivers the final output of the Python MCP tool execution to the UI.
*   **Payload**:
    ```json
    {
      "content": [{ "type": "text", "text": "Raw fallback text" }],
      "structuredContent": { "html": "<h1>Hello</h1>" },
      "isError": false
    }
    ```

### `ui/notifications/tool-input` (Notification)
If the tool is slow or requires streaming, the host passes the resolved arguments.
*   **Payload**: `{ "arguments": { "arg1": "value" } }`

### `ui/notifications/tool-input-partial` (Notification)
Streams partial JSON arguments as the LLM generates them.
*   **Payload**: `{ "arguments": { "arg1": "val..." } }`

### `ui/notifications/tool-cancelled` (Notification)
Fired if the user or host aborts the tool execution.
*   **Payload**: `{ "reason": "user action" }`

### `ui/notifications/host-context-changed` (Notification)
Fired when the Host environment changes (e.g., the user toggled Dark Mode, or the iframe was resized).
*   **Payload**: 
    ```json
    {
      "theme": "dark", 
      "displayMode": "fullscreen",
      "safeAreaInsets": { "top": 0, "bottom": 0, "left": 0, "right": 0 }
    }
    ```

---

## 4. App ➔ Host Communication

These are the messages the App can send to ask the Host to perform actions.

### `ui/notifications/size-changed` (Notification)
Tells the host that the App's internal DOM has changed size. The Host uses this to resize the iframe.
*   **Payload**: `{ "height": 500 }`

### `ui/request-display-mode` (Request)
Asks the Host to change how the App is presented.
*   **Payload**: `{ "mode": "inline" | "fullscreen" | "pip" }`

### `ui/open-link` (Request)
Asks the Host to open a URL in the user's default external web browser (escaping the sandbox).
*   **Payload**: `{ "url": "https://example.com" }`

### `ui/message` (Request)
Pushes a new message directly into the LLM conversation context on behalf of the user.
*   **Payload**:
    ```json
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "Please analyze this chart." }
      ]
    }
    ```

### `ui/update-model-context` (Request)
Updates the hidden, machine-readable context of the App. If the LLM needs to look at the app again in a future turn, it reads this data.
*   **Payload**:
    ```json
    {
      "structuredContent": { "selectedRowId": "123" }
    }
    ```

### `tools/call` (Request)
Asks the Host to execute another MCP Tool available on the server and return the result to the App.
*   **Payload**:
    ```json
    {
      "name": "read_docx",
      "arguments": { "file_path": "C:/doc.docx" }
    }
    ```

---

## 5. Minimal Vanilla JS Implementation

Here is a boilerplate snippet that implements the minimum viable protocol for an interactive, auto-resizing App without external dependencies:

```html
<script>
  const INIT_ID = 1;
  
  window.addEventListener("message", (event) => {
    if (!event.data || event.data.jsonrpc !== "2.0") return;
    const msg = event.data;

    // 1. Handshake Phase 2 & 3
    if (msg.id === INIT_ID) {
      window.parent.postMessage({
        jsonrpc: "2.0",
        method: "ui/notifications/initialized",
        params: {}
      }, "*");
      return;
    }

    // 2. Receive Data
    if (msg.method === "ui/notifications/tool-result") {
      const data = msg.params;
      // ... render data ...
    }
  });

  // 3. Auto-Resize
  const observer = new ResizeObserver(() => {
    window.parent.postMessage({
      jsonrpc: "2.0",
      method: "ui/notifications/size-changed",
      params: { height: Math.ceil(document.documentElement.getBoundingClientRect().height) }
    }, "*");
  });
  observer.observe(document.documentElement);

  // 4. Start Handshake Phase 1
  window.parent.postMessage({
    jsonrpc: "2.0",
    id: INIT_ID,
    method: "ui/initialize",
    params: {
      appInfo: { name: "Vanilla App", version: "1.0.0" },
      appCapabilities: {},
      protocolVersion: "2025-11-21"
    }
  }, "*");
</script>
```
