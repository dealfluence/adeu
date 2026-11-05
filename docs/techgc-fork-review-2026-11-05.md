# TechGC / The Suite fork review (2026-11-05)

What the production fork `TechGC/adeu` changed, why, and which of those changes upstream `dealfluence/adeu` is missing. Each verdict below rests on probe tests that I ran against the current upstream engines (Python and Node), not on reading the code alone.

## TL;DR

- The fork is Python-only. It is pinned to **v1.5.2 (2026-05-05)**, 906 upstream commits ago. It carries **15 production commits** (tags `1.5.2-thesuite.1` … `.14`), plus one unmerged refactor branch.
- About half of its fixes are **already in upstream, usually in a better form**: word-level redline splitting, the underscore-blank hang, matches inside pending deletions, duplicated text on reject-all, and per-edit batch reports.
- **Upstream lacks these, and should take them** (all reproduced on current `main`):
  1. **HIGH: Python crash when an existing `comments.xml` declares extra namespaces** (LLO-1694). A comment on a document whose existing comment holds a hyperlink fails with `XMLSyntaxError: Namespace prefix r ... is not defined`. The same code path also drops existing `mc:Ignorable` prefixes.
  2. **HIGH: anchors that cross a hyperlink never resolve** (LLO-924). The anchor `"...at legal@adeu.com before..."` returns "Target text not found" in both engines.
  3. **MEDIUM-HIGH: anchors that cross a Word cross-reference field never resolve** (LLO-910). Same failure in both engines.
  4. **MEDIUM: a punctuation-only change strikes the whole word** (LLO-970). `Cause.` → `Cause;` deletes `Cause.`. This hits commented edits and targets that are a single token, in both engines.
  5. **MEDIUM (feature): several comments on one edit, each with its own author** (LLO-1552).
  6. **LOW-MEDIUM: the comment on paragraphs added after the target covers only the last paragraph** (LLO-1580). This is a Python-only bug; Node already anchors it correctly.
  7. **LOW: per-edit reports carry no caller `index`** (LLO-1251 follow-up).
- **Your call, not a defect:**
  - Making the MCP dependencies an optional extra (TES-457). It works technically, but it breaks `uvx --from adeu adeu-server` and `langchain-adeu`.
  - Amending your own pending insertion in place (LLO-798). Upstream is safe; the fork's form is closer to how Word behaves.
  - Splitting edits that carry a comment (LLO-837/922/931). Upstream deliberately keeps them atomic.
- **Incidental upstream bug found during verification (not from the fork):** an edit that preserves a cross-reference **writes the projection markup `[~2.1~](#_Ref123)` into the document as literal text and deletes the field result**. Both engines do this. It contradicts the contract in `AI_CONTEXT.md:226`.

## 1. Method

| Step | Command / source | Result |
|---|---|---|
| Update local repo | `git fetch origin --prune` then `git merge --no-edit origin/main` | Local `main` was 31 commits behind. Merged as `eedbeec`. A stale local `python/uv.lock` edit (ooxml-integrity 0.5.0, superseded upstream by 0.8.1) was stashed as `stash@{0}` first. |
| Fetch fork | `git fetch https://github.com/TechGC/adeu.git "+refs/heads/*:refs/remotes/techgc/*"` | 10 branches |
| Fork metadata | `gh api repos/TechGC/adeu`, `/pulls?state=all`, `/tags` | Parent `dealfluence/adeu`, pushed 2026-11-04T20:11:44Z, tags `1.5.2-thesuite.1…14`, 3 internal PRs |
| Per-commit diffs | `git show <sha>` → `tmp/techgc/NN_<sha>.diff` | 21 commits read in full |
| Python probes | `uv run --project python pytest tmp/techgc_probe -p no:cacheprovider -q -rA -s` | Round 1: **15 failed / 40 passed** (`tmp/techgc_probe/run1.txt`). Round 2 (`test_probe2.py`): 5 failed / 8 passed. Round 3 (`test_probe3.py`): diagnostic, 2 passed. |
| Node probes | `npx vitest run src/zz_techgc_probe.test.ts` (in `node/packages/core`) on fixtures written by the Python probes | 1 passed (diagnostic). Output in `tmp/techgc_probe/node_probe.json` |
| Dependency check | Import `adeu`, `adeu.redline.engine`, `adeu.cli` with `fastmcp`/`mcp`/`jinja2`/`keyring` blocked in `sys.modules` | `core OK`, `cli OK` |

