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
      "type='accept': requires target_id (string like 'Chg:12' from the Markdown projection); optional comment. " +
      "type='reject': requires target_id (string like 'Chg:12'); optional comment. " +
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
      "Whether to auto-extract the post-edit document as Markdown (with CriticMarkup) and include it in the outgoing JSON under the 'markdown' field. Useful for feeding the updated state back into a downstream AI Agent for review or further edits. Adds extraction overhead per call. Has no effect when Dry Run is enabled — dry runs do not produce a post-edit buffer; inspect the per-edit 'critic_markup' / 'clean_text' previews on the returned 'edits' array instead.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["applyEdits"],
      },
    },
  },
  {
    displayName: "Dry Run",
    name: "dryRun",
    type: "boolean",
    default: false,
    description:
      "Whether to preview edits without writing them to the document. When true, the engine validates every edit, simulates the application in-memory, and returns a detailed 'edits' report (per-edit status, CriticMarkup context preview, clean text preview, warnings, and errors) — but the document is NOT modified, no redlined binary is produced, and no static-data stash is written. The incoming binary passes through unchanged on the outgoing item. Use as a self-correction primitive: when an anchor's uniqueness or wording is uncertain, dry-run first, inspect each edit's 'critic_markup' preview, then re-call with Dry Run disabled to commit. Default false (commit edits normally). Important: invalid edits in a dry run do NOT throw a BatchValidationError — they return as failed entries in the 'edits' array. Wet runs still throw atomically on the first validation failure.",
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
  const dryRun = this.getNodeParameter("dryRun", itemIndex, false) as boolean;
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
  const engine = new RedlineEngine(doc, author);
  const stats = engine.process_batch(
    changes as Parameters<RedlineEngine["process_batch"]>[0],
    dryRun,
  );

  const incomingBinary = this.getInputData()[itemIndex].binary ?? {};

  // Dry-run short-circuit: the document was not mutated, so there is no
  // redlined buffer to produce and no static-data stash to write. The
  // incoming binary passes through unchanged so downstream nodes do not
  // break if they expected continuity. The per-edit `critic_markup` and
  // `clean_text` previews on `stats.edits` give the LLM everything it needs
  // to reason about the would-be result without committing.
  if (dryRun) {
    return [
      {
        json: {
          fileName,
          author,
          dryRun: true,
          stats,
          ...(reasoning !== "" ? { reasoning } : {}),
        },
        binary: incomingBinary,
        pairedItem: { item: itemIndex },
      },
    ];
  }

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
        dryRun: false,
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
