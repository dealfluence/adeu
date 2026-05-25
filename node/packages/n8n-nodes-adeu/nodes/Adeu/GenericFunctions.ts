// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/GenericFunctions.ts

import type { IExecuteFunctions, IBinaryData, JsonObject } from "n8n-workflow";
import { NodeApiError, NodeOperationError } from "n8n-workflow";

export const DOCX_MIME_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

/**
 * Resolves a dot-notation JSON path (e.g., "body.data.changes") safely.
 */
export function getNestedProperty(
  obj: Record<string, unknown>,
  path: string,
): unknown {
  return path.split(".").reduce((acc, part) => {
    if (acc && typeof acc === "object") {
      return (acc as Record<string, unknown>)[part];
    }
    return undefined;
  }, obj as unknown);
}

/**
 * Reads a binary property from the current item and returns it as a Node Buffer
 * suitable for `DocumentObject.load(...)`. Throws a user-friendly NodeOperationError
 * if the property is missing.
 */
export async function getDocxBuffer(
  this: IExecuteFunctions,
  itemIndex: number,
  binaryPropertyName: string,
): Promise<{ buffer: Buffer; fileName: string }> {
  const item = this.getInputData()[itemIndex];

  if (!item.binary || !item.binary[binaryPropertyName]) {
    throw new NodeOperationError(
      this.getNode(),
      `No binary data found on property "${binaryPropertyName}"`,
      {
        description:
          "Verify that the upstream node produced binary data and that the property name matches.",
        itemIndex,
      },
    );
  }

  const binary = item.binary[binaryPropertyName];
  const buffer = await this.helpers.getBinaryDataBuffer(
    itemIndex,
    binaryPropertyName,
  );
  const fileName = binary.fileName ?? "document.docx";

  return { buffer, fileName };
}
/**
 * Discriminated union describing where to read a .docx binary from.
 * - `fromInput`: current item's `.binary[binaryPropertyName]` (default deterministic behavior)
 * - `fromNode`: a sibling node's output, resolved via `evaluateExpression`. Used by
 *   AI Agent tool calls, which cannot pass binary data through JSON arguments.
 */
export type BinarySource =
  | { mode: "fromInput"; binaryPropertyName: string }
  | {
      mode: "fromNode";
      sourceNodeName: string;
      binaryPropertyName: string;
    };

/**
 * Resolves a .docx binary from either the current item or a named upstream node.
 * Throws `NodeApiError` with AI-Agent-readable messages on every failure path,
 * so an Agent can self-correct on the next tool call.
 */
