// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/applyTextRevision.operation.ts

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import { DocumentObject, apply_text_revision_core } from "@adeu/core";

import {
  type BinarySource,
  DOCX_MIME_TYPE,
  buildOutputFileName,
  docOpDisplayOptions,
  getDocxBufferFromSource,
} from "../GenericFunctions";

const displayOptions = docOpDisplayOptions("applyTextRevision");

export const applyTextRevisionDescription: INodeProperties[] = [
  {
    displayName: "Reasoning",
    name: "reasoning",
    type: "string",
    default: "",
    typeOptions: {
      rows: 2,
    },
    description:
      "Why this revision is being applied. State your reasoning BEFORE the revised text — what you intend to change and why. This field is captured for auditability and is NOT forwarded into the redline engine; its only purpose is to make the AI reason first. Safe to leave empty in deterministic (non-AI) pipelines.",
    displayOptions,
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
    displayOptions,
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
    displayOptions,
  },
  {
    displayName: "Author",
    name: "author",
    type: "string",
    default: "Adeu AI",
    placeholder: "e.g. AI Reviewer",
    description:
      "Author name attached to every tracked change this revision produces (string, e.g. 'AI Reviewer'). Shows up in Word's review pane as the author of each redline.",
    displayOptions,
  },
  {
    displayName: "Revised Text",
    name: "revisedText",
    type: "string",
    default: "",
    required: true,
    typeOptions: {
      rows: 10,
    },
    description:
      "The COMPLETE revised clean text of the document. Read it first with the Extract Markdown operation using Clean View on and Page 0 (whole document), edit that text, then send all of it back — the engine diffs this against the document's clean view and turns the difference into tracked changes. " +
      "Never include CriticMarkup tags ({++ ++}, {-- --}, {>> <<}); they would be applied as literal prose and are rejected. " +
      "Never send a single page of a paginated extract: everything missing from this text is applied as a tracked deletion. " +
      "A revision that removes more than 50% of the characters (75% for documents under 2,000 characters) is refused unless 'Allow Major Deletions' is on. " +
      "After applying, the engine re-reads the document's clean text and compares it to this value; if they differ, nothing is returned and the operation fails, because some structure (headings, table rows, footnotes) cannot be removed by text replacement.",
    displayOptions,
  },
  {
    displayName: "Allow Major Deletions",
    name: "allowMajorDeletions",
    type: "boolean",
    default: false,
    description:
      "Whether to allow a revision that deletes more than 50% of the document's characters (more than 75% for documents under 2,000 characters). False (default) refuses such a revision on the assumption that the supplied text is a partial extract rather than the whole document.",
    displayOptions,
  },
];

export async function executeApplyTextRevision(
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
  const revisedText = this.getNodeParameter("revisedText", itemIndex) as string;
  const allowMajorDeletions = this.getNodeParameter(
    "allowMajorDeletions",
    itemIndex,
    false,
  ) as boolean;
  const reasoning = this.getNodeParameter("reasoning", itemIndex, "") as string;

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
  const outName = buildOutputFileName(fileName, "redlined");

  // `input_path`/`output_path` never touch a filesystem here (core does no
  // file IO — see core text-revision.ts:8-13); they only name the artifact in
  // the engine's own messages. Passing outName explicitly keeps those
  // messages free of platform path noise from the default derivation.
  const result = await apply_text_revision_core({
    doc,
    input_path: fileName,
    output_path: outName,
    revised_text: revisedText,
    author,
    allow_major_deletions: allowMajorDeletions,
  });

  const stats = result.stats as Record<string, unknown>;
  // Path-shaped fields are meaningless on the canvas: the document travels as
  // a binary property, not as a file.
  delete stats.output_path;
  delete stats.unverified_output_path;

  const outBuffer = Buffer.from(result.out_bytes);
  const binary = await this.helpers.prepareBinaryData(
    outBuffer,
    outName,
    DOCX_MIME_TYPE,
  );

  // Same stash contract as Apply Edits: n8n's AI Agent tool wrapper strips
  // `binary` from a tool's return value, so the storage id travels through
  // workflow static data for the Hydrate Tool Output operation to pick up.
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

  const incomingBinary = this.getInputData()[itemIndex].binary ?? {};

  return [
    {
      json: {
        fileName: outName,
        author,
        stats,
        ...(reasoning !== "" ? { reasoning } : {}),
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
