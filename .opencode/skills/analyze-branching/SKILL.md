---
name: analyze-branching
description: Formally analyzes control-flow branchings, cyclomatic complexity, nesting depths, and branch redundancies across Python and TypeScript engine files.
---

# Analyze Branching Skill

This skill provides formal analysis of control-flow branchings, decision points, and nesting depth across the Python (`python/src/adeu/redline/engine.py`) and TypeScript (`node/packages/core/src/engine.ts`) engine implementations.

## Usage

Run the engine branch checker script from repository root:

```bash
uv run python scripts/check_engine_branches.py
```

Or analyze individual files:

### Python Analysis
```bash
uv run python scripts/analyze_python_branches.py [path_to_file]
```

### TypeScript Analysis
```bash
node scripts/analyze_ts_branches.mjs [path_to_file]
```

## Metrics
- **Cyclomatic Complexity**: Counts decision nodes (`if`, `elif`/`else`, loops, exception handlers, logical boolean operators `and`/`or` / `&&`/`||`, conditional expressions).
- **Max Depth**: Deepest nesting level of control flow structures inside a single function.
- **Deep Nesting Warning**: Flags functions with branch nesting levels $\ge 6$.
