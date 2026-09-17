# Plan: Fix Underscore Runs and Inline Markdown Parsing in Python and Node Engines

**Goal:** Fix inline markdown parsing in both `adeu` (Python) and `@adeu/core` (TypeScript) so runs of underscores (such as `__________` fill-in-the-blank lines, `____`, `__`) and backslash-escaped formatting characters (`\_`, `\*`, `\**`) are preserved as literal text instead of being dropped as empty italic spans.

**Architecture:** Refactor `_parse_inline_markdown` in `python/src/adeu/redline/engine.py` and `node/packages/core/src/engine.ts` from naive non-greedy regexes (`(\*\*.*?\*\*)|(_.*?_)`) to a scanner-based delimiter matcher that:
1. Enforces non-empty, non-delimiter inner content for emphasis spans.
2. Distinguishes single delimiter tokens (`_`) from multi-character delimiter runs (`__`, `___`, `__________`) so underscore blanks are never treated as empty italic delimiters.
3. Adheres to CommonMark intra-word underscore rules (underscores surrounded by word characters cannot open or close emphasis, preserving identifiers like `foo_bar_baz` and `foo__bar`).
4. Recognizes backslash escapes (`\_`, `\*`, `\\`), stripping the escape character and preserving the target as literal text.
5. Leaves `_edit_declares_emphasis` and tracked insertion building (`_track_insert_inline` in Python, `_build_tracked_ins_for_line` in Node) fully intact, ensuring `<w:ins>` tags with `<w:r><w:t>__________</w:t></w:r>` are generated and survive into DOCX output.

**Tech stack:** Python 3.12 (`pytest`, `ruff`, `mypy`), TypeScript 7 / Node 22 (`vitest`, `tsup`, `eslint`). No new dependencies.

## Context

- Issue: GitHub issue #148 reported that fill-in blanks like `__________` disappear when inserted into documents because `_parse_inline_markdown` matches `__` pairs with empty inner content, returning `[]` and causing `_track_insert_inline` / `_build_tracked_ins_for_line` to omit `<w:ins>` entirely.
- Odd counts (e.g. `___`) drop $2n$ underscores and leave 1.
- Backslash escaping (`\_`, `\*`) is currently not supported.
- Verified toolchains and commands:
  - Python (`python/`):
    - Tests: `uv run pytest`
    - Lint: `uv run ruff check . && uv run ruff format --check .`
    - Typecheck: `uv run mypy src`
  - Node (`node/`):
    - Build: `npm run build`
    - Tests: `npm run test` (or `npm run test -- <pattern>` in `node/packages/core`)
    - Lint: `npm run lint`
- Affected files:
  - Python target: `python/src/adeu/redline/engine.py:1210-1258`
  - Node target: `node/packages/core/src/engine.ts:3026-3060`
  - Python tests: `python/tests/test_nested_markdown.py`
  - Node tests: `node/packages/core/src/engine.markdown_parsing.test.ts`

## Assumptions

- Delimiter runs of underscores (`__`, `____`, `__________`) without non-underscore content are literal text and never emphasis.
- Empty delimiter pairs (`****`, `__`) must not match as emphasis and must be preserved as literal text.
- Intra-word underscores (e.g. `foo_bar_baz`, `foo__bar`) do not trigger emphasis unless explicitly delimited around the word (e.g. `_foo_bar_`).
- Backslash escapes before markdown syntax (`\_`, `\*`, `\**`, `\\`) escape the delimiter so it is treated as literal text, removing the escaping backslash.
- Adeu strictly uses `**` for bold and `_` for italic per `AI_CONTEXT.md:219` (`*italic*` is parsed as literal text).
- Both engines must remain in exact behavioral parity.
- Commits are made directly to the default branch per logical task; no git push will be executed.

## Tasks

### Task 1: Python Engine Inline Markdown Parser Refactor & Direct Unit Tests
Status: DONE

