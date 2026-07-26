# Specification: Agentic CLI (The Headless Adapter)

Status: **Implemented** (July 2026)

## 1. Rationale

When an agent operates in a closed sandbox (e.g., an LLM running in a raw bash
environment or a CI pipeline), it cannot connect to an MCP server. The
`read_file_bytes` fallback in `python/src/adeu/mcp_components/shared.py`
explicitly tells sandboxed agents to install the CLI (`uv tool install adeu`)
and run the mapped commands directly.

The CLI is therefore a **Command-Line API**, not just a human convenience: it
must accept the exact same JSON change schema as the MCP tools and be able to
emit machine-readable JSON results that an agent can verify programmatically.
It must also remain strictly local and air-gapped — no cloud or auth logic may
bleed into the CLI surface, preserving its utility in high-security sandboxes.

## 2. Tool mapping (MCP ↔ CLI parity)

| MCP tool                 | CLI equivalent                             |
| ------------------------ | ------------------------------------------ |
| `read_docx`              | `adeu extract <doc> [--json]`              |
| `diff_docx_files`        | `adeu diff <orig> <mod> [--json]`          |
| `process_document_batch` | `adeu apply <doc> <changes.json> [--json]` |
| `accept_all_changes`     | `adeu accept-all <doc> [--json]`           |

This table must stay in sync with the fallback message in
`mcp_components/shared.py::read_file_bytes` — that message is the discovery
mechanism through which sandboxed agents learn the CLI surface.

`adeu accept-all` wraps `RedlineEngine.accept_all_revisions(remove_comments=True)`
and defaults its output to `<stem>_clean.docx`, byte-for-byte mirroring the
`accept_all_changes` MCP tool's behavior.

Commands that only make sense outside a sandbox (`adeu init` for Claude
Desktop config, `--live` Windows COM interop) remain available but are not
part of the agentic contract.

## 3. Machine-readable outputs (`--json`)

Every command in the mapping accepts `--json`. When passed, stdout carries a
single JSON document and the human-readable progress logs are suppressed.

* **`adeu apply --json`** — prints the engine's raw stats object:
  `actions_applied`, `actions_skipped`, `edits_applied`, `edits_skipped`,
  `skipped_details`, `edits` (per-edit reports with `status`, `target_text`,
  `new_text`, `error`, `warning`, CriticMarkup/clean-text previews), `engine`,
  `version`, plus two CLI-level fields: `output_path` (null on dry runs) and
  `dry_run`. A batch rejected by validation prints
  `{"error": "batch_validation_failed", "errors": [...]}` and exits 1.
* **`adeu extract --json`** — prints the MCP machine channel
  (`structured_content`) of the extraction: `{"markdown": ..., "title": ...,
  "file_path": ...}`. Composes with `-o` (the raw text payload still goes to
  the file; the JSON envelope goes to stdout).
* **`adeu diff --json`** — prints the raw edit array (pre-existing behavior).
* **`adeu accept-all --json`** — prints `{"status": "ok", "output_path": ...}`.
* **`adeu sanitize --json`** — prints the `sanitize_docx` MCP tool's payload
  field-for-field (`status`, `output_path`, `tracked_changes_found`,
  `tracked_changes_accepted`, `comments_removed`, `comments_kept`,
  `metadata_stripped`, `warnings`, `report_text`) plus the CLI-only `input`, so
  one parser serves both surfaces. **`status` here is the document verdict** —
  `clean`, `clean_with_warnings`, or `blocked` — *not* an invocation outcome:
  success is the exit code. A refusal (unresolved tracked changes) is a normal
  payload, `{"status": "blocked", "error": "blocked", "message": <one-line
  reason>, "report_text": <full report>}`, and exits 1.
  Batch mode (`--outdir`) wraps them in a CLI-shaped envelope — `status`
  (`ok`/`failed`, batch-level), `outdir`, `total`, `succeeded`, `blocked`,
  `results` — where `results` holds one entry per input **in input order**, so
  `len(results) == total` always holds and every failure (`blocked`,
  `invalid_docx`, `file_not_found`) is identified by its `input`. A failed
  batch adds `{"error": "batch_failed", "message": ...}` and writes no outputs.
  `--json` cannot be combined with `--report-file -` (both target stdout); the
  report is in `report_text` instead.

## 4. Strict I/O discipline

* **stdout** is reserved for document data (Markdown/CriticMarkup) and `--json`
  results — nothing else. `uvx adeu extract doc.docx > out.md` must produce a
  mathematically clean file.
* **stderr** carries all logs, debug output, progress messages, warnings, and
  error text. The CLI configures structlog with
  `PrintLoggerFactory(file=sys.stderr)` so `--debug` logging can never pollute
  stdout.
* **Exit codes**: `0` = full success. `1` = hard failure (missing/corrupt
  file, rejected batch) *or* a partially applied batch — agents must check
  `edits_skipped`/`actions_skipped` in the JSON stats to distinguish.

## 5. The agentic loop

The canonical sandbox workflow the CLI must keep supporting:

```bash
uv tool install adeu                                  # once, inside the sandbox
adeu extract contract.docx --json > doc.json          # read
# ... agent plans edits, writes changes.json ...
adeu apply contract.docx changes.json --json > result.json
jq -e '.edits_skipped == 0' result.json               # verify systematically
adeu accept-all contract_redlined.docx --json         # finalize (optional)
```

Regression coverage lives in `python/tests/test_cli_features.py`
(`test_cli_apply_json*`, `test_cli_accept_all*`, `test_cli_sanitize_json*`,
`test_cli_debug_logs_go_to_stderr_only`).
