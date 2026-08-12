import { describe, it, expect } from "vitest";
import {
  clamp_text,
  parse_page_arg,
  PAGE_RANGE_MAX_PAGES,
  offset_to_page,
  extract_comments_data,
} from "./index.js";
import { createTestDocument } from "./test-utils.js";

describe("shared primitives", () => {
  describe("clamp_text", () => {
    it("returns text unchanged when length <= cap", () => {
      expect(clamp_text("abcdef", 6)).toBe("abcdef");
    });

    it("truncates text with '...' when length > cap", () => {
      expect(clamp_text("abcdefgh", 6)).toBe("abc...");
    });

    it("enforces max(1, cap-3) floor for small caps", () => {
      expect(clamp_text("abcdefgh", 2)).toBe("a...");
    });

    it("ensures result length <= cap for cap >= 4", () => {
      const longText = "a".repeat(100);
      for (let cap = 4; cap <= 20; cap++) {
        const clamped = clamp_text(longText, cap);
        expect(clamped.length).toBeLessThanOrEqual(cap);
      }
    });
  });

  describe("parse_page_arg", () => {
    it("handles undefined and null", () => {
      expect(parse_page_arg(undefined)).toEqual(["single", 1]);
      expect(parse_page_arg(null)).toEqual(["single", 1]);
    });

    it("handles positive numbers", () => {
      expect(parse_page_arg(3)).toEqual(["single", 3]);
    });

    it("handles numeric strings", () => {
      expect(parse_page_arg("3")).toEqual(["single", 3]);
    });

    it("handles 'all' case-insensitively with padding", () => {
      expect(parse_page_arg(" all ")).toEqual(["all", null]);
      expect(parse_page_arg("ALL")).toEqual(["all", null]);
    });

    it("handles range strings with optional padding", () => {
      expect(parse_page_arg("2-6")).toEqual(["range", [2, 6]]);
      expect(parse_page_arg(" 2 - 6 ")).toEqual(["range", [2, 6]]);
    });

    it("does not throw for inverted range (start > end)", () => {
      expect(parse_page_arg("6-2")).toEqual(["range", [6, 2]]);
    });

    it("throws expected error for invalid page parameters", () => {
      const invalidCases = [0, -1, "", "x", "0-3", "2-0"];
      for (const raw of invalidCases) {
        expect(() => parse_page_arg(raw as any)).toThrowError(
          `Invalid page parameter: '${raw}'. Provide a positive integer, page range (e.g. '2-6'), or 'all'.`,
        );
      }
    });
  });

  describe("PAGE_RANGE_MAX_PAGES", () => {
    it("equals 8", () => {
      expect(PAGE_RANGE_MAX_PAGES).toBe(8);
    });
  });

  describe("offset_to_page", () => {
    it("maps offsets to 1-based page numbers correctly", () => {
      expect(offset_to_page(0, [])).toBe(1);
      expect(offset_to_page(25, [0, 10, 20])).toBe(3);
      expect(offset_to_page(5, [0, 10, 20])).toBe(1);
    });
  });

  describe("extract_comments_data", () => {
    it("returns empty object for document with no comments", async () => {
      const doc = await createTestDocument();
      const comments = extract_comments_data(doc.pkg);
      expect(comments).toEqual({});
    });
  });
});
