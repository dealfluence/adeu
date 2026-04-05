# Feature Specification: `adeu sanitize` — DOCX Metadata Scrubber

## 1. Problem

DOCX files leak information. Track changes reveal negotiation strategy. Comments contain candid internal notes. Metadata exposes author names, file server paths, DMS structure, editing timelines, and even deleted text that persists in the XML.

Law firms pay $30-80/user/month for metadata scrubbing tools. Lawyers still forget to use them. When they do use them, the tools scrub silently — no proof of what was removed.

**This tool does two things**: strips dangerous metadata, and produces a report proving what was stripped.

## 2. CLI Interface

```
adeu sanitize <input> -o <output> [flags]
```

Three paths, determined by which flags are present:

| Flag | Use case | Behavior |
|------|----------|----------|
| _(none)_ | Closing / signature | Full scrub. Refuses if unresolved track changes exist. |
| `--keep-markup` | Sending a redline | Keeps existing track changes and open comments. Strips everything else. |
| `--baseline <file>` | Forgot Track Changes / multi-round cleanup | Recomputes your delta against the original. Strips everything else. |

### 2.1 Full Sanitize (closing / signature)

```
adeu sanitize contract.docx -o clean.docx [--accept-all] [--report]
```

Strips everything. Produces a clean document with no history. Refuses if unresolved track changes exist unless `--accept-all` is passed (see §3).

### 2.2 Keep Markup (sending a redline to counterparty)

```
adeu sanitize redline.docx -o clean.docx --keep-markup [--author "Firm Name"] [--report]
```

The most common outbound workflow. The lawyer opened the counterparty's document, turned on Track Changes, made edits and added comments. The file already contains the delta as `w:ins`/`w:del` markup.

`--keep-markup` preserves that markup and strips everything else:

- Track changes (`w:ins`, `w:del`) → **kept**
- Open comments → **kept** (these are your notes to counterparty)
- Resolved comments → **stripped** (internal deliberation — see §4.3)
- Author names on markup/comments → replaced with `--author` value (or "Author")
- All metadata, rsids, DMS data, paths → **stripped**

**Warning on empty markup**: If the document contains no track changes and no comments, the tool warns:

```
WARNING: Document contains no tracked changes or comments.
  Output will be identical to a full sanitize.
  If you edited without Track Changes, use --baseline to reconstruct the redline.
```

### 2.3 Baseline Sanitize (reconstructing the delta)

```
adeu sanitize edited.docx --baseline original.docx -o clean.docx [--author "Firm Name"] [--report]
```

For when Track Changes was off, or when a document has accumulated multiple rounds of markup that need to be collapsed into a clean delta.

The tool:
1. Extracts text from both documents (using `ingest.py`)
2. Computes word-level diff (using `diff.py`)
3. Produces a new DOCX based on the baseline with `w:ins`/`w:del` showing only your changes
4. Strips all metadata from the result

Comments:
- Comments present in the baseline document → **stripped** (theirs)
- Comments not in the baseline → **kept** (yours, subject to resolved/open rule)
- Resolved comments → **stripped** regardless of origin

**Baseline rule**: The baseline is always the last document you *received* from the counterparty — the file you opened and started editing. Your delta is measured from that point. Using an earlier version (e.g., your own original proposal instead of their redline of it) would incorrectly attribute their changes to you.

**Divergence warning**: If the baseline and working document differ by more than 50% of content, the tool warns:

```
WARNING: Baseline and working document differ by 73%.
  This may indicate the wrong baseline file was selected.
  Proceeding — review the output carefully.
```

### 2.4 Batch Mode

```
adeu sanitize *.docx --outdir final/ [--report] [--report-file report.txt]
adeu sanitize *.docx --baseline baselines/ --outdir outgoing/ [--report]
```

Processes multiple files. One report per file plus a consolidated summary. Non-zero exit code if any document has issues (see §6.4).

