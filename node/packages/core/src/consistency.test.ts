import { describe, it, expect } from "vitest";
import {
  readFileSync,
  existsSync,
  readdirSync,
  writeFileSync,
  unlinkSync,
} from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execSync, execFileSync } from "node:child_process";
import { tmpdir } from "node:os";

import { DocumentObject } from "./docx/bridge.js";
import { RedlineEngine } from "./engine.js";
import { extractTextFromBuffer } from "./ingest.js";
import { serializeXml } from "./docx/dom.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const CORPUS_DIR = resolve(
  __dirname,
  "../../../../shared/cross_platform_tests",
);
const PYTHON_ABSTRACT_CMD = resolve(
  __dirname,
  "../../../../python/scripts/abstract_xml.py",
);
const PYTHON_DIR = resolve(__dirname, "../../../../python");

const CT_COMMENTS =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml";

function normalizeMdTimestamps(mdText: string): string {
  return mdText.replace(/@ \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z/g, "@ DATE");
}

function xmllintCheck(xmlContent: string, label: string): void {
  // Cross-platform lookup: `which` on POSIX, `where` on Windows.
  const locator = process.platform === "win32" ? "where" : "which";
  let xmllintBin: string | null = null;
  try {
    xmllintBin =
      execSync(`${locator} xmllint`, { encoding: "utf-8" })
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter(Boolean)[0] || null;
  } catch {
    /* not found */
  }
  if (!xmllintBin) {
    // Optional external XML validation: skip when xmllint is unavailable
    // (common on Windows). The in-code namespace assertion still runs.
    return;
  }
  const tmpFile = resolve(tmpdir(), `adeu_consistency_${Date.now()}_${label}`);
  try {
    writeFileSync(tmpFile, xmlContent, "utf-8");
    execFileSync(xmllintBin, ["--noout", tmpFile]);
  } catch (err: any) {
    throw new Error(
      `xmllint validation failed for ${label}:\n${err.stderr ?? err.message}`,
    );
  } finally {
    if (existsSync(tmpFile)) unlinkSync(tmpFile);
  }
}

async function validateCommentsXmlNamespaces(
  outBuffer: Buffer,
  folder: string,
): Promise<void> {
  const doc = await DocumentObject.load(outBuffer);
  const commentsPart = doc.pkg.parts.find((p) => p.contentType === CT_COMMENTS);
  if (!commentsPart) return;

  const commentsXml = serializeXml(
    commentsPart._element.ownerDocument ?? commentsPart._element,
  );

  expect(commentsXml).toContain(
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"',
  );
  xmllintCheck(commentsXml, `${folder}_comments.xml`);
}

describe("Polyglot Consistency Framework (TS vs Python)", () => {
  if (!existsSync(CORPUS_DIR)) {
    it.skip("Cross-platform test corpus not found", () => {});
    return;
  }

  const testFolders = readdirSync(CORPUS_DIR, { withFileTypes: true })
    .filter((dirent) => dirent.isDirectory())
    .map((dirent) => dirent.name);

  for (const folder of testFolders) {
    const testDir = resolve(CORPUS_DIR, folder);
    const testJsonPath = resolve(testDir, "test.json");
    const inputDocxPath = resolve(testDir, "input.docx");

    if (!existsSync(testJsonPath) || !existsSync(inputDocxPath)) {
      continue;
    }

    const testConfig = JSON.parse(readFileSync(testJsonPath, "utf-8"));
    const isReadOnly = testConfig.read_only || false;
    // CRITICAL: We must inherit the author from the JSON so the XML Abstraction comparison
    // doesn't fail on `w:author="Adeu AI"` vs `w:author="Adeu AI (TS)"`.
    const author = testConfig.author || "Adeu AI";

    describe(`Corpus Scenario: [${folder}]`, () => {
      it("Strictly matches the Python Golden Masters", async () => {
        const inputBuffer = readFileSync(inputDocxPath);
        let outBuffer: Buffer;

        // 1. Process Edits (if not read-only)
        if (isReadOnly) {
          outBuffer = inputBuffer;
        } else {
          const doc = await DocumentObject.load(inputBuffer);
          const engine = new RedlineEngine(doc, author);

          engine.process_batch(testConfig.changes || []);
          outBuffer = await doc.save();

          // 2. Validate comments XML namespaces when requested by test.json
          if (testConfig.validate_comments_xml_namespaces) {
            await validateCommentsXmlNamespaces(outBuffer, folder);
          }

          // 3. Assert XML Structure Parity (via Python Bridge)
          const goldenXmlPath = resolve(testDir, "golden_abstract.xml");
          if (existsSync(goldenXmlPath)) {
            const expectedXml = readFileSync(goldenXmlPath, "utf-8");

            const tmpDocx = resolve(
              tmpdir(),
              `adeu_test_${folder}_${Date.now()}.docx`,
            );
            writeFileSync(tmpDocx, outBuffer);

            try {
              // Pipe to Python to bypass Node vs Python XML serialization differences
              const cmd = `uv run python "${PYTHON_ABSTRACT_CMD}" "${tmpDocx}"`;
              const actualXml = execSync(cmd, {
                cwd: PYTHON_DIR,
                encoding: "utf-8",
                stdio: ["pipe", "pipe", "inherit"],
                env: { ...process.env, PYTHONIOENCODING: "utf-8" },
              });
              // Normalize line endings for reliable string comparison
              const normExpected = expectedXml.replace(/\r\n/g, "\n").trim();
              const normActual = actualXml.replace(/\r\n/g, "\n").trim();

              expect(normActual).toBe(normExpected);
            } finally {
              if (existsSync(tmpDocx)) unlinkSync(tmpDocx);
            }
          }
        }

        // 4. Assert Markdown Extraction Parity (Raw View)
        const rawMdPath = resolve(testDir, "golden_raw.md");
        if (existsSync(rawMdPath)) {
          const expectedRaw = readFileSync(rawMdPath, "utf-8").replace(
            /\r\n/g,
            "\n",
          );
          const actualRaw = normalizeMdTimestamps(
            await extractTextFromBuffer(outBuffer, false),
          ).replace(/\r\n/g, "\n");
          expect(actualRaw).toBe(expectedRaw);
        }

        // 5. Assert Markdown Extraction Parity (Clean View)
        const cleanMdPath = resolve(testDir, "golden_clean.md");
        if (existsSync(cleanMdPath)) {
          const expectedClean = readFileSync(cleanMdPath, "utf-8").replace(
            /\r\n/g,
            "\n",
          );
          const actualClean = normalizeMdTimestamps(
            await extractTextFromBuffer(outBuffer, true),
          ).replace(/\r\n/g, "\n");
          expect(actualClean).toBe(expectedClean);
        }
      });
    });
  }
});
