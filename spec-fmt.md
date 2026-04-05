# Feature Specification: `adeu sanitize` — DOCX Metadata Scrubber

## 1. Problem

DOCX files leak information. Track changes reveal negotiation strategy. Comments contain candid internal notes. Metadata exposes author names, file server paths, DMS structure, editing timelines, and even deleted text that persists in the XML.

Law firms pay $30-80/user/month for metadata scrubbing tools. Lawyers still forget to use them. When they do use them, the tools scrub silently — no proof of what was removed.

**This tool does two things**: strips dangerous metadata, and produces a report proving what was stripped.

## 2. CLI Interface

### 2.1 Primary Command

```
adeu sanitize <input.docx> -o <output.docx> [--report] [--mode full|outbound]
```

### 2.2 Batch Mode

```
adeu sanitize contracts/*.docx --outdir final/ --report
```

Processes multiple files. Consolidated report across all documents. Non-zero exit code if any document has issues.

### 2.3 Modes

**`--mode full`** (default): Preparing a clean document for signature or archival.
- Accept all track changes (requires `--accept-all`, otherwise refuses)
- Remove all comments
- Scrub all author/timestamp metadata
- Strip all internal infrastructure traces

**`--mode outbound`**: Sending a redline or marked-up draft to counterparty.
- Keep your track changes (the redline IS the deliverable)
- Keep open comments (your notes to counterparty)
- Remove resolved comments (internal deliberation)
- Replace all author names with a single identity (`--author "Firm A"`)
- Strip all internal infrastructure traces

## 3. Safety Gate

`sanitize --mode full` **refuses to run** if the document contains unresolved track changes:

```
$ adeu sanitize contract.docx -o clean.docx
ERROR: Document contains 7 unresolved tracked changes.
  3 insertions, 4 deletions — review in Word first, or use --accept-all.
  Use --report to preview what would be accepted.
```

`--accept-all` overrides this. The report lists every change that was auto-accepted:

```
$ adeu sanitize contract.docx -o clean.docx --accept-all --report
Auto-accepted: 7 tracked changes
  p.12: Deleted "Vendor" → Inserted "Supplier" (by Opposing Counsel)
  p.34: Inserted "not to exceed $500,000" (by Opposing Counsel)
  ...
```

This prevents a counterparty's unreviewed insertion from being silently accepted as final text.

## 4. What Gets Stripped

### 4.1 Always (both modes)

| Category | What | Why it leaks |
|----------|------|-------------|
| **rsid attributes** | `w:rsidR`, `w:rsidRPr`, etc. on every run | Reconstructs editing session order |
| **Paragraph IDs** | `w14:paraId`, `w14:textId` | No user value, noise |
| **proofErr** | Spellcheck markers | No user value |
| **Template path** | `Template` in `docProps/app.xml` | Reveals `\\FIRM-DMS\templates\...` paths |
| **Printer** | Printer references in `docProps/app.xml` | Reveals office location/infrastructure |
| **Custom XML** | `customXml/` parts (iManage, NetDocuments, etc.) | DMS matter numbers, client codes |
| **Doc properties** | `TotalTime`, `Words`, revision count in `docProps/` | Editing timeline, effort spent |
| **Author metadata** | `dc:creator`, `cp:lastModifiedBy` | Who worked on it |
| **Timestamps** | `dcterms:created`, `dcterms:modified` | When it was worked on |
| **Hidden text** | Runs with `w:vanish` or `w:webHidden` | Invisible in Word, readable in XML |
| **Orphaned runs** | Content outside paragraph flow (fast-save remnants) | Previously deleted text still in file |
| **Hyperlink audit** | Internal URLs (SharePoint, intranet) | Reveals internal infrastructure |
| **Image alt text** | Auto-generated `descr` attributes on images | Often contains source filenames |
| **Embedded OLE metadata** | Document properties inside embedded objects | Nested documents carry full metadata |

### 4.2 Mode-Specific