Each probe asserts the behaviour the fork commit introduced. A failing probe therefore means upstream does not do what the fork does. In most cases that is a gap; where upstream made a deliberate choice, this report says so.

## 2. Fork topology

All branches fork from `1e35c45` ("v 1.5.2", 2026-05-05). They form one linear stack; each `llo-*` branch is a snapshot of the stack at a release tag:

```
1e35c45 v1.5.2
 ├─ 086b412 thesuite.1  amend same-author <w:ins> in place          (llo-798-fix-multi-author-apply)
 ├─ f889038 thesuite.2  surgical split (SequenceMatcher)            (llo-798…; base of refactor-llo-837-for-upstream)
 ├─ 0d3d083 thesuite.3  LLO-910 matcher over cross-ref fields       (fix/markup-tolerant-matcher-thesuite.3)
 ├─ 187a4a1 thesuite.4  LLO-922 split on shared-block size
 ├─ 6d6c173 thesuite.4.1 LLO-929 contiguity guard (no word soup)
 ├─ 335dbaa thesuite.5  LLO-924 matcher over bold + hyperlink
 ├─ 8393ca8 thesuite.6  LLO-931 multi-large-block split             (fix/llo-context-collapse-thesuite.4)
 ├─ 118bf7a thesuite.7  LLO-970 punctuation-only trim
 ├─ 801d977 thesuite.8  LLO-1170 underscore-blank infinite loop
 ├─ 11016d0 thesuite.9  LLO-1251 per-edit outcomes
 ├─ 99b86c0 thesuite.10 LLO-1251 index against caller's list
 ├─ 5e4a152 thesuite.11 TES-457 MCP deps optional                   (tes-457-optional-mcp-deps, llo-1251-edit-results)
 ├─ 3d58eb4 thesuite.12 LLO-1552 list of comments w/ authors        (llo-1552-multi-comment)
 ├─ 9cc8b1f thesuite.13 LLO-1580 comment on added paragraphs        (llo-1580-comment-on-leading-newline-insert)
 └─ 309fc4c thesuite.14 LLO-1694 keep comments-root namespaces      (llo-1694-keep-comments-root-namespaces = tip)

refactor-llo-837-for-upstream (open internal PR #1): 6 commits that extract f889038 into
`_try_surgical_split` with tests, as prep for an upstream PR that was never opened.
techgc/main = upstream 210db88 (2026-05-18), no fork commits.
```

The fork's motivation is The Suite's contract-redlining agent ("Lloyd"), which runs multi-round, multi-party negotiation ("redline-on-redline"). A downstream "legal-substance verifier" rejects outputs whose reject-all state duplicates text, and the team measures "restate ratio", the share of an edit that is deleted and re-typed unchanged. Commit messages cite replay numbers from production; for example, 8393ca8 reports aggregate restate falling from 10.4% to 6.4% over 288 production edits.

No upstream PRs or issues come from the fork's authors (`gh pr list -R dealfluence/adeu --search author:jairneto-thesuite` → `[]`).

## 3. Verdict per change

### Already in upstream, or upstream is better: leave as is

