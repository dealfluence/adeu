import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
} from "n8n-workflow";
import {
  DocumentObject,
  _extractTextFromDoc,
  extract_outline,
  paginate,
  split_structural_appendix,
} from "@adeu/core";

import {
  type BinarySource,
  getDocxBufferFromSource,
} from "../GenericFunctions";

export const extractOutlineDescription: INodeProperties[] = [
  {
    displayName: "Input Binary Property",
    name: "binaryPropertyName",
    type: "string",
    default: "data",
    required: true,
    placeholder: "e.g. data",
    description:
      "Name of the binary property holding the .docx file (string, e.g. 'data'). In 'From Connected Input' mode this reads from the current item; in 'From Another Node' mode this specifies which property on the source node's output to read. Must be a valid .docx.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["extractOutline"],
      },
    },
  },
];

export async function executeExtractOutline(
  this: IExecuteFunctions,
  itemIndex: number,
): Promise<INodeExecutionData[]> {
  const binaryPropertyName = this.getNodeParameter(
    "binaryPropertyName",
    itemIndex,
  ) as string;

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

  const doc = await DocumentObject.load(buffer);

  // Project the body using the raw (uncleaned) view so heading detection
  // matches what the LLM sees when calling extractMarkdown with default settings.
  // Pass return_paragraph_offsets so extract_outline can use the fast path.
  const projection = _extractTextFromDoc(doc, false, true, true) as {
    text: string;
    paragraph_offsets: Map<unknown, [number, number]>;
  };

  const [body] = split_structural_appendix(projection.text);
  const pagination = paginate(body, "");

  const outline = extract_outline(
    doc,
    body,
    pagination.body_pages,
    pagination.body_page_offsets,
    projection.paragraph_offsets as Map<Element, [number, number]>,
  );

  return [
    {
      json: {
        fileName,
        total_pages: pagination.total_pages,
        outline: outline.map((node) => ({
          level: node.level,
          text: node.text,
          page: node.page,
          style: node.style,
          has_table: node.has_table,
          footnote_ids: node.footnote_ids,
        })),
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