| What | `--mode full` | `--mode outbound` |
|------|--------------|-------------------|
| Track changes | Remove (requires `--accept-all`) | **Keep** |
| Open comments | Remove | **Keep** |
| Resolved comments | Remove | Remove |
| Author on track changes | Remove | Replace with `--author` value |
| Author on comments | Remove | Replace with `--author` value |
| Run coalescing | Yes | Yes (respecting track change boundaries) |
| Empty rPr/pPr cleanup | Yes | Yes |

## 5. The Report

The report is the key differentiator. Existing tools scrub silently. This tool proves what it did.

```
═══════════════════════════════════════════
Sanitize Report: MSA_Final.docx
Mode: full (--accept-all)
═══════════════════════════════════════════

TRACKED CHANGES (auto-accepted)
  7 total: 3 insertions, 4 deletions
  ├─ p.12: "Vendor" → "Supplier"
  ├─ p.34: Inserted "not to exceed $500,000"
  ├─ p.51: Deleted entire clause 8.3(b)
  └─ ... (4 more)

COMMENTS (removed)
  3 total: 2 resolved, 1 open
  ├─ p.7: [Resolved] "Check indemnity cap with client" (J. Smith)
  ├─ p.22: [Resolved] "Confirmed with tax team" (A. Lee)
  └─ p.45: [Open] "Counterparty won't accept this" (D. Park)

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
  ⚠ Hyperlink in p.67 targets internal URL: https://firm.sharepoint.com/...

═══════════════════════════════════════════
Result: CLEAN (1 warning)
═══════════════════════════════════════════
```

For batch mode, one report per file plus a summary:

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

## 6. Implementation

### 6.1 Architecture

```
src/adeu/
  sanitize/
    __init__.py          # CLI entry point, orchestration
    report.py            # Report generation
    transforms.py        # All transforms (flat module, no over-abstraction)
```

No profiles, no transform IDs, no `--exclude` flags. Two modes, that's it.

### 6.2 Transform Execution

Each transform is a function:

```python
def strip_rsid(tree: etree._ElementTree) -> list[str]:
    """Remove rsid attributes. Returns list of human-readable actions taken."""
```

Returns a list of strings for the report. The orchestrator collects them.

### 6.3 Relationship to Existing Code

- Run coalescing: reuse from `utils/docx.py`
- Track change acceptance (S01): inverse of `RedlineEngine` — unwrap `w:ins`, remove `w:del`
- Comment extraction for report: reuse `ingest.py` comment parsing
- The `sanitize` command is independent of `fmt normalize`/`pack` — those are a separate feature (git versioning) that can be built later if there's demand

### 6.4 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All documents sanitized successfully |
| 1 | One or more documents blocked (unresolved track changes without `--accept-all`) |
| 2 | Processing error (corrupt DOCX, I/O error) |

## 7. What Was Cut (and Why)

| Feature | Why cut |
|---------|---------|
| `fmt normalize` (explode to XML) | No user pulled for it. Lawyers don't use git. Can be added later. |
| `fmt pack` (reassemble DOCX) | Only needed if normalize exists. |
| Git smudge/clean filters | Architecturally broken (directory vs stream). Support nightmare. |
| File watcher | Over-engineering. Solves a problem nobody expressed urgently. |
| Profiles (`normalize`, `internal`, `minimal`) | Two modes (`full`, `outbound`) cover all expressed needs. |
| Transform exclusion (`--exclude T08`) | Power-user feature nobody asked for. If a transform causes issues, fix the transform. |
| Pretty-printing / attribute canonicalization | Only valuable for git diffing. Sanitize doesn't need it — output is a DOCX. |
| History export | Real need but separate product. Requires git integration that was cut. |
| Archival tagging | Same — depends on git. |

## 8. Future Scope (if sanitize proves valuable)

1. **`adeu fmt normalize`/`pack`** — Git versioning for firms that want audit trails. Only build if a customer asks.
2. **Outlook/DMS integration** — The CLI is the engine. The product is a pre-send hook in Outlook or a workflow step in iManage/NetDocuments. Build integrations after the engine is solid.
3. **CI/CD for legal** — Run sanitize in a pipeline before documents hit a deal room or VDR.