| Fork change | Upstream today | Evidence |
|---|---|---|
| **086b412 (part 1)**: a deletion inside a pending `<w:ins>` emitted a sibling `<w:del>`; reject-all then restored it as duplicated text | `track_delete_run` now replaces the run in place, so the `<w:del>` nests inside the enclosing `<w:ins>` and rejecting the insertion removes both (`python/src/adeu/redline/engine.py:2158-2171`) | Probes `test_086b412_same_author_…_reject_all…` and `…other_author…_reject_all…` **pass**: reject-all gives `'Start '`, accept-all gives `'Start Round2'` |
| **086b412 (part 2)**: resolve against the Clean View first so anchors never land in pending `<w:del>` text | Raw matching skips occurrences inside deletions (`_first_live_index` / `_range_in_deletion`, `mapper.py:1431-1481`). An anchor that matches only deleted text gets an explicit error ("matches text inside a tracked deletion by X…") | `test_clean_view_first_concern_deleted_and_live_occurrence` **passes**: the anchor lands on the live copy, and accept/reject texts are exact |
| **f889038 / 187a4a1 / 6d6c173 / 8393ca8 + refactor branch**: character-level `SequenceMatcher` splits a long, over-anchored modification into small sub-edits, gated by thresholds and guards | Every modification **without a comment** is word-diffed into minimal sub-edits (`_word_diff_sub_edits` → `generate_edits_from_text`, `engine.py:5039-5148`). Splitting on word tokens rules out the character-level "word soup" the fork had to guard against | Passing: clause restate under 100 chars (only `written ` and `, conditioned, or delayed` inserted); multi-large-block (only `defend, `, `liabilities, `, ` or relating to` inserted); genuine rewrite stays one replacement. The scattered rewrite gives 3 readable word-level hunks (`externally`, `communicate about`, `on a need-to-know basis and`). That fails the fork's own `<=2` cap but is not soup. |
| **801d977**: `trim_common_context` looped forever on odd-length `___` blanks | `_count_standalone_underscores` ignores underscores that belong to a run (`diff.py:17-36`), so a blank never reads as an unbalanced italic marker | `test_801d977_underscore_runs_terminate[1..27]` (shared prefix, shared suffix, inside a link) and the end-to-end signature-block case **pass** within the 5 s budget |
| **11016d0 / 99b86c0**: `edit_results` with one entry per caller edit, `applied/partial/skipped`, numbered against the caller's change list | `process_batch` returns `edits` (one report per edit: status, error, warning, previews, pages, heading path, `occurrences_modified`) and `failed` keyed by the **caller's** index. Batches are sequential and transactional by default (`engine.py:3556-3730`). | `test_11016d0_per_edit_reports_one_per_input_edit` and `test_11016d0_failed_index_names_the_casualty_in_partial_mode` **pass** (`failed: [{index: 1, …}]`). One small residual gap is in §4.7. |

### Upstream lacks these: recommended (details in §4)

| # | Fork change | Severity | Python | Node |
|---|---|---|---|---|
| 4.1 | 309fc4c LLO-1694: keep namespaces when patching the comments root | **HIGH** (crash) | Crash + drops Ignorable | Keeps declarations; never adds or merges `mc:Ignorable` |
| 4.2 | 335dbaa LLO-924: anchor across hyperlink (and bold) | **HIGH** | Not found | Not found |
| 4.3 | 0d3d083 LLO-910: anchor across cross-reference fields | **MEDIUM-HIGH** | Not found | Not found |
| 4.4 | 118bf7a LLO-970: trim punctuation-only changes | **MEDIUM** | Whole word restated | Whole word restated |
| 4.5 | 3d58eb4 LLO-1552: list of comments with per-comment authors | **MEDIUM** (feature) | Rejected by pydantic | Crashes with `TypeError` |
| 4.6 | 9cc8b1f LLO-1580: comment covers every added paragraph | **LOW-MEDIUM** | Last paragraph only | Correct |
| 4.7 | 99b86c0: caller index on each report | **LOW** | No `index` key | not probed |

### Your decision: trade-offs (details in §5)

| Fork change | Upstream position |
|---|---|
| 5e4a152 TES-457: `fastmcp`/`jinja2`/`keyring` become an `mcp` extra | Mandatory dependencies. Making them optional is feasible but breaking. |
| 086b412 (amend semantics): a same-author edit of one's own pending insertion leaves no deletion mark | Upstream nests a `<w:del>` in your own `<w:ins>`. Safe, but it shows `{--Round1--}{++Round2++}`. |
| Surgical split **with a comment** | Upstream keeps commented edits atomic on purpose (`4e3655f`, "CLI QA 2026-07-22 bug #1"), so a commented clause restate deletes and re-inserts the whole changed span |

## 4. Gaps worth closing

### 4.1 Comments root drops namespace declarations (crash). Fork 309fc4c, LLO-1694

**Fork rationale:** `_ensure_namespaces` rebuilds the `<w:comments …>` start tag from a fixed list with a regex. That discards every other declaration on the root. If an existing comment uses one of the discarded prefixes (`r:id` on a hyperlink), re-parsing fails, so every redline on that document crashes. The same rebuild overwrites any existing `mc:Ignorable` value.

**Upstream state:** Python still has the pre-fork code (`python/src/adeu/redline/comments.py:350-396`). Node's `_ensureNamespaces` (`node/packages/core/src/comments.ts:240-263`) only adds missing `xmlns:*` attributes. It keeps `r`, but it never writes or merges `mc:Ignorable`.

