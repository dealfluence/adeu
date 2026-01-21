# Feature Specification: DOCX Comment Representation (CriticMarkup)

## 1. Objective
To expose Microsoft Word Comments (Instructions, Feedback, Negotiations) to LLMs in a text-only representation that:
1.  Preserves the **exact scope** of the comment (which words are targeted).
2.  Preserves **metadata** (Author, Date, Threading).
3.  Is **unambiguous** to parse (does not collide with legal syntax like `[Date]`).
4.  Allows the `RedlineEngine` to map edits back to the original DOCX without corrupting the comment XML structure.

## 2. The Format: Extended CriticMarkup
We utilize the **CriticMarkup** syntax for highlighting and commenting.

### 2.1 Basic Syntax
```text
{==Target Text==}{>>Metadata: Comment Body<<}
```
*   `{== ... ==}`: The "Highlight" (Scope of the comment).
*   `{>> ... <<}`: The "Comment" (Metadata and Content).

### 2.2 Metadata Schema
The content inside `{>> ... <<}` follows a structured format to support multiple attributes and threads.

**Single Comment:**
```text
{>>[Author Name @ YYYY-MM-DD] Comment text...<<}
```

**Threaded/Multiple Comments:**
If multiple comments target the same range (or we flatten overlapping ranges), we separate them with newlines:
```text
{>>
[Author A @ Date] Initial comment.
[Author B @ Date] Reply or second comment.
<<}
```

**Resolved Comments:**
If a comment is marked "Resolved" in Word (`w15:done="1"`), we append `[RESOLVED]` to the header.
```text
{>>[Author Name @ Date (RESOLVED)] Comment text...<<}
```

## 3. Handling Complex Scenarios

### 3.1 Overlapping Ranges
Word allows arbitrary overlaps (Comment A on words 1-3, Comment B on words 2-4). Text cannot overlap. We **flatten** overlaps by breaking the text into segments.

**Input (Conceptual):**
`[Word1 (Word2] Word3)`
*   Comment A: Word1, Word2
*   Comment B: Word2, Word3

**Output (CriticMarkup):**
```text
{==Word1 ==}{>>[A] ...<<}{==Word2==}{>>[A] ...
[B] ...<<}{== Word3==}{>>[B] ...<<}
```
*   Segment 1 (`Word1`): Only Comment A.
*   Segment 2 (`Word2`): Both Comment A and B.
*   Segment 3 (`Word3`): Only Comment B.

### 3.2 Paragraph Spanning
If a comment spans multiple paragraphs, the CriticMarkup syntax wraps the newlines.
```text
{==Paragraph 1 text.

Paragraph 2 text.==}{>>[Author] Comment spanning both.<<}
```

### 3.3 Empty Ranges (Point Comments)
Comments anchored to a single cursor position (length 0) are rendered with a zero-width marker or empty highlight.
```text
Text ends here.{== ==}{>>[Author] Missing period.<<}
```

## 4. Implementation Details

### 4.1 Ingestion (`ingest.py`)
*   **Iterator**: The `iter_paragraph_content` utility yields a linear stream of `Run` objects and `CommentEvent` (Start/End).
*   **State Machine**: A stack-based processor tracks active comment IDs.
    *   When the set of active IDs changes (start/end event), it closes the current `{==...==}` block, appends the `{>>...<<}` block for the *previous* state, and starts a new `{==...==}` block for the *new* state.

### 4.2 Mapping (`mapper.py`)
*   **Virtual Text**: The symbols `{==`, `==}`, `{>>`, `<<}`, and the comment body are treated as **Virtual Text**.
*   **Matching**: The `DocumentMapper` ignores these tokens when calculating XML indices.
*   **Patching**: The `RedlineEngine` applies edits to the *Real Text*. It does not attempt to modify the comments themselves (unless the user explicitly deletes the text range containing the comment, in which case Word handles the comment deletion naturally).

## 5. Examples

**Simple Negotiation:**
```text
The Vendor shall be liable for {==indirect damages==}{>>[Opposing Counsel] We request this be removed.<<} arising from...
```

**Legal Placeholder Collision:**
```text
The {==[Vendor/Supplier]==}{>>[Internal] Define who this is.<<} shall pay...
```

**Punctuation Precision:**
```text
Term: {==5 years.==}{>>[Client] Change to 3.<<}
```
