// FILE: node/packages/n8n-nodes-adeu/nodes/Adeu/GenericFunctions.ts

import type { IExecuteFunctions, JsonObject } from "n8n-workflow";
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
