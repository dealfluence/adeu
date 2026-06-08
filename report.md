# Adeu — Issue #23 Acceptance & Feedback-Layer Handover

**Status:** Engine fixes verified PASS (5/5, both engines). Reporting/feedback layer requires work before this can be called shippable for LLM-driven use.
**Audience:** Adeu engineering (Python oracle + Node port).
**Prepared by:** QA/UX pass over `adeu-python` and `Adeu` MCP servers.
**Scope:** Verification of the five Issue #23 fixes, plus a separate and more important set of findings about how the tools report results to their caller.

---

## 0. TL;DR

The five Issue #23 bugs are genuinely fixed. The two items that were failing in the prior acceptance report — 23-4 (multi-paragraph silent corruption) and 23-5 (Python/Node ambiguity-check divergence) — now pass on both engines with byte-identical OOXML output.

That is the good news, and it is not the point of this document.

During verification, the **engine produced correct accepted-text only when I cross-checked every call by re-reading the document.** The tools' own return values reported success on at least one transformation that silently scrambled the output. The verification oracle (`debug_xml_diff`) reported cosmetic packaging noise as content differences on every single call. And the only honest feedback signal in the system — the CriticMarkup read-back via `read_docx` — is a separate, optional tool call that sits off the main workflow path.

For a product positioned as "Track Changes for the LLM era," the caller is an LLM. An LLM cannot see Word's UI. **The return strings from these tools are the entire feedback channel — they are, functionally, the product surface.** Right now those strings are optimized to look reassuring rather than to be informative. This document specifies exactly where, why that is dangerous, and what to change.

---

## 1. Acceptance results (the part that passed)

### 1.1 Method

Each bug was exercised against a purpose-built fixture on both servers:

- `adeu-python` — the oracle implementation, debug flag on.
- `Adeu` — the Node port.

Outputs were cross-compared at the OOXML level via `debug_xml_diff` and behaviourally via `read_docx`. Original fixtures were never mutated; every run wrote to a fresh `out_*.docx`.

Fixtures (all schema-validated before use):

| Fixture | Purpose | Critical property |
|---|---|---|
| `at_23_1_fresh.docx` | 23-1 from-scratch comment | **Zero** comment parts in the ZIP (stub stripped) — see §1.3 |
| `at_23_1_withcomment.docx` | 23-1 regression guard | One pre-existing comment |
| `at_23_2_italic.docx` | 23-2 italic inheritance | An italic run (`Affiliate`) uniquely targetable |
| `at_23_3_delta.docx` | 23-3 delta placement | Stable anchors (see §2.1 for the anchor-choice trap) |
| `at_23_4_multipara.docx` | 23-4 plain-prose N→N / N→0 / N→1 | Two adjacent plain paragraphs |
| `at_23_4_list.docx` | 23-4 list-boundary variant | Two bulleted items across the boundary |
| `at_23_5_dupdel.docx` | 23-5 ambiguity vs deleted text | Duplicated line, **first copy pre-deleted** as a real `w:del` |

### 1.2 Scorecard

| Bug | Python | Node | Parity | Verdict |
|---|---|---|---|---|
| 23-1 comments.xml namespace (from-scratch) | PASS | PASS | identical | **PASS** |
| 23-2 inserted-run formatting | PASS (roman, empty `rPr`) | PASS | byte-identical body | **PASS** |
| 23-3 delta placement / separator-before | PASS | PASS | byte-identical | **PASS** |
| 23-4 multi-paragraph target | PASS (rejects cleanly) | PASS (rejects cleanly) | identical error messages | **PASS** — *was the release blocker* |
| 23-5 ambiguity vs deleted text | PASS (edits live copy) | PASS | byte-identical body | **PASS** — *oracle now fixed* |

**23-4** was the prior release blocker (plain-prose N→N silently merged two paragraphs and jammed both deltas into the first). It now rejects with a clear, actionable boundary error — and the error string is byte-identical across Python and Node.