**Reproduced:**
- Python, `test_309fc4c_existing_hyperlinked_comment_keeps_r_namespace` → `lxml.etree.XMLSyntaxError: Namespace prefix r for id on hyperlink is not defined`.
- Python, `test_309fc4c_existing_ignorable_prefixes_survive` → `mc:Ignorable` comes out as `['w14','w15','w16cid','w16cex']`; the `w16se` the document already had is gone.
- Node, `comments_plain_root` → the root ends up with no `mc:Ignorable` at all. With `comments_w16se` it stays `mc:Ignorable="w16se"`, without `w14 w15`. Python, by contrast, writes `mc:Ignorable="w14 w15 w16cid w16cex"` (`test_python_comments_plain_root_gets_ignorable`).
- Rebinding a foreign `w14` URI already works in Python (`test_309fc4c_w14_bound_to_other_uri_is_rebound` passes).

**Why it matters:** Counterparty documents with comments are the normal input in negotiation workflows, and Word puts hyperlinks in comment bodies. On the Python engine, one such document makes every commented edit fail.

**Port:** Take the fork's approach in Python: rebuild the root with lxml from `{**element.nsmap, **missing}`, deep-copy the children, and merge `mc:Ignorable` as `dict.fromkeys([*existing, "w14","w15","w16cid","w16cex"])`. In Node, add the same Ignorable merge for parity. The fork's 4 tests in `tests/test_comments.py` (commit 309fc4c) port almost unchanged.

### 4.2 Anchors that cross a hyperlink. Fork 335dbaa, LLO-924

**Fork rationale:** The extractor and the LLM anchor on clean text. The matcher's haystack renders links as `[label](url)` and bold as `**x**`, so a plain anchor that crosses a hyperlinked email fails.

**Upstream state:** The matcher has a "plain projection" rung, but it strips only virtual bold/italic markers (`_get_plain_projection`, `python/src/adeu/redline/mapper.py:1383-1429`). Link brackets and URLs stay in.

**Reproduced:**
- `full_text = 'Email the team at [**legal@adeu.com**](mailto:legal@adeu.com) before Friday.'`. The anchor `"Email the team at legal@adeu.com before Friday."` gives `Target text not found` in Python and Node.
- The same failure occurs for a plain, non-bold hyperlink (`test_hyperlink_anchor_plain_link_not_bold`).
- Copying the markup verbatim works (`test_hyperlink_anchor_when_llm_copies_markup` passes), as does an anchor that stops before the link.

**Why it matters:** Emails, URLs and defined-term links are common in contracts. LLMs routinely drop markdown link syntax, just as they drop `**`, which upstream already tolerates.

**Port:** Extend the existing plain projection, rather than adding the fork's separate character-level normalizer: also skip link-syntax spans (`[`, `](url)`) and keep the offset map. Mirror the change in Node's mapper. Leave cross-reference syntax (`[~`) alone here; §4.3 covers it.

### 4.3 Anchors that cross a cross-reference field. Fork 0d3d083, LLO-910

**Fork rationale:** Cross-reference fields project as `[~2.1~](#_Ref123)` and bookmarks as `{#_Ref123}`. An anchor on the visible text (`Section 2.1, nothing…`) misses, and the batch fails with ALL_ANCHORS_REJECTED. The fork adds a fallback that matches against a field-visible projection and maps the hit back to markup-inclusive offsets, so the field itself is preserved.

**Upstream state:** Same projection, no fallback.

**Reproduced:** `full_text = '{#_Ref123}2.1 License Grant.\n\nExcept as expressly granted in Section [~2.1~](#_Ref123), nothing else is licensed hereunder.'`. The visible-text anchor gives `find_match_index → -1`, and `process_batch` reports `Target text not found` (Python). Node gives the same "not found".

**Port:** Do this together with §4.2, as one plain-projection extension: `[~X~](#…)` → `X`, `{#…}` → nothing. Map the span back so it includes the field markup; the existing xref-preservation validation (`engine.py:370-404`) then applies. **Fix the bug in §6.1 first.** Until it is fixed, routing more edits through the xref path writes more literal markup into documents.

### 4.4 Punctuation-only change restates the whole word. Fork 118bf7a, LLO-970

**Fork rationale:** The prefix backtrack in `trim_common_context` accepts only whitespace as a word boundary. So `Cause.` → `Cause;` backtracks to 0, and the whole token is struck through and re-typed.

