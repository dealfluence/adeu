import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import { DocumentObject, finalize_document } from "@adeu/core";

import {
  type BinarySource,
  DOCX_MIME_TYPE,
  buildOutputFileName,
  getDocxBufferFromSource,
} from "../GenericFunctions";

export const finalizeDocumentDescription: INodeProperties[] = [
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
        operation: ["finalizeDocument"],
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
      "Name of the binary property on the outgoing item that will hold the finalized .docx file (string, e.g. 'data'). If equal to the input property name, the original binary is overwritten on the outgoing item.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["finalizeDocument"],
      },
    },
  },
  {
    displayName: "Sanitize Mode",
    name: "sanitizeMode",
    type: "options",
    default: "full",
    description:
      "How aggressively to sanitize the document before distribution. One of: 'baseline' (minimal metadata cleanup only), 'full' (strip metadata AND require all tracked changes resolved or auto-accepted), 'keep-markup' (strip metadata but preserve visible tracked changes and comments, optionally with author rewrite).",
    options: [
      {
        name: "Baseline",
        value: "baseline",
        description:
          "Minimal sanitization: strip RSID attributes, paragraph IDs, and proof errors only. Leaves tracked changes, comments, authors, and other metadata untouched.",
      },
      {
        name: "Full",
        value: "full",
        description:
          "Strip metadata (authors, RSIDs, paragraph IDs, proof errors) and require all tracked changes and comments to be resolved. If pending tracked changes exist and 'Accept All Tracked Changes' is false, the operation is blocked and an error is thrown.",
      },
      {
        name: "Keep Markup",
        value: "keep-markup",
        description:
          "Strip metadata but preserve all visible tracked changes and comments. Use 'Author Override' to rewrite the author name on every preserved redline and comment (e.g. replace 'AI Reviewer' with your firm name).",
      },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["finalizeDocument"],
      },
    },
  },
  {
    displayName: "Accept All Tracked Changes",
    name: "acceptAll",
    type: "boolean",
    default: false,
    description:
      "Boolean. Only applies when Sanitize Mode is 'full'. When true, auto-accepts all pending tracked changes before sanitization. When false (default), the operation throws an error if any pending tracked changes exist, forcing the caller to resolve them first. If multiple distinct authors are detected in pending changes, a warning is included in the report to alert you of potential silent smuggles.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["finalizeDocument"],
        sanitizeMode: ["full"],
      },
    },
  },
  {
    displayName: "Author Override",
    name: "authorOverride",
    type: "string",
    default: "",
    placeholder: "e.g. My Firm",
    description:
      "Only applies when Sanitize Mode is 'keep-markup'. Optional string. When set, replaces the author name on every preserved tracked change and comment with this value (e.g. 'My Law Firm'). Leave empty to keep original authors intact.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["finalizeDocument"],
        sanitizeMode: ["keep-markup"],
      },
    },
  },
  {
    displayName: "Protection Mode",
    name: "protectionMode",
    type: "options",
    default: "none",
    description:
      "Optionally lock the finalized document. 'none' (default) leaves the document unlocked. 'read_only' injects a native Word read-only enforcement flag into settings.xml — Word users see a read-only banner and cannot edit without explicitly unlocking. AES encryption is not supported in this engine.",
    options: [
      { name: "None", value: "none" },
      { name: "Read Only", value: "read_only" },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["finalizeDocument"],
      },
    },
  },
];

export async function executeFinalizeDocument(
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
  const sanitizeMode = this.getNodeParameter("sanitizeMode", itemIndex) as
    | "full"
    | "keep-markup"
    | "baseline";

  let acceptAll = false;
  if (sanitizeMode === "full") {
    acceptAll = this.getNodeParameter("acceptAll", itemIndex) as boolean;
  }

  let author: string | null = null;
  if (sanitizeMode === "keep-markup") {
    const raw = this.getNodeParameter("authorOverride", itemIndex) as string;
    author = raw.trim() === "" ? null : raw;
  }

  const protectionParam = this.getNodeParameter(
    "protectionMode",
    itemIndex,
  ) as string;
  const protectionMode: "read_only" | null =
    protectionParam === "read_only" ? "read_only" : null;

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
  const { reportText, outBuffer } = await finalize_document(doc, {
    filename: fileName,
    sanitize_mode: sanitizeMode,
    accept_all: acceptAll,
    protection_mode: protectionMode,
    author,
  });

  // `finalize_document` returns reportText only (no buffer) when sanitization
  // is blocked. Surface that as an error so the user knows why.
  if (!outBuffer) {
    throw new Error(reportText);
  }

  const outName = buildOutputFileName(fileName, "finalized");
  const binary = await this.helpers.prepareBinaryData(
    outBuffer,
    outName,
    DOCX_MIME_TYPE,
  );

  const incomingBinary = this.getInputData()[itemIndex].binary ?? {};

  return [
    {
      json: {
        fileName: outName,
        sanitizeMode,
        report: reportText,
      },
      binary: {
        ...incomingBinary,
        [outputBinaryPropertyName]: binary,
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
