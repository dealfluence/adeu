// FILE: test/codex-consistency.test.ts
//
// Guards the n8n codex file + monorepo version lockstep. The actual rules live
// in scripts/check_release_consistency.mjs (single source of truth, also run
// as the first gate of the Release workflow); this test wires them into the
// node test suite so CI and the pre-push hook fail fast on any drift.
//
// Background: n8n Cloud verification once rejected a release because
// Adeu.node.json's `nodeVersion` had been bumped to the npm package version
// instead of mirroring the node class's `version`. This test makes that class
// of mistake impossible to merge.

import { describe, it, expect } from "vitest";
import { resolve } from "node:path";
import { runChecks } from "../../../../scripts/check_release_consistency.mjs";

describe("Release consistency", () => {
  it("codex file and monorepo versions are internally consistent", () => {
    const repoRoot = resolve(__dirname, "../../../..");
    const { errors } = runChecks(repoRoot);
    expect(errors).toEqual([]);
  });
});
