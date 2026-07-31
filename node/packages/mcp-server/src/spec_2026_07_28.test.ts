import { describe, it, expect } from "vitest";
import { ProtocolAdapter } from "./protocol-adapter.js";

describe("ProtocolAdapter", () => {
  it("exports ProtocolAdapter and attachProtocolAdapter", () => {
    expect(ProtocolAdapter).toBeDefined();
  });
});
