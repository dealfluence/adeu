import { realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { defineConfig } from "vitest/config";

// Receipt fixtures compare paths literally. An OS temp directory may be
// spelled with a Windows short name or a symlink to its actual location.
const temp = realpathSync.native(tmpdir());

export default defineConfig({
  test: { env: { TEMP: temp, TMP: temp, TMPDIR: temp } },
});
