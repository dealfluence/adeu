---
name: adeu-redlining
description: Use this skill for reviewing, editing, or negotiating existing Word documents (.docx) where "Track Changes" or precise redlining is required. Use it to propose edits, accept/reject changes, or reply to comments. Do NOT use for creating new blank documents from scratch (use docx skill for that).
license: MIT (See LICENSE.txt)
---

# Adeu: Professional Redlining & Review

## Overview
Adeu acts as a "Virtual DOM" for DOCX files. It allows you to read documents as Markdown, propose edits, and apply them as native XML `w:ins` (insertions) and `w:del` (deletions) without breaking document structure.

## Capabilities
1.  **Extract**: Read text with high fidelity (preserving comments and structural headers).
2.  **Diff**: Compare two DOCX files to see what changed.
3.  **Edit**: Apply search-and-replace edits that result in Track Changes.
4.  **Review**: Accept/Reject existing changes or Reply to comments.

## Execution Model
This skill relies on the `adeu` CLI tool. Always execute via `uvx` to ensure the environment is correct.

```bash
uvx adeu --version