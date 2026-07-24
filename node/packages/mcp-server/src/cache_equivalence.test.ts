// FILE: node/packages/mcp-server/src/cache_equivalence.test.ts
// Byte-identity contract of the doc-cache (docs/PERFORMANCE.md §5.1):
// every response built from cached products must equal the response the
// historical uncached path would have produced — agent-visible text is a
// contract, and the cache must be invisible in it.
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { DocumentObject, _extractTextFromDoc } from "@adeu/core";
import { DocCache, DocCacheEntry } from "./doc-cache.js";
import {
  build_paginated_response,
  build_full_document_response,
  build_search_response,
  build_appendix_response,
  build_outline_response,
  render_outline_response,
} from "./response-builders.js";

const FIXTURES = [
  resolve(__dirname, "../tests/fixtures/gap2_minimal_repro.docx"),
  resolve(__dirname, "../tests/fixtures/gap1_minimal_repro.docx"),
];

const loadDoc = (buf: Buffer, opts?: any) => DocumentObject.load(buf, opts);

interface Case {
  path: string;
  entry: DocCacheEntry;
  freshRaw: string;
}
let cases: Case[] = [];

beforeAll(async () => {
  const cache = new DocCache(4);
  cases = [];
  for (const path of FIXTURES) {
    const entry = await cache.get(path, () => readFileSync(path), loadDoc);
    const doc = await DocumentObject.load(readFileSync(path));
    const freshRaw = _extractTextFromDoc(doc, false, true) as string;
    cases.push({ path, entry, freshRaw });
  }
});

describe("cache-served responses are byte-identical to uncached ones", () => {
  it("full mode, every page", () => {
    for (const { path, entry, freshRaw } of cases) {
      const total = entry.raw_bundle.pagination.total_pages;
      for (let p = 1; p <= total; p++) {
        const cached = build_paginated_response(
          entry.raw_text,
          p,
          path,
          entry.raw_bundle,
        );
        const fresh = build_paginated_response(freshRaw, p, path);
        expect(cached).toEqual(fresh);
      }
    }
  });

  it("full mode, page='all'", () => {
    for (const { path, entry, freshRaw } of cases) {
      const cached = build_full_document_response(
        entry.raw_text,
        path,
        entry.raw_bundle,
      );
      const fresh = build_full_document_response(freshRaw, path);
      expect(cached).toEqual(fresh);
    }
  });

  it("search mode (literal, regex, case-insensitive, page-filtered)", () => {
    for (const { path, entry, freshRaw } of cases) {
      const variants: Array<[string, boolean, boolean, any]> = [
        ["the", false, true, undefined],
        ["THE", false, false, undefined],
        ["t\\w+e", true, true, undefined],
        ["the", false, true, 1],
        ["the", false, true, "all"],
      ];
      for (const [q, rx, cs, page] of variants) {
        const cached = build_search_response(
          entry.raw_text,
          q,
          rx,
          cs,
          page,
          path,
          entry.raw_bundle,
        );
        const fresh = build_search_response(freshRaw, q, rx, cs, page, path);
        expect(cached).toEqual(fresh);
      }
    }
  });

  it("appendix mode", () => {
    for (const { path, entry, freshRaw } of cases) {
      const cached = build_appendix_response(
        entry.raw_text,
        1,
        path,
        entry.raw_bundle,
      );
      const fresh = build_appendix_response(freshRaw, 1, path);
      expect(cached).toEqual(fresh);
    }
  });

  it("outline mode: cached nodes render identically to the full builder", async () => {
    for (const { path, entry } of cases) {
      const doc = await DocumentObject.load(readFileSync(path));
      const extract_res = _extractTextFromDoc(doc, false, true, true) as {
        text: string;
        paragraph_offsets: Map<any, [number, number]>;
      };
      for (const [maxLevel, verbose] of [
        [2, false],
        [6, true],
        [0, false], // clamp behavior must match
        [99, true],
      ] as Array<[number, boolean]>) {
        const fresh = build_outline_response(
          doc,
          extract_res.text,
          path,
          maxLevel,
          verbose,
          extract_res.paragraph_offsets,
        );
        const cached = render_outline_response(
          entry.outline_nodes,
          entry.raw_bundle.pagination.total_pages,
          path,
          maxLevel,
          verbose,
        );
        expect(cached).toEqual(fresh);
      }
    }
  });

  it("clean view: cache-filled text equals fresh clean extraction responses", async () => {
    const cache = new DocCache(4);
    for (const { path } of cases) {
      const reader = () => readFileSync(path);
      const entry = await cache.get(path, reader, loadDoc);
      const cleanText = await cache.ensureCleanText(entry, reader, loadDoc);
      const cleanBundle = await cache.ensureCleanBundle(entry, reader, loadDoc);

      const doc = await DocumentObject.load(readFileSync(path));
      const freshClean = _extractTextFromDoc(doc, true, true) as string;

      const cached = build_paginated_response(cleanText, 1, path, cleanBundle);
      const fresh = build_paginated_response(freshClean, 1, path);
      expect(cached).toEqual(fresh);
    }
  });
});