**23-5** was the prior parity break (Python counted tracked-deleted text toward ambiguity and rejected; Node did not). Python now correctly ignores the `w:del` copy and edits the live occurrence. Output is byte-identical to Node.

### 1.3 One fixture-construction note the team should know

`at_23_1_fresh.docx` had to be hand-stripped of its comment parts. The fixture was generated with docx-js (`docx@9.7.1`), which **silently emits a `word/comments.xml` stub plus its relationship and content-type entries in every document, even when no comments are added.** If left in place, every "from-scratch comment" test would have exercised the *append-to-existing-part* path instead — while reporting that the from-scratch path was verified.

This is not an Adeu bug, but it is the same disease this document is about: a tool producing output that structurally differs from what was requested, with no signal of the difference. Flagging it because **if you build your own regression fixtures with docx-js, your "no comments" fixtures will not actually be comment-free.** Strip `word/comments*.xml`, the matching `<Relationship>` in `word/_rels/document.xml.rels`, and the `<Override>` in `[Content_Types].xml`.

---

## 2. The findings that matter (feedback-layer defects)

These are ordered by blast radius — how much damage each can do to a caller who is *not* manually re-reading every result. The LLM operator is exactly that caller.

### 2.1 FINDING A — `process_document_batch` reports mechanical success on semantically wrong output

**Severity: High. This is the headline finding.**

**What happened.** While testing 23-3 (delta placement), I issued a `modify` whose `new_text` began with a new paragraph followed by the verbatim anchor:

```json
{
  "type": "modify",
  "target_text": "ANCHOR_LINE governs the interpretation of this Agreement.",
  "new_text": "NEW_PARA inserted before.\n\nANCHOR_LINE governs the interpretation of this Agreement."
}
```

The tool returned:

```
Batch complete. Saved to: ...out_23_3b_py.docx
Edits: 1 applied, 0 skipped.
```

That is the success signal. The actual accepted text in the output document was:

```
NEW_PARA inserted before.LINE governs the interpretation of this Agreement.
ANCHOR_
```

The paragraph separator was **dropped** (`before.LINE`, no space, no break), the word `ANCHOR_LINE` was **split** across two locations, and a stray `ANCHOR_` fragment was deposited in a **new paragraph of its own**. The document's meaning was corrupted. The tool called it a success.

**Why this is bad.** The tool's notion of "success" is *"I located the target and applied a diff."* The caller's notion of success is *"the document now says what I meant."* These are different claims. `process_document_batch` reports the first while the caller — who has no other window into the result — reasonably reads it as the second. A reviewer (or an LLM acting as one) who fires a batch and trusts `Edits: 1 applied` ships a corrupted contract and never knows.

I only caught this because I made a *separate* `read_docx` call afterward and read the prose with my own eyes. The success string actively pointed me away from doing that.

**Root cause (for context, see §2.2).** The specific corruption above was provoked by the diff tokenizer splitting on the underscore in `ANCHOR_LINE`. But the *defect being reported here is not the tokenizer* — it is that the success message makes no claim about output fidelity and cannot distinguish a clean edit from a scrambled one.

**How to fix.** `process_document_batch` must return *what it actually did*, per edit, not just a count:

- For each applied edit, return the resulting CriticMarkup span (`{--…--}{++…++}`) **and** the before/after *accepted* text for the affected region.
- Replace `Edits: 1 applied` with something like:
  ```
  Edit 1: matched "ANCHOR_LINE governs…"
    accepted text now reads: "NEW_PARA inserted before.LINE governs… / ANCHOR_"
  ```
  At which point the corruption is visible in the return value itself, on the first call, with no second tool call required.

This single change would have caught the 23-3 failure immediately. The caller should never have to issue a second tool call to discover whether the first one meant what they asked.

### 2.2 FINDING B — the diff tokenizer is invisible until it corrupts, and splits inside punctuated tokens

