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
      sourceBinaryId?: string;
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
  const sourceBinaryId =
    "sourceBinaryId" in source ? source.sourceBinaryId : undefined;

  // If a Source Binary ID is explicitly provided (such as in consecutive tool calls),
  // skip evaluation of upstream node names and pull directly from binary storage.
  if (sourceBinaryId && sourceBinaryId.trim() !== "") {
    try {
      const stream = await this.helpers.getBinaryStream(sourceBinaryId);
      const buffer = await this.helpers.binaryToBuffer(stream);
      return { buffer, fileName: "document.docx" };
    } catch (err) {
      throw new NodeApiError(
        this.getNode(),
        { message: (err as Error).message } as JsonObject,
        {
          message: `Failed to load document from Binary ID '${sourceBinaryId}'.`,
          description:
            `The binary with ID '${sourceBinaryId}' could not be loaded. ` +
            "This usually means the binary has expired, been garbage-collected, or the ID is incorrect.",
          itemIndex,
        },
      );
    }
  }

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
    // The leaf `.binary.<prop>` lookup returned nothing. n8n collapses two
    // distinct failure modes into the same `undefined` result here:
    //   (a) the source node produced no output at all, or
    //   (b) the source node ran but does not have the requested binary
    //       property on its output item.
    // Probe `.first().json` to disambiguate: it returns `undefined` only when
    // the node truly has no output, but returns an object (possibly empty)
    // whenever the node ran. This probe only fires on the error path, so it
    // costs nothing in the hot path.
    const probeExpression = `{{ $('${escapedNodeName}').first().json }}`;
    let nodeRanProbe: unknown;
    try {
      nodeRanProbe = this.evaluateExpression(probeExpression, itemIndex);
    } catch {
      nodeRanProbe = undefined;
    }

    if (nodeRanProbe === undefined || nodeRanProbe === null) {
      throw new NodeApiError(
        this.getNode(),
        { message: "Source node produced no output" } as JsonObject,
        {
          message: `Source node '${sourceNodeName}' has no output.`,
          description:
            `The expression '${expression}' resolved to undefined and the node appears not to have executed in this run. ` +
            `Verify that '${sourceNodeName}' is upstream of this Adeu node, that its name matches exactly (case-sensitive), and that it has run successfully.`,
          itemIndex,
        },
      );
    }

    // The node ran but the requested binary property is missing.
    // Best-effort: try to list which binary properties exist. n8n's expression
    // engine restricts direct access to the parent `.binary` proxy, so this
    // probe may itself return undefined — in which case we omit the listing.
    let availableHint = "";
    try {
      const binaryBagProbe = this.evaluateExpression(
        `{{ Object.keys($('${escapedNodeName}').first().binary || {}) }}`,
        itemIndex,
      );
      if (Array.isArray(binaryBagProbe) && binaryBagProbe.length > 0) {
        availableHint =
          ` Available binary properties on '${sourceNodeName}': ` +
          binaryBagProbe.map((p) => `'${p}'`).join(", ") +
          ".";
      } else if (Array.isArray(binaryBagProbe) && binaryBagProbe.length === 0) {
        availableHint = ` Source node '${sourceNodeName}' has no binary properties at all.`;
      }
    } catch {
      // Probe failed — n8n's proxy restrictions block parent `.binary` access.
      // Fall through with no hint.
    }

    throw new NodeApiError(
      this.getNode(),
      { message: "Binary property not found" } as JsonObject,
      {
        message: `Source node '${sourceNodeName}' has no binary on property '${binaryPropertyName}'.`,
        description:
          `The expression '${expression}' resolved to undefined.${availableHint} ` +
          `Update 'Binary Property Name' to match an existing property, or change the source node so it outputs the .docx on the expected property.`,
        itemIndex,
      },
    );
  }

  const binaryData = resolvedBinaryBag;

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

const MATCH_MODE_SYNONYMS: Record<string, "strict" | "first" | "all"> = {
  strict: "strict",
  exact: "strict",
  single: "strict",
  first: "first",
  "first-occurrence": "first",
  "first-only": "first",
  all: "all",
  global: "all",
  bulk: "all",
  alloccurrences: "all",
  "all-occurrences": "all",
  every: "all",
};

/**
 * Normalizes an individual DocumentChange item in place: infers a missing `type`
 * when unambiguous and coerces match_mode synonyms.
 */
export function coerceChangeItemInPlace(item: any): void {
  if (item === null || typeof item !== "object" || Array.isArray(item)) return;

  // Infer missing `type` when fields fit unambiguously
  if (!("type" in item) || item.type === undefined || item.type === null) {
    if ("cells" in item) item.type = "insert_row";
    else if ("text" in item && "target_id" in item) item.type = "reply";
    else if ("target_text" in item && "new_text" in item) item.type = "modify";
  }

  // Normalize match_mode
  if ("match_mode" in item) {
    const raw = item.match_mode;
    if (typeof raw !== "string") {
      delete item.match_mode;
    } else {
      const mapped = MATCH_MODE_SYNONYMS[raw.trim().toLowerCase()];
      if (mapped === undefined) delete item.match_mode;
      else item.match_mode = mapped;
    }
  }
}

/**
 * Coerces and normalizes an array of DocumentChange items, parsing any
 * stringified elements and repairing recoverable fields.
 */
export function coerceChangesArray(changes: unknown[]): unknown[] {
  return changes.map((item: any) => {
    let obj: any = item;
    if (typeof item === "string") {
      try {
        const parsed = JSON.parse(item);
        obj = parsed !== null && typeof parsed === "object" ? parsed : item;
      } catch {
        obj = item;
      }
    }
    if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) {
      coerceChangeItemInPlace(obj);
    }
    return obj;
  });
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
    } else if (lower.includes("comment range from another author")) {
      messageContext =
        "A match_mode='all' edit would sweep through another author's comment range.";
      descriptionContext =
        "Bulk replacements are blocked from running through foreign comment ranges. Target the commented text deliberately with match_mode 'strict' or 'first', or scope the edit outside of it:\n" +
        joined;
    } else if (lower.includes("another author") || lower.includes("nested")) {
      messageContext =
        "An edit overlaps with a pending tracked change by another author.";
      descriptionContext =
        "Accept or reject the conflicting change first, or scope your edit outside of it:\n" +
        joined;
    } else if (lower.includes("control character")) {
      messageContext = "An edit contains invalid XML control characters.";
      descriptionContext =
        "Remove non-printable control characters from your edit string before submitting:\n" +
        joined;
    } else if (lower.includes("criticmarkup tags")) {
      messageContext = "Do not manually include CriticMarkup tags in new_text.";
      descriptionContext =
        "The engine applies tracked change tags automatically. Pass clean replacement text in `new_text`:\n" +
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
