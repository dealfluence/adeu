# Contributing to Adeu

Thank you for your interest in contributing to Adeu! We welcome bug reports, feature requests, and pull requests from the community.

## Development Environment

Adeu relies on Python toolchain managed by [uv](https://docs.astral.sh/uv/).

### 1. Setup

Clone the repository and install the dependencies:

```bash
git clone https://github.com/dealfluence/adeu.git
cd adeu/python
uv sync --all-extras --dev
```

### 2. Code Quality & Linting

We enforce strict code formatting and type hinting to maintain the integrity of the complex XML manipulation logic.

Before submitting a pull request, ensure all checks pass:

```bash
# Format code
cd python && uv run ruff format .

# Run linter
cd python && uv run ruff check . --fix

# Run static type checker
cd python && uv run mypy src
```

### Git Hooks (recommended)

Enable the shared git hooks once per clone so staged code is auto-formatted and
checked on every commit:

```bash
# from the repo root
git config core.hooksPath .githooks
# or: sh scripts/setup-hooks.sh
```

The hook only touches the directories your commit changes:

- **`python/` / `langchain/`** — runs `ruff check --fix` and `ruff format`
  (fixes are applied in place and re-staged into the commit), then `mypy`.
- **`node/` (n8n-nodes-adeu)** — runs `eslint --fix` on touched `.ts` files.

A companion `pre-push` hook runs each changed area's test suite before a push
(`pytest` for `python/` and `langchain/`, `npm run build && npm test` for
`node/`) — kept off the commit path so commits stay fast. Bypass in a pinch
with `git push --no-verify`.

Both hooks need `uv` on your `PATH` (and `node` + `npm install` in `node/` if you
touch the n8n package); areas whose tools are missing are skipped with a
warning, so CI remains the source of truth. The hooks are POSIX `sh` and work on
macOS, Linux, and Windows (Git Bash).

### 3. Testing

Adeu has an extensive test suite (nearly 400 tests) that validates behavior against complex OOXML edge cases and Live Word COM interactions.

Run the test suite using `pytest`:

```bash
# Run all tests
cd python && uv run pytest

# Run tests with coverage
cd python && uv run pytest --cov=src
```

*(Note: Tests involving the Live Word COM engine are automatically skipped on non-Windows platforms).*

## Pull Request Guidelines

1. **Check Existing Issues**: Before starting work on a major feature, please check the [Issue Tracker](https://github.com/dealfluence/adeu/issues) to see if someone is already working on it or to discuss your proposed approach.
2. **Keep PRs Focused**: Submit separate pull requests for unrelated changes.
3. **Include Tests**: If you are fixing a bug, include a regression test (e.g., `tests/test_repro_issue_name.py`). If adding a feature, include unit tests that prove it works.
4. **Do Not Break XML Validation**: Changes to the `RedlineEngine` must ensure that output documents are strictly valid OpenXML. We do not tolerate "silent" XML corruption.
5. **Update Documentation**: If your change modifies user-facing behavior or MCP tool schemas, update the `README.md` and docstrings accordingly.

## Code of Conduct

By participating in this project, you agree to abide by standard open-source community guidelines. Be respectful, constructive, and collaborative.
