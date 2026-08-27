import { describe, it, expect } from "vitest";
import { coerceChangeItemInPlace } from "./index.js";

describe("coerceChangeItemInPlace: match_mode values that collide with Object.prototype keys", () => {
  it.each(["constructor", "__proto__", "toString"])(
    'drops match_mode "%s" instead of resolving it through the prototype',
    (bad) => {
      const item: any = {
        type: "modify",
        target_text: "a",
        new_text: "b",
        match_mode: bad,
      };

      coerceChangeItemInPlace(item);

      expect("match_mode" in item).toBe(false);
    },
  );

  it("still maps a real synonym", () => {
    const item: any = {
      type: "modify",
      target_text: "a",
      new_text: "b",
      match_mode: "First_Only",
    };

    coerceChangeItemInPlace(item);

    expect(item.match_mode).toBe("first");
  });
});
