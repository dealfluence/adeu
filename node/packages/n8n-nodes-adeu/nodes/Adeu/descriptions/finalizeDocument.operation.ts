import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import { DocumentObject, finalize_document } from "@adeu/core";

import {
  DOCX_MIME_TYPE,
  buildOutputFileName,
  getDocxBuffer,
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
      "Name of the binary property on the incoming item that holds the .docx file",
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
      "Name of the binary property on the outgoing item that will hold the finalized .docx file",
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
    options: [
      {
        name: "Baseline",
        value: "baseline",
        description:
          "Minimal sanitization (strip rsid, paraIds, proof errors only)",
      },
      {
        name: "Full",
        value: "full",
        description:
          "Strip metadata and require all tracked changes/comments to be resolved or auto-accepted",
      },
      {
        name: "Keep Markup",
        value: "keep-markup",
        description:
          "Strip metadata but preserve visible tracked changes and comments",
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
      "Whether to auto-accept all pending tracked changes during Full sanitization. If false and tracked changes exist, the operation is blocked.",
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
      "When using Keep Markup, replace all visible authors on tracked changes and comments with this name. Leave empty to keep original authors.",
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
      "Optionally lock the finalized document. Encryption (AES) is not supported in this engine — falls back to read-only.",
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

  const { buffer, fileName } = await getDocxBuffer.call(
    this,
    itemIndex,
    inputBinaryPropertyName,
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
