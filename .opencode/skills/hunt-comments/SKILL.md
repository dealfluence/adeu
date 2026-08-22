---
name: hunt-comments
description: Use when hunting down verbose, narrative, or dead comments in the codebase to reduce comment noise and character counts.
---

# Hunt Comments Skill

This skill provides a systematic approach to finding and reducing comment density and character bloat across source code files.

## Workflow

1. **Run the Comment Density Spotter**:
   Execute `python scripts/count_comment_chars.py` to rank files by comment character count:
   
   ```bash
   uv run python scripts/count_comment_chars.py -n 25
   ```

2. **Target High-Density Files**:
   Focus on top-ranked files with high comment ratios (>20% comment characters).

3. **Identify & Remove Non-Essential Commentary**:
   - Delete narrative historical explanations ("Why we added this in 2024...", "Fix for issue #123 where...").
   - Strip redundant docstrings that simply restate function signatures, types, or obvious operations.
   - Collapse multi-paragraph block comments into brief 1-line "why" notes or remove them entirely if code is self-explanatory.
   - Retain only essential architectural invariants (e.g., OPC part boundaries, Word signed int32 overflow rules, security guards).

4. **Verify Behavior**:
   Always run test suites to verify zero functional or formatting regressions:
   - Python: `uv run pytest` & `uv run mypy src`
   - Node: `npm run build && npm test`
   - LangChain: `uv run pytest` in `langchain`
