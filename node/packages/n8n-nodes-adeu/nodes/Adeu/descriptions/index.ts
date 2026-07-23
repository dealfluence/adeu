// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/index.ts
import type { INodeProperties } from "n8n-workflow";

import { extractMarkdownDescription } from "./extractMarkdown.operation";
import { extractOutlineDescription } from "./extractOutline.operation";
import { applyEditsDescription } from "./applyEdits.operation";
import { generateDiffDescription } from "./generateDiff.operation";
import { finalizeDocumentDescription } from "./finalizeDocument.operation";
import { hydrateToolOutputDescription } from "./hydrateToolOutput.operation";

export const documentDescription: INodeProperties[] = [
  {
    displayName: "Operation",
    name: "operation",
    type: "options",
    noDataExpression: true,
    displayOptions: {
      show: {
        resource: ["document"],
      },
    },
    options: [
      {
        name: "Apply Edits",
        value: "applyEdits",
        action: "Apply tracked changes to document",
        description:
          "Apply a JSON array of DocumentChange objects (modify, accept, reject, reply, insert_row, delete_row) as native Word tracked changes and comments. modify edits support match_mode ('strict'|'first'|'all') and regex (boolean) for targeted multi-occurrence writes. The whole batch is pre-validated atomically: if any single edit is invalid, the entire batch is rejected and the document is left untouched. Supports a Dry Run mode for previewing edits — when enabled, the engine returns a per-edit report (status, CriticMarkup preview, errors, warnings) without modifying the document or producing a redlined binary.",
      },
      {
        name: "Extract Markdown",
        value: "extractMarkdown",
        action: "Extract markdown from document",
        description:
          "Project the .docx into LLM-friendly Markdown with CriticMarkup ({++ins++}, {--del--}, {>>comment<<}) plus a Semantic Appendix listing defined terms, cross-references, and potential typos. Each tracked change is tagged with a stable ID (Chg:N, Com:N) for use in Apply Edits. Supports an optional Page parameter to fetch only one page of a large document (paired with Extract Outline for navigation).",
      },
      {
        name: "Extract Outline",
        value: "extractOutline",
        action: "Extract a structural outline of the document",
        description:
          "Return a token-cheap structural map of the .docx: a JSON array of headings with their level (1-6), text, page number, paragraph style, whether the section directly contains a table, and any footnote/endnote IDs scoped to that section. Also returns total_pages. Use this as a navigation primitive for large documents — call it first to discover structure, then call Extract Markdown with a specific Page to drill into the relevant section.",
      },
      {
        name: "Finalize",
        value: "finalizeDocument",
        action: "Sanitize metadata and lock the document for distribution",
        description:
          "Strip author names, internal IDs, RSIDs, and proof errors; optionally auto-accept all pending tracked changes; optionally lock the document read-only. Use before sending to counterparties or signers.",
      },
      {
        name: "Generate Diff",
        value: "generateDiff",
        action: "Generate diff between documents",
        description:
          "Produce a sub-word level @@ Word Patch @@ diff between two .docx files. Reads the document text via the same Markdown projection used by Extract Markdown, so the diff respects CriticMarkup and Clean View settings.",
      },
      {
        name: "Hydrate Tool Output",
        value: "hydrateToolOutput",
        action: "Hydrate a redlined binary stashed by Apply Edits as a tool",
        description:
          "Read a binary's storage id from workflow static data (where Apply Edits stashes it when run as an AI Agent tool) and re-attach the binary to a main-flow item. " +
          "Necessary because n8n's AI Agent tool wrapper strips binaries from tool outputs, so the redlined .docx produced by an apply_edits tool call cannot reach downstream nodes directly. " +
          "Place this node downstream of the AI Agent on the main connection, then route its output into Write Binary File, Gmail (attachment), Slack (file upload), etc.",
      },
    ],
    default: "extractMarkdown",
  },
  {
    displayName: "Document Source",
    name: "documentSource",
    type: "options",
    default: "fromInput",
    description:
      "Where to read the .docx file from. 'From Connected Input' reads the binary on the current input item — use this in deterministic pipelines (e.g. Gmail Trigger → Adeu → Gmail Reply). 'From Another Node' fetches the binary from a named upstream node by name — use this when the Adeu node is called as an AI Agent tool, since the Agent cannot pass binary data through JSON arguments.",
    options: [
      {
        name: "From Connected Input",
        value: "fromInput",
        description:
          "Read the binary from the current item's binary attachment. Default behavior for deterministic workflows.",
      },
      {
        name: "From Another Node",
        value: "fromNode",
        description:
          "Read the binary from a named upstream node. Required when this node is used as an AI Agent tool.",
      },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: [
          "applyEdits",
          "extractMarkdown",
          "extractOutline",
          "finalizeDocument",
        ],
      },
    },
  },
  {
    displayName: "Source Node Name",
    name: "sourceNodeName",
    type: "string",
    default: "",
    required: true,
    placeholder: "e.g. Gmail Trigger",
    description:
      "Exact name of the node in this workflow whose output binary holds the .docx file (string, case-sensitive). Must match the node's label in the canvas exactly, including spaces and punctuation. The referenced node must have already executed in this run and produced binary output on the property named in 'Input Binary Property'.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: [
          "applyEdits",
          "extractMarkdown",
          "extractOutline",
          "finalizeDocument",
        ],
        documentSource: ["fromNode"],
      },
    },
  },
  {
    displayName: "Source Binary ID",
    name: "sourceBinaryId",
    type: "string",
    default: "",
    placeholder: "e.g. filesystem-v2:...",
    description:
      "Optional. When set (such as by an AI Agent during a multi-turn conversation), the node will load the document directly from this binary storage ID instead of reading from the source node. Leave empty on the first call to default to the baseline source node.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: [
          "applyEdits",
          "extractMarkdown",
          "extractOutline",
          "finalizeDocument",
        ],
        documentSource: ["fromNode"],
      },
    },
  },
  ...extractMarkdownDescription,
  ...extractOutlineDescription,
  ...applyEditsDescription,
  ...generateDiffDescription,
  ...finalizeDocumentDescription,
  ...hydrateToolOutputDescription,
];
