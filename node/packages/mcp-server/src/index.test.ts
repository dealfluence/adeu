import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));

function walkDir(dir: string, callback: (filepath: string) => void) {
  const files = readdirSync(dir);
  for (const file of files) {
    const filepath = join(dir, file);
    if (statSync(filepath).isDirectory()) {
      walkDir(filepath, callback);
    } else {
      callback(filepath);
    }
  }
}

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

  it('should not contain console.log in production code (MCP stdio safety)', () => {
    const violations: string[] = [];
    
    const coreSrc = resolve(__dirname, '../../core/src');
    const mcpSrc = resolve(__dirname, '../src');

    [coreSrc, mcpSrc].forEach(srcDir => {
      walkDir(srcDir, (filepath) => {
        if (!filepath.endsWith('.ts') || filepath.endsWith('.test.ts') || filepath.endsWith('test-utils.ts')) return;
        
        const lines = readFileSync(filepath, 'utf-8').split('\n');
        lines.forEach((line, i) => {
          if (line.includes('console.log') && !line.trim().startsWith('//')) {
            violations.push(`${filepath.split(/packages[\\/]/)[1]}:${i + 1}`);
          }
        });
      });
    });
    
    expect(violations, 'Found console.log in production code. This corrupts MCP stdio streams! Use console.error instead.').toEqual([]);
  });
});