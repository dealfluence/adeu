# Spec: Content-Control Projection

Status: frozen v1 · Task: CC-1 · Acceptance: [A1](acceptance/A1-projection.md)
Extends AI_CONTEXT.md §13 (Domain Gaps & Projection Syntax). All rules apply identically
to both engines (Virtual Text contract: ingest and mapper MUST emit byte-identical text).

## 1. Model

Every `w:sdt` in a projected part (body, headers, footers, notes, table content) is
classified once, in document order, by its `sdtPr`:

| Class | Detected by (first match wins) | Projection |
| --- | --- | --- |
| `checkbox` | `w14:checkbox` | `[x]` / `[ ]` token (§4) |
| `dropdown` | `w:dropDownList` | anchored leaf (§3) |
| `combobox` | `w:comboBox` | anchored leaf |
| `date` | `w:date` | anchored leaf |
| `picture` | `w:picture` | NOT anchored; content projects as today (image marker); ledger-only |
| `building-block` | `w:docPartObj` / `w:docPartList` | NOT anchored; content projects as today; ledger-only |
| `group` | `w:group` | block boundary tokens with `group` flag (§5) |
| `repeating` | `w15:repeatingSection` | NOT anchored in v1; ledger-only |
| `repeating-item` | `w15:repeatingSectionItem` | NOT anchored in v1; ledger-only |
| `text` | `w:text` | anchored leaf |
| `richtext` | `w:richText` present, or none of the above | anchored leaf if no nested `w:sdt`; otherwise treated as `group`-less container: contents project, control is ledger-only |

Ordinals `CC:N` are assigned 1-based in projection order across ALL classes and ALL
parts (headers → body → footers → notes, matching the flattened projection). Ordinals are
stable across reads of an unchanged document (same rule as `Chg:N`). The OOXML `w:id`
(random, possibly negative) is never shown inline; the ledger may expose it.

## 2. Token grammar

```
open        := "{#cc:" N ( " " flag )* "}"
close       := "{#/cc:" N "}"
flag        := "locked" | "bound" | "group"     (emitted in exactly this order)
checkbox    := "[x]" | "[ ]"
placeholder := "{>>placeholder: " TEXT "<<}"
```

- `locked` is emitted iff the control itself is content-locked: `w:lock` val
  `sdtContentLocked` or `contentLocked`. (`sdtLocked` alone = delete-locked but
  editable — ledger-only detail, no inline flag.)
- `bound` is emitted iff `sdtPr` carries `w:dataBinding`.
- `group` is emitted only on group controls (which are inherently locked regions; the
  `locked` flag is NOT additionally emitted).
- Tokens are VIRTUAL spans (no physical width) with two deliberate exceptions in §3/§4.
  They MUST survive every marker-stripping pass (outline, search snippets, previews) via
  the existing `{#…}` placeholder-protection mechanism (QA F4/F22b). `[x]`/`[ ]` MUST be
  added to that protected set.
- Fabricating, altering, or deleting anchor tokens via text replacement is rejected at
  validation (same Strict Refusal class as bookmarks/xrefs) — except the two sanctioned
  edit surfaces below.

## 3. Anchored leaf controls

- **Filled, inline:** `{#cc:N}` + content + `{#/cc:N}` inline in the paragraph. Content
  between anchors is real, matchable text; CriticMarkup for tracked changes renders
  inside the pair as normal.
- **Filled, block-level:** open token on its own line immediately before the first
  wrapped block; close token on its own line immediately after the last one. Standard
  `\n\n` paragraph separators surround the token lines; a single `\n` joins token line ↔
  wrapped content. **Exception:** inside a table cell, block-level anchors render inline
  within the cell segment (rows are single projected lines; token lines would break the
  `|` grammar).
- **Cell-level control** (`sdtContent > w:tc`): anchors render inline inside that cell's
  segment. **Row-level control** (`sdtContent > w:tr`): open token before the first
  cell's text, close after the last cell's text, on the row's line.
