Here is a detailed, concise Markdown document outlining the authentication architecture we just built. You can save this as `docs/MCP_AUTH_ARCHITECTURE.md` or add it to your `ARCHITECTURE.md`.

---

# Adeu MCP Server Authentication Architecture

## 1. Overview

The Adeu Model Context Protocol (MCP) server runs locally as a `stdio` subprocess (e.g., spawned by Claude Desktop). Because it operates locally but needs to access the Adeu Cloud backend (FastAPI), it requires a secure way to authenticate as the specific Microsoft/Google user sitting at the computer.

To achieve this, we use the **OAuth 2.0 Loopback IP Address Flow** (RFC 8252) combined with **Personal Access Tokens (PATs)** securely stored in the user's OS Keychain.

## 2. Architecture Diagram

```ascii
┌──────────────────┐               ┌───────────────────────┐              ┌──────────────────┐
│                  │  1. Browser   │                       │  2. OAuth    │                  │
│    Local MCP     │ ────────────> │   Adeu Cloud Backend  │ ───────────> │   Microsoft /    │
│  (Adeu Python)   │               │       (FastAPI)       │              │   Google SSO     │
│                  │               │                       │              │                  │
│  [Port: 54321]   │ <──────────── │  4. Redirect w/ PAT   │ <─────────── │   3. Auth Code   │
└────────┬─────────┘               └───────────┬───────────┘              └──────────────────┘
         │                                     │
         │          5. API Request             │
         └─────────────────────────────────────┘
             Authorization: Bearer adeu_pat_...
```

## 3. The Step-by-Step Authentication Flow

1. **Tool Invocation**: The user asks the LLM to perform a cloud action. The LLM invokes the `login_to_adeu_cloud` tool.
2. **Ephemeral Server**: The MCP server starts a temporary, local HTTP server on a random open port (e.g., `54321`).
3. **Browser Popup**: The MCP server uses `webbrowser.open()` to direct the user to the Cloud Backend:
   `https://[BACKEND_URL]/api/v1/auth/desktop/init?port=54321`
4. **Session Routing (Backend)**: The backend saves the local port (`54321`) into the user's secure HTTP-only session cookie and redirects the user to the standard Microsoft/Google OAuth flow.
5. **OAuth Callback**: The identity provider redirects back to the backend's standard `/auth/callback`. The backend exchanges the code, fetches the user profile, and logs the user in (or respects their existing web session).
6. **PAT Generation**: Because the backend sees `desktop_port` in the session, it generates a secure Personal Access Token (`adeu_pat_...`). It hashes this token (SHA-256) and stores it in the `APIKey` database table.
7. **Loopback Redirect**: The backend issues a 302 Redirect to the local machine:
   `http://localhost:54321/callback?api_key=adeu_pat_...`
8. **Secure Storage**: The local ephemeral server catches the `api_key`, displays a "Success" HTML page to the user, saves the key to the OS Keychain (macOS Keychain / Windows Credential Manager) using the `keyring` library, and immediately shuts down.

## 4. Security Posture & Rationale

- **No Client Secrets Local**: The Microsoft OAuth `client_secret` is never shipped in the local Python package. All OAuth exchanges happen securely on the Cloud Backend.
- **Decoupled Lifecycles (PATs)**: Instead of passing the Microsoft Access Token to the local app (which expires in 1 hour), the backend issues a long-lived Adeu API Key. This prevents constant browser popups.
- **Encrypted at Rest**: The API key is stored in the OS Keychain, meaning it is encrypted at rest using the user's OS login password.
- **Zero Redirect URI Configuration**: By tracking the local port in the FastAPI session cookie, we bounce the user back to `localhost` _after_ the standard OAuth flow completes. This means no manual `http://localhost:*` wildcard configurations are needed in the Azure Portal.
- **Database Hashing**: The Cloud Backend only stores the SHA-256 hash of the API key in the `APIKey` table. A database leak will not expose plaintext desktop keys.

## 5. Backend Components

1. **`APIKey` Model (`models.py`)**: Stores `user_id`, `key_hash`, `name`, and `last_used_at`.
2. **`desktop_init` (`auth_endpoints.py`)**: Accepts the local port, stores it in `request.session`, and starts the standard OAuth flow.
3. **`callback` Interception (`auth_endpoints.py`)**: At the end of the standard callback, if `desktop_port` exists in the session, it generates the PAT, writes the hash to the DB, and redirects to localhost.
4. **`get_current_user` Dependency (`security.py`)**: Updated to check `request.headers.get("Authorization")` for `Bearer adeu_pat_...`. It hashes the token, verifies it against the `APIKey` table, updates `last_used_at`, and returns the authorized `User`.

## 6. Developer Guide: Writing Cloud-Enabled Tools

LLMs act rigidly based on tool descriptions and error outputs. To ensure the LLM automatically recovers from expired or missing API keys, **all** cloud-reliant tools must follow this standard pattern:

```python
from adeu.auth import DesktopAuthManager
import urllib.request
import urllib.error

@mcp.tool()
def fetch_cloud_data() -> str:
    """
    Fetches data from the Adeu Cloud backend.
    """
    # 1. Attempt to get the key silently
    api_key = DesktopAuthManager.get_api_key()

    # 2. Instruct the LLM to login if missing
    if not api_key:
        return "Authentication Required: You are not logged in. Please call the `login_to_adeu_cloud` tool first to authenticate, then try this task again."

    # 3. Make the secure request
    url = f"{BACKEND_URL}/api/v1/some-endpoint"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})

    try:
        with urllib.request.urlopen(req) as response:
            return response.read().decode("utf-8")

    except urllib.error.HTTPError as e:
        # 4. Handle expired/revoked keys by clearing them and instructing the LLM
        if e.code == 401:
            DesktopAuthManager.clear_api_key()
            return "Authentication Expired: Your session is invalid. I have cleared your old credentials. Please call the `login_to_adeu_cloud` tool to re-authenticate, then try this task again."

        return f"Error from backend: {e.code}"
```