export async function getDocxBufferFromSource(
  this: IExecuteFunctions,
  itemIndex: number,
  source: BinarySource,
): Promise<{ buffer: Buffer; fileName: string }> {
  if (source.mode === "fromInput") {
    return getDocxBuffer.call(this, itemIndex, source.binaryPropertyName);
  }

  // --- fromNode branch ---
  const { sourceNodeName, binaryPropertyName } = source;

  // Pre-validate: empty source node name is the #1 expected mistake for AI Agents
  // that haven't been told to set the source node correctly.
  if (!sourceNodeName || sourceNodeName.trim() === "") {
    throw new NodeApiError(
      this.getNode(),
      { message: "Source Node Name is empty" } as JsonObject,
      {
        message:
          "Source Node Name is required when Document Source is 'From Another Node'.",
        description:
          "Set 'Source Node Name' to the exact name of the node in this workflow whose output holds the .docx binary (e.g. the trigger node). Node names are case-sensitive and must match the node label in the canvas exactly.",
        itemIndex,
      },
    );
  }

  // Resolve the binary metadata via expression. We escape single quotes in the
  // node name to prevent expression injection (n8n node names can legally
  // contain apostrophes).
  const escapedNodeName = sourceNodeName.replace(/'/g, "\\'");
  const expression = `{{ $('${escapedNodeName}').first().binary.${binaryPropertyName} }}`;

  let resolvedBinaryBag: IBinaryData | undefined;
  try {
    resolvedBinaryBag = this.evaluateExpression(expression, itemIndex) as
      | IBinaryData
      | undefined;
  } catch (err) {
    throw new NodeApiError(
      this.getNode(),
      { message: (err as Error).message } as JsonObject,
      {
        message: `Failed to resolve binary from source node '${sourceNodeName}'.`,
        description:
          `Could not evaluate '${expression}'. Verify the source node name is exactly correct (case-sensitive, including spaces and punctuation). ` +
          `Underlying error: ${(err as Error).message}`,
        itemIndex,
      },
    );
  }

  if (
    resolvedBinaryBag === undefined ||
    resolvedBinaryBag === null ||
    typeof resolvedBinaryBag !== "object"
  ) {
    const typeofValue = typeof resolvedBinaryBag;
    const stringifiedValue =
      resolvedBinaryBag !== undefined
        ? JSON.stringify(resolvedBinaryBag)
        : "undefined";
    throw new NodeApiError(
      this.getNode(),
      { message: "Binary property not found" } as JsonObject,
      {
        message: `Source node '${sourceNodeName}' has no binary on property '${binaryPropertyName}'.`,
        description: `Verify that the source node outputs the .docx on the expected property '${binaryPropertyName}'. (Resolved Type: ${typeofValue}, Resolved Value: ${stringifiedValue})`,
        itemIndex,
      },
    );
  }

  const binaryData = resolvedBinaryBag;
  if (!binaryData) {
    const availableProps = Object.keys(resolvedBinaryBag);
    const availableList =
      availableProps.length > 0
        ? `Available binary properties on '${sourceNodeName}': ${availableProps.map((p) => `'${p}'`).join(", ")}.`
        : `Source node '${sourceNodeName}' has no binary properties at all.`;
    throw new NodeApiError(
      this.getNode(),
      { message: "Binary property not found" } as JsonObject,
      {
        message: `Source node '${sourceNodeName}' has no binary on property '${binaryPropertyName}'.`,
        description: `${availableList} Update 'Binary Property Name' to one of the available property names, or change the source node so it outputs the .docx on the expected property.`,
        itemIndex,
      },
    );
  }

  // Decode the binary. n8n supports two storage modes:
  // - External storage (S3/filesystem): `id` is set, `data` is empty.
  // - Inline storage (default): `data` holds base64 directly.
  let buffer: Buffer;
  try {
    if (binaryData.id) {
      const stream = await this.helpers.getBinaryStream(binaryData.id);
      buffer = await this.helpers.binaryToBuffer(stream);
    } else if (binaryData.data) {
      buffer = Buffer.from(binaryData.data, "base64");
    } else {
      throw new Error(
        "Resolved binary metadata has neither an 'id' nor 'data' field.",
      );
    }
  } catch (err) {
    throw new NodeApiError(
      this.getNode(),
      { message: (err as Error).message } as JsonObject,
      {
        message: `Failed to read binary content from source node '${sourceNodeName}'.`,
        description:
          `The binary metadata was found on property '${binaryPropertyName}', but its contents could not be loaded. ` +
          `Underlying error: ${(err as Error).message}`,
        itemIndex,
      },
    );
  }

  const fileName = binaryData.fileName ?? "document.docx";
  return { buffer, fileName };
}
/**
 * Builds a default output filename from an input filename and a suffix.
 */
export function buildOutputFileName(
  inputFileName: string,
  suffix: string,
): string {
  const lastDot = inputFileName.lastIndexOf(".");
  const base =
    lastDot === -1 ? inputFileName : inputFileName.substring(0, lastDot);
  return `${base}_${suffix}.docx`;
}

/**
 * Parses a JSON-text parameter into an object/array. Natively strips Markdown
 * code blocks (e.g., ```json ... ```) to prevent syntax parsing failures.
 */
export function parseJsonParameter<T>(
  this: IExecuteFunctions,
  raw: unknown,
  itemIndex: number,
  parameterName: string,
): T {
  if (raw === undefined || raw === null || raw === "") {
    throw new NodeOperationError(
      this.getNode(),
      `Parameter "${parameterName}" is empty`,
      {
        description: `Provide a JSON value for "${parameterName}".`,
        itemIndex,
      },
    );
  }

  if (typeof raw === "object") {
    return raw as T;
  }

  let cleaned = (raw as string).trim();

  // Strip leading and trailing markdown code block wrapper if present
  if (cleaned.startsWith("```")) {
    cleaned = cleaned.replace(/^```[a-zA-Z]*\n?/, "");
    cleaned = cleaned.replace(/\n?```$/, "");
  }
  cleaned = cleaned.trim();

  try {
    return JSON.parse(cleaned) as T;
  } catch (error) {
    throw new NodeOperationError(
      this.getNode(),
      `Parameter "${parameterName}" is not valid JSON`,
      {
        description: (error as Error).message,
        itemIndex,
      },
    );
  }
}

/**
 * Translates errors thrown by `@adeu/core` (notably `BatchValidationError`)
 * into n8n's `NodeApiError` with actionable feedback for AI agents.
 */
export function mapAdeuErrorToNodeApiError(
  this: IExecuteFunctions,
  error: Error,
  itemIndex: number,
): NodeApiError {
  const message = error.message ?? "Unknown error";
  const errorName = error.name ?? "";

  const errors = (error as unknown as { errors?: string[] }).errors;
  const joined = Array.isArray(errors) ? errors.join("\n") : message;
  const lower = joined.toLowerCase();

  if (errorName === "BatchValidationError") {
    let messageContext = "Batch validation failed.";
    let descriptionContext = "Review the listed failures:\n" + joined;

    if (lower.includes("target text not found")) {
      messageContext = "An edit could not be applied: target text not found.";
      descriptionContext =
        "Verify the exact `target_text` string — including punctuation and whitespace — against the document. Check the individual failures:\n" +
        joined;
    } else if (lower.includes("ambiguous match")) {
      messageContext = "An edit matched multiple locations in the document.";
      descriptionContext =
        "Provide more surrounding context in `target_text` to uniquely identify the location:\n" +
        joined;
    } else if (lower.includes("read-only") || lower.includes("readonly")) {
      messageContext = "An edit targeted a read-only structural element.";
      descriptionContext =
        "Cross-references, footnotes, hyperlinks, and the Structural Appendix cannot be modified via text replacement:\n" +
        joined;
    } else if (lower.includes("another author") || lower.includes("nested")) {
      messageContext =
        "An edit overlaps with a pending tracked change by another author.";
      descriptionContext =
        "Accept or reject the conflicting change first, or scope your edit outside of it:\n" +
        joined;
    }

    return new NodeApiError(
      this.getNode(),
      { message: joined, errors } as JsonObject,
      {
        message: messageContext,
        description: descriptionContext,
        itemIndex, // Applied to pass node-operation-error-itemindex rule
      },
    );
  }

  if (
    lower.includes("invalid docx") ||
    lower.includes("missing word/document.xml")
  ) {
    return new NodeApiError(this.getNode(), { message } as JsonObject, {
      message: "The document could not be opened.",
      description:
        "Verify the input binary is a valid .docx file (not .doc, .pdf, or another format).",
      itemIndex, // Applied to pass node-operation-error-itemindex rule
    });
  }

  return new NodeApiError(this.getNode(), { message } as JsonObject, {
    message: "Adeu engine error.",
    description: message,
    itemIndex, // Applied to pass node-operation-error-itemindex rule
  });
}