In batch baseline mode, files are matched by name: `contracts/NDA.docx` looks for `baselines/NDA.docx`. Missing baseline is a fatal error for that file.

### 2.5 Common Flags

| Flag | Description |
|------|-------------|
| `-o <path>` | Output file (single) or `--outdir <dir>` (batch) |
| `--report` | Print report to stderr |
| `--report-file <path>` | Write report to file (for compliance archival) |
| `--author <name>` | Replace all author names with this value. Used with `--keep-markup` or `--baseline`. |
| `--accept-all` | Accept all unresolved track changes (full sanitize only). Required if track changes exist. |

## 3. Safety Gate

`sanitize` (without `--keep-markup` or `--baseline`) **refuses** if the document contains unresolved track changes:

```
$ adeu sanitize contract.docx -o clean.docx
ERROR: Document contains 7 unresolved tracked changes.
  3 insertions, 4 deletions — review in Word first, or use --accept-all.
  Run with --report to preview what would be accepted.
```

`--accept-all` overrides this. The report lists every change that was auto-accepted:

```
$ adeu sanitize contract.docx -o clean.docx --accept-all --report
Auto-accepted: 7 tracked changes
  §4.2 Indemnification: "Vendor" → "Supplier"
  §8.1 Term: Inserted "not to exceed $500,000"
  ...
```

This prevents a counterparty's unreviewed insertion from being silently accepted as final text.

The safety gate does **not** apply to `--keep-markup` (markup is being preserved, not accepted) or `--baseline` (delta is recomputed from scratch).

## 4. What Gets Stripped

### 4.1 Always Stripped

| Category | What | Why it leaks |
|----------|------|-------------|
| **rsid attributes** | `w:rsidR`, `w:rsidRPr`, `w:rsidRDefault`, `w:rsidP`, `w:rsidDel`, `w:rsidSect` | Reconstructs editing session order |
| **Paragraph IDs** | `w14:paraId`, `w14:textId` | No user value, noise |
| **proofErr** | `w:proofErr` spellcheck markers | No user value |
| **Template path** | `Template` in `docProps/app.xml` | Reveals `\\FIRM-DMS\templates\...` paths |
| **Printer** | Printer references in `docProps/app.xml` | Reveals office location/infrastructure |
| **Custom XML** | `customXml/` parts (iManage, NetDocuments, etc.) | DMS matter numbers, client codes |
| **Doc properties** | `TotalTime`, `Words`, `Characters`, `Paragraphs`, `Lines`, revision count | Editing timeline, effort spent |
| **Author metadata** | `dc:creator`, `cp:lastModifiedBy` in `docProps/core.xml` | Who worked on it |
| **Timestamps** | `dcterms:created`, `dcterms:modified` in `docProps/core.xml` | When it was worked on |
| **Hidden text** | Runs with `w:vanish` or `w:webHidden` in `w:rPr` | Invisible in Word, readable in XML |
| **Orphaned runs** | Content outside paragraph flow (fast-save remnants) | Previously deleted text still in file |
| **Internal hyperlinks** | Links targeting internal URLs (SharePoint, intranet patterns) | Reveals internal infrastructure |
| **Image alt text** | Auto-generated `descr` attributes on `wp:docPr` | Often contains source filenames |
| **Embedded OLE metadata** | Document properties inside embedded objects | Nested documents carry full metadata |
| **Empty property elements** | `<w:rPr/>`, `<w:pPr/>` with no children | Noise |
| **Resolved comments** | Comments marked resolved (`w15:done="1"`) | Internal deliberation artifacts |

### 4.2 Flag-Dependent Behavior

| What | No flag (full) | `--keep-markup` | `--baseline` |
|------|---------------|----------------|-------------|
| Track changes | Remove (requires `--accept-all`) | **Keep as-is** | **Recompute from baseline diff** |
| Open comments | Remove | **Keep** | **Keep if not in baseline** |
| Resolved comments | Remove | Remove | Remove |
| Author on tracked changes | Remove | Replace with `--author` | Replace with `--author` |
| Author on comments | Remove | Replace with `--author` | Replace with `--author` |
| Run coalescing | Yes | Yes (respects `w:ins`/`w:del` boundaries) | Yes (on freshly generated markup) |

