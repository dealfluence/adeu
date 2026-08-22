// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/applyEdits.operation.ts

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import {
  DocumentObject,
  RedlineEngine,
  extractTextFromBuffer,
} from "@adeu/core";

import {
  type BinarySource,
  DOCX_MIME_TYPE,
  N8N_ID_DISCOVERY_HINT,
  buildOutputFileName,
  coerceChangesArray,
  getDocxBufferFromSource,
  getNestedProperty,
  parseJsonParameter,
} from "../GenericFunctions";

export const applyEditsDescription: INodeProperties[] = [
  {
    displayName: "Reasoning",
    name: "reasoning",
    type: "string",
    default: "",
    typeOptions: {
      rows: 2,
    },
    description:
      "Why these edits are being made. State your reasoning BEFORE the changes — what you intend to change and why — then produce the Changes (JSON) array. This field is captured for auditability and is NOT forwarded into the redline engine; its only purpose is to make the AI reason first, which improves edit quality. Safe to leave empty in deterministic (non-AI) pipelines.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Input Binary Property",
    name: "binaryPropertyName",
    type: "string",
    default: "data",
    required: true,
    placeholder: "e.g. data",
    description:
      "Name of the binary property holding the .docx file (string, e.g. 'data'). In 'From Connected Input' mode this reads from the current item; in 'From Another Node' mode this specifies which property on the source node's output to read. The file must be a valid .docx.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Output Binary Property",
    name: "outputBinaryPropertyName",
    type: "string",
    default: "data",
    required: true,
    placeholder: "e.g. data",
    description:
      "Name of the binary property on the outgoing item that will hold the redlined .docx file (string, e.g. 'data'). If equal to the input property name, the original binary is overwritten on the outgoing item.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Author",
    name: "author",
    type: "string",
    default: "Adeu AI",
    placeholder: "e.g. AI Reviewer",
    description:
      "Author name attached to all tracked changes and comments produced by this operation (string, e.g. 'AI Reviewer'). Shows up in Word's review pane as the author of every redline and comment created in this batch.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Edits Source",
    name: "editsSource",
    type: "options",
    noDataExpression: true,
    default: "fromInputJson",
    description:
      "Where to read the list of DocumentChange objects from. Use 'Define Below' to read the array from the Changes (JSON) field on this node — this is the typical AI Agent path, since the LLM generates the array as a tool call argument that lands on that field via $fromAI(). Use 'From Input JSON' to read the array from a property on the upstream item's JSON, for deterministic pipelines where a non-AI node (HTTP Request, Code, etc.) has pre-populated it.",
    options: [
      {
        name: "Define Below",
        value: "defineBelow",
        description:
          "Read the array from the Changes (JSON) field on this node. Use for AI Agent workflows — the LLM populates that field via $fromAI() as a tool call argument.",
      },
      {
        name: "From Input JSON",
        value: "fromInputJson",
        description:
          "Read the array from a property on the upstream item's JSON. Use for deterministic pipelines where a non-AI node has pre-populated the changes array.",
      },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "JSON Path on Input Item",
    name: "editsJsonPath",
    type: "string",
    default: "changes",
    required: true,
    placeholder: "e.g. data.changes",
    description:
      "Property path on the input item JSON whose value is the array of DocumentChange objects (string, dot-notation supported, e.g. 'changes' or 'data.changes'). Must resolve to an array; throws an error otherwise.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
        editsSource: ["fromInputJson"],
      },
    },
  },
  {
    displayName: "Changes (JSON)",
    name: "editsJson",
    type: "string",
    default:
      '[\n  {\n    "type": "modify",\n    "target_text": "State of New York",\n    "new_text": "State of Delaware",\n    "comment": "Standardizing governing law."\n  }\n]',
    required: true,
    description:
      "JSON-encoded string containing an array of DocumentChange objects. Each object has a 'type' field discriminator and type-specific fields. " +
      "type='modify': requires target_text (string, copied EXACTLY from the source including punctuation, spacing, and case) and new_text (string); optional comment (string). " +
      "Optional match_mode (one of 'strict' | 'first' | 'all', default 'strict'): 'strict' fails on ambiguous matches; 'first' silently anchors to the first occurrence; 'all' applies the same replacement to every occurrence in linear document order. " +
      "Optional regex (boolean, default false): when true, target_text is interpreted as an ES2022 RegExp pattern and new_text may reference capture groups via $1, $2, etc. Combine with match_mode='all' for global regex replacements. " +
      "Never include CriticMarkup tags like {++ ++} or {-- --} in new_text — the engine applies tracking automatically. Never target text already inside another author's pending tracked change. " +
      "type='accept': requires target_id (string like 'Chg:12' from the Markdown projection); optional part (string like 'word/header1.xml' — revision ids are numbered per package part, so pass it only when the same id exists in several parts and the batch reports the ambiguity); optional comment. " +
      "type='reject': requires target_id (string like 'Chg:12'); optional part as for accept; optional comment. " +
      "type='reply': requires target_id (string like 'Com:45') and text (string). " +
      "type='insert_row': requires target_text (string anchoring a table cell), position ('above' or 'below'), and cells (array of strings, one per column). " +
      "type='delete_row': requires target_text (string anchoring the row to delete). " +
      "The whole batch is validated atomically: if any single edit fails (target text not found, ambiguous match under match_mode='strict', read-only target, overlapping another author's change), the entire batch is rejected and the document is left untouched. " +
      'Example: \'[{"type":"modify","target_text":"within thirty (30) days","new_text":"within forty-five (45) days","comment":"Per playbook.","match_mode":"all"}]\'. ' +
      "Markdown code fences (```json ... ```) wrapping the value are stripped automatically.",
    typeOptions: {
      rows: 10,
    },
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
        editsSource: ["defineBelow"],
      },
    },
  },
  {
    displayName: "Return Markdown Output",
    name: "returnMarkdown",
    type: "boolean",
    default: true,
    description:
      "Whether to auto-extract the post-edit document as Markdown (with CriticMarkup) and include it in the outgoing JSON under the 'markdown' field. Useful for feeding the updated state back into a downstream AI Agent for review or further edits. Adds extraction overhead per call.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Allow Partial Application",
    name: "allowPartial",
    type: "boolean",
    default: false,
    description:
      "Whether to keep the changes that validate and report the ones that fail, instead of rejecting the whole batch. " +
      "False (default) is transactional: if any single change is invalid, nothing is applied and the document is returned untouched. " +
      "True is salvage mode: valid changes are applied and saved, 'status' on the output JSON reads 'partial', and 'stats.failed' lists each failure with its 0-based index in the submitted array. " +
      "Salvage mode is for AI Agent loops that will fix and resubmit the rejected changes; do not use it where a half-applied redline would be shipped as final.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  // CC-4 write-gate overrides (spec-gates.md §1). Three separate toggles
  // rather than one "force": they license different things, and a single
  // switch would make a user who wanted one silently accept all three.
  {
    displayName: "Ignore Content Control Locks",
    name: "ignoreControlLocks",
    type: "boolean",
    default: false,
    description:
      "Whether to apply edits even inside content-locked or grouped content controls. " +
      "Word itself refuses these edits, so by default Adeu rejects them rather than reporting a success " +
      "the document will not contain. Enable only when the document owner has accepted that the lock is wrong.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Ignore Document Protection",
    name: "ignoreDocumentProtection",
    type: "boolean",
    default: false,
    description:
      "Whether to apply changes even when the document carries enforced editing protection " +
      "(read-only, fill-in-forms, comments-only or tracked-changes-only). " +
      "The restriction is honoured as the author's stated intent, not cracked; this bypasses it deliberately.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Allow Untracked Writes",
    name: "allowUntrackedWrites",
    type: "boolean",
    default: false,
    description:
      "Whether to permit writes that Word records WITHOUT tracked changes. Applies only to " +
      "fill-in-forms-protected documents, where Word does not record revisions at all and Adeu cannot " +
      "honour its always-tracked guarantee. Separate from Ignore Document Protection because it concedes " +
      "Adeu's own output guarantee rather than bypassing the author's restriction; every such write is flagged.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
];
export async function executeApplyEdits(
  this: IExecuteFunctions,
  itemIndex: number,
): Promise<INodeExecutionData[]> {
  const inputBinaryPropertyName = this.getNodeParameter(
    "binaryPropertyName",
    itemIndex,
  ) as string;
  const outputBinaryPropertyName = this.getNodeParameter(
    "outputBinaryPropertyName",
    itemIndex,
  ) as string;
  const author = this.getNodeParameter("author", itemIndex) as string;
  const editsSource = this.getNodeParameter("editsSource", itemIndex) as string;
  const returnMarkdown = this.getNodeParameter(
    "returnMarkdown",
    itemIndex,
  ) as boolean;
  const allowPartial = this.getNodeParameter(
    "allowPartial",
    itemIndex,
    false,
  ) as boolean;
  const ignoreControlLocks = this.getNodeParameter(
    "ignoreControlLocks",
    itemIndex,
    false,
  ) as boolean;
  const ignoreDocumentProtection = this.getNodeParameter(
    "ignoreDocumentProtection",
    itemIndex,
    false,
  ) as boolean;
  const allowUntrackedWrites = this.getNodeParameter(
    "allowUntrackedWrites",
    itemIndex,
    false,
  ) as boolean;
  const reasoning = this.getNodeParameter("reasoning", itemIndex, "") as string;

  // Resolve the changes array
  let changes: unknown;
  if (editsSource === "fromInputJson") {
    const jsonPath = this.getNodeParameter(
      "editsJsonPath",
      itemIndex,
    ) as string;
    const inputJson = this.getInputData()[itemIndex].json;
    changes = getNestedProperty(inputJson as Record<string, unknown>, jsonPath);
    if (changes === undefined) {
      throw new Error(
        `No property "${jsonPath}" found on the input item JSON. Verify the upstream node produced it, or switch "Edits Source" to "Define Below".`,
      );
    }
  } else {
    const raw = this.getNodeParameter("editsJson", itemIndex);
    changes = parseJsonParameter.call(this, raw, itemIndex, "Changes (JSON)");
  }

  if (!Array.isArray(changes)) {
    throw new Error("Changes must be an array of DocumentChange objects.");
  }

  changes = coerceChangesArray(changes);

  const documentSource = this.getNodeParameter(
    "documentSource",
    itemIndex,
    "fromInput",
  ) as "fromInput" | "fromNode";

  const source: BinarySource =
    documentSource === "fromNode"
      ? {
          mode: "fromNode",
          sourceNodeName: this.getNodeParameter(
            "sourceNodeName",
            itemIndex,
            "",
          ) as string,
          binaryPropertyName: inputBinaryPropertyName,
          sourceBinaryId: this.getNodeParameter(
            "sourceBinaryId",
            itemIndex,
            "",
          ) as string,
        }
      : { mode: "fromInput", binaryPropertyName: inputBinaryPropertyName };

  const { buffer, fileName } = await getDocxBufferFromSource.call(
    this,
    itemIndex,
    source,
  );

  const doc = await DocumentObject.load(buffer);
  const engine = new RedlineEngine(doc, author, {
    id_discovery_hint: N8N_ID_DISCOVERY_HINT,
    ignore_control_locks: ignoreControlLocks,
    ignore_document_protection: ignoreDocumentProtection,
    allow_untracked_writes: allowUntrackedWrites,
  });
  const stats = engine.process_batch(
    changes as Parameters<RedlineEngine["process_batch"]>[0],
    undefined,
    allowPartial,
  );

  const incomingBinary = this.getInputData()[itemIndex].binary ?? {};

  const outBuffer = await doc.save();
  const outName = buildOutputFileName(fileName, "redlined");

  const binary = await this.helpers.prepareBinaryData(
    outBuffer,
    outName,
    DOCX_MIME_TYPE,
  );

  // AI Agent tool wrapper strips `binary` from the return value before
  // anything downstream can see it, so when running as a tool we stash the
  // binary's storage id in workflow static data. A downstream Code node can
  // call `getBinaryStream(id)` to reconstruct the buffer and re-attach it as
  // binary on a main-flow item. Static data is a JSON object that the tool
  // wrapper has no reason to touch, so it survives the round-trip.
  //
  // `isToolExecution()` was added relatively recently — older n8n versions
  // may not have it. Guard with a typeof check so the node degrades cleanly
  // (regular-node behavior, no stash) instead of throwing.
  const isToolExec =
    typeof this.isToolExecution === "function" && this.isToolExecution();

  let redlinedBinaryId: string | undefined;
  if (isToolExec && binary.id) {
    const staticData = this.getWorkflowStaticData("global");
    staticData.adeu_last_redlined = {
      id: binary.id,
      fileName: outName,
      mimeType: DOCX_MIME_TYPE,
      timestamp: Date.now(),
    };
    redlinedBinaryId = binary.id;
  }

  // Auto-extract post-edit markdown if requested (using CriticMarkup view as preferred)
  let markdown: string | undefined;
  if (returnMarkdown) {
    markdown = await extractTextFromBuffer(outBuffer, false);
  }

  return [
    {
      json: {
        fileName: outName,
        author,
        status: (stats as { status?: string }).status ?? "ok",
        stats,
        ...(reasoning !== "" ? { reasoning } : {}),
        ...(markdown !== undefined ? { markdown } : {}),
        ...(redlinedBinaryId !== undefined ? { redlinedBinaryId } : {}),
      },
      binary: {
        ...incomingBinary,
        [outputBinaryPropertyName]: binary,
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