**Severity: Medium-High (interacts with Finding A to produce silent corruption).**

**What happened.** The anchor `ANCHOR_LINE` is parsed by the diff engine as two tokens, `ANCHOR_` and `LINE`, splitting on the underscore. When inserting text before this anchor, the differ aligned on the longest common run (`LINE governs…`) and treated everything before it as a replace — striking `ANCHOR_`, re-inserting `<new text> ANCHOR_`, and (in the `\n\n` case) losing the separator. Reproduced identically on both engines, so it is a shared-engine behaviour, not a port bug.

I confirmed this was the tokenizer and not the fixture by re-running the same operation against a **plain-prose** anchor (`"Disputes shall be resolved in the courts of competent jurisdiction."`, no underscore). That produced the correct, clean result:

```
ANCHOR_LINE governs the interpretation of this Agreement.

{++New leading paragraph.++}

Disputes shall be resolved in the courts of competent jurisdiction.
```

So the engine is correct on clean prose. **The hazard is that punctuated anchors — defined terms, IDs, clause numbers, anything with `_`, `/`, `-`, or letter/digit boundaries — can trigger mid-token splits, and the tool gives zero signal that this is happening.** I had to burn four exploratory calls (`23_3` a→b→c→d) reverse-engineering the existence of the tokenizer from the shape of its damage. A caller who isn't doing forensic diffing will never know the tokenizer exists, let alone that their anchor tripped it.

Real-world relevance is high: contracts are *full* of `Section 4.2`, `_Affiliate_`, `AGR-2026-001`-style tokens. These are precisely the anchors a reviewer would naturally pick because they're distinctive.

**How to fix.**

1. **Echo the tokenization.** When an anchor contains split-prone punctuation, return the token boundaries the engine used, or warn explicitly: `anchor split into [ANCHOR_][LINE]; insertion will redline mid-word — consider a longer plain-prose anchor.`
2. Longer-term, reconsider whether intra-word punctuation should be a token boundary at all for matching purposes, or whether the matcher should prefer whole-anchor alignment when the full `target_text` is present verbatim in `new_text`.

Don't make the caller infer the tokenizer from its scars.

### 2.3 FINDING C — `debug_xml_diff` reports cosmetic packaging deltas as content differences, on every call

**Severity: Medium. Dangerous because it's noise in the verification instrument.**

**What happened.** `debug_xml_diff` is the tool I relied on for every parity verdict. On *every single call*, it surfaced three empty relationship-stub files as differences:

```
+=== FILE: word/_rels/endnotes.xml.rels ===
+<Relationships .../>            (empty)
+=== FILE: word/_rels/fontTable.xml.rels ===
+<Relationships .../>            (empty)
+=== FILE: word/_rels/footnotes.xml.rels ===
+<Relationships .../>            (empty)
```

These are empty stubs the two engines package differently. They have nothing to do with redlining, track changes, or document content. Worse, they flip between "added" and "removed" depending on the *direction* of the diff (which file is `file_a`), so they're not even a stable, learnable artifact.

**Why this is bad.** This is noise in the one instrument used to catch real problems. It sits directly above the content I care about in every diff. Every parity check required me to mentally subtract three phantom files before reading the real delta. On a large or busy diff, a genuine one-line divergence buried under this stub noise is easy to miss. **Noise in the verification oracle is more dangerous than noise in the thing being verified**, because it erodes trust in the tool whose entire job is to be trustworthy. The oracle has a high false-positive floor, and a high false-positive floor trains the operator to skim.

**How to fix.**

1. Default `debug_xml_diff` to **content-only**: suppress empty `_rels` stubs and direction-dependent packaging artifacts. Offer a `--include-packaging` flag for the rare case someone needs them.
2. Normalize diff direction so the same two files produce the same delta regardless of argument order.

### 2.4 FINDING D — `debug_xml_diff` proves nothing by truncation; it never asserts equivalence

