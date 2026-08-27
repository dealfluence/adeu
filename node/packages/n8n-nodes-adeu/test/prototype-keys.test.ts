import { describe, it, expect, vi } from "vitest";

// n8n-workflow cannot be imported for real under vitest (its package exports
// map is incompatible with the resolver), so stub the two value imports
// GenericFunctions needs. Mirrors test/examples.test.ts.
vi.mock("n8n-workflow", () => {
  class NodeOperationError extends Error {
    constructor(_node: unknown, message: unknown) {
      super(typeof message === "string" ? message : "NodeOperationError");
      this.name = "NodeOperationError";
    }
  }
  class NodeApiError extends Error {
    constructor(_node: unknown, _error: unknown, options?: any) {
      super(options?.message ?? "NodeApiError");
      this.name = "NodeApiError";
    }
  }
  return { NodeOperationError, NodeApiError };
});

// Import MUST come after vi.mock() in source order; vitest hoists the mock.
import {
  coerceChangeItemInPlace,
  getNestedProperty,
} from "../nodes/Adeu/GenericFunctions";

describe("n8n node: user strings that collide with Object.prototype keys", () => {
  it.each(["constructor", "__proto__"])(
    'drops match_mode "%s"',
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

  it("still maps a real match_mode synonym", () => {
    const item: any = {
      type: "modify",
      target_text: "a",
      new_text: "b",
      match_mode: "Exact",
    };

    coerceChangeItemInPlace(item);

    expect(item.match_mode).toBe("strict");
  });

  it.each(["constructor", "__proto__", "toString", "a.constructor"])(
    'resolves the JSON path "%s" to undefined',
    (path) => {
      expect(getNestedProperty({ a: { b: 1 } }, path)).toBeUndefined();
    },
  );

  it("still resolves real paths, including array indices", () => {
    expect(getNestedProperty({ a: { b: 1 } }, "a.b")).toBe(1);
    expect(
      getNestedProperty({ changes: [{ type: "modify" }] }, "changes.0.type"),
    ).toBe("modify");
  });
});