### 4.3 Comment Convention: Resolved = Internal, Open = External

The tool cannot read a lawyer's intent for each comment. Instead, it relies on a simple convention aligned with how lawyers already work:

- **Open comments** are for the counterparty ("We cannot accept uncapped indemnity")
- **Resolved comments** are internal notes that have served their purpose ("Client approved $2M cap")

**Workflow**: Before running `sanitize --keep-markup` or `--baseline`, resolve any internal comments in Word. Leave comments intended for the counterparty open. The tool strips resolved, keeps open.

The report makes this reviewable (see §5) — listing exactly which comments will be visible and which were stripped.

### 4.4 Run Coalescing Safety

Run coalescing (merging adjacent `w:r` elements with identical `w:rPr`) **never crosses track change boundaries**. Runs inside `w:ins`, `w:del`, `w:moveTo`, or `w:moveFrom` elements are treated as isolated groups. This preserves track change structure.

## 5. The Report

The report is the key differentiator. Existing tools scrub silently. This tool proves what it did — and shows what will be visible to the recipient.

References use the **nearest heading** from the document's outline structure (extracted via `ingest.py`), not paragraph indices. If no heading exists, falls back to `¶<n>` (paragraph count from document start).

### 5.1 Full Sanitize Report

```
═══════════════════════════════════════════
Sanitize Report: MSA_Final.docx
═══════════════════════════════════════════

TRACKED CHANGES (auto-accepted via --accept-all)
  7 total: 3 insertions, 4 deletions
  ├─ §4.2 Indemnification: "Vendor" → "Supplier"
  ├─ §8.1 Term: Inserted "not to exceed $500,000"
  ├─ §9.3 Limitation of Liability: Deleted clause 9.3(b) entirely
  └─ (4 more)

COMMENTS (removed)
  3 total: 2 resolved, 1 open
  ├─ §4.2 [Resolved] "Check indemnity cap with client" (J. Smith)
  ├─ §7.1 [Resolved] "Confirmed with tax team" (A. Lee)
  └─ §9.3 [Open] "Counterparty won't accept this" (D. Park)

METADATA (scrubbed)
  Authors found: J. Smith, A. Lee, D. Park
  Template: \\FIRM-DMS\templates\MSA_Standard_v4.dotx
  Last printer: HP LaserJet 4th Floor East
  Custom XML: 1 part (iManage metadata)

STRUCTURAL (cleaned)
  rsid attributes: 247 removed
  Empty property elements: 12 removed
  Orphaned runs: 0
  Hidden text: 0

WARNINGS
  ⚠ §12.1: Hyperlink targets internal URL (https://firm.sharepoint.com/...)

═══════════════════════════════════════════
Result: CLEAN (1 warning)
═══════════════════════════════════════════
```

### 5.2 Keep-Markup / Baseline Report

When using `--keep-markup` or `--baseline`, the report adds a section showing what **will be visible** to the counterparty:

```
═══════════════════════════════════════════
Sanitize Report: NDA_v2_redline.docx
--keep-markup --author "Smith & Associates"
═══════════════════════════════════════════

VISIBLE TO COUNTERPARTY
  Tracked changes: 12 (5 insertions, 7 deletions)
  Open comments: 2
  ├─ §3.1 Confidentiality: "We cannot accept a 5-year tail period"
  └─ §6.2 Governing Law: "Please confirm jurisdiction"
  Author on all markup: "Smith & Associates"

STRIPPED
  Resolved comments: 4
  ├─ §3.1 "Partner approved 3-year tail as fallback" (J. Chen)
  ├─ §4.1 "Check with tax team" (D. Park)
  ├─ §5.3 "Client OK with this" (J. Chen)
  └─ §8.1 "Standard language, no change needed" (A. Lee)

METADATA (scrubbed)
  Authors found: J. Chen, D. Park, A. Lee
  Template: \\FIRM-DMS\templates\NDA_Mutual_v2.dotx
  Custom XML: 1 part (iManage metadata)

STRUCTURAL (cleaned)
  rsid attributes: 183 removed
  Empty property elements: 8 removed

═══════════════════════════════════════════
Result: CLEAN
═══════════════════════════════════════════
```

