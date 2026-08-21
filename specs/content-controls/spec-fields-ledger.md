# Spec: Fields Ledger & Discovery

Status: frozen v1 · Task: CC-2 · Acceptance: [A2](acceptance/A2-fields-ledger.md)

House rule (AI_CONTEXT §13): visibility gaps are solved by extending `read_docx`'s
projection, not by adding MCP tools. Discovery therefore ships as a read mode.

## 1. Surfaces

- MCP (both servers): `read_docx(mode="fields")`, paginated with `fields_offset`
  (integer, default 0 — mirrors `changes_offset`). `search_query` and `page` do not
  apply to this mode in v1 (documented in the tool description).
- CLI: `adeu extract <file> --mode fields` (same rendered text; `--json` wraps it as
  `{"content": …}` per CLI stream conventions).
- Schema constraints (AI_CONTEXT §7a): `mode` stays a plain string enum — adding
  `"fields"` to the enum introduces no union; `fields_offset` publishes `type: number`.
  Description budget: the `read_docx` description must stay ≤ 2048 chars including the
  build tag after documenting the mode (CC-7 pins this).

## 2. Header

```
# Fields: <basename>
Protection: <as banner, spec-projection §7> · <N> content controls — <e> empty · <l> locked · <b> bound
```

Zero-control document: header + `No content controls.` (plus the protection line —
the Ontario Juries Form 1 case: protection matters even with zero SDTs).

## 3. Ledger line format (exact)

One line per control, ordinal order, `\n`-separated:

```
CC:<N>  <class>  ["<alias>"] [(tag: <tag>)] — <loc> [— in CC:<M>] [— <state>…] [— value: "<preview>"] [— placeholder: "<text>"] [— options: <a> | <b> | …] [— format: <fmt>] [— <extent>]
```

Segments, in this order, each emitted only when applicable:

1. `CC:<N>` — two-space padded to align (`CC:1 `, `CC:14 `).
2. Class word: `text`, `richtext`, `dropdown`, `combobox`, `date`, `checkbox`,
   `picture`, `building-block`, `group`, `repeating`, `item`.
3. `"<alias>"` then `(tag: <tag>)` — omitted when absent (anonymous controls show
   neither; the corpus norm). Tags render verbatim (spaces and `#` are legal).
4. `<loc>`: `p<synthetic page>` plus ` · <heading path>` when a heading path exists
   (same breadcrumb source as search results); ` — table cell` / ` — table row` for
   cell-/row-level controls.
5. `in CC:<M>` for controls nested in a group or repeating section.
6. State tokens (upper-case, in this order):
   - `EMPTY` (placeholder/empty state)
   - `LOCKED (contents)` for `sdtContentLocked`/`contentLocked`;
     `LOCKED (group)` for groups; `LOCKED (no-delete)` for bare `sdtLocked`
   - `BOUND → <xpath>` for data bindings
   - `TEMPORARY` for `w:temporary`
7. `value: "<preview>"` — current visible text, whitespace-collapsed, truncated at
   80 chars with a trailing `…`. Checkboxes render `checked` / `unchecked` instead of a
   value segment.
8. `placeholder: "<text>"` — for EMPTY controls with placeholder text (80-char cap).
9. `options: A | B | C` — dropdown/combobox `listItem` display texts, first 8, then
   `| … (+K more)`.
10. `format: <w:dateFormat>` for date controls.
11. Extent for containers: `wraps <n> blocks, <m> nested fields` (group),
    `<k> items` (repeating).

## 4. Pagination

Ledger caps at 100 lines per response; more controls ⇒ final line
`… <remaining> more — pass fields_offset=<next> to continue.` (FedRAMP rev4 = 5,007
lines total; the cap keeps the budget guard philosophy).

## 5. Appendix integration

`mode="appendix"` gains a `## Content Controls` section containing ONLY the two header
lines from §2 plus the surface-aware hint to the fields mode. The full ledger never
renders in the appendix (bounded-appendix rule).

## 6. Diff & reports

- Per-edit reports (`process_document_batch`) add `field: CC:<N> "<alias>"` to the
  report block when an edit's resolved range lies inside a control (audit-trail symmetry
  with `heading_path`).
- `diff_docx_files` labeling of field value changes is P3 (CC-9), not v1.

## 7. Cross-document search

Explicit non-goal for the engine in v1. Batch surfaces already iterate files (n8n,
mailroom, CLI loops); a recipe example lands with CC-9. The ledger's exact, stable line
format is the enabler — treat it as an output contract.
