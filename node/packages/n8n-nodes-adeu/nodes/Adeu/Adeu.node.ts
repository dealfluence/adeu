import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from "n8n-workflow";
import { NodeConnectionTypes } from "n8n-workflow";

import { documentDescription } from "./descriptions";
import { executeExtractMarkdown } from "./descriptions/extractMarkdown.operation";
import { executeApplyEdits } from "./descriptions/applyEdits.operation";
import { executeGenerateDiff } from "./descriptions/generateDiff.operation";
import { executeFinalizeDocument } from "./descriptions/finalizeDocument.operation";
import { mapAdeuErrorToNodeApiError } from "./GenericFunctions";

export class Adeu implements INodeType {
  description: INodeTypeDescription = {
    displayName: "Adeu",
    name: "adeu",
    icon: "file:adeu.svg",
    group: ["transform"],
    version: 1,
    subtitle: '={{$parameter["operation"]}}',
    description:
      "Apply AI-driven tracked changes, extract Markdown, generate diffs, and sanitize Microsoft Word (.docx) documents.",
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
            case "applyEdits":
              result = await executeApplyEdits.call(this, i);
              break;
            case "generateDiff":
              result = await executeGenerateDiff.call(this, i);
              break;
            case "finalizeDocument":
              result = await executeFinalizeDocument.call(this, i);
              break;
            default:
              throw new Error(`Unsupported operation: ${operation}`);
          }
        } else {
          throw new Error(`Unsupported resource: ${resource}`);
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
