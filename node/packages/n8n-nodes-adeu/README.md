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

## 🛠️ Error Handling & Troubleshooting

Because Adeu enforces **Atomic Batch Validation**, any error in the LLM's JSON will throw a `NodeApiError` and halt the node. The error message will tell you exactly which edit failed and why.

* **"Target text not found"**: The LLM hallucinated a word, altered the spacing, or the text doesn't exist in the baseline document.
* **"Ambiguous match"**: The LLM used a `target_text` (like "the Company") that appears multiple times. The error details will show you the exact occurrences. Advise the LLM to include more surrounding context (e.g., "the Company shall indemnify").
* **"Modification targets an active insertion..."**: The LLM tried to `modify` text that another author is currently tracking. Adeu explicitly blocks this to maintain virtual DOM integrity and clean redline threading. You must `accept` or `reject` that prior change first.
* **"Read-only elements"**: The LLM tried to modify structural items like cross-references or footnotes. 

**Tip**: If you are running bulk processing workflows, you can enable n8n's **"Continue On Fail"** setting on the `Apply Edits` node. If the LLM generates a flawed batch, n8n will catch the error, output an `{ "error": "..." }` JSON object for that specific document, and continue processing the rest of the files in your queue.