- **Goal:** Refactor `RedlineEngine._parse_inline_markdown` in `python/src/adeu/redline/engine.py` to correctly preserve underscore runs, handle intra-word underscores, support backslash escaping, and reject empty delimiter pairs. Expand `python/tests/test_nested_markdown.py` to cover all cases.
- **Difficulty:** EASY
- **Verify:** `isolated` (`uv run pytest tests/test_nested_markdown.py` from `python/`)
- **Files:**
  - `python/src/adeu/redline/engine.py:1210-1258`
  - `python/tests/test_nested_markdown.py`
- **Details:**
  1. In `python/src/adeu/redline/engine.py`, replace the naive regex in `_parse_inline_markdown` with a delimiter scanner that preserves literal text:
     ```python
     @staticmethod
     def _is_escaped(text: str, pos: int) -> bool:
         """True if the character at pos is preceded by an odd number of backslashes."""
         count = 0
         p = pos - 1
         while p >= 0 and text[p] == "\\":
             count += 1
             p -= 1
         return count % 2 == 1

     @staticmethod
     def _unescape_markdown(text: str) -> str:
         """Replaces escaped markdown characters with literal form (e.g. \\_ -> _, \\* -> *, \\\\ -> \\)."""
         return re.sub(r"\\([\\*_])", r"\1", text)

     @staticmethod
     def _is_word_char(c: str) -> bool:
         return c.isalnum() or c == "_"

     def _find_next_emphasis(self, text: str) -> Optional[Tuple[int, int, str, str]]:
         """
         Finds the earliest valid emphasis delimiter pair in text.
         Returns (start_idx, end_idx, tag_type, inner_content) or None.
         tag_type is 'bold' or 'italic'.
         """
         n = len(text)
         i = 0
         while i < n:
             # Check for bold opening: **
             if i + 1 < n and text[i : i + 2] == "**" and not self._is_escaped(text, i):
                 if i + 2 < n and text[i + 2] != "*" and not text[i + 2].isspace():
                     j = i + 2
                     while j + 1 < n:
                         if text[j : j + 2] == "**" and not self._is_escaped(text, j):
                             if not text[j - 1].isspace():
                                 inner = text[i + 2 : j]
                                 if inner and not all(c == "*" for c in inner):
                                     return (i, j + 2, "bold", inner)
                             j += 2
                         else:
                             j += 1

             # Check for italic opening: _
             if text[i] == "_" and not self._is_escaped(text, i):
                 is_prev_underscore = i > 0 and text[i - 1] == "_"
                 is_next_underscore = i + 1 < n and text[i + 1] == "_"
                 is_prev_word = i > 0 and self._is_word_char(text[i - 1])
                 is_next_space = i + 1 == n or text[i + 1].isspace()

                 if (
                     (not is_prev_underscore)
                     and (not is_next_underscore)
                     and (not is_prev_word)
                     and (not is_next_space)
                 ):
                     j = i + 1
                     while j < n:
                         if text[j] == "_" and not self._is_escaped(text, j):
                             is_close_prev_space = text[j - 1].isspace()
                             is_close_prev_underscore = text[j - 1] == "_"
                             is_close_next_underscore = j + 1 < n and text[j + 1] == "_"
                             is_close_next_word = j + 1 < n and self._is_word_char(text[j + 1])

                             if (
                                 (not is_close_prev_space)
                                 and (not is_close_prev_underscore)
                                 and (not is_close_next_underscore)
                                 and (not is_close_next_word)
                             ):
                                 inner = text[i + 1 : j]
                                 if inner and not all(c == "_" for c in inner):
                                     return (i, j + 1, "italic", inner)
                         j += 1

             i += 1

         return None

     def _parse_inline_markdown(
         self, text: str, base_style: Optional[Dict[str, Any]] = None
     ) -> List[Tuple[str, Dict[str, Any]]]:
         """
         Recursively parses bold (**) and italic (_) markdown, preserving underscore runs
         and literal escaped characters.
         """
         if base_style is None:
             base_style = {}

         if not text:
             return []

         match = self._find_next_emphasis(text)
         if not match:
             return [(self._unescape_markdown(text), base_style)]

         start, end, tag_type, inner_content = match
         pre_text = text[:start]
         post_text = text[end:]

         results = []
         if pre_text:
             results.append((self._unescape_markdown(pre_text), base_style))

         new_style = base_style.copy()
         if tag_type == "bold":
             new_style["bold"] = True
         else:
             new_style["italic"] = True

         results.extend(self._parse_inline_markdown(inner_content, new_style))
         results.extend(self._parse_inline_markdown(post_text, base_style))

         return results
     ```
  2. In `python/tests/test_nested_markdown.py`, add unit tests verifying:
     - Even underscores: `__`, `____`, `__________` -> preserved with `{}`
     - Odd underscores: `___`, `_____` -> preserved with `{}`
     - Intra-word underscores: `foo_bar_baz` -> preserved with `{}`, `foo__bar` -> preserved with `{}`
     - Delimited text with underscores: `_foo_bar_` -> `("foo_bar", {"italic": True})`
     - Delimited bold with asterisks: `**bold**` -> `("bold", {"bold": True})`
     - Empty bold: `****` -> preserved with `{}`
     - Escaped underscores: `\_` -> `("_", {})`, `\_\_` -> `("__", {})`, `foo\_bar` -> `("foo_bar", {})`
     - Escaped asterisks: `\*` -> `("*", {})`, `\*\*` -> `("**", {})`, `\**` -> `("**", {})`
     - Escaped italic: `\_italic_` -> `("_italic_", {})`
     - Escaped inside italic: `_foo\_bar_` -> `("foo_bar", {"italic": True})`
     - Existing tests: `A _B **C** B_ A`, `_Start **Bold** End_`, `**Bold**_Italic_` continue to pass unchanged.