**Severity: Medium.**

**What happened.** For 23-1, the diff output stopped after the opening `<w:comments>` tag. I concluded "from there on the files are identical." That is an *inference from the tool going quiet*, not a statement the tool made. The tool does not say "and the remainder matches." It just stops.

Consequently, for the 23-1 namespace bug specifically — the entire reason 23-1 exists — **I never actually saw the `w14`/`w15`/`w16cid` declarations in any tool output.** I inferred the fix was present because `read_docx` didn't throw a `NamespaceError`. I verified the *absence of the symptom*, not the *presence of the fix*. For a namespace-declaration bug, "it didn't crash on reopen" is weaker evidence than "the declaration is present and correct," and the tooling only gave me the former.

**Why this is bad.** "The diff stopped printing" should never be load-bearing evidence of equivalence. Absence of output and proof of sameness are different things, and a verification tool that conflates them lets the operator believe more than the tool actually demonstrated.

**How to fix.**

1. End every `debug_xml_diff` run with an explicit verdict line: `RESULT: documents are content-identical` or `RESULT: 3 content differences across 2 parts`.
2. Provide a positive-assertion mode that can confirm a specific element exists with specific attributes (e.g. "comments.xml root declares w14, w15, w16cid"), so a fix's *presence* can be verified, not just its symptom's absence.

### 2.5 FINDING E — write and verify live on different surfaces, and the honest one is off the main path

**Severity: Medium (workflow / discoverability).**

**What happened.** I wrote with `process_document_batch` and verified with `read_docx`. The `read_docx` CriticMarkup view (`{--deleted--}{++inserted++}{>>comment<<}`) is genuinely good — it is the only reason I caught Finding A. But it is a *separate, manual, optional* tool call. The natural workflow — call batch, read `Batch complete`, move on — routes the caller directly *around* the only honest feedback in the system.

**Why this is bad.** The truthful signal exists but is opt-in and off-path; the reassuring-but-incomplete signal (`Edits: N applied`) is the default and on-path. The system's ergonomics actively encourage the operator to skip the step that would catch corruption.

**How to fix.** Close the loop on one surface. Either:

1. Let `process_document_batch` optionally return the post-edit `read_docx` CriticMarkup view inline (`return_review: true`), or
2. If kept separate, document the `batch → read_docx` pairing as the *required* verification workflow, not an optional follow-up.

Move the honest feedback onto the path the caller already walks.

### 2.6 FINDING F — neither tool reports its engine or version

**Severity: Low-Medium (blocks reproducibility for a two-implementation product).**

**What happened.** The original Issue #23 bug report explicitly asked the team to "confirm exact version on your side." I could not. Neither `adeu-python` nor `Adeu` reports its build, engine, or version in any tool result. I am parity-testing two implementations of the same spec with no version stamp on either output.

**Why this is bad.** A product that ships fixes across two implementations and is being acceptance-tested for *parity* needs every result to state which engine and which version produced it. Otherwise every parity verdict is implicitly "these two unknown builds happened to agree," and any future regression report can't be tied to a build.

**How to fix.** Stamp every batch/diff/read result with `engine: python|node` and a semantic version. Cheap to add, and it makes every report in this document reproducible.

---

## 3. Engine-behaviour notes (lower priority, not feedback-layer)

These are about what the engine *does*, not how it reports. Recorded so they aren't lost, but they are secondary to §2.

- **23-2 has no force-preserve-formatting option.** Insertions come out roman (explicit empty `<w:rPr/>`) unless Markdown-styled. The original bug (italic inheritance) is resolved-by-default — the inserted run no longer inherits the anchor's italic. But there is still no way to *deliberately* preserve the source run's formatting on an insertion. **Recommendation:** close 23-2 as resolved-by-default and document "insertions are roman unless Markdown-styled," or add an explicit override field if force-preserve is a real use case.