**Upstream state:** Backtrack logic unchanged (`diff.py:55-63`), with the same logic in Node `diff.ts:40`. Upstream's word-diff path hides the problem when the anchor has context and the edit carries no comment. Two paths still use `trim_common_context` directly: commented edits (`_single_commented_sub_edit`, `engine.py:5007`) and bare-token targets.

**Reproduced:**

| Case | Upstream result |
|---|---|
| unit `trim_common_context("jair.","jair;")` | `('jair.','jair;')`; fork expects `('.',';')` |
| `"thirty days."→"thirty days;"`, no comment | deletes `['.']` ✔ (word-diff path) |
| same, **with comment** | deletes `['days.']` ✘ |
| `"without Cause."→"without Cause; provided…"`, no comment | deletes `['.']` ✔ |
| same, **with comment** | deletes `['Cause.']` ✘ (Python and Node) |
| target exactly `"Cause."` | deletes `['Cause.']` ✘ |

**Port:** The fork's `_splits_prefix_token` treats a word↔terminal-punctuation boundary (`.,;!?`) as a clean cut, unless the punctuation sits between two word characters. That keeps `10,000.00`, `example.com` and `https://x` intact. Its guard tests (`color/colour`, money, URLs, `text.`→`document.`) all pass on upstream today, so they are ready-made regression tests. Port to `diff.py` and `diff.ts` together. `trim_common_context` also feeds `markup.py:632`, `document.py:1057` and `live_word.py:1161`, so run the conformance goldens afterwards.

### 4.5 Several comments per edit, each with its own author. Fork 3d58eb4, LLO-1552

**Fork rationale:** The Suite attaches an *internal* note and a *counterparty-facing* note to the same change, under different author labels (e.g. "Jane Doe (Internal)" / "Jane Doe (Vendor)"). The fork widens `ModifyText.comment` to `str | list[EditComment{text, author?}]`. Each entry becomes its own comment on the same anchor, nested ranges included. A `comment_text` property joins the list for surfaces that take a single string (markdown preview, Word COM).

**Upstream state:** `comment: Optional[str]` (`python/src/adeu/models.py:146`). Every comment is written under the engine author. `ReplyComment` can thread replies afterwards, but those are replies, not sibling comments, and the author is still the engine author.

**Reproduced:**
- Python: pydantic `ValidationError: Input should be a valid string`.
- Node: `process_batch` crashes with `TypeError: text.match is not a function`. Node has no runtime check on `comment` (see §6.2).

**Assessment:** This is a feature, not a defect, and it is a public-schema change. It touches the Python and Node models, the MCP tool schemas (both servers), n8n, langchain, `GEMINI.md`, and the markdown preview. Before adopting the fork's shape, decide:
- (a) whether per-comment authors are allowed at all, since they let one call impersonate several reviewers (upstream already warns on author impersonation, `engine.py:3439-3444`);
- (b) whether a list of plain strings under the engine author covers most of the value.

### 4.6 The comment on paragraphs added after the target covers only the last one. Fork 9cc8b1f, LLO-1580

**Fork rationale:** When `new_text` appends paragraphs after the target, the inline part of the insertion is empty, and v1.5.2 dropped the comment entirely. The fork anchors it from the first added paragraph to the last.

**Upstream state:** The comment is no longer dropped. But on **Python** it covers only the last added paragraph, for both `\n` and `\n\n` separators. `tmp/techgc_probe/inspect_added_paras.py` shows `<w:commentRangeStart>` placed inside the second added `<w:p>`. **Node** anchors both paragraphs correctly (`"anchored": "Confidentiality: …Non-Solicitation: …"`).

**Port:** Python-only parity fix. Find the Node path that spans the comment and mirror it. The fork's test (`test_paragraphs_added_after_the_target_keep_their_comment`) asserts exactly this.

### 4.7 No caller index on per-edit reports. Fork 99b86c0

Upstream's `edits` list contains only edits; actions are filtered out. In a mixed batch (`[AcceptChange, ModifyText]`), `edits[0]` therefore describes change #1, and no field says so. `failed[]` already carries the caller index. `test_99b86c0_report_carries_callers_index_in_mixed_batch` fails because the report has no `index` key. The fix is cheap and additive: add `"index": orig_idx` in `_build_edit_report` and in the failed-report literals (`engine.py:3599`, `3652`), plus the Node twin. The fork's `parts_applied/parts_total`, which reports partial landing of a split edit, has no upstream equivalent. I did not check whether a word-diffed edit can land partially upstream.