- **Done when:** `uv run pytest tests/test_nested_markdown.py` passes all test cases with 0 failures.

### Task 2: Python End-to-End Tracked Insertion Verification (`<w:ins>`)
Status: DONE

- **Goal:** Verify that inserting fill-in blanks like `__________` into a DOCX document using `RedlineEngine.process_batch` generates real `<w:ins>` tags with literal runs and survives into the saved document.
- **Difficulty:** EASY
- **Verify:** `isolated` (`uv run pytest tests/test_nested_markdown.py` from `python/`)
- **Files:**
  - `python/tests/test_nested_markdown.py`
- **Details:**
  1. Add an end-to-end test `test_tracked_insert_underscore_run_survives()` in `python/tests/test_nested_markdown.py`:
     ```python
     def test_tracked_insert_underscore_run_survives():
         doc = Document()
         doc.add_paragraph("Name: [blank]")
         stream = BytesIO()
         doc.save(stream)
         stream.seek(0)
         engine = RedlineEngine(stream)

         from adeu.models import ModifyText
         stats = engine.process_batch([
             ModifyText(type="modify", target_text="Name: [blank]", new_text="Name: __________")
         ])
         assert stats.applied == 1

         # Verify XML structure has w:ins containing run with __________
         out_doc = Document(stream)
         p = out_doc.paragraphs[0]
         xml_str = p._element.xml
         assert "w:ins" in xml_str
         assert "__________" in xml_str

         # Verify clean text extraction includes __________
         from adeu.ingest import extract_text_from_stream
         stream.seek(0)
         clean_text = extract_text_from_stream(stream, clean_view=True)
         assert "Name: __________" in clean_text
     ```
  2. Add test `test_tracked_insert_escaped_characters_survive()` verifying `foo\_bar` inserts `foo_bar`.
- **Done when:** `uv run pytest tests/test_nested_markdown.py` passes all unit and integration tests.

