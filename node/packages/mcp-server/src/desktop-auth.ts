// FILE: node/packages/mcp-server/src/desktop-auth.ts
import { createServer, Server } from "node:http";
import { exec } from "node:child_process";
import { homedir, platform } from "node:os";
import { join } from "node:path";
import {
  writeFileSync,
  readFileSync,
  mkdirSync,
  existsSync,
  rmSync,
  chmodSync,
} from "node:fs";
import { FRONTEND_URL } from "./shared.js";

const ADEU_DIR = join(homedir(), ".adeu");
const CRED_PATH = join(ADEU_DIR, "credentials.json");

function openBrowser(url: string) {
  if (platform() === "darwin") exec(`open "${url}"`);
  else if (platform() === "win32") exec(`start "" "${url}"`);
  else exec(`xdg-open "${url}"`);
}

export class DesktopAuthManager {
  static getApiKey(): string | null {
    if (!existsSync(CRED_PATH)) return null;
    try {
      const data = JSON.parse(readFileSync(CRED_PATH, "utf-8"));
      return data.api_key || null;
    } catch {
      return null;
    }
  }

  static setApiKey(apiKey: string): void {
    if (!existsSync(ADEU_DIR)) {
      mkdirSync(ADEU_DIR, { recursive: true });
    }
    writeFileSync(CRED_PATH, JSON.stringify({ api_key: apiKey }));
    // Restrict read/write to the current user only (equivalent to 0o600)
    chmodSync(CRED_PATH, 0o600);
  }

  static clearApiKey(): void {
    if (existsSync(CRED_PATH)) {
      rmSync(CRED_PATH);
    }
  }

  static async authenticateInteractive(): Promise<string> {
    return new Promise((resolve, reject) => {
      let server: Server;

      const timeout = setTimeout(
        () => {
          if (server) server.close();
          reject(new Error("Authentication timed out after 5 minutes."));
        },
        5 * 60 * 1000,
      );

      server = createServer((req, res) => {
        const url = new URL(req.url || "", `http://${req.headers.host}`);

        if (url.pathname === "/callback") {
          const apiKey = url.searchParams.get("api_key");

          res.writeHead(apiKey ? 200 : 400, { "Content-Type": "text/html" });
          const title = apiKey
            ? "Authentication Successful!"
            : "Authentication Failed";
          const text = apiKey
            ? "Your Adeu MCP server has been successfully authenticated. You can safely close this window and return to Claude."
            : "No API key received. Please try again.";
          const color = apiKey ? "#107c10" : "#d83b01";

          res.end(`
            <!DOCTYPE html><html><head><title>${title}</title>
            <style>body{font-family:sans-serif;text-align:center;padding:50px;background:#f3f2f1;}.container{background:white;padding:40px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);max-width:500px;margin:0 auto;}h1{color:${color};}p{color:#605e5c;line-height:1.5;}</style>
            </head><body><div class="container"><h1>${title}</h1><p>${text}</p>
            <script>setTimeout(()=>window.close(), 3000);</script>
            </div></body></html>
          `);

          clearTimeout(timeout);
          // Allow response to send before closing server
          setTimeout(() => server.close(), 100);

          if (apiKey) {
            this.setApiKey(apiKey);
            resolve(apiKey);
          } else {
            reject(new Error("No API key received in callback."));
          }
        } else {
          res.writeHead(404);
          res.end();
        }
      });

      server.listen(0, "127.0.0.1", () => {
        const address = server.address();
        if (address && typeof address !== "string") {
          const authUrl = `${FRONTEND_URL}/login?desktop_port=${address.port}`;
          openBrowser(authUrl);
        }
      });
    });
  }

  static async ensureAuthenticated(): Promise<string> {
    const key = this.getApiKey();
    if (key) return key;
    return this.authenticateInteractive();
  }
}

export async function getCloudAuthToken(): Promise<string> {
  const key = DesktopAuthManager.getApiKey();
  if (!key) {
    throw new Error(
      "Authentication Required: You are not logged in. Please call the `login_to_adeu_cloud` tool first to authenticate, then try this task again.",
    );
  }
  return key;
}