### 5.3 Batch Summary

```
═══════════════════════════════════════════
Batch Summary: 4 documents processed
═══════════════════════════════════════════
  ✓ MSA_Final.docx          — clean (1 warning)
  ✓ NDA_Final.docx          — clean
  ✗ SOW_1.docx              — BLOCKED: 3 unresolved track changes
  ✓ SOW_2.docx              — clean
═══════════════════════════════════════════
Exit code: 1 (1 document blocked)
```

Individual per-file reports precede the summary. With `--report-file`, the full output (all per-file reports + summary) is written to the specified path.

## 6. Implementation

### 6.1 Architecture

```
src/adeu/
  sanitize/
    __init__.py          # CLI wiring, orchestration
    report.py            # Report generation, heading resolution
    transforms.py        # All strip/clean transforms
```

### 6.2 Transform Execution

Each transform is a function that mutates the XML tree and returns report lines:

```python
def strip_rsid(tree: etree._ElementTree) -> list[str]:
    """Remove rsid attributes. Returns human-readable summary."""
```

The orchestrator:
1. Loads the DOCX via `python-docx` / `zipfile`
2. Iterates over all XML parts
3. Applies each transform, collecting report lines
4. Handles flag-specific logic (`--keep-markup`, `--baseline`, `--accept-all`)
5. Repacks the DOCX
6. Emits the report

### 6.3 Relationship to Existing Code

- **Run coalescing**: reuse from `utils/docx.py` (`normalize_docx`)
- **Track change acceptance**: inverse of `RedlineEngine` — unwrap `w:ins` (keep content), remove `w:del` (remove content), strip `w:rPrChange`
- **Comment extraction for report**: reuse `ingest.py` comment parsing (author, date, scope, resolved status)
- **Heading resolution for report**: reuse `ingest.py` heading detection (outline level / style name)
- **Baseline diffing**: adeu's core pipeline. `--baseline` mode uses `ingest.py` to extract text from both documents, `diff.py` to compute word-level changes, and `RedlineEngine` to inject `w:ins`/`w:del` into a clean copy. Conceptually: accept all existing markup, then re-redline against the baseline.

### 6.4 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All documents sanitized successfully |
| 1 | One or more documents blocked (unresolved track changes without `--accept-all`) |
| 2 | Processing error (corrupt DOCX, I/O error, missing baseline in batch) |

## 7. Scenarios Validated

### 7.1 Receive counterparty draft, redline, send back

```
$ adeu sanitize redline.docx -o out.docx --keep-markup --author "Firm A" --report
```

Associate had Track Changes on. Markup is preserved, metadata stripped, author names unified. Report confirms which comments go out (open) and which were stripped (resolved).

If Track Changes was off:
```
$ adeu sanitize edited.docx --baseline incoming.docx -o out.docx --author "Firm A" --report
```

### 7.2 Receive counterparty redline of your proposal, respond

```
$ adeu sanitize response.docx --baseline counterparty_redline.docx -o out.docx --author "Firm A" --report
```

Baseline is the counterparty's redline (what you received), not your original proposal. Multi-round markup is collapsed into a clean delta showing only your changes.

### 7.3 Internal collaboration before sending

Three lawyers edit with Track Changes on. Before sending:

1. Each lawyer resolves their internal comments in Word
2. Leave counterparty-facing comments open
3. Run:
```
$ adeu sanitize draft.docx --baseline counterparty_v3.docx -o out.docx --author "Firm A" --report
```