## 5. Trade-offs to decide

### 5.1 MCP server dependencies as an optional extra. Fork 5e4a152, TES-457

- **Fork:** `fastmcp[apps]`, `jinja2` and `keyring` move to `[project.optional-dependencies] mcp`. Redline-only consumers stop installing fastmcp, mcp, authlib, joserfc and starlette.
- **Upstream:** `fastmcp[apps]>=3.4.7,<4` and `jinja2` are core dependencies (`python/pyproject.toml`). `keyring` is already gone. The `fastmcp` subtree accounts for 70 of the 80 locked runtime packages; some, such as pydantic, are shared with core (`uv export` / `uv tree --package fastmcp`).
- **Feasible:** importing `adeu`, `adeu.redline.engine`, `adeu.ingest/diff/markup` and `adeu.cli` works with `fastmcp`, `mcp`, `jinja2` and `keyring` blocked.
- **It breaks:**
  - `uvx --from adeu adeu-server` (`README.md:40,84-85`; it would need `adeu[mcp]`);
  - `langchain-adeu`, which imports `adeu.mcp_components.tools.document`, and that imports fastmcp (`langchain/langchain_adeu/diff_docx.py:31`);
  - the live-Word path in `adeu.cli` (lazy imports of `mcp_components.tools.live_word`).
- **Verdict:** Worth doing for embedders, as the fork shows, but only with a release note, updated install instructions, `langchain-adeu` depending on `adeu[mcp]` (or that import decoupled), and a CI job that imports the core without the extra.

### 5.2 Amending your own pending insertion. Fork 086b412, amend semantics

- **Fork:** If the run sits inside a `<w:ins>` by the same author, the run is just removed from that `<w:ins>`, and no deletion is recorded. This matches Word: deleting text you inserted yourself under Track Changes removes it outright. That is from my knowledge of Word; I did not check it against live Word in this session.
- **Upstream:** Nests a `<w:del>` (same author) inside the author's own `<w:ins>`. The result is correct: reject-all gives `'Start '` and accept-all `'Start Round2'`. But the review pane shows `{--Round1--}{++Round2++}` and three linked change ids for what a human reviewer sees as one proposal (`test_086b412_same_author_amend_emits_no_deletion…`, CriticMarkup `Start {--Round1--}{++Round2++}` with Chg:1/2/3 paired). Node behaves the same (`same_author_amend: dels ["Round1"]`).
- **Related:** Upstream lets a same-author edit **overlap** its own pending insertion, but blocks the overlap for another author with an "Accept first …" error (`test_probe3.py`). The fork applies both.
- **Verdict:** Optional UX improvement for multi-round workflows. It changes audit semantics (the earlier proposal disappears from history), so it needs an explicit decision, perhaps behind a flag. It is not a correctness fix.

### 5.3 Splitting edits that carry a comment (fork's surgical split vs upstream's atomic comments)

- **Fork:** Splits even commented edits and attaches the comment to the first sub-edit only.
- **Upstream:** Changed this deliberately in `4e3655f` (2026-07-22): when only one fragment carries the comment, rejecting that fragment silently destroyed the comment. A commented `modify` is therefore never word-split (`engine.py:5048-5060`).
- **Cost, reproduced in both engines:** a commented clause restate deletes and re-inserts the whole span between the first and last change.
  - `approval … withheld.` → `written approval … withheld, conditioned, or delayed.` with a comment: the full 64-character clause is deleted and re-inserted.
  - Without the comment, only the two insertions are tracked.
  - The fork's multi-block indemnity fixture shows the same with a comment.
  - A commented edit whose changes sit close together stays small (`test_f889038_long_paragraph_small_change_WITH_COMMENT` passes: `Business Hours and shall promptly` → `normal business hours and shall`).
- **Verdict:** Do not adopt the fork's comment-on-first-fragment behaviour; it is exactly the bug upstream fixed. If the restate cost matters (LLM edits nearly always carry a rationale comment), there is a middle ground: split into word-level fragments, but anchor **one** comment whose range starts before the first fragment and ends after the last, with the reference run outside any `<w:ins>`/`<w:del>`. Rejecting a single fragment then cannot remove the comment. This needs a design note and tests in both engines.

## 6. Incidental upstream findings (not fork changes)