- **Empty (`w:showingPlcHdr` or no visible content):** projects as the adjacent pair
  with the placeholder bubble between anchors when placeholder text exists:
  `{#cc:N}{>>placeholder: Click or tap here to enter text.<<}{#/cc:N}`.
  The ghost text NEVER projects as body text. The bubble is virtual chrome (raw view
  only; dropped in clean view and from search-path breadcrumbs like other bubbles).
  - **Sanctioned edit surface #1:** the empty pair is deliberately *matchable* (the
    `{#cell:paraId}` precedent): a `ModifyText` whose target is `{#cc:N}{#/cc:N}` (or an
    insertion resolved between the anchors) is the text-first fill; it MUST route through
    fill semantics (spec-set-field.md §4), not raw run insertion.
- Placeholder detection: `w:showingPlcHdr` present ⇒ content is ghost. A control with no
  `showingPlcHdr` and no visible text projects as the bare empty pair (no bubble).

## 4. Checkbox controls

- Project as `[x]` (checked per `w14:checked w14:val` ∈ {"1","true"}) or `[ ]` — never
  the raw glyph run (`☒`/`☐`/Wingdings). No anchor pair (corpus reality: 3,800+ per
  document; the ledger carries identity).
- The 3-character token maps onto the 1-character glyph run; the mapper accounts for the
  width difference exactly as style markers are accounted (virtual + real span mix).
- **Sanctioned edit surface #2:** a `ModifyText` swapping exactly `[ ]` ↔ `[x]` (with
  disambiguating context as usual) is the text-first toggle and MUST route through
  checkbox semantics (glyph swap + `w14:checked` update, spec-set-field.md §5.5). Any
  other textual mutation of the token is rejected (A3.8).

## 5. Group controls

- Block boundary tokens with the `group` flag: `{#cc:N group}` / `{#/cc:N}` on their own
  lines around the wrapped blocks (cell-context exception as §3).
- Content inside a group projects normally (including nested anchored leaf controls,
  which keep their own ordinals and editability).

## 6. Clean view (`clean_view=true`)

- Anchor tokens and checkbox tokens persist (structural, like `{#_Bookmark}`).
- Placeholder bubbles are dropped: an empty field contributes `{#cc:N}{#/cc:N}` and no
  text (an unfilled field has no accepted-state content).
- Content controls whose content is entirely inside accepted-away deletions project as
  the empty pair.

## 7. Banner (full view header)

When a document has ≥1 content control OR any `w:documentProtection`, the full-view
header (after the File Path line) gains one line, engine-emitted:

```
> **Protection:** <none|read-only|fill-in-forms only|comments only|tracked-changes only>[ (enforced)] · **Fields:** <N> content controls — <e> empty · <l> locked · <b> bound
```

- `enforced` iff `w:enforcement` ∈ {"1","true"}. Protection with zero controls renders
  `**Fields:** no content controls`. Zero controls and no protection ⇒ no banner (plain
  documents gain zero noise).
- `empty` counts controls in placeholder/empty state; `locked` counts content-locked
  leaves + group containers; `bound` counts `w:dataBinding` carriers.
- Surfaces append their own discovery hint (surface-aware, QA F11 class): MCP servers
  append `· read mode="fields" for the field ledger`, the CLI appends
  `· run adeu extract <file> --mode fields for the field ledger`.

## 8. Token-cost bound

Projection chrome added by this spec (anchor tokens + flags + checkbox tokens minus the
glyphs they replace) MUST stay ≤ 5% of total projection length on the FedRAMP rev4
corpus document (A5.6). If a future document class breaks this, the escape hatch is a
`fields_inline=false` read option (P3, not v1) — never a second syntax.

## 9. Implementation notes (non-normative)

- Emit `sdt_start`/`sdt_end` events from `traverse_node` (inline) and the block iterators
  (block/cell/row levels) carrying: ordinal, class, flags, alias, tag, placeholder text,
  and (dropdown/combobox) options — the ledger and ingest both consume the same events
  (state-machine parity, AI_CONTEXT §10).
- Anchor ordinal assignment must be a single pre-pass shared by ingest and mapper (the
  `get_paragraph_prefix` precedent: shared helper ⇒ contract holds by construction).
- `w:sdtEndPr` and `w:sdtPr/w:rPr` are preserved untouched (surgical mode); projection
  reads, never rewrites.
