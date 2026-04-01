# FILE: src/adeu/mcp/tools/auth.py
import json
import urllib.error
import urllib.request

from adeu.auth import DesktopAuthManager
from adeu.mcp_components.shared import BACKEND_URL
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool


@tool(
    description="Logs the user into the Adeu Cloud backend. Securely opens a browser window for authentication.",
    annotations={"openWorldHint": True},
)
async def login_to_adeu_cloud(ctx: Context) -> str:
    await ctx.info("Initiating cloud authentication workflow")
    try:
        await ctx.debug("Checking DesktopAuthManager for API key")
        api_key = DesktopAuthManager.ensure_authenticated()
        if not api_key:
            await ctx.error("Failed to obtain API key from login flow")
            raise ToolError("Error: Could not obtain API key from the login flow.")

        url = f"{BACKEND_URL}/api/v1/auth/me"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

        try:
            await ctx.debug("Verifying token with backend", extra={"url": url})
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode("utf-8"))
                email = data.get("email", "Unknown Email")

                await ctx.info(
                    "Login successful",
                    extra={"email": email},
                )
                return f"Login successful! Connected to Adeu Cloud as: {email}."

        except urllib.error.HTTPError as e:
            if e.code == 401:
                await ctx.warning("Session expired or invalid token. Clearing API key.")
                DesktopAuthManager.clear_api_key()
                raise ToolError(
                    "Your previous session expired. The stale key has been cleared. "
                    "Please call the `login_to_adeu_cloud` tool ONE MORE TIME to log in fresh."
                ) from e
            await ctx.error(
                "HTTP Error verifying login",
                extra={"status_code": e.code, "reason": e.reason},
            )
            raise ToolError(f"HTTP Error verifying login: {e.code} - {e.reason}") from e

    except Exception as e:
        await ctx.error("Exception during login process", extra={"error": str(e)})
        raise ToolError(f"Error during login process: {str(e)}") from e


@tool(
    description="Logs out of the Adeu Cloud backend by clearing the local API key from the OS Keychain.",
    annotations={"openWorldHint": True},
)
async def logout_of_adeu_cloud(ctx: Context) -> str:
    await ctx.info("Initiating cloud session logout")
    try:
        DesktopAuthManager.clear_api_key()
        await ctx.debug("API key cleared from OS Keychain successfully")
        return "Successfully logged out. The local API key has been removed from the Keychain."
    except Exception as e:
        await ctx.error("Failed to clear API key during logout", extra={"error": str(e)})
        raise ToolError(f"Error during logout: {str(e)}") from e
