// FILE: nodes/Adeu/descriptions/hydrateToolOutput.operation.ts

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
  IDataObject,
} from "n8n-workflow";
import { NodeOperationError } from "n8n-workflow";

import { DOCX_MIME_TYPE } from "../GenericFunctions";

/**
 * Shape of the entry that `apply_edits` writes into workflow static data when
 * it runs as an AI Agent tool. We don't import the literal shape from
 * applyEdits.operation.ts to keep the module dependency one-way (descriptions
 * shouldn't depend on each other); the structural type is duplicated here.
 */
interface StashedBinaryEntry {
  id: string;
  fileName: string;
  mimeType: string;
  timestamp: number;
}

function isStashedBinaryEntry(value: unknown): value is StashedBinaryEntry {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.id === "string" &&
    typeof v.fileName === "string" &&
    typeof v.mimeType === "string"
  );
}

export const hydrateToolOutputDescription: INodeProperties[] = [
  {
    displayName: "Static Data Key",
    name: "staticDataKey",
    type: "string",
    default: "adeu_last_redlined",
    required: true,
    placeholder: "e.g. adeu_last_redlined",
    description:
      "Key in workflow global static data to read the stashed binary metadata from (string). " +
      "When the Apply Edits operation runs as an AI Agent tool, n8n's tool wrapper strips the binary from its output before any downstream node can see it. " +
      "To work around that, Apply Edits writes the binary's storage id into workflow static data under 'adeu_last_redlined'. " +
      "This operation reads that entry back, fetches the original buffer via getBinaryStream, and attaches a fresh IBinaryData onto the outgoing item. " +
      "Leave the default unless you've customized the stash key on the Apply Edits side.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["hydrateToolOutput"],
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
      "Name of the binary property on the outgoing item that will hold the hydrated .docx (string, e.g. 'data'). " +
      "Downstream nodes (e.g. Write Binary File, Gmail send) should be configured to read from this property name.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["hydrateToolOutput"],
      },
    },
  },
  {
    displayName: "On Missing",
    name: "onMissing",
    type: "options",
    default: "emit_empty",
    description:
      "What to do when no stashed binary entry is found under the configured key. " +
      "'Emit Empty' is the typical AI Agent path: emits an item with json.hydrated=false and no binary, so a downstream If node can gate any write step on whether the LLM actually called Apply Edits this turn. " +
      "'Throw' is for deterministic pipelines where the stash MUST be present and a missing entry indicates a workflow bug.",
    options: [
      {
        name: "Emit Empty",
        value: "emit_empty",
        description:
          "Emit a single item with JSON.hydrated=false and no binary. Downstream If on JSON.hydrated gates the write.",
      },
      {
        name: "Throw",
        value: "throw",
        description:
          "Throw a NodeOperationError. Use when a missing stash indicates a workflow bug.",
      },
    ],
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["hydrateToolOutput"],
      },
    },
  },
  {
    displayName: "Clear After Read",
    name: "clearAfterRead",
    type: "boolean",
    default: true,
    description:
      "Whether to delete the static data entry after a successful hydration so it does not leak across workflow runs. Set false if you want to inspect or re-read the entry from a later node in the same run.",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["hydrateToolOutput"],
      },
    },
  },
  {
    displayName: "Output Path Template",
    name: "outputPathTemplate",
    type: "string",
    default: "",
    placeholder:
      "e.g. C:\\Users\\Uzair\\.n8n-files\\{baseName}_{timestamp}.docx",
    description:
      "Optional template to compute the final write path on disk, returned on the output JSON as 'outputPath'. " +
      "If set, you can configure downstream Write nodes to simply read '{{ $json.outputPath }}' with zero escaping risks. " +
      "Supports these placeholders: '{baseName}' (filename with extension stripped), " +
      "'{timestamp}' (current ISO 8601 timestamp with colons and dots replaced by dashes for Windows file compatibility), " +
      "'{fileName}' (the stashed filename including extension), " +
      "and '{ext}' (e.g., '.docx').",
    displayOptions: {
      show: {
        resource: ["document"],
        operation: ["hydrateToolOutput"],
      },
    },
  },
];

export async function executeHydrateToolOutput(
  this: IExecuteFunctions,
  itemIndex: number,
): Promise<INodeExecutionData[]> {
  const staticDataKey = this.getNodeParameter(
    "staticDataKey",
    itemIndex,
  ) as string;
  const outputBinaryPropertyName = this.getNodeParameter(
    "outputBinaryPropertyName",
    itemIndex,
  ) as string;
  const onMissing = this.getNodeParameter("onMissing", itemIndex) as
    | "emit_empty"
    | "throw";
  const clearAfterRead = this.getNodeParameter(
    "clearAfterRead",
    itemIndex,
  ) as boolean;

  const staticData = this.getWorkflowStaticData("global") as IDataObject;
  const entry = staticData[staticDataKey];

  if (!isStashedBinaryEntry(entry)) {
    if (onMissing === "throw") {
      throw new NodeOperationError(
        this.getNode(),
        `No stashed binary found under static data key "${staticDataKey}".`,
        {
          description:
            "The Apply Edits operation writes a stash entry into workflow static data only when it runs as an AI Agent tool and produces a binary. " +
            "Verify that Apply Edits was actually called by the agent this turn, that it ran successfully, and that the key matches the one used on the writing side.",
          itemIndex,
        },
      );
    }
    return [
      {
        json: { hydrated: false },
        pairedItem: { item: itemIndex },
      },
    ];
  }

  const { id, fileName, mimeType } = entry;

  let buffer: Buffer;
  try {
    const stream = await this.helpers.getBinaryStream(id);
    buffer = await this.helpers.binaryToBuffer(stream);
  } catch (err) {
    throw new NodeOperationError(
      this.getNode(),
      `Failed to load stashed binary (id=${id}) from external storage.`,
      {
        description:
          `The stash entry under "${staticDataKey}" points at binary id "${id}", but the underlying data could not be loaded. ` +
          "This usually means the binary has been garbage-collected (n8n's execution data retention may be too short) or external storage is unavailable. " +
          `Underlying error: ${(err as Error).message}`,
        itemIndex,
      },
    );
  }

  const effectiveMimeType = mimeType || DOCX_MIME_TYPE;
  const binaryData = await this.helpers.prepareBinaryData(
    buffer,
    fileName,
    effectiveMimeType,
  );

  if (clearAfterRead) {
    delete staticData[staticDataKey];
  }

  const outputPathTemplate = this.getNodeParameter(
    "outputPathTemplate",
    itemIndex,
    "",
  ) as string;

  let outputPath: string | undefined;
  if (outputPathTemplate.trim() !== "") {
    const baseName = fileName.replace(/\.docx$/i, "");
    const ext = fileName.match(/\.docx$/i)?.[0] ?? ".docx";
    const timestamp = new Date()
      .toISOString()
      .replace(/[:.]/g, "-")
      .replace(/Z$/, "");
    outputPath = outputPathTemplate
      .replaceAll("{baseName}", baseName)
      .replaceAll("{timestamp}", timestamp)
      .replaceAll("{fileName}", fileName)
      .replaceAll("{ext}", ext);
  }

  return [
    {
      json: {
        hydrated: true,
        fileName,
        sourceBinaryId: id,
        mimeType: effectiveMimeType,
        ...(outputPath !== undefined ? { outputPath } : {}),
      },
      binary: {
        [outputBinaryPropertyName]: binaryData,
      },
      pairedItem: { item: itemIndex },
    },
  ];
}
