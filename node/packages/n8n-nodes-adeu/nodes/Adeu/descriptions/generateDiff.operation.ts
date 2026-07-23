// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/generateDiff.operation.ts

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import {
  DocumentObject,
  _extractTextFromDoc,
  type ExtractStructure,
  extractTextFromBuffer,
  create_word_patch_diff,
  create_unified_diff,
  generate_structured_edits,
  collect_media_difference_warnings,
} from "@adeu/core";

import {
  type BinarySource,
  getDocxBufferFromSource,
} from "../GenericFunctions";

export const generateDiffDescription: INodeProperties[] = [
  // --- Original document source ---
  {
    displayName: "Original Document Source",
    name: "originalDocumentSource",
    type: "options",
    default: "fromInput",
    description:
      "Where to read the original (baseline) .docx file from. 'From Connected Input' reads from the current item; 'From Another Node' reads from a named upstream node — required when this node is called as an AI Agent tool.",
    options: [
      { name: "From Connected Input", value: "fromInput" },
      { name: "From Another Node", value: "fromNode" },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
      },
    },
  },
  {
    displayName: "Original Source Node Name",
    name: "originalSourceNodeName",
    type: "string",
    default: "",
    required: true,
    placeholder: "e.g. Download Original",
    description:
      "Exact name of the node whose output binary holds the original (baseline) .docx (string, case-sensitive). Must match the node label in the canvas exactly.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
        originalDocumentSource: ["fromNode"],
      },
    },
  },
  {
    displayName: "Original Binary Property",
    name: "originalBinaryPropertyName",
    type: "string",
    default: "data",
    required: true,
    placeholder: "e.g. data",
    description:
      "Name of the binary property holding the original (baseline) .docx file (string, e.g. 'data'). In 'From Connected Input' mode this reads from the current item; in 'From Another Node' mode this specifies which property on the source node's output to read.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
      },
    },
  },
  // --- Modified document source ---
  {
    displayName: "Modified Document Source",
    name: "modifiedDocumentSource",
    type: "options",
    default: "fromInput",
    description:
      "Where to read the modified (compared-to) .docx file from. 'From Connected Input' reads from the current item; 'From Another Node' reads from a named upstream node — required when this node is called as an AI Agent tool.",
    options: [
      { name: "From Connected Input", value: "fromInput" },
      { name: "From Another Node", value: "fromNode" },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
      },
    },
  },
  {
    displayName: "Modified Source Node Name",
    name: "modifiedSourceNodeName",
    type: "string",
    default: "",
    required: true,
    placeholder: "e.g. Apply Edits",
    description:
      "Exact name of the node whose output binary holds the modified (compared-to) .docx (string, case-sensitive). Must match the node label in the canvas exactly.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
        modifiedDocumentSource: ["fromNode"],
      },
    },
  },
  {
    displayName: "Modified Binary Property",
    name: "modifiedBinaryPropertyName",
    type: "string",
    default: "data2",
    required: true,
    placeholder: "e.g. data2",
    description:
      "Name of the binary property holding the modified (compared-to) .docx file (string, e.g. 'data2'). In 'From Connected Input' mode this reads from the current item and must be different from the original property; in 'From Another Node' mode this specifies which property on the source node's output to read.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
      },
    },
  },
  {
    displayName: "Clean View",
    name: "cleanView",
    type: "boolean",
    default: true,
    description:
      "Whether to compare the Accept All clean view of both documents. When true (default, recommended), diffs reflect the final content as if all tracked changes were accepted. When false, diffs the raw CriticMarkup-projected text including pending change markers — useful for auditing tracked-change differences themselves.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
      },
    },
  },
  {
    displayName: "Diff Format",
    name: "diffFormat",
    type: "options",
    default: "wordPatch",
    description:
      "Format of the output diff. 'Word Patch' (default) produces an Adeu @@ Word Patch @@ text string showing sub-word modifications. 'Unified' produces a standard Git-style unified diff string. 'Structured Changes' produces a JSON array of DocumentChange objects suitable for feeding directly into Apply Edits.",
    options: [
      {
        name: "Word Patch",
        value: "wordPatch",
        description: "Adeu @@ Word Patch @@ sub-word text diff format",
      },
      {
        name: "Unified Diff",
        value: "unified",
        description: "Standard Git-style unified text diff format",
      },
      {
        name: "Structured Changes (JSON)",
        value: "structuredChanges",
        description:
          "JSON array of DocumentChange objects that transform original into modified",
      },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["generateDiff"],
      },
    },
  },
];

