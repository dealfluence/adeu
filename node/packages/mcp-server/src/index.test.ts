import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));

describe('MCP Server', () => {
  it('guarantees that @adeu/core dependency version is aligned with the core package version', () => {
    const mcpPackageJsonPath = resolve(__dirname, '../package.json');
    const corePackageJsonPath = resolve(__dirname, '../../core/package.json');

    expect(existsSync(mcpPackageJsonPath)).toBe(true);
    expect(existsSync(corePackageJsonPath)).toBe(true);

    const mcpPackageJson = JSON.parse(readFileSync(mcpPackageJsonPath, 'utf-8'));
    const corePackageJson = JSON.parse(readFileSync(corePackageJsonPath, 'utf-8'));

    const coreWorkspaceVersion = corePackageJson.version;
    const coreDependencyRange = mcpPackageJson.dependencies['@adeu/core'];

    // Ensure the declared dependency range is at least the workspace version of core
    // to prevent resolving stale/cached core versions that lack newly added exports.
    expect(coreDependencyRange).toBe(`^${coreWorkspaceVersion}`);
  });
});