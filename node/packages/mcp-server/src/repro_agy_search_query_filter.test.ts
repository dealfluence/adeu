import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, ChildProcess } from "node:child_process";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { DocumentObject } from "@adeu/core";

async function createTestDocument(): Promise<DocumentObject> {
  const fixturePath = resolve(__dirname, "../../../../shared/fixtures/initial.docx");
  const buf = readFileSync(fixturePath);
  const doc = await DocumentObject.load(buf);
  const body = doc.element;
  while (body.firstChild) {
    body.removeChild(body.firstChild);
  }
  return doc;
}

function addParagraph(doc: DocumentObject, text: string): Element {
  const xmlDoc = doc.element.ownerDocument!;
  const p = xmlDoc.createElement('w:p');
  const r = xmlDoc.createElement('w:r');
  const t = xmlDoc.createElement('w:t');
  
  t.textContent = text;
  if (text.includes(' ') || text.includes('\n')) {
    t.setAttribute('xml:space', 'preserve');
  }
  
  r.appendChild(t);
  p.appendChild(r);
  doc.element.appendChild(p);
  return p;
}

describe("QA Regression Test - read_docx search_query paragraph filtering", () => {
  let serverProc: ChildProcess;
  let testDocPath: string;

  beforeAll(async () => {
    testDocPath = join(tmpdir(), `adeu_regression_unicode_${Date.now()}.docx`);

    // 1. Build document containing some English text, Chinese text, Accented text, etc.
    const doc = await createTestDocument();
    addParagraph(doc, "Unicode Test Document");
    addParagraph(doc, "This is some English text.");
    addParagraph(doc, "Chinese: 🚀 这是一个测试文档，包含中文字符 and 表情符号。");
    addParagraph(doc, "Accented: Café, naïve, résumé, garçon, déjà vu, Straße.");
    addParagraph(doc, "Emojis & Symbols: 🌟 🦄 💻 ⚡ ️⚽ ️日本語 🌍");

    writeFileSync(testDocPath, await doc.save());

    // 2. Boot the compiled MCP server
    const serverPath = resolve(__dirname, "../dist/index.js");
    if (!existsSync(serverPath)) {
      throw new Error(
        "MCP server not built. Run 'npm run build' before running tests.",
      );
    }

    serverProc = spawn("node", [serverPath]);
  });

  afterAll(() => {
    if (serverProc && !serverProc.killed) serverProc.kill();
    if (existsSync(testDocPath)) unlinkSync(testDocPath);
  });

  // Helper to interact with the stdio JSON-RPC server
  function sendRpc(method: string, params: any, id: number = 1): Promise<any> {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("RPC Timeout")), 5000);

      const listener = (data: Buffer) => {
        const lines = data.toString().trim().split("\n");
        for (const line of lines) {
          if (!line.startsWith("{")) continue;
          try {
            const res = JSON.parse(line);
            if (res.id === id) {
              clearTimeout(timeout);
              serverProc.stdout?.removeListener("data", listener);
              resolve(res);
            }
          } catch (e) {
            // Ignore incomplete chunks
          }
        }
      };

      serverProc.stdout?.on("data", listener);
      serverProc.stdin?.write(
        JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      );
    });
  }

  it("should filter results to matching paragraphs when search_query is provided", async () => {
    const res = await sendRpc(
      "tools/call",
      {
        name: "read_docx",
        arguments: {
          file_path: testDocPath,
          search_query: "Chinese",
          reasoning: "Filter document to target paragraph.",
        },
      },
      301,
    );

    expect(res.error).toBeUndefined();
    expect(res.result).toBeDefined();
    
    // Check that it is NOT a hard error
    expect(res.result.isError).toBeUndefined();
    
    const responseText = res.result.content[0].text;
    
    // The query MUST be matched and returned (the matched word is highlighted in bold).
    expect(responseText).toContain("**Chinese**: 🚀 这是一个测试文档，包含中文字符 and 表情符号。");
    
    // CRITICAL: The search query should filter out non-matching paragraphs to conserve LLM context window tokens.
    // Therefore, other paragraphs in the document should NOT be included in the returned text.
    expect(responseText).not.toContain("This is some English text.");
    expect(responseText).not.toContain("Accented: Café");
    expect(responseText).not.toContain("Emojis & Symbols");
  });
});
