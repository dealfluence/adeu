// FILE: node/packages/mcp-server/src/conformance-utils.ts
//
// Loader half of the cross-engine conformance harness (spec §8.3): the golden
// files, the approx-token unit every budget in the spec is expressed in, and
// the fixture projection the Node builders are fed.
//
// The fixtures and goldens are generated, committed artifacts:
//   node shared/conformance/build_fixtures.mjs
//   cd python && uv run python ../shared/conformance/capture_goldens.py
// See shared/conformance/README.md.

import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  DocumentObject,
  RedlineEngine,
  _extractTextFromDoc,
  extract_comments_data,
  extract_outline,
  OutlineNode,
  paginate,
  split_structural_appendix,
} from "@adeu/core";
import type { ProjectionBundle } from "./response-builders.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

export const CONFORMANCE_DIR = resolve(__dirname, "../../../../shared/conformance");
export const FIXTURE_DIR = resolve(CONFORMANCE_DIR, "fixtures");
export const GOLDEN_DIR = resolve(CONFORMANCE_DIR, "goldens");

/** The budget unit the spec's token ceilings are expressed in. */
export const approxTokens = (s: string) => Math.floor(s.length / 4);

/** Helper for constructing minimal test batch stats objects. */
export function createTestStats(edits: any[] = [], overrides: Record<string, any> = {}) {
  return {
    actions_applied: 0,
    actions_skipped: 0,
    edits_applied: edits.filter((e) => e.status === "applied").length || (edits.length ? 0 : 1),
    edits_skipped: edits.filter((e) => e.status === "failed" || e.status === "skipped").length,
    edits,
    skipped_details: [],
    ...overrides,
  };
}

/**
 * The golden text for `case`, or null when it has not been captured. Line
 * endings are normalised: capture_goldens.py writes "\n", but git on Windows
 * may hand the file back as CRLF.
 */
export function golden(name: string): string | null {
  const p = resolve(GOLDEN_DIR, name.endsWith(".txt") ? name : `${name}.txt`);
  if (!existsSync(p)) return null;
  return readFileSync(p, "utf-8").replace(/\r\n/g, "\n");
}

/** The line-1 banner every builder prefixes to its LLM content. */
const FILE_PATH_BANNER = /^> \*\*File Path:\*\* `([^`]*)`/;

/**
 * Node output normalised the same way, ready to compare against a golden.
 *
 * The banner path is re-canonicalised to the POSIX placeholder the goldens
 * hold: the builders run `file_path` through Node's `resolve()`, which is
 * load-bearing on the real MCP path but rewrites `/fixtures/x.docx` to
 * `D:\fixtures\x.docx` on win32. Undoing the platform-specific part here keeps
 * the comparison byte-exact everywhere without weakening the builders.
 */
export const normalize = (s: string) =>
  s
    .replace(/\r\n/g, "\n")
    .replace(
      FILE_PATH_BANNER,
      (_line, p: string) =>
        `> **File Path:** \`${p.replace(/\\/g, "/").replace(/^[A-Za-z]:/, "")}\``,
    );

/**
 * The path string that goes into every response: a STABLE PLACEHOLDER, never a
 * real path. capture_goldens.py passes exactly this, so no machine-specific
 * absolute path is baked into a golden.
 */
export const placeholderPath = (fixture: string) => `/fixtures/${fixture}.docx`;

export const fixturePath = (fixture: string) => resolve(FIXTURE_DIR, `${fixture}.docx`);

export interface ProjectedFixture {
  doc: DocumentObject;
  /** The projection every builder consumes: raw view, appendix excluded. */
  text: string;
  bundle: ProjectionBundle;
  /** The heading map the doc-cache hands the outline and guard builders. */
  outlineNodes: OutlineNode[];
  commentsData: Record<string, any>;
  /** Live change ids, as the disk MCP path collects them. */
  changeIds: Set<string>;
  filePath: string;
}

/**
 * Projects a fixture exactly as adeu.mcp_components.doc_cache._fill_view does
 * on the Python side (clean_view=false, include_appendix=false, then
 * paginate(body, "")), so a builder fed this bundle is fed what the server
 * really serves.
 */
export async function projectFixture(fixture: string): Promise<ProjectedFixture> {
  const bytes = readFileSync(fixturePath(fixture));
  const doc = await DocumentObject.load(bytes);
  // Paragraph offsets are requested for the same reason capture_goldens.py
  // requests them: extract_outline's fast path is the one the server runs.
  const { text, paragraph_offsets } = _extractTextFromDoc(
    doc,
    false,
    false,
    true,
  ) as { text: string; paragraph_offsets: Map<any, [number, number]> };
  const [body, appendix] = split_structural_appendix(text);
  const pagination = paginate(body, "");

  // A separate load for the id sweep, so the engine constructor's document
  // touches cannot leak into the projected copy. `_existing_change_ids` is the
  // same private method the Python MCP handler calls (tools/document.py:446).
  const idDoc = await DocumentObject.load(bytes);
  const changeIds = new Set<string>(
    (new RedlineEngine(idDoc) as any)._existing_change_ids() as string[],
  );

  return {
    doc,
    text,
    bundle: { body, appendix, pagination },
    outlineNodes: extract_outline(
      doc,
      body,
      pagination.body_pages,
      pagination.body_page_offsets,
      paragraph_offsets,
    ),
    commentsData: extract_comments_data(doc.pkg) as Record<string, any>,
    changeIds,
    filePath: placeholderPath(fixture),
  };
}
