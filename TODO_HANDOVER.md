# 🛑 Handover: Invisible Modern Comments Bug

**Date:** January 22, 2026
**Status:** Critical / In Progress
**Component:** `src/adeu/redline/comments.py`

## 🐛 The Issue
When `Adeu` adds a reply to a document that *already* contains Modern Comments (threaded), the new reply is **invisible** in Microsoft Word, even though the text appears in `read_docx` extraction.

## 🔍 Root Cause Analysis
The issue is **XML Part Duplication** caused by a failure to detect existing auxiliary parts.

1.  **Word's Structure**: Modern Word docs use a triad of files for comments:
    *   `word/comments.xml` (Content)
    *   `word/commentsIds.xml` (Durable IDs)
    *   `word/commentsExtensible.xml` (Metadata/Dates)
2.  **The Failure**: When loading a document, `python-docx` (or our wrapper) fails to match the existing `commentsIds.xml` because Word uses a **versioned Relationship Type** (e.g., `.../2016/09/relationships/commentsIds`) instead of the standard base URI.
3.  **The Consequence**:
    *   Adeu assumes the part doesn't exist.
    *   Adeu creates a **new** part: `word/commentsIds1.xml`.
    *   Adeu writes the new comment's ID to this new file.
    *   **Result**: The `.docx` ZIP now contains *both* files and *two* relationships in `_rels/document.xml.rels`.
    *   **Word's Behavior**: Word loads the original `commentsIds.xml` (linked via `rId8`), finds no ID for the new comment, and treats the comment as corrupt/hidden.

## 🛠️ Attempts & Fixes Applied So Far
*   ✅ **Fixed Date Formatting**: Switched to strict UTC ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`) without microseconds.
*   ✅ **Fixed Namespace**: Forced `w14` and `w15` declarations onto the root `<w:comments>` element.
*   ✅ **Fixed Structure**: Added mandatory `<w:annotationRef/>` and `<w:pStyle w:val="CommentText"/>` to generated comments.
*   ❌ **Partial Fix**: Attempted to check a list of `rel_types` (versioned and unversioned), but the logic still fell through to "Create New" in some cases.

## 📋 Next Steps (The Fix)
The `_get_or_create_part` method in `comments.py` must be refactored to stop trusting `rel.reltype` string matching and instead trust the **Content Type** of the parts in the package.

**Algorithm to Implement:**
1.  Iterate `self.doc.part.package.parts` (the raw ZIP contents).
2.  Look for any part where `.content_type` matches the target (e.g., `application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml`).
3.  **If Found**:
    *   Check if a Relationship already exists for it.
    *   If yes, use that Relationship (upgrade the part to `XmlPart` if needed).
    *   If no (orphan part), create a new Relationship pointing to this *existing* part.
4.  **If Not Found**:
    *   Only *then* create a new Part.

## 🧪 Verification
1.  Run `python debug_rels.py path/to/doc.docx`.
2.  **FAIL**: If you see `commentsIds.xml` AND `commentsIds1.xml`.
3.  **PASS**: If you see only `commentsIds.xml`, and new content is appended to it.