# Spec: Real-Document Corpus

Status: frozen v1 · Task: CC-3 · Acceptance: [A5](acceptance/A5-corpus-validation.md)

## 1. Policy

- The corpus is **real, publicly available government documents** (US federal, US
  courts, US state, Canadian federal, Ontario) that exercise content-control features at
  production scale. They validate what synthetic fixtures cannot: anonymous controls,
  checkbox walls, cell-level wrappers, enforced protection, custom placeholder prose.
- **Never committed, never redistributed.** `shared/corpus/` is gitignored (only
  `README.md` and `manifest.json` are tracked). We are not the publisher of these
  documents; they are fetched from their official sources on demand.
- **Deterministic CI does not need them.** Every acceptance example except A5 runs on
  synthetic fixtures. Corpus tests skip cleanly when a document is absent
  (`pytest.skip` / vitest `ctx.skip` via the shared `corpus_path()` helpers, CC-3).
- **Upstream drift is expected.** Government sites revise documents in place. The
  manifest pins `sha256` + `bytes` as observed 2026-08-21; the fetcher WARNS on
  mismatch and keeps the file (it never fails the fetch for drift). A5 invariants are
  written as inequalities (`>= 3800 checkboxes`), not exact counts, so reasonable
  upstream revisions don't break the suite. A hash-drift warning is the signal to
  re-run the corpus facts scan and refresh the manifest in a small PR.

## 2. Mechanism

- `shared/corpus/manifest.json` — machine-readable source of truth: key → file, url,
  landing_page, org, title, sha256, bytes, sdt_facts (the scan snapshot), notes.
- `python scripts/fetch_corpus.py [--only KEY[,KEY…]] [--force] [--list]` — stdlib-only
  (no deps), Windows-safe. For each manifest entry: skip if present (unless `--force`),
  download with a browser User-Agent, follow redirects, verify PK zip magic, compare
  sha256/bytes (warn on drift), write atomically. Entries with `"url": null` or a
  blocked host print the landing page + manual-fetch instructions instead of failing
  silently. Exit 0 when every requested entry is present on disk afterwards; exit 1
  otherwise (so CI can gate the optional corpus job).
- Canada.ca / gc.ca aggressively bot-block (observed 2026-08-21): expect `blocked`
  entries there — the mechanism is "fetch if possible, tell a human exactly what to do
  when not." A manually downloaded file dropped into `shared/corpus/` under the
  manifest's `file` name is fully equivalent.
- Env override `ADEU_CORPUS_DIR` relocates the corpus directory (tests and fetcher both
  honor it).

## 3. Corpus roster (what each document proves)

| Key | Document (org) | Why it's in the corpus |
| --- | --- | --- |
| `fedramp_ssp_rev4` | FedRAMP SSP Moderate Baseline, rev4 (GSA/FedRAMP) | Scale: 5,007 SDTs, every class, 371 cell-level, 94 bound, 3 `w:temporary`, 718 placeholders. Token-cost bound target (A5.6) |
| `fedramp_ssp_appx_a_moderate` | FedRAMP SSP Appendix A Moderate, rev5 | 3,804-checkbox wall — `[x]`/`[ ]` projection + ledger pagination stress |
| `fedramp_ssp_rev5` | FedRAMP SSP rev5 baseline | Binding-heavy modern template (55 bound) |
| `fedramp_sar` | FedRAMP SAR template | Building-block controls mixed with rich text |
| `dau_acquisition_plan` | Acquisition Plan template (DAU/DoD) | 48 locked controls, 40 custom-prose placeholders, ZERO tags/aliases — anonymous-control reality |
| `ca_talent_recruitment` | Digital talent recruitment letter (Canada.ca) | Canadian federal; dropdown whose first option is its own prompt ("Choose a type.") |
| `wawd_esi_agreement` | Model ESI agreement (US Dist. Ct. W.D. Wash.) | Court document; all fields bound + placeholder; tags with spaces/`#` |
| `odot_uic_drywell` | UIC drywell template (Oregon DOT, .dotx) | .dotx handling; cell-level controls; picture controls |
| `on_juries_form1` | Juries Form 1 (Ontario court) | ZERO SDTs but enforced `edit="forms"` protection with password hash — G4/G5 gates on a real court form |
| `hc_diagnostic_nonlab` | Diagnostic (non-lab) template (Health Canada) | Building-block chrome only; **negative `w:sdt` id** in the wild |

`sdt_facts` in the manifest records the 2026-08-21 scan (total, per-class counts,
locks, bound, placeholders, cell/row-level, protection) — A5 derives its inequality
floors from these, at ~95% of observed values to absorb drift.

## 4. Licensing note

These are government works published for public use (US federal works are public
domain; Canadian/Ontario documents are Crown-copyright materials publicly distributed
for their intended use). We still do not vendor them: fetch-from-source keeps
provenance clean and the repo small.
