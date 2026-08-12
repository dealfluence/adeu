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

/** Node output normalised the same way, ready to compare against a golden. */
export const normalize = (s: string) => s.replace(/\r\n/g, "\n");

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
  const text = _extractTextFromDoc(doc, false, false) as string;
  const [body, appendix] = split_structural_appendix(text);

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
    bundle: { body, appendix, pagination: paginate(body, "") },
    commentsData: extract_comments_data(doc.pkg) as Record<string, any>,
    changeIds,
    filePath: placeholderPath(fixture),
  };
}
