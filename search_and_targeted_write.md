# Specification: Search in `read_docx` and Targeted Write in `process_document_batch`

**Status:** Approved Specification  
**Author:** Mikko Korpela & Collaborative AI Partner  
**Date:** June 25, 2026  

---

## 1. Problem Statement

1. **Context Redundancy**: The `read_docx` tool returns either the full document or a full page. On moderate to large documents (8–20KB of text per page), multi-turn agent loops quickly fill the context window with redundant content. This leads to high token consumption and increased latency.
2. **Ambiguity Failures**: The `process_document_batch` tool rejects any `ModifyText` operation if the target string appears more than once. Agents lack a mechanism to indicate intent (e.g., "replace only the first instance" or "replace all instances") and cannot perform robust pattern-based replacements across repetitive clauses.

---

## 2. Specification Overview

To solve these limitations while keeping the architecture robust and deterministic, we implement two primary enhancements:

1. **Flat Search Parameters on `read_docx`**: Add optional, flat parameters to filter extracted text by exact substring or regular expression, returning only matching paragraphs and their immediate context in a paginated, token-efficient view.
2. **`match_mode` and `regex` Parameters on `ModifyText`**: Introduce a configuration block to control how multiple occurrences are resolved and explicitly opt into regular expression substitutions.
3. **Structured Alignment between Discovery & Application**: Align the feedback structures of search matches and batch edit confirmations to provide a high-signal audit trail.

---

## 3. `read_docx` Search API

### 3.1 Flat Tool Parameters
We keep the tool parameters flat to simplify integration and CLI mapping:

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `search_query` | `string` | `None` (Null) | The substring or regex pattern to search for. When provided, filters results to matching paragraphs. |
| `search_regex` | `boolean` | `false` | Set to `true` to interpret `search_query` as a regular expression. |
| `search_case_sensitive` | `boolean` | `true` | Set to `false` to perform case-insensitive matching. |
| `page` | `integer` or `"all"` | `1` (MCP) / `"all"` (CLI) | Paginates search matches in chunks of 10 matches per page. |

#### 3.1.1 CLI vs. MCP Pagination Behavior
* **CLI tool**: `extract` supports `--page all` (or omitting `--page` entirely) to output the entire unpaginated document or all search matches in one go.
* **MCP Server**: Strictly defaults to paginated chunks (`page=1`) to protect client context window limits.

---

### 3.2 The Channel Contract (Response Shape)

Per **Invariant 7**, we ensure functional parity across both channels by returning identical Markdown formatting for both the LLM and the UI widget.

#### 3.2.1 LLM-Facing Content (`content` Channel)
The `content` channel outputs a raw Markdown string:

```markdown
> **Search Results** — Found 12 matches for query `two (\d+) years` in `contract.docx`.
> Showing page 1 of 2 (matches 1-10). To see more matches, call `read_docx` with `search_query='two (\\d+) years'`, `search_regex=true`, and `page=2`.

---

### Match 1 (p3)
**Path:** `2. Confidentiality > 2.1 Obligations`
> ... The Receiving Party shall notify the Disclosing Party promptly upon becoming aware of any unauthorized disclosure. ...
**The Recipient shall maintain confidentiality of all Confidential Information for a period of {--two (2) years--}{++five (5) years++} from the date of disclosure.**
> ... The obligations under this Section 2 shall survive termination of this Agreement. ...

*Occurrences:* This exact phrasing appears 1 time in the document.

---

### Match 2 (p12)
**Path:** `6. Term & Termination`
> ... Either party may terminate this agreement upon thirty (30) days written notice. ...
**This agreement shall remain in effect for a period of two (2) years.**
> ... Upon termination, all outstanding fees shall immediately become due. ...

*Occurrences:* This exact phrasing appears 2 times in the document.
```

If zero matches are found:
```markdown
> **Search Results** — No matches found for query `two (\d+) years` in `contract.docx`.

Verify your search spelling, or try setting `search_case_sensitive` to false or enabling `search_regex` if you used pattern wildcards.
```

#### 3.2.2 Machine-Facing Content (`structured_content` Channel)
To ensure compatibility with our postMessage, resizing, and rendering UI widgets, the `structured_content` channel wraps the identical search Markdown string in the standard JSON structure:

```json
{
  "markdown": "> **Search Results** — Found 12 matches for query `two (\\d+) years` ...",
  "file_path": "/absolute/path/to/contract.docx",
  "title": "Search: contract.docx"
}
```

---

## 4. Outline Page Ranges

To make outline navigation more useful, headings project their calculated page ranges rather than just their start page. The redundant heading style labels (e.g. `Heading 1`) are removed, as the Markdown depth headers (`#`) already represent this hierarchy.

### 4.1 Range Mapping Algorithm
This is calculated on the flat array of generated `OutlineNode` objects post-pagination, removing the need for nested XML traversal:

