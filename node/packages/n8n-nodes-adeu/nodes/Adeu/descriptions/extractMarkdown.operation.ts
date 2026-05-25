import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import { extractTextFromBuffer } from "@adeu/core";

import {
  type BinarySource,
  getDocxBufferFromSource,
} from "../GenericFunctions";
export const extractMarkdownDescription: INodeProperties[] = [
  {
    displayName: "Input Binary Property",
    name: "binaryPropertyName",
    type: "string",
    default: "data",
    required: true,
    placeholder: "e.g. data",
    description:
      "Name of the binary property holding the .docx file (string, e.g. 'data'). In 'From Connected Input' mode this reads from the current item; in 'From Another Node' mode this specifies which property on the source node's output to read. Must be a valid .docx (not .doc, .pdf, or another format).",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["extractMarkdown"],
      },
    },
  },
  {
    displayName: "Clean View",
    name: "cleanView",
    type: "boolean",
    default: false,
    description:
      "Boolean. When true, projects the document as if all pending tracked changes were accepted (simulates Accept All). When false (default), all tracked changes are surfaced inline as CriticMarkup ({++ins++}, {--del--}, {>>comment<<}) so an AI can review and resolve them. Use false to review counterparty edits; use true to generate net-new redlines against a clean baseline.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["extractMarkdown"],
      },
    },
  },
];

export async function executeExtractMarkdown(
  this: IExecuteFunctions,
  itemIndex: number,
): Promise<INodeExecutionData[]> {
  const binaryPropertyName = this.getNodeParameter(
    "binaryPropertyName",
    itemIndex,
  ) as string;
  const cleanView = this.getNodeParameter("cleanView", itemIndex) as boolean;

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
          binaryPropertyName,
        }
      : { mode: "fromInput", binaryPropertyName };

  const { buffer, fileName } = await getDocxBufferFromSource.call(
    this,
    itemIndex,
    source,
  );
  const markdown = await extractTextFromBuffer(buffer, cleanView);

  return [
    {
      json: {
        fileName,
        cleanView,
        markdown,
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
