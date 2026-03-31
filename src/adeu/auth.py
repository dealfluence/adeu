# FILE: src/adeu/auth.py

import http.server
import logging
import os
import socketserver
import threading
import urllib.parse
import webbrowser

import keyring

logger = logging.getLogger(__name__)


FRONTEND_URL = os.environ.get("ADEU_FRONTEND_URL", "http://localhost:5173")  # Default to local React dev server
BACKEND_URL = os.environ.get("ADEU_BACKEND_URL", "http://localhost:8000")  # Default to local React dev server
KEYRING_SERVICE_NAME = "adeu_mcp_server"
KEYRING_ACCOUNT_NAME = "api_key"


class AuthServer(socketserver.TCPServer):
    """Custom TCP server that stores the authentication state."""

    api_key: str | None = None


class AuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the redirect callback from the FastAPI backend."""

    server: AuthServer  # Type hint for mypy to recognize custom attributes

    def log_message(self, format, *args):
        # Suppress default HTTP logging to keep the MCP stdio clean
        pass

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)

        if parsed_path.path == "/callback":
            query = urllib.parse.parse_qs(parsed_path.query)

            if "api_key" in query:
                self.server.api_key = query["api_key"][0]
                self._send_html_response(success=True)
            else:
                self.server.api_key = None
                self._send_html_response(success=False, message="No API key received in callback.")
        else:
            self.send_response(404)
            self.end_headers()

        # Shut down the server in a separate thread to avoid deadlocking the handler
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _send_html_response(self, success: bool, message: str = ""):
        self.send_response(200 if success else 400)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        if success:
            title = "Authentication Successful!"
            text = (
                "Your Adeu MCP server has been successfully authenticated. "
                "You can safely close this window and return to Claude."
            )
            color = "#107c10"  # Green
        else:
            title = "Authentication Failed"
            text = f"There was an error authenticating the MCP server: {message} Please try again."
            color = "#d83b01"  # Red

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{title}</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                                 Helvetica, Arial, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background-color: #f3f2f1;
                }}
                .container {{
                    background: white;
                    border-radius: 8px;
                    padding: 40px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    max-width: 500px;
                    margin: 0 auto;
                }}
                h1 {{ color: {color}; }}
                p {{ color: #605e5c; line-height: 1.5; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>{title}</h1>
                <p>{text}</p>
                <script>
                    // Attempt to close the window automatically after 3 seconds
                    setTimeout(function() {{ window.close(); }}, 3000);
                </script>
            </div>
        </body>
        </html>
        """
        self.wfile.write(html.encode("utf-8"))


class DesktopAuthManager:
    """Manages the authentication lifecycle for the local MCP server."""

    @staticmethod
    def get_api_key() -> str | None:
        """Retrieve the API key from the OS Keychain."""
        try:
            return keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_NAME)
        except Exception as e:
            logger.error(f"Failed to access keychain: {e}")
            return None

    @staticmethod
    def set_api_key(api_key: str) -> None:
        """Store the API key securely in the OS Keychain."""
        try:
            keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_NAME, api_key)
        except Exception as e:
            logger.error(f"Failed to save to keychain: {e}")

    @staticmethod
    def clear_api_key() -> None:
        """Remove the API key from the OS Keychain."""
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, KEYRING_ACCOUNT_NAME)
        except Exception:
            pass  # Ignore if it doesn't exist

    @classmethod
    def authenticate_interactive(cls) -> str:
        """
        Spins up a local server, opens the browser to log in, and waits for the API key.
        Returns the raw API key upon success.
        """
        # Create an ephemeral TCP server (port 0 lets the OS pick a free port)
        with AuthServer(("localhost", 0), AuthCallbackHandler) as httpd:
            # We access index 1 because server_address for IPv4 is a (host, port) tuple
            port = httpd.server_address[1]

            # Direct the user to the React UI login page instead of the backend directly
            auth_url = f"{FRONTEND_URL}/login?desktop_port={port}"

            logger.info(f"Opening browser for authentication: {auth_url}")
            # Open the user's default web browser
            webbrowser.open(auth_url)

            # Block and wait for the callback to hit the local server
            httpd.serve_forever()

            api_key = httpd.api_key
            if not api_key:
                raise RuntimeError("Authentication failed: No API key received.")

            # Save the key securely
            cls.set_api_key(api_key)
            logger.info("Successfully stored Adeu API Key in OS Keychain.")
            return api_key

    @classmethod
    def ensure_authenticated(cls) -> str:
        """
        Returns the existing API key, or triggers interactive auth if missing.
        """
        api_key = cls.get_api_key()
        if api_key:
            return api_key

        logger.info("No API key found in Keychain. Starting interactive authentication...")
        return cls.authenticate_interactive()