- **23-4 took the guard-everything route, not the decomposition route.** The change note for 23-4 proposed transparently decomposing multi-paragraph `target_text` into per-paragraph edits for the N→N and N→0 cases. That is **not** what shipped. The engine now *uniformly rejects* any multi-paragraph target (N→N, N→0, N→1 all rejected; list-boundary returns "target not found"). This is functionally safe and is the correct call for a release blocker — a clear refusal beats a silent corruption. **Recommendation:** confirm "reject all multi-paragraph targets" is the intended *final* behaviour, and document it. If N→N decomposition is still wanted as a feature, it is currently unimplemented.

- **The 23-4 refusal is a model for the rest of the system.** It detects a structural hazard, refuses, and explains why in one actionable sentence, identically across both engines. Finding A (silent success on wrong output) is the *same class of bug* 23-4 just fixed — a structural hazard in the diff path that the tool fails to detect. **Apply the 23-4 philosophy to the diff-placement hazard:** detect the mid-token / separator-loss case and either refuse it or surface it, rather than reporting success.

- **No hangs observed this session.** The prior report noted a transient ~4-minute double-hang on the 23-5 input (suspected MCP bridge). It did not reproduce — every call this session returned in well under a second, including the same 23-5 input. No action unless it recurs.

---

## 4. Consolidated recommendations, prioritized

| # | Recommendation | Finding | Priority |
|---|---|---|---|
| 1 | `process_document_batch` returns per-edit CriticMarkup + before/after accepted text, not just an applied count | A | **P0** |
| 2 | Add a `dry_run` / preview mode: return the proposed diff + accepted text *without writing the file* | A, B | **P0** |
| 3 | Surface tokenization: warn when an anchor splits on `_ / -` or letter/digit boundaries, echo the token boundaries used | B | **P1** |
| 4 | `debug_xml_diff` defaults to content-only; suppress empty `_rels` stubs and packaging artifacts; direction-stable | C | **P1** |
| 5 | `debug_xml_diff` ends with an explicit verdict line; add positive-assertion mode for "element X has attribute Y" | D | **P1** |
| 6 | Stamp every tool result with `engine` + `version` | F | **P1** |
| 7 | Optionally return the post-edit review view inline from `process_document_batch`, or document batch→read_docx as required | E | **P2** |
| 8 | Decide & document 23-4 final behaviour (uniform reject vs. decomposition); close/clarify 23-2 | §3 | **P2** |

**On `dry_run` (rec #2) specifically:** for an LLM operator that cannot see Word's canvas, a preview is the equivalent of a human watching the cursor before committing. It is the single most effective guard against bad-anchor corruption, because it catches the problem *before* anything is written to disk. If only one P0 ships, ship this one.

---

## 5. The throughline (why this document exists)

The engine is now fairly trustworthy. **The reporting around it is not.**

Every tool in the set reports *mechanical* success — count of edits applied, count of XML parts changed, output written — and **none reports *semantic* success**: did the document end up saying the right thing. A human working in Word would catch every defect in §2 instantly, because they would *see* the rendered document. Adeu's operator is an LLM that sees only the return strings. Those strings are not a status readout adjacent to the product; for this product, **they are the product surface.**

Until those strings carry semantic outcome rather than mechanical tallies, the only safe operating protocol is the one I was forced into this session: *distrust every reported success and re-read the entire document after every call.* That is a workable protocol for a paranoid QA pass. It is not a workable protocol for the product's actual users, and a less cautious operator would skip exactly the cross-checks that caught the corruption here.

Fixing the engine was the hard part and it's done. Fixing the feedback layer is the cheaper part and it's what stands between "the fixes pass" and "the product is safe to drive blind."

---

*End of handover. All `out_23_*.docx` verification artifacts remain in `C:\Users\mikko\Desktop\4youClaude\` alongside the fixtures; they can be regenerated from the fixtures using the batch calls described in §1, and can be deleted once reviewed.*