Report shows "VISIBLE TO COUNTERPARTY" (what goes out) and "STRIPPED" (what was removed). Partner reviews and signs off.

### 7.4 Deal closing (batch finalization)

```
$ adeu sanitize final/*.docx --outdir executed/ --report --report-file closing_report.txt
```

Safety gate blocks any document with unresolved track changes. Paralegal fixes the blocked documents, reruns. Report is saved to matter file for 7-year compliance retention.

## 8. What Was Cut (and Why)

| Feature | Why cut |
|---------|---------|
| `fmt normalize` (explode DOCX to XML) | No user pull. Lawyers don't use git. Can be added later. |
| `fmt pack` (reassemble DOCX from XML) | Only needed if normalize exists. |
| Git integration (smudge/clean, hooks, watcher) | Architecturally complex, support-heavy. Git is not a collaboration tool for lawyers. |
| Profiles / transform exclusion | Three flags (`--keep-markup`, `--baseline`, `--accept-all`) cover all scenarios. No configuration needed. |
| Pretty-printing / attribute canonicalization | Only valuable for git diffing. Sanitize outputs a DOCX, not readable XML. |
| History export / archival tagging | Real need but separate product. Depends on git integration that was cut. |

## 9. MCP Tool

In addition to the CLI, sanitize is exposed as an MCP tool so AI agents can sanitize documents as part of their workflow — e.g., an agent that drafts a redline and sanitizes it before presenting the output to the user.

### 9.1 Tool Definition

```
sanitize_docx(
    file_path: str,           # Absolute path to the DOCX file
    output_path: str | None,  # Output path (default: <stem>_sanitized.docx)
    keep_markup: bool = False, # Keep track changes and open comments
    baseline_path: str | None, # Path to baseline document for delta recomputation
    author: str | None,       # Replace author names (used with keep_markup or baseline)
    accept_all: bool = False,  # Accept unresolved track changes (full sanitize only)
) -> SanitizeResult
```

Returns a structured result with the report data (not just a string), so the agent can reason about what was found:

```python
class SanitizeResult:
    output_path: str
    status: "clean" | "clean_with_warnings" | "blocked"
    tracked_changes_found: int
    tracked_changes_accepted: int
    comments_removed: int
    comments_kept: int
    metadata_stripped: list[str]   # ["Template path", "3 authors", "iManage custom XML"]
    warnings: list[str]           # ["Hyperlink in §12.1 targets internal URL"]
    report_text: str              # Full human-readable report (same as CLI output)
```

### 9.2 Agent Workflows

**Redline + sanitize in one flow:**
An agent using `process_document_batch` to apply edits can follow up with `sanitize_docx` to produce a clean outbound version:

```
1. Agent calls read_docx(file_path="incoming.docx")
2. Agent reasons about edits
3. Agent calls process_document_batch(original_docx_path="incoming.docx", changes=[...])
4. Agent calls sanitize_docx(file_path="incoming_processed.docx", keep_markup=True, author="Firm A")
5. Agent presents the sanitized file + report to the user
```

**Pre-send check (read-only):**
An agent could also use sanitize in a dry-run/inspection mode — by checking the report without writing a new file. This is a future consideration; for v1 the tool always produces an output file.

### 9.3 Relationship to `accept_all_changes`

The existing `accept_all_changes` MCP tool does a subset of what `sanitize_docx` does (accepts revisions, strips comments). `sanitize_docx` with `accept_all=True` is a strict superset — it also strips metadata, hidden text, orphaned runs, etc. The existing tool remains for backward compatibility but `sanitize_docx` is preferred for outbound documents.

## 10. Future Scope

1. **Outlook / DMS integration** — The CLI is the engine. The product is a pre-send hook in Outlook or a workflow step in iManage/NetDocuments.
2. **CI/CD for legal** — Run sanitize in a pipeline before documents hit a deal room or VDR. Exit codes and `--report-file` already support this.
3. **`adeu fmt normalize`/`pack`** — Git versioning for firms that want audit trails. Only build if a customer asks.
