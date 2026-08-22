---
name: check-duplication
description: Use when checking for or pruning duplicate code blocks across source files.
---

# Check Code Duplication Skill

This skill provides a systematic approach to finding and pruning duplicate code blocks and repeated boilerplate across Python, TypeScript, and JavaScript source files.

## Workflow

1. **Run the Duplication Spotter**:
   Execute `python scripts/check_code_duplication.py` to identify duplicate code blocks:
   
   ```bash
   uv run python scripts/check_code_duplication.py -m 6 -n 20
   ```

2. **Target High-Score Duplications**:
   Focus on blocks with high duplication scores (lines $\times$ occurrences) in source modules.

3. **Prune & Deduplicate**:
   - Extract repeated boilerplate or constructor patterns into shared helper functions.
   - Collapse repeated dictionary/object constructions into helper functions.
   - Maintain 3+ call sites requirement for new abstractions.

4. **Verify Behavior**:
   Always run test suites to ensure zero functional regressions:
   - Python: `uv run pytest` & `uv run mypy src`
   - Node: `npm run build && npm test`
   - LangChain: `uv run pytest` in `langchain`
