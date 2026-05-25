# Technical Resolution Report: Sibling Node Binary Access for AI Agent Tools

This document provides a comprehensive technical overview of the issues identified, their root causes, and the engineering resolution implemented in the `n8n-nodes-adeu` community node to support sibling node binary access during AI Agent tool executions.

---

## 1. Problem Description

When calling the Adeu node as an AI Agent tool (using `documentSource: "fromNode"` to retrieve a `.docx` file from an upstream node such as `Read Binary File`), the execution failed with variations of the following error:

```json
{
  "errorMessage": "Source node 'Read Binary File' did not produce any binary data.\n\nDetails: The node ran but produced no binary attachments. Check that the source node outputs a .docx file...",
  "errorDescription": "The node ran but produced no binary attachments..."
}
```

This error occurred even though the upstream `Read Binary File` node ran successfully and outputted a valid `.docx` attachment on the default `data` property.

---

## 2. Detailed Root Cause Analysis

The failure was caused by three distinct n8n expression engine behaviors that compounded when executing a custom node within an AI Agent's tool context:

### Context A: Paired Item Linking Failure (`.item` vs. `.first()`)
In deterministic n8n pipelines, the `.item` accessor is used to link the current node's active item index with the matching output index of the referenced node. 

However, AI Agent tools are executed via the `ai_tool` connection port (a non-standard main execution path). Because tool nodes run without a standard input item context, **paired item linking is completely unavailable**. When the expression evaluated `$('Node Name').item`, n8n returned `undefined` because there was no active input item to link against. 

### Context B: Object Serialization and Proxy Constraints (`.binary` vs. `.binary.data`)
To optimize memory usage and prevent large base64 strings from bloating expressions, n8n's modern expression engine restricts direct access to the parent `.binary` dictionary (returning `undefined`). 

However, n8n allows direct retrieval of specific leaf properties (such as `.binary.data`). Since `.binary.data` represents only the metadata of the file (such as its filesystem `id`, `fileName`, and `mimeType`), it is lightweight and bypasses the proxy restrictions.

### Context C: Programmatic Expression Coercion (`={{ ... }}` vs. `{{ ... }}`)
In the n8n UI, expressions are represented with a leading `=` to indicate dynamic evaluation (e.g., `={{ ... }}`). When evaluating expressions programmatically via `this.evaluateExpression`:
*   **Template Mode (`={{ ... }}`):** If n8n detects static characters (like the leading `=`) outside of the curly braces, it treats the string as a template. When an inner expression evaluates to an object, n8n coerces the object to a string (`"[object Object]"`), resulting in the final evaluated string `"=[object Object]"`. This caused the node's strict object-type validation (`typeof resolvedValue !== "object"`) to fail.
*   **Literal Mode (`{{ ... }}`):** Excluding the leading `=` and wrapping the expression strictly in double curly braces instructs n8n's `evaluateExpression` to evaluate the expression as a raw object. This returns the actual underlying `IBinaryData` metadata object natively without any string coercion.

---

## 3. The Implemented Solution

The programmatic expression constructed in `GenericFunctions.ts` was updated to combine the three resolutions:

1.  **Replaced `.item` with `.first()`:** Bypasses paired item linking, ensuring the tool node can locate the target node's data in both agentic and deterministic execution paths.
2.  **Referenced the leaf property `.binary.${binaryPropertyName}`:** Bypasses n8n's performance and security filters by requesting the lightweight file metadata directly instead of the restricted parent dictionary.
3.  **Removed the leading `=` from programmatic evaluation:** Excludes the leading `=` and preserves curly braces (e.g., `{{ $('Node Name').first().binary.data }}`), ensuring the expression engine evaluates the query as a single root expression and returns the raw JavaScript metadata object directly.

---

## 4. Codebase Modifications

### Runtime Logic Update

In `nodes/Adeu/GenericFunctions.ts`, the expression resolver was modified as follows:

```typescript
// FILE: nodes/Adeu/GenericFunctions.ts

// 1. Changed expression from `={{ $('Node').item.binary }}` to `{{ $('Node').first().binary.propertyName }}`
const escapedNodeName = sourceNodeName.replace(/'/g, "\\'");
const expression = `{{ $('${escapedNodeName}').first().binary.${binaryPropertyName} }}`;

let resolvedBinaryBag: IBinaryData | undefined;
try {
  resolvedBinaryBag = this.evaluateExpression(expression, itemIndex) as
    | IBinaryData
    | undefined;
} catch (err) {
  // ... error handling ...
}

// 2. Verified that the returned value is a raw object, bypassing stringified "[object Object]"
if (resolvedBinaryBag === undefined || resolvedBinaryBag === null || typeof resolvedBinaryBag !== "object") {
  throw new NodeApiError( ... );
}

const binaryData = resolvedBinaryBag;
```

### Unit Test Correction

In `test/Adeu.node.test.ts`, the test assertions were updated to verify that the expression evaluated during tool mock testing strictly adheres to the corrected programmatic format:

```typescript
// FILE: test/Adeu.node.test.ts

expect(
  mockExecuteFunctions.evaluateExpression as ReturnType<typeof vi.fn>,
).toHaveBeenCalledWith(
  "{{ $('Trigger').first().binary.data }}",
  expect.any(Number),
);
```

These changes maintain full backward compatibility with deterministic workflows while resolving the platform-level restrictions of accessing binary data across nodes inside AI Agent tool executions.