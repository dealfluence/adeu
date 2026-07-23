// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/Adeu.node.ts
import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from "n8n-workflow";
import { NodeConnectionTypes, NodeOperationError } from "n8n-workflow";
import { executeHydrateToolOutput } from "./descriptions/hydrateToolOutput.operation";
import { documentDescription } from "./descriptions";
import { executeExtractMarkdown } from "./descriptions/extractMarkdown.operation";
import { executeExtractOutline } from "./descriptions/extractOutline.operation";
import { executeApplyEdits } from "./descriptions/applyEdits.operation";
import { executeGenerateDiff } from "./descriptions/generateDiff.operation";
import { executeFinalizeDocument } from "./descriptions/finalizeDocument.operation";
import { mapAdeuErrorToNodeApiError } from "./GenericFunctions";

export class Adeu implements INodeType {
  description: INodeTypeDescription = {
    displayName: "Adeu",
    name: "adeu",
    icon: { light: "file:adeu.svg", dark: "file:adeu-dark.svg" },
    group: ["transform"],
    version: 1,
    subtitle: '={{$parameter["operation"]}}',
    description:
      "Operate on Microsoft Word (.docx) files: extract LLM-friendly Markdown with CriticMarkup, navigate large documents via a structural outline, apply tracked changes and comments, generate sub-word diffs, and sanitize/finalize documents. " +
      "Five operations on the Document resource: " +
      "(1) Extract Markdown — project a .docx into Markdown plus a Semantic Appendix; toggle Clean View to simulate Accept All; pass an optional Page number to fetch only one page of a large document. " +
      "(2) Extract Outline — return a token-cheap structural map (headings with level, page number, paragraph style, has_table, footnote IDs) plus total_pages. Pair with Extract Markdown to navigate large documents. " +
      "(3) Apply Edits — apply a JSON array of DocumentChange objects as native Word tracked changes; modify edits support match_mode ('strict'|'first'|'all') and regex (boolean); the entire batch is pre-validated atomically and rejected if any single edit is invalid. Supports a Dry Run flag that previews edits without committing them. " +
      "(4) Generate Diff — produce a @@ Word Patch @@ sub-word level diff between two .docx files. " +
      "(5) Finalize Document — strip metadata, optionally accept all pending markup, and optionally lock the file read-only. " +
      "DocumentChange schema (used by Apply Edits): each object has a 'type' field discriminator. " +
      "type='modify' requires target_text (string, copied EXACTLY from the source including punctuation, spacing, and case) and new_text (string); optional comment (string); optional match_mode ('strict'|'first'|'all', default 'strict'); optional regex (boolean, default false — when true, target_text is an ES2022 RegExp pattern and new_text may reference $1, $2 capture groups). " +
      "type='accept' or type='reject' requires target_id (string like 'Chg:12' from the Markdown projection); optional 'comment'. " +
      "type='reply' requires target_id (string like 'Com:45') and text (string). " +
      "type='insert_row' requires target_text (string), position ('above' or 'below'), and cells (array of strings). " +
      "type='delete_row' requires target_text (string). " +
      "Never wrap new_text in CriticMarkup tags like {++ ++} or {-- --}; the engine applies tracking automatically. " +
      "Never target text already wrapped in another author's pending tracked change; accept or reject their change first by target_id. " +
      "Binary handling for AI Agents: this tool cannot receive .docx files through JSON arguments because JSON cannot carry binary data. To process a document, set 'Document Source' to 'From Another Node' and 'Source Node Name' to the exact name of the workflow node that produced the .docx binary (typically the trigger node, e.g. 'Gmail Trigger', 'When clicking Test workflow', or an HTTP Request node configured to download a file). Node names are case-sensitive and must match the canvas label exactly. For the Generate Diff operation, set both 'Original Source Node Name' and 'Modified Source Node Name' independently.",
    defaults: {
      name: "Adeu",
    },
    usableAsTool: true,
    inputs: [NodeConnectionTypes.Main],
    outputs: [NodeConnectionTypes.Main],
    credentials: [],
    properties: [
      {
        displayName: "Resource",
        name: "resource",
        type: "options",
        noDataExpression: true,
        options: [
          {
            name: "Document",
            value: "document",
          },
        ],
        default: "document",
      },
      ...documentDescription,
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const returnData: INodeExecutionData[] = [];

    const resource = this.getNodeParameter("resource", 0) as string;
    const operation = this.getNodeParameter("operation", 0) as string;

    for (let i = 0; i < items.length; i++) {
      try {
        let result: INodeExecutionData[];

        if (resource === "document") {
          switch (operation) {
            case "extractMarkdown":
              result = await executeExtractMarkdown.call(this, i);
              break;
            case "extractOutline":
              result = await executeExtractOutline.call(this, i);
              break;
            case "applyEdits":
              result = await executeApplyEdits.call(this, i);
              break;
            case "generateDiff":
              result = await executeGenerateDiff.call(this, i);
              break;
            case "finalizeDocument":
              result = await executeFinalizeDocument.call(this, i);
              break;
            case "hydrateToolOutput":
              result = await executeHydrateToolOutput.call(this, i);
              break;
            default:
              throw new NodeOperationError(
                this.getNode(),
                `Unsupported operation: ${operation}`,
                { itemIndex: i },
              );
          }
        } else {
          throw new NodeOperationError(
            this.getNode(),
            `Unsupported resource: ${resource}`,
            { itemIndex: i },
          );
        }

        returnData.push(...result);
      } catch (error) {
        if (this.continueOnFail()) {
          returnData.push({
            json: {
              error: (error as Error).message,
            },
            pairedItem: { item: i },
          });
          continue;
        }
        const err = error as Error;

        if (err.name === "NodeOperationError" || err.name === "NodeApiError") {
          throw err;
        }
        throw mapAdeuErrorToNodeApiError.call(this, err, i);
      }
    }

    return [returnData];
  }
}