export async function executeGenerateDiff(
  this: IExecuteFunctions,
  itemIndex: number,
): Promise<INodeExecutionData[]> {
  const originalBinaryPropertyName = this.getNodeParameter(
    "originalBinaryPropertyName",
    itemIndex,
  ) as string;
  const modifiedBinaryPropertyName = this.getNodeParameter(
    "modifiedBinaryPropertyName",
    itemIndex,
  ) as string;
  const cleanView = this.getNodeParameter("cleanView", itemIndex) as boolean;
  const diffFormat = this.getNodeParameter(
    "diffFormat",
    itemIndex,
    "wordPatch",
  ) as "wordPatch" | "unified" | "structuredChanges";

  const originalDocumentSource = this.getNodeParameter(
    "originalDocumentSource",
    itemIndex,
    "fromInput",
  ) as "fromInput" | "fromNode";
  const modifiedDocumentSource = this.getNodeParameter(
    "modifiedDocumentSource",
    itemIndex,
    "fromInput",
  ) as "fromInput" | "fromNode";

  const originalSource: BinarySource =
    originalDocumentSource === "fromNode"
      ? {
          mode: "fromNode",
          sourceNodeName: this.getNodeParameter(
            "originalSourceNodeName",
            itemIndex,
            "",
          ) as string,
          binaryPropertyName: originalBinaryPropertyName,
        }
      : { mode: "fromInput", binaryPropertyName: originalBinaryPropertyName };

  const modifiedSource: BinarySource =
    modifiedDocumentSource === "fromNode"
      ? {
          mode: "fromNode",
          sourceNodeName: this.getNodeParameter(
            "modifiedSourceNodeName",
            itemIndex,
            "",
          ) as string,
          binaryPropertyName: modifiedBinaryPropertyName,
        }
      : { mode: "fromInput", binaryPropertyName: modifiedBinaryPropertyName };

  const { buffer: originalBuffer, fileName: originalName } =
    await getDocxBufferFromSource.call(this, itemIndex, originalSource);
  const { buffer: modifiedBuffer, fileName: modifiedName } =
    await getDocxBufferFromSource.call(this, itemIndex, modifiedSource);

  const mediaWarnings = collect_media_difference_warnings(
    new Uint8Array(originalBuffer),
    new Uint8Array(modifiedBuffer),
  );

  if (diffFormat === "structuredChanges") {
    const docOrig = await DocumentObject.load(originalBuffer);
    const docMod = await DocumentObject.load(modifiedBuffer);

    const projOrig = _extractTextFromDoc(
      docOrig,
      cleanView,
      false,
      false,
      true,
    ) as {
      text: string;
      structure: ExtractStructure;
    };
    const projMod = _extractTextFromDoc(
      docMod,
      cleanView,
      false,
      false,
      true,
    ) as {
      text: string;
      structure: ExtractStructure;
    };

    const { edits, warnings: structWarnings } = generate_structured_edits(
      projOrig.text,
      projOrig.structure,
      projMod.text,
      projMod.structure,
    );

    const allWarnings = [...mediaWarnings, ...structWarnings];

    return [
      {
        json: {
          originalFileName: originalName,
          modifiedFileName: modifiedName,
          cleanView,
          diffFormat,
          changes: edits,
          ...(allWarnings.length > 0 ? { warnings: allWarnings } : {}),
        },
        pairedItem: { item: itemIndex },
      },
    ];
  }

  // includeAppendix=false: the generated appendix is not document content —
  // diffing it produces phantom changes no apply can consume (QA 2026-07-18 H1).
  const originalText = await extractTextFromBuffer(
    originalBuffer,
    cleanView,
    false,
  );
  const modifiedText = await extractTextFromBuffer(
    modifiedBuffer,
    cleanView,
    false,
  );

  let diff =
    diffFormat === "unified"
      ? create_unified_diff(originalText, modifiedText)
      : create_word_patch_diff(
          originalText,
          modifiedText,
          originalName,
          modifiedName,
        );

  // A text diff cannot see image bytes: when embedded media differ, an empty
  // diff must never read as "the documents are identical" (QA 2026-07-19 F-04).
  if (mediaWarnings.length > 0) {
    const warningText = mediaWarnings.map((w) => `⚠️  ${w}`).join("\n") + "\n\n";
    diff = warningText + diff;
  }

  return [
    {
      json: {
        originalFileName: originalName,
        modifiedFileName: modifiedName,
        cleanView,
        diffFormat,
        diff,
        ...(mediaWarnings.length > 0 ? { warnings: mediaWarnings } : {}),
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