1. For each `OutlineNode` $N_i$ at index $i$ with hierarchical heading level $L$:
2. Search forward for the next node $N_j$ (where $j > i$) such that $\text{Level}(N_j) \le L$.
3. If $N_j$ is found:
   * If $\text{Page}(N_j) > \text{Page}(N_i)$: The section ends on $\text{Page}(N_j) - 1$.
   * If $\text{Page}(N_j) == \text{Page}(N_i)$: The section ends on the same page ($\text{Page}(N_i)$).
4. If no such node is found (it is the last section of its level):
   * The section ends on `total_pages` of the document body.

### 4.2 Projected Outline Format
```
# 2. Confidentiality (p3–p4)
## 2.1 Obligations of Confidentiality (p3)
## 2.2 Survival (p4)
# 3. Miscellaneous (p5)
```

---

## 5. `process_document_batch` Targeted Writes

We extend the `ModifyText` model to include configuration parameters that explicitly direct search-and-replace matches.

### 5.1 Updated `ModifyText` Schema

```typescript
export interface ModifyText {
  type: 'modify';
  target_text: string;
  new_text: string;
  comment?: string | null;
  
  // Resolution Strategy
  match_mode?: 'strict' | 'first' | 'all'; // Default: 'strict'
  
  // Explicit Regex Flag
  regex?: boolean; // Default: false
}
```

### 5.2 Strategy Definitions

* **`"strict"` (Default)**: Raises a `BatchValidationError` if `target_text` matches more than once in the flat `full_text` view of the document. This ensures safety.
* **`"first"`**: Resolves to the first occurrence in the linear document flow, applying the replacement and ignoring subsequent duplicates.
* **`"all"`**: Applies the replacement to every occurrence.

---

### 5.3 Technical Constraints & Safety Rules

1. **Deterministic Linear Order**: The linear order of occurrences is defined entirely by their sequence of appearance in the flattened, projected CriticMarkup representation (`full_text`). This abstracts away the physical XML storage layout and maps directly to what the LLM observes.
2. **Transactional Integrity**: If `match_mode` is set to `"all"`, and even a single match falls into a safety-blocked zone (such as a nested revision by a different author, violating **Invariant 11**), the entire batch must fail validation and roll back.
3. **Double-Sided Paragraph Merges**: If `regex` is used, and a pattern match spans across a structural paragraph boundary (`\n\n`), the engine will strictly reject the edit with a `BatchValidationError` to safely prevent structural document corruption.

---

### 5.4 Alignment and Traversal Parity with `read_docx` Search

To provide a high-signal audit trail, the batch execution results returned from both Python and Node.js engines will mirror the structural layout of the `read_docx` search tool. This ensures the agent is able to correlate its write operations with its earlier observations.

#### 5.4.1 Shared Edit Report Schema
Every applied change inside the returned batch execution statistics dictionary (`stats["edits"]`) is enriched to capture where and how the changes were resolved:

```typescript
export interface EditReport {
  status: "applied" | "failed";
  target_text: string;
  new_text: string;
  warning?: string | null;
  error?: string | null;
  critic_markup?: string | null;
  clean_text?: string | null;
  
  // New Alignment Fields
  pages: number[];              // e.g. [3] for first mode, [3, 12] for all mode
  heading_path?: string;        // breadcrumb heading path of the primary modification
  occurrences_modified: number; // total count of occurrences modified
}
```

#### 5.4.2 Visual Text Report Alignment
The formatted report string returned from `process_document_batch` adopts identical heading markers, page notations, and breadcrumb structures as `read_docx` search matches:

```markdown
### Edit 1 ✅ [applied] (p3)
**Path:** `2. Confidentiality > 2.1 Obligations`
**Mode:** `first` (1 occurrence modified; 2 duplicate occurrences skipped)
*Preview (CriticMarkup):*
> ... notify the Disclosing Party promptly upon becoming aware **The Recipient shall maintain confidentiality for a period of {--two (2) years--}{++five (5) years++}.** The obligations under this Section 2 ...
*Preview (Clean):*
> ... notify the Disclosing Party promptly upon becoming aware **The Recipient shall maintain confidentiality for a period of five (5) years.** The obligations under this Section 2 ...
```

When `match_mode` is `"all"`, the report consolidates all affected pages in order:
```markdown
### Edit 2 ✅ [applied] (p3, p12)
**Path:** `6. Term & Termination`
**Mode:** `all` (2 occurrences modified)
```

---

## 6. Regex Engine Specifications & Parity

To prevent parity failures across different runtime environments without adding translation complexity, we establish explicit platform boundaries and declare them in the tool schemas:

1. **Python Runtime (`re` engine)**:
   * Evaluates standard Python regex patterns.
   * Supports `\1` and `\g<1>` capture group backreferences in `new_text`.
2. **TypeScript/Node.js Runtime (`RegExp` engine)**:
   * Evaluates standard ECMAScript regex patterns (targeting ES2022 to support lookbehinds safely).
   * Supports `$1`, `$2` capture group backreferences in `new_text`.

The tool descriptions on both servers will instruct the LLM of the active environment's engine behavior so it generates matching patterns and backreferences naturally.