### Task 3: Node Engine Inline Markdown Parser Refactor & Unit Tests
Status: IN_PROGRESS (attempt 3 - escalated to @opus-coder)
Failed-cycles: 2
Attempt ledger:
- attempt 1: regex replacement with ASCII \w and \s checks -> FAIL: Unicode word characters and whitespace parity discrepancy between JS /\w//\s/ and Python str.isalnum()/str.isspace()
- attempt 2: Unicode-aware word character regex /[\p{L}\p{N}_]/u and Python-matching _is_space helper -> FAIL on final verification: JS UTF-16 code units vs Python code points for astral plane characters (surrogate pairs like \u{20000}_foo_ and _foo_\u{1D7CE}) where lone surrogate is checked instead of full code point
- attempt 3: Make neighbour lookarounds code-point aware in TypeScript (handling surrogate pairs before and after delimiter), test astral plane parity across both engines

- **Goal:** Refactor `RedlineEngine._parse_inline_markdown` in `node/packages/core/src/engine.ts` to achieve 100% parity with Python, correctly preserving underscore runs, handling intra-word underscores, and supporting backslash escapes. Create `node/packages/core/src/engine.markdown_parsing.test.ts` with comprehensive unit and integration tests.
- **Difficulty:** EASY
- **Verify:** `isolated` (`npm run test -- engine.markdown_parsing.test.ts` from `node/packages/core`)
- **Files:**
  - `node/packages/core/src/engine.ts:3026-3060`
  - `node/packages/core/src/engine.markdown_parsing.test.ts`
- **Details:**
  1. In `node/packages/core/src/engine.ts`, replace the naive regex in `_parse_inline_markdown` with helper methods:
     ```typescript
     private _is_escaped(text: string, pos: number): boolean {
       let count = 0;
       let p = pos - 1;
       while (p >= 0 && text[p] === "\\") {
         count++;
         p--;
       }
       return count % 2 === 1;
     }

     private _unescape_markdown(text: string): string {
       return text.replace(/\\([\\*_])/g, "$1");
     }

     private _is_word_char(c: string): boolean {
       return /\w/.test(c);
     }

     private _find_next_emphasis(
       text: string,
     ): [number, number, "bold" | "italic", string] | null {
       const n = text.length;
       let i = 0;
       while (i < n) {
         // Check for bold opening: **
         if (
           i + 1 < n &&
           text.substring(i, i + 2) === "**" &&
           !this._is_escaped(text, i)
         ) {
           if (i + 2 < n && text[i + 2] !== "*" && !/\s/.test(text[i + 2])) {
             let j = i + 2;
             while (j + 1 < n) {
               if (
                 text.substring(j, j + 2) === "**" &&
                 !this._is_escaped(text, j)
               ) {
                 if (!/\s/.test(text[j - 1])) {
                   const inner = text.substring(i + 2, j);
                   if (inner && !/^[*]+$/.test(inner)) {
                     return [i, j + 2, "bold", inner];
                   }
                 }
                 j += 2;
               } else {
                 j++;
               }
             }
           }
         }

         // Check for italic opening: _
         if (text[i] === "_" && !this._is_escaped(text, i)) {
           const isPrevUnderscore = i > 0 && text[i - 1] === "_";
           const isNextUnderscore = i + 1 < n && text[i + 1] === "_";
           const isPrevWord = i > 0 && this._is_word_char(text[i - 1]);
           const isNextSpace = i + 1 === n || /\s/.test(text[i + 1]);

           if (
             !isPrevUnderscore &&
             !isNextUnderscore &&
             !isPrevWord &&
             !isNextSpace
           ) {
             let j = i + 1;
             while (j < n) {
               if (text[j] === "_" && !this._is_escaped(text, j)) {
                 const isClosePrevSpace = /\s/.test(text[j - 1]);
                 const isClosePrevUnderscore = text[j - 1] === "_";
                 const isCloseNextUnderscore = j + 1 < n && text[j + 1] === "_";
                 const isCloseNextWord =
                   j + 1 < n && this._is_word_char(text[j + 1]);

                 if (
                   !isClosePrevSpace &&
                   !isClosePrevUnderscore &&
                   !isCloseNextUnderscore &&
                   !isCloseNextWord
                 ) {
                   const inner = text.substring(i + 1, j);
                   if (inner && !/^[_]+$/.test(inner)) {
                     return [i, j + 1, "italic", inner];
                   }
                 }
               }
               j++;
             }
           }
         }

         i++;
       }

       return null;
     }

     private _parse_inline_markdown(
       text: string,
       baseStyle: any = {},
     ): [string, any][] {
       if (!text) return [];

       const match = this._find_next_emphasis(text);
       if (!match) {
         return [[this._unescape_markdown(text), baseStyle]];
       }

       const [start, end, tagType, innerContent] = match;
       const preText = text.substring(0, start);
       const postText = text.substring(end);

       const results: [string, any][] = [];
       if (preText) {
         results.push([this._unescape_markdown(preText), baseStyle]);
       }

       const newStyle = { ...baseStyle };
       if (tagType === "bold") {
         newStyle.bold = true;
       } else {
         newStyle.italic = true;
       }

       results.push(...this._parse_inline_markdown(innerContent, newStyle));
       results.push(...this._parse_inline_markdown(postText, baseStyle));

       return results;
     }
     ```
  2. Create `node/packages/core/src/engine.markdown_parsing.test.ts` testing:
     - Direct `_parse_inline_markdown` calls (via `(engine as any)._parse_inline_markdown(text)`):
       - Even underscores: `__`, `____`, `__________`
       - Odd underscores: `___`, `_____`
       - Intra-word underscores: `foo_bar_baz`, `foo__bar`
       - Delimited underscores: `_foo_bar_` -> `["foo_bar", { italic: true }]`
       - Escapes: `\_`, `\_\_`, `foo\_bar`, `\*`, `\*\*`, `\**`, `\_italic_`, `_foo\_bar_`
       - Empty delimiters: `****`
       - Nested / complex formatting: `A _B **C** B_ A`, `_Start **Bold** End_`, `**Bold**_Italic_`
     - End-to-end tracked insertions:
       - Apply edit `{ type: "modify", target_text: "Name: [blank]", new_text: "Name: __________" }`
       - Verify CriticMarkup output contains `{++Name: __________++}`
       - Verify accepted document output contains `"Name: __________"`.
