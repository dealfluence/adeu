import { describe, it, expect } from "vitest";
import { coerceChangeItemInPlace, CHANGE_ITEM_SCHEMA } from "./index.js";

describe("comment-only modify boundary normalization", () => {
  it("populates new_text = target_text when type is modify, new_text is missing, and non-empty comment is present (including heading syntax)", () => {
    const item: any = { type: "modify", target_text: "## Term", comment: "why" };
    coerceChangeItemInPlace(item);
    expect(item).toEqual({
      type: "modify",
      target_text: "## Term",
      comment: "why",
      new_text: "## Term",
    });

    const parsed = CHANGE_ITEM_SCHEMA.parse(item);
    expect(parsed.new_text).toBe("## Term");
  });

  it("leaves explicit new_text: '' untouched (empty string means delete)", () => {
    const item: any = { type: "modify", target_text: "X", new_text: "", comment: "why" };
    coerceChangeItemInPlace(item);
    expect(item.new_text).toBe("");

    const parsed = CHANGE_ITEM_SCHEMA.parse(item);
    expect(parsed.new_text).toBe("");
  });

  it("leaves new_text absent when comment is absent or whitespace-only", () => {
    const item1: any = { type: "modify", target_text: "X" };
    coerceChangeItemInPlace(item1);
    expect(item1.new_text).toBeUndefined();

    const item2: any = { type: "modify", target_text: "X", comment: "   " };
    coerceChangeItemInPlace(item2);
    expect(item2.new_text).toBeUndefined();
  });

  it("does NOT infer type or populate new_text when type is absent with target_text + comment", () => {
    const item: any = { target_text: "X", comment: "why" };
    coerceChangeItemInPlace(item);
    expect(item.type).toBeUndefined();
    expect(item.new_text).toBeUndefined();
  });
});
