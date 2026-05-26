# n8n-nodes-adeu

[![npm version](https://img.shields.io/npm/v/n8n-nodes-adeu.svg)](https://www.npmjs.com/package/n8n-nodes-adeu)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

An [n8n](https://n8n.io) community node for **[Adeu](https://adeu.ai)** — the AI-native Virtual DOM for Microsoft Word.

This node bridges the gap between Large Language Models (LLMs) and Microsoft Word. It translates complex OpenXML (`.docx`) files into token-efficient Markdown, allows AI models to reason over legal or technical text, and translates the AI's JSON output back into **native Word Tracked Changes and Comments** — all completely in-process, without your documents ever leaving the n8n runtime.

---

## 📦 Installation

Depending on your self-hosted n8n configuration, you can install this node via the UI, environment variables, or manually.

### Method 1: GUI Installation (Recommended)
1. In n8n, go to **Settings** > **Community Nodes**.
2. Select **Install**.
3. Enter `n8n-nodes-adeu` in the **Enter npm package name** field.
4. Check **I understand the risks of installing unverified code from a public source**.
5. Select **Install**.

### Method 2: Environment Variables
For automated deployments, you can bootstrap your n8n instance with a fixed set of packages via environment variables:
```bash
export N8N_COMMUNITY_PACKAGES_MANAGED_BY_ENV=true
export N8N_COMMUNITY_PACKAGES='[{"name":"n8n-nodes-adeu"}]'
```
*Note: Enabling this makes the Community Nodes settings UI read-only and will automatically uninstall any packages not listed in the JSON array.*

### Method 3: Manual Installation (Queue Mode)
If your n8n instance runs in queue mode or you prefer terminal installation, you can install the node manually:
```bash
docker exec -it n8n sh
mkdir -p ~/.n8n/nodes
cd ~/.n8n/nodes
npm i n8n-nodes-adeu
```
Restart your n8n instance after installation.

---

## 🚀 Key Capabilities

- **CriticMarkup Projection**: Translates existing Word tracked changes into standard Markdown (`{++inserted++}`, `{--deleted--}`).
- **Semantic Appendix**: Automatically extracts defined terms, cross-references, and potential typos to give LLMs deeper context.
- **Native Redlining**: Apply `modify`, `accept`, `reject`, and `reply` actions directly to the OOXML tree.
- **Document Sanitization**: Strip metadata, auto-accept markup, and apply read-only locks before sending to counterparties.

---

## ⚙️ Operations

The node exposes one resource (**Document**) with four operations:

### 1. Extract Markdown
Projects a `.docx` file into LLM-friendly Markdown.
- **Input**: `.docx` binary.
- **Output**: JSON `{ markdown, fileName, cleanView }`.
- **Clean View toggle**: 
  - `False` (Raw View): Shows all pending tracked changes via CriticMarkup. Best for resolving counterparty edits.
  - `True` (Clean View): Simulates an "Accept All" state, hiding markup. Best for generating net-new redlines on a clean baseline.

### 2. Apply Edits
Applies a JSON array of `DocumentChange` operations back to the Word document as tracked changes and comments.
- **Input**: `.docx` binary + a `changes` JSON array (read from an upstream node or defined inline).
- **Output**: A new redlined `.docx` binary + JSON application stats.
- **Atomic Batch Validation**: Adeu pre-validates the *entire* array of edits before touching the document. If even one edit is invalid (e.g., target text not found, ambiguous match), the engine safely rejects the entire batch to prevent partial or corrupted document states.

### 3. Generate Diff
Produces a sub-word level `@@ Word Patch @@` diff between two versions of a document.
- **Input**: Two `.docx` binaries on the same item (e.g., `data` and `data2`).
- **Output**: JSON `{ diff, originalFileName, modifiedFileName }`.

### 4. Finalize Document
Prepares a document for signature or external distribution.
- **Modes**:
  - `Full`: Strips all metadata and requires all tracked changes/comments to be resolved (or auto-accepted).
  - `Keep Markup`: Strips metadata but preserves visible tracked changes. Allows you to override the `Author` name (e.g., change "Adeu AI" to "My Law Firm").
  - `Baseline`: Only strips background noise (RSIDs, proof errors) without touching metadata.
- **Protection**: Can inject a native Word "Read-Only" lock into the document settings.

---

## 🧠 The `DocumentChange` Schema

To use the **Apply Edits** operation, your LLM must output a JSON array of objects matching this schema.

| Type | Required Fields | Description |
| :--- | :--- | :--- |
| `modify` | `target_text`, `new_text` | Replaces baseline text. Use the `comment` field to attach a comment bubble. |
| `accept` | `target_id` | Accepts an existing tracked change (e.g., `Chg:123`). |
| `reject` | `target_id` | Rejects an existing tracked change. |
| `reply` | `target_id`, `text` | Replies to an existing comment (e.g., `Com:456`). |
| `insert_row` | `target_text`, `position`, `cells` | Inserts a new table row `above` or `below` the target cell text. |
| `delete_row` | `target_text` | Deletes the table row containing the target text. |

**Example LLM Output:**
```json
[
  {
    "type": "reject",
    "target_id": "Chg:12",
    "comment": "We cannot accept 60-day terms."
  },
  {
    "type": "modify",
    "target_text": "within thirty (30) days",
    "new_text": "within forty-five (45) days",
    "comment": "Compromise per our playbook."
  }
]
```

---

## 🏗️ Typical Pipeline

```
[ Gmail Trigger (Incoming Doc) ]
        │
        ▼
[ Adeu: Extract Markdown ] 
        │
        ▼
[ AI Node (LLM) ]          ← Outputs a JSON array of `DocumentChange` objects
        │
        ▼
[ Adeu: Apply Edits ]      ← Pre-validates and writes redlines atomically
        │
        ▼
[ Gmail: Reply with Doc ]
```

---

## 💡 Prompting Best Practices for LLMs

To achieve the highest batch success rate when prompting models like Gemini, GPT-4o, or Claude to generate edits:

1. **Enforce Exact Matching**: Instruct the LLM: *"The `target_text` must be copied EXACTLY from the source document — including identical punctuation, spacing, and capitalization."*
2. **Short but Unique**: Instruct the LLM: *"Keep `target_text` short, but ensure it is unique enough to not match multiple locations in the document."*
3. **No Fake Markup**: Instruct the LLM: *"Do NOT include CriticMarkup tags like `{++` or `{--` in your `new_text`. The engine will apply the redline tracking automatically."*
4. **Mind the Overlap Constraint**: Adeu's engine strictly prevents `modify` (text-replace) edits from overlapping with or targeting text that is *already* inside a pending tracked change. Instruct the LLM: *"You cannot `modify` text that is wrapped in counterparty tracking markup. You must `accept` or `reject` their change using its ID."*

---

## 🤖 AI Agent Tool Setup: `$fromAI` Recipes

When wiring this node into an AI Agent as a tool, n8n auto-generates `$fromAI()` expressions for AI-bindable fields. The **second argument** of `$fromAI` is the only per-parameter schema description the LLM actually receives — but n8n does **not** propagate node-source `description` metadata into that slot. Auto-generated stubs look like:

```
{{ $fromAI('Changes__JSON_', ``, 'string') }}
```

The empty backticks mean the LLM sees no schema for that field and will hallucinate the structure.

**To apply any recipe below:** Open the tool node → click the target field → **disable** "Let the model define this parameter" (it locks the field to n8n's auto-generated empty-description stub) → switch to **Expression** mode → paste the recipe. The `$fromAI()` call inside your expression still binds the field to the LLM — you're just bypassing the auto-stub so you can supply a richer schema description.

> **Stub caching gotcha:** Once a `$fromAI` expression is saved into a workflow, n8n caches it permanently in that workflow's JSON. Updating this package does not retroactively update expressions in existing workflows — you must hand-edit them or delete and re-add the tool node.

### What you do NOT bind to the LLM

Some fields are **plumbing** — they configure which input/output port the node uses, not semantic content. Plumbing fields belong in the node editor; binding them to `$fromAI` lets the LLM produce confusing errors unrelated to the user's actual request.

**Set these manually in the node editor — do NOT use `$fromAI`:**

- **`Document Source`** (`fromInput` vs `fromNode`) — workflow topology decision.
- **`Input Binary Property`** — wiring decision; downstream of the source node, not per-call.
- **`Output Binary Property`** (Apply Edits, Finalize Document) — names where the outgoing binary lands on the workflow item. Downstream nodes need this fixed; an LLM picking `output_data` one call and `result` the next would break the pipeline. Default `'data'` is almost always correct.
- **`Edits Source`** (Apply Edits) — controls whether the node reads the changes array from the `Changes (JSON)` field on the node itself (`defineBelow`) or from a property on the upstream item (`fromInputJson`). For AI Agent workflows, **set this to `Define Below` in the editor**. This is what activates the `Changes (JSON)` field as the LLM's entry point — the recipe in the Apply Edits section below populates that field via `$fromAI`, and the LLM hands its generated `Changes_JSON` string directly to the tool as a call argument. The `fromInputJson` branch is only for deterministic pipelines where an upstream non-AI node has pre-populated a `changes` property on the item.

AI Agents cannot pass binary `.docx` data through JSON arguments anyway — that's why `fromNode` exists: it resolves the binary from a named upstream node (e.g. `Read Binary File`, `Gmail Trigger`) at execution time. The trigger source is `$fromAI`-bindable below because a system prompt can legitimately offer the LLM a choice between multiple binary-producing nodes.
---

### Extract Markdown

**Source Node Name** (when `Document Source` is `From Another Node`):
```
={{ $fromAI('Source_Node_Name', `Exact name of the workflow node that produced the .docx binary (string, case-sensitive, e.g. 'Read Binary File' or 'Gmail Trigger'). Must match the node label in the canvas exactly. If your system prompt specifies which node holds the document, always use that name.`, 'string') }}
```

**Clean View:**
```
={{ $fromAI('Clean_View', `Boolean. Set false (default) to surface all pending tracked changes as CriticMarkup tags {++ins++}, {--del--}, {>>comment<<} — use when reviewing counterparty edits or any document with pending markup. Set true to project the document as if all tracked changes were accepted (simulates Accept All) — use only when generating net-new redlines against a clean baseline.`, 'boolean', false) }}
```

---

### Apply Edits

**Source Node Name** (when `Document Source` is `From Another Node`):
```
={{ $fromAI('Source_Node_Name', `Exact name of the workflow node that produced the .docx binary (string, case-sensitive). Must match the node label in the canvas exactly. If your system prompt specifies which node holds the document, always use that name.`, 'string') }}
```

```

**Author:**
```
={{ $fromAI('Author', `Author name attached to every tracked change and comment produced by this batch (string, e.g. 'AI Reviewer' or 'Acme Legal AI'). Appears in Word's review pane as the author of every redline. Choose a name your end users will recognize as the AI reviewer.`, 'string', 'Adeu AI') }}
```


**Changes (JSON):**
```
={{ $fromAI('Changes_JSON', `JSON-encoded string containing an array of DocumentChange objects. Each object is one of: {"type":"modify","target_text":"<verbatim from source>","new_text":"<replacement>","comment":"<optional>"} | {"type":"accept","target_id":"Chg:12","comment":"<optional>"} | {"type":"reject","target_id":"Chg:12","comment":"<optional>"} | {"type":"reply","target_id":"Com:45","text":"<reply>"} | {"type":"insert_row","target_text":"<cell text anchoring row>","position":"above" or "below","cells":["col1","col2"]} | {"type":"delete_row","target_text":"<cell text anchoring row>"}. RULES: target_text must be copied VERBATIM from the source including punctuation/whitespace/case and must uniquely anchor one location; never include CriticMarkup tags like {++ or {-- in new_text — the engine applies tracking automatically; use Chg:N and Com:N IDs exactly as surfaced by extract_markdown; the entire array must be a single JSON-encoded string. Atomic batch: if any single edit is invalid the whole array is rejected with an error telling you which edit failed — use that to self-correct on the next call.`, 'string') }}
```

**Return Markdown Output:**
```
={{ $fromAI('Return_Markdown', `Boolean. When true (default), the tool returns the post-edit document as Markdown with CriticMarkup so you can verify what changed and reason about follow-up edits. Set false only to skip extraction when you are confident no follow-up review is needed.`, 'boolean', true) }}
```

---

### Generate Diff

**Original Source Node Name** (when `Original Document Source` is `From Another Node`):
```
={{ $fromAI('Original_Source_Node_Name', `Exact name of the workflow node that produced the baseline (before) .docx binary (string, case-sensitive). Must match the node label exactly.`, 'string') }}
```

**Modified Source Node Name** (when `Modified Document Source` is `From Another Node`):
```
={{ $fromAI('Modified_Source_Node_Name', `Exact name of the workflow node that produced the modified (after) .docx binary (string, case-sensitive). Must match the node label exactly. Must reference a different node from the original source — otherwise the diff will be empty.`, 'string') }}
```

**Clean View:**
```
={{ $fromAI('Clean_View', `Boolean. Set true (recommended default) to compare the Accept All clean view of both documents — diffs reflect final content as if all tracked changes were accepted. Set false to diff the raw CriticMarkup-projected text including pending change markers — useful for auditing tracked-change differences themselves.`, 'boolean', true) }}
```

---

### Finalize Document

**Source Node Name** (when `Document Source` is `From Another Node`):
```
={{ $fromAI('Source_Node_Name', `Exact name of the workflow node that produced the .docx binary (string, case-sensitive). Must match the node label exactly.`, 'string') }}
```



**Sanitize Mode:**
```
={{ $fromAI('Sanitize_Mode', `One of 'baseline', 'full', or 'keep-markup'. 'full' (recommended for distribution) strips author metadata, RSIDs, paragraph IDs, and proof errors AND requires all tracked changes to be resolved — pair with Accept_All=true to auto-accept. 'keep-markup' strips metadata but preserves visible tracked changes and comments — use when sending markup for counterparty review; pair with Author_Override to rewrite author names. 'baseline' is minimal cleanup only (RSIDs and proof errors) — leaves tracked changes and metadata intact.`, 'string', 'full') }}
```

**Accept All Tracked Changes** (only meaningful when `Sanitize Mode` is `full`):
```
={{ $fromAI('Accept_All', `Boolean. Only applies when Sanitize_Mode is 'full'. Set true to auto-accept all pending tracked changes before sanitization. Set false (default) to block finalization and raise an error if any pending tracked changes exist, forcing them to be resolved explicitly. If multiple distinct authors are detected in pending changes when true, the report will include a warning about potential silent smuggles.`, 'boolean', false) }}
```

**Author Override** (only meaningful when `Sanitize Mode` is `keep-markup`):
```
={{ $fromAI('Author_Override', `Optional string. Only applies when Sanitize_Mode is 'keep-markup'. When set, replaces the author name on every preserved tracked change and comment with this value (e.g. 'Acme Legal'). Leave empty to keep original authors intact.`, 'string', '') }}
```

**Protection Mode:**
```
={{ $fromAI('Protection_Mode', `One of 'none' or 'read_only'. 'none' (default) leaves the document unlocked. 'read_only' injects a native Word read-only enforcement flag into settings.xml — Word users see a read-only banner and cannot edit without explicitly unlocking. Use 'read_only' for distribution to signers or counterparties when you want to discourage casual edits.`, 'string', 'none') }}
```

---

> **Tip:** The default-value (4th) argument of `$fromAI()` lets the LLM omit the parameter entirely and fall back to a sensible default. Use defaults aggressively on optional fields so the LLM only has to specify what actually varies per call.

---

## 🛠️ Error Handling & Troubleshooting

Because Adeu enforces **Atomic Batch Validation**, any error in the LLM's JSON will throw a `NodeApiError` and halt the node. The error message will tell you exactly which edit failed and why.

* **"Target text not found"**: The LLM hallucinated a word, altered the spacing, or the text doesn't exist in the baseline document.
* **"Ambiguous match"**: The LLM used a `target_text` (like "the Company") that appears multiple times. The error details will show you the exact occurrences. Advise the LLM to include more surrounding context (e.g., "the Company shall indemnify").
* **"Modification targets an active insertion..."**: The LLM tried to `modify` text that another author is currently tracking. Adeu explicitly blocks this to maintain virtual DOM integrity and clean redline threading. You must `accept` or `reject` that prior change first.
* **"Read-only elements"**: The LLM tried to modify structural items like cross-references or footnotes. 

**Tip**: If you are running bulk processing workflows, you can enable n8n's **"Continue On Fail"** setting on the `Apply Edits` node. If the LLM generates a flawed batch, n8n will catch the error, output an `{ "error": "..." }` JSON object for that specific document, and continue processing the rest of the files in your queue.
