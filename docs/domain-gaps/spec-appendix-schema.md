# Appendix Schema & Data Extraction Blueprint

This document defines the heuristics and XML crawl strategies for generating the read-only "Structural Appendix". It acts as a deterministic "Language Server (LSP)" hover panel for the LLM.

## 1. Symbol Table & Code Diagnostics (Defined Terms)

Finding defined terms is treated as building a PL "Symbol Table" or index.

### Extraction Strategy:
1.  **Declaration Parsing (Strong Verbs & Glossary):** 
    *   Scan for paragraphs containing patterns like `"Term"` or `“Term”` followed by `means` or `shall mean`.
    *   Extract the *value* of the definition (the text following the term). 
    *   *Truncation Rule:* If the definition spans more than 400 characters or multiple list items, truncate and report its location (e.g., `[Located in Section 1.1]`) to save LLM context window space.
2.  **Inline Declaration Parsing:**
    *   Scan for parentheticals containing capitalized terms wrapped in quotes, e.g., `(the "Term")` or `(hereinafter, the "Term")`.
3.  **Static Analysis (The Linter):**
    *   Execute a global scan of the plain-text projection to map Usages against Declarations.
    *   **Validation Rule:** A term *must be used* to be considered a term. If usage count is 0, it is discarded from the index to prevent indexing phantom examples or dead code.

### Diagnostic Rules:
| Code | Trigger Condition | Diagnostic Output |
| :--- | :--- | :--- |
| **Duplicate** | Multiple Glossary matches found for the exact same Term. | `[Error] Duplicate Definition: 'Term' is defined multiple times.` |
| **Typo** | Text contains a capitalized phrase within a Levenshtein distance of 1-2 from a valid Term. | `[Info] Possible Typo: Found 'Materal'. Did you mean 'Material'?` |

## 2. Bookmarks & Back-References (Deterministic)

We must map the exact relationship between anchors and pointers.

### Extraction Strategy:
1.  **First Pass (Anchors):**
    *   Scan the document for `<w:bookmarkStart w:name="X">`. 
    *   Exclude internal noise bookmarks (e.g., names starting with `_GoBack` or `_MailAutoSig`).
    *   Record the text of the paragraph containing the bookmark (truncated to 60 chars) as the "Anchored to" text.
2.  **Second Pass (Pointers):**
    *   Scan the document for field codes: `<w:fldSimple w:instr="REF X">` or `<w:instrText>REF X</w:instrText>`.
    *   Record the text of the paragraph *containing* the reference as the "Referenced from" text.
3.  **Resolution:**
    *   Group by Bookmark ID.
    *   *Output:*
        ```
        - {Bookmark ID} → Anchored to: "{Heading/Para Text}"
          - Referenced from: "{Usage Para 1}", "{Usage Para 2}"
        ```

## 3. Table of Contents / Table of Authorities Boundaries

To prevent the LLM from attempting to edit auto-generated TOC text, we must collapse the entire block into a single `[~Table of Contents — N entries~]` token.

### Extraction Strategy:
Word TOCs are built using Field Codes, typically wrapped in an `sdt` (Structured Document Tag) but occasionally left bare.

1.  **Block Start Detection:**
    *   Scan for `<w:fldChar w:fldCharType="begin">` immediately followed by `<w:instrText>TOC ...</w:instrText>`.
    *   OR scan for `<w:sdt>` containing `<w:docPartGallery w:val="Table of Contents"/>`.
2.  **Entry Counting:**
    *   Count the number of `w:p` tags containing `w:hyperlink` or `PAGEREF` fields within the block.
3.  **Block End Detection:**
    *   Continue consuming paragraphs until `<w:fldChar w:fldCharType="end">` is reached for the TOC field, OR the closing `</w:sdt>` is reached.
4.  **Mapper Rule:**
    *   The `DocumentMapper` registers the *entire block of XML* as a single Virtual Span corresponding to the placeholder text. The real text inside the TOC is never projected to the LLM.