Found while verifying §4.3. Reported only; not fixed.

### 6.1 HIGH: cross-reference-preserving edits write literal projection markup and delete the field result (both engines)

The edit `"Section [~2.1~](#_Ref123), nothing else is licensed hereunder."` → `"Section [~2.1~](#_Ref123), no other licence is granted hereunder."` passes validation. That is expected: the counts and hashes match, per `engine.py:370-404`. But the apply step produces:

```xml
<w:del><w:r><w:delText>Section </w:delText></w:r></w:del>
<w:r><w:fldChar w:fldCharType="begin"/></w:r> … REF _Ref123 … <w:fldChar w:fldCharType="separate"/>
<w:del><w:r><w:delText>2.1</w:delText></w:r></w:del>          ← field result deleted
<w:r><w:fldChar w:fldCharType="end"/></w:r>
<w:del><w:r><w:delText>, nothing else is licensed</w:delText></w:r></w:del>
<w:ins><w:r><w:t>Section [~2.1~](#_Ref123), no other licence is granted</w:t></w:r></w:ins>  ← literal markup
```

Node gives the same result (`xref_markup_copy.literal_markup_written: true`). Once accepted, the document reads "…granted in Section [~2.1~](#_Ref123), no other licence…" around an empty REF field. `AI_CONTEXT.md:226` says this path exists "to prevent dependency corruption". **Hypothesis, not verified:** `_Ref123` contains `_`, which sets `has_markdown` (`engine.py:5443-5482`) and routes the edit to the whole-span proxy. There, `trim_common_context` backtracks to 0 because `#_R` counts as a standalone italic underscore. Readers who copy what `read_docx` shows, which is what the tool asks them to do, hit this on any edit near a cross-reference.

### 6.2 LOW: Node `process_batch` does not validate `comment`

A non-string `comment` crashes with `TypeError: text.match is not a function` instead of a validation error. MCP callers are protected by the Zod schema; direct SDK and n8n callers are not.

## 7. What not to take from the fork

- The **v1.5.2 character-level `SequenceMatcher` splitter**, its threshold tuning and the `refactor-llo-837-for-upstream` branch: superseded by upstream's word-level diff.
- **Resolving against the Clean View first** as a global ordering change: upstream already skips deleted text and gives clearer errors. Reordering would change ambiguity semantics across the board.
- The fork's **`edit_results` shape**: upstream's `edits`/`failed` reports are richer. Only the `index` field (§4.7) is worth copying.

## 8. Porting notes

- **Parity is required** (`AGENTS.md`, "Dual-Engine Parity"). Every item in §4 except 4.6, which is already correct in Node, needs the Python and Node changes in the same PR. The fork never touched Node.
- The fork's tests are good regression material. Most run against upstream with only import changes; the probe file `tmp/techgc_probe/test_probe.py` is already an adapted copy. For the hang tests, replace `signal.SIGALRM` (POSIX-only) with a thread or `pytest-timeout` budget so they run on Windows; the probe uses a daemon thread.
- Suggested order: 4.1 → 6.1 → 4.2 + 4.3 (one projection change) → 4.4 → 4.6 → 4.7 → decisions in §5 → 4.5.

## 9. Gaps in this review

- I did not run the full upstream test suites (`uv run pytest`, `npm run test`) after the merge. No code changed, so they were not needed for this report.
- I did not verify Word's "deleting your own insertion removes it" behaviour against live Word.
- I did not check whether a word-diffed upstream edit can land partially (fork's `partial` status).
- I could not see the fork's private tracker (LLO-*/TES-* tickets); the rationale above comes from commit messages, code comments and internal PR #1.

## 10. Housekeeping left behind

- `stash@{0}`: the stale local `python/uv.lock` edit (ooxml-integrity 0.5.0). `origin/main` pins 0.8.1, so it is safe to drop (`git stash drop stash@{0}`).
- `node/packages/core/src/zz_techgc_probe.test.ts`: a temporary probe test. **Delete it before running `npm test` or committing.** It reads fixtures from an absolute `D:/` path. My `Remove-Item` call was denied by policy, so I left it in place.
- `tmp/techgc/` (fork diffs) and `tmp/techgc_probe/` (probe tests, fixtures, outputs) are untracked scratch kept as evidence.
- `refs/remotes/techgc/*`: the fetched fork refs; nothing was added to `git remote`.
