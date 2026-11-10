// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/descriptions/extractMarkdown.operation.ts
import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import { NodeOperationError } from "n8n-workflow";
import {
  collectFields,
  DocumentObject,
  extractTextFromBuffer,
  PAGE_RANGE_MAX_PAGES,
  paginate,
  parse_page_arg,
  readDocumentProtection,
  renderLedger,
  split_structural_appendix,
} from "@adeu/core";

import {
  type BinarySource,
  docOpDisplayOptions,
  getDocxBufferFromSource,
} from "../GenericFunctions";

const displayOptions = docOpDisplayOptions("extractMarkdown");

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
    displayOptions,
  },
  {
    displayName: "Mode",
    name: "mode",
    type: "options",
    default: "full",
    description:
      "Projection mode. 'Full' (default) returns the document body in Markdown/CriticMarkup. 'Fields (Content Controls)' returns the form-fields ledger with CC:<N> IDs, tags, aliases, types, lock/binding state, and current values for use with set_field.",
    options: [
      {
        name: "Full Document",
        value: "full",
        description: "Full or page-scoped Markdown/CriticMarkup projection",
      },
      {
        name: "Fields (Content Controls)",
        value: "fields",
        description:
          "Form-fields ledger listing every content control (CC:<N>, tag, alias, type, state, value)",
      },
    ],
    displayOptions,
  },
  {
    displayName: "Fields Offset",
    name: "fieldsOffset",
    type: "number",
    default: 0,
    typeOptions: {
      minValue: 0,
    },
    description:
      "0-based entry offset when paging through a long Fields ledger",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["extractMarkdown"],
        mode: ["fields"],
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
    displayOptions,
  },
  {
    displayName: "Include Appendix",
    name: "includeAppendix",
    type: "boolean",
    default: true,
    description:
      "Whether to include the Structural Appendix (defined terms, cross-reference anchors, and potential typos) at the end of the projection. True by default for rich LLM context.",
    displayOptions,
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
  const mode = this.getNodeParameter(
    "mode",
    itemIndex,
    "full",
  ) as "full" | "fields";
  const cleanView = this.getNodeParameter("cleanView", itemIndex, false) as boolean;
  const includeAppendix = this.getNodeParameter(
    "includeAppendix",
    itemIndex,
    true,
  ) as boolean;
  const pageRaw = this.getNodeParameter("page", itemIndex, 0) as number | string;

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

  if (mode === "fields") {
    const fieldsOffset = this.getNodeParameter(
      "fieldsOffset",
      itemIndex,
      0,
    ) as number;
    const doc = await DocumentObject.load(buffer);
    const rawText = await extractTextFromBuffer(buffer, false, false);
    const [body] = split_structural_appendix(rawText);
    const pagination = paginate(body, "");
    const entries = collectFields(doc, rawText, pagination.body_page_offsets);
    const protection = readDocumentProtection(doc);
    const markdown = renderLedger(fileName, entries, protection, fieldsOffset);
    return [
      {
        json: {
          fileName,
          mode: "fields",
          total_fields: entries.length,
          markdown,
        },
        pairedItem: { item: itemIndex },
      },
    ];
  }

  const fullMarkdown = await extractTextFromBuffer(
    buffer,
    cleanView,
    includeAppendix,
  );

  // Page 0 / omitted / "0" / "all" => full document, no pagination metadata (preserves backward compatibility).
  if (
    !pageRaw ||
    pageRaw === 0 ||
    (typeof pageRaw === "string" &&
      (pageRaw.trim() === "0" || pageRaw.trim().toLowerCase() === "all"))
  ) {
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

  if (typeof pageRaw === "string" && pageRaw.includes("-")) {
    const [kind, val] = parse_page_arg(pageRaw.trim().replace(/^['"]|['"]$/g, ""));
    if (kind === "range" && Array.isArray(val)) {
      const [startPage, endPage] = val;
      if (startPage > pagination.total_pages) {
        throw new NodeOperationError(
          this.getNode(),
          `Requested page ${startPage} exceeds total_pages (${pagination.total_pages}). Call extractOutline to discover the page count first.`,
          { itemIndex },
        );
      }
      const lastPage = Math.min(
        endPage,
        startPage + PAGE_RANGE_MAX_PAGES - 1,
        pagination.total_pages,
      );
      const selectedPages = pagination.pages.slice(startPage - 1, lastPage);
      const combinedMarkdown = selectedPages
        .map((p) => p.page_content)
        .join("\n\n");
      const totalChanges = selectedPages.reduce(
        (acc, p) => acc + p.tracked_change_count,
        0,
      );
      return [
        {
          json: {
            fileName,
            cleanView,
            markdown: combinedMarkdown,
            page: `${startPage}-${ endPage }`,
            total_pages: pagination.total_pages,
            has_next: lastPage < pagination.total_pages,
            has_prev: startPage > 1,
            tracked_change_count: totalChanges,
          },
          pairedItem: { item: itemIndex },
        },
      ];
    }
  }

  const page = typeof pageRaw === "number" ? pageRaw : Number(pageRaw);
  if (!Number.isInteger(page) || page < 1 || page > pagination.total_pages) {
    throw new NodeOperationError(
      this.getNode(),
      `Requested page ${pageRaw} exceeds total_pages (${pagination.total_pages}). Call extractOutline to discover the page count first.`,
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