- **Done when:** `npm run test -- engine.markdown_parsing.test.ts` passes with 0 failures.

### Task 4: Full Suite Parity, Build & Lint Verification (Both Python & Node)
Status: DONE

- **Goal:** Verify that the full Python and Node test suites pass with zero regressions, and that all linting and typechecking pass cleanly.
- **Difficulty:** EASY
- **Verify:** `full`
  - Python: `uv run pytest` && `uv run ruff check .` && `uv run ruff format --check .` && `uv run mypy src` (from `python/`)
  - Node: `npm run build` && `npm run test` && `npm run lint` (from `node/`)
- **Files:** No new code files (verification task).
- **Details:**
  1. In `python/`, run:
     - `uv run pytest` (all tests pass)
     - `uv run ruff check .` (0 errors)
     - `uv run ruff format --check .` (clean formatting)
     - `uv run mypy src` (clean type checks)
  2. In `node/`, run:
     - `npm run build` (build succeeds, bundles verify)
     - `npm run test` (all workspace tests pass)
     - `npm run lint` (all workspaces clean)
- **Done when:** All verification commands exit 0 with no errors.

## Risks & Mitigations

- **Risk:** Backslash escaping might inadvertently alter intended literal backslashes if not constrained.
  - **Mitigation:** Only backslashes immediately preceding markdown formatting characters (`_`, `*`, `\`) are treated as escape sequences via `r"\\([\\*_])"`. Other backslashes remain literal.
- **Risk:** Worst-case performance on long unstructured strings with unmatched delimiters.
  - **Mitigation:** Delimiter matching is linear per character scan without backtracking or nested regexes. Runtime is bounded by string length.
- **Risk:** Cross-engine drift between TypeScript and Python implementations.
  - **Mitigation:** Both engines use an identical scanner algorithm, identical word character boundary conditions, identical escape rules, and mirrored unit test suites.

PLAN COMPLETE
