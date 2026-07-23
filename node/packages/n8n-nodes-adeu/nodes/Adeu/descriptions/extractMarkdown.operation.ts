// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/extractMarkdown.operation.ts
import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import { NodeOperationError } from "n8n-workflow";
import {
  extractTextFromBuffer,
  paginate,
  split_structural_appendix,
} from "@adeu/core";

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
      "Whether to project the document as if all pending tracked changes were accepted (simulates Accept All). When false (default), all tracked changes are surfaced inline as CriticMarkup ({++ins++}, {--del--}, {>>comment<<}) so an AI can review and resolve them. Use false to review counterparty edits; use true to generate net-new redlines against a clean baseline.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["extractMarkdown"],
      },
    },
  },
  {
    displayName: "Include Appendix",
    name: "includeAppendix",
    type: "boolean",
    default: true,
    description:
      "Whether to include the Structural Appendix (defined terms, cross-reference anchors, and potential typos) at the end of the projection. True by default for rich LLM context.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["extractMarkdown"],
      },
    },
  },
  {
    displayName: "Page",
    name: "page",
    type: "number",
    default: 0,
    typeOptions: {
      minValue: 0,
    },
    description:
      "Optional 1-based page number to return only one page of the projected document (integer, default 0). " +
      "When 0 (default), the full Markdown body plus the Structural Appendix is returned in the 'markdown' field and pagination metadata is omitted. " +
      "When >= 1, only that page is returned in the 'markdown' field and the JSON output includes pagination metadata: page, total_pages, has_next, has_prev, tracked_change_count. " +
      "Pages are ~19,000-character chunks of the projected body with the Structural Appendix appended to each page. Use the extractOutline operation first to discover how many pages exist and what each page contains. " +
      "If the requested page exceeds total_pages, an error is thrown.",
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
  const includeAppendix = this.getNodeParameter(
    "includeAppendix",
    itemIndex,
    true,
  ) as boolean;
  const page = this.getNodeParameter("page", itemIndex, 0) as number;

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
          sourceBinaryId: this.getNodeParameter(
            "sourceBinaryId",
            itemIndex,
            "",
          ) as string,
        }
      : { mode: "fromInput", binaryPropertyName };

  const { buffer, fileName } = await getDocxBufferFromSource.call(
    this,
    itemIndex,
    source,
  );
  const fullMarkdown = await extractTextFromBuffer(
    buffer,
    cleanView,
    includeAppendix,
  );

  // Page 0 / omitted => full document, no pagination metadata (preserves backward compatibility).
  if (!page || page === 0) {
    return [
      {
        json: {
          fileName,
          cleanView,
          markdown: fullMarkdown,
        },
        pairedItem: { item: itemIndex },
      },
    ];
  }

  // Paginate. Split body from structural appendix so paginate() can append the appendix to each page.
  const [body, appendix] = split_structural_appendix(fullMarkdown);
  const pagination = paginate(body, appendix);

  if (page > pagination.total_pages) {
    throw new NodeOperationError(
      this.getNode(),
      `Requested page ${page} exceeds total_pages (${pagination.total_pages}). Call extractOutline to discover the page count first.`,
      { itemIndex },
    );
  }

  const pageInfo = pagination.pages[page - 1];

  return [
    {
      json: {
        fileName,
        cleanView,
        markdown: pageInfo.page_content,
        page: pageInfo.page,
        total_pages: pageInfo.total_pages,
        has_next: pageInfo.has_next,
        has_prev: pageInfo.has_prev,
        tracked_change_count: pageInfo.tracked_change_count,
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
