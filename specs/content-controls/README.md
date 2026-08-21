# Content Controls (`w:sdt`) — Initiative Workspace

*Operator order*: This task is distributed to two machines. Windows and OSX. You know which you are. The task is to be worked on only content-controls-specs branch. Use explicitly unsigned commits while working. Commit and push and pull often. Communicate with the other agent.

Status: **specs frozen for v1**. Every `[COM-PENDING]` section was resolved by CC-6's
Word COM battery on 2026-08-21 — two confirmed, three amended; see PROGRESS.md. Two
findings outside the tagged sections await Mikko's sign-off (G5 forms protection, CC-9
bound-store reject resync).
Driver: Template-heavy legal workflows run on Word
content controls, and Adeu currently flattens them into invisible plumbing.

## What this directory is

The complete, self-contained work package for making Adeu content-control native:

| File | Role |
| --- | --- |
| [TASKS.md](TASKS.md) | Task board. **The coordination state lives here, in git.** |
| [PROGRESS.md](PROGRESS.md) | Append-only log: findings, decisions, deviations, COM results. |
| [spec-projection.md](spec-projection.md) | Normative projection grammar (`{#cc:N}` anchors, `[x]` checkboxes, placeholder bubbles). |
| [spec-fields-ledger.md](spec-fields-ledger.md) | `mode="fields"` ledger, appendix summary, protection/fields banner. |
| [spec-gates.md](spec-gates.md) | Write-path gates: locks, groups, document protection, error contracts, overrides. |
| [spec-set-field.md](spec-set-field.md) | The `set_field` change type: per-control-type semantics, schema constraints. |
| [spec-corpus.md](spec-corpus.md) | Real-document corpus: sources, fetch mechanism, drift policy. |
| [acceptance/](acceptance/) | ATDD examples. **These are the definition of done** for every task. |

## Ground rules for agents working here

1. **Read first:** this file, the spec you're implementing, its acceptance file, and the
   repo-level `AGENTS.md` (commands, verification workflow) + `AI_CONTEXT.md` §13
   (projection dialect) and §3 (Virtual Text contract).
2. **Dual-engine parity is mandatory.** Every projection/edit behavior lands in
   `python/` AND `node/packages/core` in the same task, with matching tests
   (AGENTS.md "Dual-Engine Parity"). MCP surface changes additionally update
   `node/packages/mcp-server` and `python/src/adeu/mcp_components/`.
3. **Claim before you work.** Edit your task's row in [TASKS.md](TASKS.md) to
   `in-progress` with your agent name, date, and branch — in the *first* commit on your
   branch. One agent per task. Don't start a task whose `Depends on` entries aren't `done`.
4. **Acceptance examples are the contract.** A task is `done` only when every acceptance
   example it references passes as a real test in both engines (plus the MCP/CLI surfaces
   the example marks), and the full suites + lint/type checks pass per AGENTS.md.
   If spec prose and an acceptance example disagree, **stop and flag it** in PROGRESS.md —
   do not guess; spec prose is authoritative until amended.
5. **Close the loop in the same PR:** update TASKS.md (status → `done`, commit hash) and
   append a short PROGRESS.md entry. A task whose board row is stale is not done.
6. **Corpus documents are never committed.** They are fetched on demand
   (`python scripts/fetch_corpus.py`) into `shared/corpus/` (gitignored). Corpus-backed
   tests must skip cleanly when a document is absent — CI runs green without downloads.
7. **Spec changes are controlled.** v1 specs are frozen. `[COM-PENDING]` sections may be
   amended by CC-6 findings; anything else needs Mikko's sign-off, recorded in PROGRESS.md.

## Fifteen-second orientation

Today both engines flatten `w:sdt` content into plain text: field boundaries, types,
dropdown options, lock state, placeholder-vs-content, data bindings, and document
protection are all invisible — and the write path happily edits inside locked controls,
edits forms-protected boilerplate, and "fills" empty fields by redlining placeholder
ghost text (verified 2026-08-21 against v2.4.1; see the proposal artifact for probes).
The Python engine additionally *drops* SDT-wrapped table rows/cells (CC-0, data loss).

v1 fixes this in three moves: **see** the fields (anchored projection + fields ledger),
**respect** the rails (gates on locks/groups/protection with explicit overrides), and
**touch** fields safely (`set_field`, which fills the way Word fills).
