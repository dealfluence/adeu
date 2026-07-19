# The Adeu Text Projection: Fidelity Model

Adeu edits DOCX files through a text proxy: `extract` (CLI) / `read_docx`
(MCP) project the document into Markdown-flavored text, and `apply` /
`process_document_batch` translate text changes back into tracked OOXML
changes. This page states precisely which document properties that projection
**preserves exactly**, which it **normalizes**, and which it **omits** — so
agents and automation never assume a guarantee the representation does not
make (QA 2026-07-19 ADEU-QA-007).

Terminology used below:

- **Exact** — round-trips byte-for-byte through extract → apply.
- **Normalized** — projected in a canonical form; applying text back
  preserves the underlying structure, but the projection is not a literal
  rendering of the XML.
- **Read-only** — visible in the projection but rejected as an edit target
  (`BatchValidationError` on any attempt to modify or fabricate it).
- **Omitted** — not visible in the projection at all; untouched by edits.

| Property | Fidelity | Details |
|---|---|---|
| Body text | Exact | Character-for-character, including Unicode. Smart quotes are matched tolerantly on *lookup* but never rewritten in the document. |
| Paragraph identity | Exact | One projected block (`\n\n`-separated) per `w:p`, in strict XML document order (tables interleaved where they occur). |
| Bold / italic | Normalized | Projected as `**bold**` and `_italic_` (never `*italic*`). Markers hug non-whitespace; adjacent same-style runs are coalesced. Other run formatting (underline, color, fonts, size) is **omitted** from the text but preserved through edits — untouched runs are never rewritten. |
| Headings | Normalized | Style-based headings project as `#`–`######`. Depth is clamped to 6. |
| Lists | Normalized | Numbered items always project as the constant `1. ` (Markdown renumbers); bullets as `* `; nesting as 4 spaces per level. The real `numPr`/`ilvl` structure is preserved and reconstructed on write. **Do not assume the projected number is the rendered number.** |
| Tables | Normalized | Cells joined by ` \| `, rows by single newlines. The `\|` boundary is a hard cell wall: text edits across it are split per-cell; structural row/column changes via text are rejected (use `insert_row` / `delete_row`). |
| Headers / footers / footnotes / endnotes | Normalized | Projected as separate parts flattened into one string. Edits can never cross a part boundary; comments are rejected outside the main body. Footnotes project as `[^fn-ID]` markers with bi-directional editing. |
| Hyperlinks | Normalized | `[text](url)`. Editing text produces tracked changes; editing the URL retargets the relationship silently (no redline). |
| Images / drawings | Read-only | Projected as `![alt](docx-image:N)`. Cannot be added, removed, or retitled via text; diff reports image differences as warnings, never as edits. |
| Cross-references | Read-only | Projected as `[~text~](#_Ref)`. Display text is computed by Word; modifying it is rejected. |
| Bookmarks / anchors | Read-only | Projected inline as `{#name}`. `{#cell:paraId}` anchors are valid *targets* for writing into empty table cells, but the tokens themselves cannot be edited or fabricated. |
| Tracked changes / comments | Normalized | CriticMarkup (`{++ins++}`, `{--del--}`, `{>>meta<<}`). A replacement's del+ins pair is annotated `(pairs with Chg:N)` — one accept/reject resolves the whole pair. The clean view (`--clean-view`) renders the accepted state instead. |
| Fields (TOC, page numbers, computed values) | Omitted / read-only | Field *results* may appear as text where Word cached them; field codes are not projected. TOC boundaries are guarded via the structural appendix. |
| Page breaks / line breaks | Normalized | Both project as a newline. The specific `w:br` type is preserved in the XML — runs containing breaks are immutable boundaries — but is not distinguishable in the text. |
| Section breaks / page geometry | Omitted | Margins, columns, orientation and `w:sectPr` are invisible to the projection and never modified (a deleted paragraph's `sectPr` is explicitly carried over). |
| Text boxes / drawing canvases / SmartArt | Omitted | Content inside floating drawing objects is not projected and cannot be edited via text. |
| "Pages" (`--page`) | Synthetic | Adeu pages are **length-based content chunks sized for LLM consumption**. They do not correspond to printed Word pages or explicit page breaks; banners say so explicitly. Outline mode ignores `--page`. |
| Document metadata | Out of scope for extract | Core/custom properties, document variables (`w:docVar`), RSIDs and timestamps are invisible to extract; `adeu sanitize` is the tool that manages them, and its report enumerates the categories it scrubbed. |

## The load-bearing guarantees

1. **Untouched content is never rewritten.** The engine operates in surgical
   mode: paragraphs your edits do not touch keep their exact XML, whitespace
   included.
2. **Text edits become tracked changes** (`w:ins`/`w:del`) attributed to the
   configured author — never silent rewrites.
3. **Structural constructs fail loudly.** Anything the text proxy cannot
   safely express (read-only markers, cross-part spans, table structure via
   text, unbalanced paragraph merges) is rejected with a
   `BatchValidationError` before anything is written — never applied
   approximately.
4. **Exit status is authoritative.** File existence does not imply success:
   a failed `apply` writes no file at the requested output path (a
   `.unverified.docx` diagnostic may exist alongside), and `sanitize` prints
   `Result: CLEAN` only after re-scanning the saved bytes.
