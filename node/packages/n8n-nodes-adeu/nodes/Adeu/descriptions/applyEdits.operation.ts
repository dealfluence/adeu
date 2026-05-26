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
  getDocxBufferFromSource,
  getNestedProperty,
  parseJsonParameter,
} from "../GenericFunctions";

export const applyEditsDescription: INodeProperties[] = [
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
      "type='modify': requires target_text (string, copied EXACTLY from the source including punctuation, spacing, and case) and new_text (string); optional comment (string). Never include CriticMarkup tags like {++ ++} or {-- --} in new_text — the engine applies tracking automatically. Never target text already inside another author's pending tracked change. " +
      "type='accept': requires target_id (string like 'Chg:12' from the Markdown projection); optional comment. " +
      "type='reject': requires target_id (string like 'Chg:12'); optional comment. " +
      "type='reply': requires target_id (string like 'Com:45') and text (string). " +
      "type='insert_row': requires target_text (string anchoring a table cell), position ('above' or 'below'), and cells (array of strings, one per column). " +
      "type='delete_row': requires target_text (string anchoring the row to delete). " +
      "The whole batch is validated atomically: if any single edit fails (target text not found, ambiguous match, read-only target, overlapping another author's change), the entire batch is rejected and the document is left untouched. " +
      'Example: \'[{"type":"modify","target_text":"within thirty (30) days","new_text":"within forty-five (45) days","comment":"Per playbook."}]\'. ' +
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
      "Boolean. When true (default), auto-extracts the post-edit document as Markdown (with CriticMarkup) and includes it in the outgoing JSON under the 'markdown' field. Useful for feeding the updated state back into a downstream AI Agent for review or further edits. Adds extraction overhead per call.",
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
  );

  const outBuffer = await doc.save();
  const outName = buildOutputFileName(fileName, "redlined");

  const binary = await this.helpers.prepareBinaryData(
    outBuffer,
    outName,
    DOCX_MIME_TYPE,
  );

  // Auto-extract post-edit markdown if requested (using CriticMarkup view as preferred)
  let markdown: string | undefined;
  if (returnMarkdown) {
    markdown = await extractTextFromBuffer(outBuffer, false);
  }

  const incomingBinary = this.getInputData()[itemIndex].binary ?? {};

  return [
    {
      json: {
        fileName: outName,
        author,
        stats,
        ...(markdown !== undefined ? { markdown } : {}),
      },
      binary: {
        ...incomingBinary,
        [outputBinaryPropertyName]: binary,
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
