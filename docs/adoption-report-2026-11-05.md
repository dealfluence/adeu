# Adeu adoption report: who uses it, where, and how much

Research date: 2026-11-05. Scope: PyPI `adeu`, `langchain-adeu`; npm `@adeu/core`, `@adeu/mcp-server`, `n8n-nodes-adeu`; the Claude Desktop `.mcpb`, Claude Code plugin, Agent Skill and Gemini CLI extension; and every public mention we could find.

How to read this: every claim is labelled.

- **Verified**: I fetched the primary source this session (registry API, GitHub API, the repository file, or the article itself).
- **Reported**: it came from a search snippet or a third-party aggregator, and I did not open the primary source.

Third-party aggregators (ncnodes, Agentage, PulseMCP and similar) and one of my own research subagents produced numbers that contradict the primary APIs. Section 9 lists the corrections. Trust the primary numbers below over any aggregator figure.

---

## 1. Summary

1. **Usage is real and still accelerating.** PyPI `adeu` did 32,892 non-mirror downloads in the 30 days ending 2026-11-04. Daily volume went from about 100/day in April to about 1,900/day on Nov 3–4. The PyPI download bump that followed the 3.0.7 release (2026-10-05) never faded: daily downloads have stayed above 500 every day since.
2. **The Python package and the MCP server carry the adoption.** `@adeu/core` (10.7k/30d) and `@adeu/mcp-server` (2.9k/30d) are solid. `langchain-adeu` (~0.9–3k/30d) and `n8n-nodes-adeu` (342/30d) are marginal.
3. **Hidden production consumers.** Download-by-version data shows several unknown pipelines pinned to old versions. `@adeu/core@3.0.2` takes about 1,077 downloads a week while our own packages account for 4 of them. `adeu==3.0.4` took 14.5k downloads in 90 days. These are CI/deploy loops in someone's private repo. They are the biggest single usage signal, and we can't name them.
4. **Named users we can confirm in public code** (section 4): LaunchStack (897★), PII-Shield (153★), claude-for-legal-finland (115★), and claude-contract-negotiator. There is also an active private-use fork by Vela Wood. NVIDIA SkillSpector (24.4k★) carries our registry entry as a test fixture.
5. **Independent benchmark.** Vespper (YC F24, a direct competitor) ran Adeu 3.0.7 on 180 Harvey LAB legal tasks. Adeu had the highest completion rate of any DOCX MCP tested (94%). However, a plain python-docx baseline beat every MCP on score and cost (section 6).
6. **Things that need fixing now** (section 8):
   - The official MCP Registry still advertises 1.7.1 (npm) and 1.5.2 (PyPI), and scanners and clients are pinning those.
   - The README still points at Smithery, which shut down on Oct 31.
   - The repo has no `SECURITY.md` and private vulnerability reporting is off. That is why a vulnerability report landed as a public issue (#176).

---

## 2. Registry numbers (verified)

### 2.1 PyPI

| Package | Last 30 days | Last day | All-time | Source |
|---|---|---|---|---|
| `adeu` | **32,892** (sum of pypistats daily `without_mirrors`, Oct 6 to Nov 4) | 1,923 (Nov 4) | 149,540 (pepy, includes CI) | pypistats `/overall`, pepy.tech |
| `langchain-adeu` | 918 (pypistats `recent`); 3,044 (pepy, includes CI) | 39 | 28,279 (pepy) | pypistats, pepy.tech |

Caveat: pypistats' `/recent` endpoint reports `last_month: 11,577` for `adeu`, which contradicts its own daily series. The daily series agrees with pepy (37.9k/30d with CI). I use the daily series.

**Growth curve for `adeu`** (pypistats daily, non-mirror):

| Period | Typical daily downloads | Notable spikes |
|---|---|---|
| Apr 2026 (series starts Apr 8) | 30–270 | |
| May–Jun | 100–650 | 06-04: 1,031; 06-18: 860 |
| Jul | 120–630 | 07-29: 1,174 |
| Aug | 70–450 | **08-16 to 08-18: 1,147 / 907 / 811**, right after Artur Serov's LQ.AI post (08-15) and the LegalQuants reshare (08-16) |
| Sep | 75–430 | **09-22: 1,713** (cause unknown; no release, post, or listing found for that date) |
| Oct 5 onward | 560–1,660, rising | 3.0.7 released 10-05; demand has stayed high since |
| Nov 1–4 | 923, 1,458, 1,893, 1,923 | highest four-day stretch on record |

**Who is downloading `adeu`** (pypistats, last 30 days):

- **Python version:** 3.12 = 15,922, 3.14 = 14,985, 3.13 = 746, unknown = 1,019.
- **Operating system:** about 97% Linux.

The high 3.14 share is the signature of `uvx --from adeu adeu-server`: uv picks the newest interpreter, so these are MCP-server launches by end users. The 3.12 share looks like pinned Docker/CI images. Linux dominance means servers and CI, not lawyers' laptops.

**Version distribution, last 90 days** (pepy.tech): 3.0.7 at 24.3% (17.4k) and **3.0.4 at 20.3% (14.5k)**. 1.30.0 (3.8k), 1.0.0 (2.3k) and 2.4.1 (1.8k) are still pulled. Something with a lockfile on 3.0.4 runs a lot. Old majors still getting thousands of pulls means several abandoned-but-running pipelines.

Release history: 96 PyPI releases. The first was 0.0.1 on 2025-08-04 (a name reservation); the latest is 3.0.7 on 2026-10-05 (libraries.io).

### 2.2 npm

| Package | Last week | Last 30 days | Last 365 days | npm "dependents" |
|---|---|---|---|---|
| `@adeu/core` | ~3,047 (sum of per-version counts) | **10,691** | 38,785 | 2 (our own mcp-server and n8n packages) |
| `@adeu/mcp-server` | 786 | **2,853** | 22,293 | 0 |
| `n8n-nodes-adeu` | 37 | 342 | 9,915 | 0 |
| `adeu` (name reservation, 0.0.1) | 1 | 5 | | 0 |

Source: `api.npmjs.org/downloads/point/*`, `api.npmjs.org/versions/*/last-week`, registry search API.

**Per-version breakdown, last week. This is the most revealing data in the report.**

- `@adeu/core@3.0.2`: **1,077/week**. Our own packages on 3.0.2 pulled it about 4 times (mcp-server 3.0.2 = 3, n8n 3.0.2 = 1). So roughly 1,070 weekly installs come from **one or more unknown projects with `@adeu/core` locked at 3.0.2**, almost certainly in CI.
- `@adeu/core@1.28.0`: 324/week. `2.0.0`: 123, `1.19.1`: 86, `1.12.1`: 83, `1.31.0`: 62. More pinned third-party consumers. PII-Shield's `^1.8.0` range resolves to the 1.x line.
- `@adeu/mcp-server@1.7.1`: 87/week. This is exactly the version the official MCP Registry marks `isLatest` (section 8.1). Registry-driven clients and scanners install it.
- `@adeu/mcp-server@3.0.7`: 667/week. These are `npx -y @adeu/mcp-server` users on the latest version.
- `n8n-nodes-adeu`: about one download per published version per week. That is the pattern of mirror and registry crawlers. Real n8n usage is in the low tens per week.

`@adeu/mcp-server` bundles all of its dependencies (`node/packages/mcp-server/tsup.config.ts:59`, `noExternal: [/(.*)/]`). So `npx @adeu/mcp-server` users do not show up in `@adeu/core` counts, and **`@adeu/core` downloads are SDK consumers**, not MCP users.

### 2.3 Other distribution channels (verified)

| Channel | Metric | Source |
|---|---|---|
| Claude Desktop `Adeu.mcpb` (GitHub Releases) | **283 downloads in total** across 67 releases (v0.7.0 to v3.0.7) | GitHub releases API |
| Gemini CLI extension archives (`adeu.zip`, `win32/linux/darwin.adeu.zip`, `adeu.tar.gz`) | 90 + 86 + 85 + 60 + 8 = 329 | GitHub releases API |
| skills.sh (`npx skills add dealfluence/adeu`) | 34 total installs (`adeu-redlining` 16, plus three dev skills at 6 each) | skills.sh/dealfluence/adeu |
| GitHub repo | 170★, 26 forks, 2 watchers, created 2025-12-30 | GitHub API |
| deps.dev | 1 dependent each for npm `@adeu/core` and PyPI `adeu` | deps.dev API |
| libraries.io | SourceRank 12, 1 dependent package, 1 dependent repo | libraries.io |

The desktop extension is a minor channel: about 4 downloads per release. Claude Desktop users mostly arrive through npx/uvx config.

---

## 3. Listings and directories

| Where | What is listed | Status |
|---|---|---|
| Official MCP Registry `ai.adeu/adeu` | PyPI 1.5.2 (2026-05-05) and npm 1.7.1 (2026-05-16, `isLatest`) | **Verified. Stale: we're on 3.0.7** |
| LangChain integrations, all providers page | Adeu / `langchain-adeu` | Reported (directory subagent) |
| Gemini CLI extensions gallery | `@dealfluence/adeu` | Verified via search snippet of geminicli.com/extensions |
| Glama | `glama.ai/mcp/servers/dealfluence/adeu` | Verified via search snippet; quality grade "A" reported, not checked |
| ClaudePluginHub | plugin `adeu-redlining` (shows v1.14.0, 162★: stale snapshot) | Verified via search snippet |
| agentmods.dev, flowy.sh, mcpmarket.com (skills, "Professional DOCX Redlining", installed via `skillfish`) | the `adeu-redlining` skill | Verified via search snippets |
| PulseMCP | "official", ~16.9k estimated visitors | Reported; direct fetch blocked by Cloudflare |
| Agentage catalog, odel.app, LobeHub (6 installs), ncnodes, Fatstack (issue #136), allmcps, Freshcrate | aggregators fed by the MCP Registry or npm | Reported |
| Awesome lists: `appcypher/awesome-mcp-servers`, `tanko-r/awesome-legal-tech`, `Vaquill-AI/awesome-legaltech` | Adeu entries | Reported (directory subagent) |
| Smithery | `smithery.ai/servers/adeu/adeu` | **Dead**: Smithery shut down hosted installs on 2026-10-31 (issue #182) |

**Not listed** (reported by the directory subagent): punkpeye/awesome-mcp-servers (it says our PR #5896 was closed unmerged; I didn't re-check), wong2/awesome-mcp-servers, mcp.so, mcp.directory, Cline MCP marketplace, Docker MCP catalog, n8n Cloud verified nodes, n8n community forum (0 threads), Composio, Hugging Face, Stanford CodeX TechIndex.

---

## 4. Who is using Adeu: named projects

### 4.1 Confirmed in public code (verified)

| Project | Stars | What they use, and how | Evidence |
|---|---|---|---|
| **Deodat-Lawson/LaunchStack** | 897 | PyPI `adeu`. A dedicated Python microservice, `services/adeu-ai-docs-editing`, for tracked-changes Word editing inside their platform. The only third-party repo on our GitHub dependents graph. | GitHub dependents page; `api.github.com/repos/Deodat-Lawson/LaunchStack/contents/services` |
| **gregmos/PII-Shield** (Grigorii Moskalev) | 153 | npm `@adeu/core ^1.8.0`. PII anonymisation for legal documents; uses `RedlineEngine` to emit the anonymisation as tracked changes ("REDLINE mode"). Ships as an MCP `.mcpb` plus a CLI. | `nodejs-v2/package.json` |
| **akunikkola/claude-for-legal-finland** (Aku Nikkola) | 115 | `npx -y @adeu/mcp-server`, configured as the redlining server across its Finnish-law Claude plugins, next to `oik.ai` case-law search. Aku Nikkola runs Legit/Haavi and also demoed at the DLA Piper event (section 5). | `juristi/.mcp.json` |
| **sarturko-maker/Claude-Plugin-MCP** (`claude-contract-negotiator` v2.0.0) | 14 | PyPI `adeu>=0.7.0`. Multi-round contract negotiation with layered tracked changes. The handle suggests Artur Serov (not verified). | `pyproject.toml` |

### 4.2 Confirmed users without a public dependency file

| Who | Evidence | What |
|---|---|---|
| **Artur Serov**, Head of EMEA Legal & Compliance (Wesco), LegalQuants contributor | His LinkedIn post of 2026-08-15, reshared by Adeu and LegalQuants (verified via search) | An open-source LQ.AI fork on LangGraph Deep Agents for in-house counsel. In his words, Adeu supplies "redlining tools (Adeu's real tracked changes in Word)". A legalquants.com profile also lists his "Oscar GC" Electron app using "a contract-redline service". |
| **Vela Wood** (`vela-wood/adeu` fork) | Fork verified; pushed **2026-10-19** | Fork with recent commits. The subagent identifies Vela Wood as a venture/corporate law firm (not verified). |
| **Ahti Climate** (FuelEU Maritime compliance) | adeu.ai/case-studies/ahti-climate (verified) | Customer of the commercial Adeu product running "Adeu in Claude Desktop", for same-day redlines of inbound contracts. It's the hosted product, not strictly the OSS packages. |
| **Fondia** (CEO Timo Lappi) | Testimonial on adeu.ai | Reported: the testimonial is for "Adeu Ambient", not the OSS packages. |

### 4.3 Developers who identified themselves through issues (verified issue titles and bodies)

These are people building on the library, from the GitHub issues API. "Third-party" here means not mkorpela, Git-Uzair, or a bot.

| Handle | Issue / PR | What it tells us |
|---|---|---|
| `bradvogel` | #148 | Builds contract generation on `@adeu/core` 3.0.5; hit underscore fill-in blanks being dropped |
| `viresh-patensity` | #114 | `@adeu/core` 2.4.1 on Node 24 / Windows; the handle suggests Patensity (patent tooling), which is a likely commercial user |
| `vidrpatel` | #112, #124 | Deep engine bugs (Markdown bullet misparse, `Object.prototype` token collisions). Someone feeding real documents through it. |
| `Rubenkl` | #23, #26 | Used `@adeu/mcp-server` from Claude Code; found the `comments.xml` namespace bug and a 1.11.2 startup crash |
| `magichomie2019-web` | #34 | Comment duplication across runs |
| `houfu` | #157, #158, #159 | Author of PyPI `redlines` (3.7M downloads). Wrote a competitive analysis of Adeu (section 6.2). |
| `Dmitry-Kov` | #137, #139, PRs #138, #140, #156, #161 | Author of `ooxml-integrity`. Contributed fixtures, and Adeu now dev-depends on his validator (Dependabot PR #185). |
| `6uimorais` | #141, PR #142 | Fixed duplicate `w:del` IDs in both engines |
| `vishalhabib99` | PR #163 | Search match-budget fix |
| `ruhroh` | PR #98 | Made `fastmcp` an optional extra (55 MB vs 144 MB install), which shows someone embedding the CLI and SDK without MCP. Closed without a merge flag. |
| `zriyansh` | #20 | Pairing Adeu with Vaquill legal-research MCPs; asked for jurisdiction-aware validation hooks |
| `itsbrex`, `mangodxd` | PRs #44, #9 | Documentation and housekeeping |
| `stillmarcus24` (Still OS Digital Holdings) | #176 | Arbitrary-file-write report against `@adeu/mcp-server` 3.0.7. Filed publicly because the repo has no security contact. Closed as completed. |
| `dazzaji` | #1 | First external issue (Jan 2026): VS Code visualisation workflow |

### 4.4 Visibility, but not usage

| Project | Stars | What |
|---|---|---|
| **NVIDIA/SkillSpector** | 24,423 | Security scanner for agent skills and MCP servers. `tests/fixtures/mcp_registry/mcp_registry.json` includes `ai.adeu/adeu` → `@adeu/mcp-server@1.7.1` as one of three sample registry entries (verified). |
| dheerajjha/mcp-migrate | 12 | MCP spec-migration linter. The subagent reports Adeu is used as a test fixture (repo verified; file not checked). |
| saagpatel/mcp-trust | 1 | Seeds `@adeu/mcp-server` 1.7.1 as a scan candidate (reported) |

### 4.5 Forks (28 in total, GitHub API)

- **Forks with real activity:**
  - vela-wood (10-19)
  - MorkMindy74 (10-04)
  - Dmitry-Kov (10-04)
  - itsbrex (09-29)
  - 6uimorais (09-14)
- **Renamed forks, showing intent:**
  - `karolinaranjo/word-agentic-pipeline`
  - `legalninja33/adeu_docx_redlining`
  - `WilliamACLove/adeu-redlines-`
  - `iflow-mcp/dealfluence-adeu`, an MCP aggregator mirror
- **Organisation forks:**
  - cicero-im
  - ockhamlabs
  - mseep-ai, an MCP mirror bot
  - ceh-forks
  - Secure-Code-Zyloch-Basic, a security-scanner style org
- The other 13 are one-shot forks with no commits after forking.

---

## 5. Public mentions, in date order

| Date | Where | What | Status |
|---|---|---|---|
| 2025-06-18 | LegalTechTalk hackathon, London | Dealfluence took 1st place with an early redlining workflow (per adeu.ai) | Reported |
| 2026-01-26 | adeu.ai blog | Launch of Adeu OSS plus the commercial product | Reported |
| 2026-05-05 / 05-16 | Official MCP Registry | `ai.adeu/adeu` published | Verified |
| 2026-06-16 | `dealfluence/docx-benchmark` | Our own agentic DOCX benchmark (AGPL) | Reported |
| 2026-06-16 | `LegalQuants/adeu-demo` | Repo from Mikko's webinar on `.env`, hooks and Actions. **Not a usage of Adeu**, despite the name. | Verified |
| 2026-08-15 | LinkedIn, Artur Serov | Open-source LQ.AI Deep Agents fork that redlines with Adeu; 7–8 reactions | Verified via search |
| 2026-08-26 | houfu/redlines, `docs/competitive-landscape-2026-08.md` | A full section on Adeu, with a comparison matrix; proposes "redlines computes the structural diff, adeu writes the tracked changes" | Verified |
| 2026-08-30 | Reddit r/n8n | Announcement thread for `n8n-nodes-adeu` | Reported (Reddit blocks fetches) |
| 2026-09-28 | Hacker News, "Launch HN: Vespper (YC F24) – SOTA Docx MCP" (38 points) | The founders name "SuperDoc, Office CLI, Adeu" as the established opinionated-editing MCPs | Verified (HN API) |
| 2026-10-23 | DLA Piper Finland, Helsinki breakfast event | Janne Mäkelä demoed "Adeu: Sopimusprosessin automatisointi" (contract-process automation), alongside Fortum's legal-tech director | Verified |
| 2026-10-24 | vespper.com/blog/docx-mcps-harvey-lab (also on HN, 2 points; r/legaltech) | Independent benchmark including Adeu 3.0.7 (section 6.1) | Verified |
| 2026-10-27 | adeu.ai case study | Ahti Climate | Verified |

**Nothing found** (web subagent): no Show HN or Product Hunt launch of our own, no coverage in Artificial Lawyer, LawSites, Legal IT Insider or Law.com, no Stack Overflow questions, no YouTube tutorials, no academic citations, no newsletter features (TLDR, Ben's Bites, PyCoder's, Python Weekly). Organic, community-led discussion is still thin. Adoption is happening in code, not in press.

---

## 6. How the market sees us

### 6.1 Vespper's Harvey LAB benchmark (2026-10-24, verified)

**Setup:**

- **Arms:** SuperDoc MCP 1.5.0, Office CLI 1.0.155, **Adeu MCP 3.0.7**, Stemma MCP 0.6.0, and a python-docx + LibreOffice "DOCX skill" baseline.
- **Tasks:** 180 Harvey LAB editing tasks.
- **Model:** grok-4.7, used as both agent and judge.
- **Harness:** Harvey's, which has no context compaction.

**Results:**

- **Completion rate:** the baseline finished 180/180. Among MCPs, **Adeu had the highest completion at 94%**, and SuperDoc the lowest at 61% (its context blew up on oversized inspect responses).
- **Score and cost:** the baseline had the best mean rubric score and was cheapest by about 2×. OfficeCLI had the best all-pass rate.
- **Tokens:** on the editing phase, MCP arms used a median of 11.2M+ input tokens against about 6M for the baseline.
- **Cheating** (the agent bypassing the MCP for python-docx or pandoc): rare across all arms.

**Takeaways:**

- The benchmark comes from a competitor.
- Our edge there is robustness on long multi-turn runs.
- The weakness the whole MCP category shares is token cost, and that is the line competitors will attack.

### 6.2 houfu's competitive landscape (2026-08-26, verified)

- **Category:** classifies Adeu as an "edit applier" (LLM edits a projection, and the edits are written back as `w:ins`/`w:del`), next to superdoc-redlines, docx-mcp and Anthropic's docx skill.
- **Strengths:**
  - **"Distribution is where adeu is strongest"**: plugin, skill, `.mcpb`, Gemini extension, n8n, LangChain, benchmark.
  - The TextSpan-mapped projection is a reader worth borrowing rather than rebuilding.
- **Weaknesses:**
  - `adeu diff` is a thin word-level diff: no move detection, no formatting changes.
  - Addressing by `target_text` search is "a known ceiling" (it cites issues #28 and #29 asking for coordinate targeting).
- **Adoption estimate at the time:** about 91k lifetime downloads and about 20k per 30 days. Today it's about 33k per 30 days.
- **Collaboration:** it explicitly suggests reaching out to Dealfluence for a joint "structural diff plus tracked-change writer" integration.

---

## 7. Adoption by package: how much each one matters

| Package | Signal | Verdict |
|---|---|---|
| PyPI `adeu` | 33k/30d, rising; uvx-style Python 3.14 traffic; pinned production versions | **Core asset.** MCP server, CLI and SDK all live here. |
| npm `@adeu/core` | 10.7k/30d; at least 2 public dependents; about 1k/week from a hidden 3.0.2 pin | **Healthy SDK adoption**, mostly invisible consumers |
| npm `@adeu/mcp-server` | 2.9k/30d; 85% on latest | Healthy; the npx path for Claude/Cursor users |
| Claude Code plugin / Agent Skill | 34 skills.sh installs; listed on 4+ skill directories | Early; directories are copying it automatically |
| Claude Desktop `.mcpb` | 283 downloads in total | Minor |
| `langchain-adeu` | ~0.9k (pypistats) to 3k (pepy) per 30d | Weak. Listed in LangChain's docs, but little pull. |
| `n8n-nodes-adeu` | 342/30d, mostly crawler-shaped; 0 forum threads; not a verified n8n Cloud node | **Weakest channel** |

---

## 8. Action items this research surfaced

1. **Republish to the MCP Registry on every release.** `ai.adeu/adeu` is frozen at npm 1.7.1 / PyPI 1.5.2. Two scanners (NVIDIA SkillSpector, mcp-trust) and every registry-fed directory and client show or install 1.7.1, which is the 87 downloads a week on `@adeu/mcp-server@1.7.1`. Automate `server.json` publishing in the release workflow.
2. **Remove the Smithery badge and install section from the README** (`README.md:8`, `README.md:91-94`). Smithery stopped hosted installs on 2026-10-31. Issue #182 was closed, but the README still advertises it.
3. **Add `SECURITY.md` and enable GitHub private vulnerability reporting.** #176 (arbitrary file write in `@adeu/mcp-server` 3.0.7) was filed in public because there was no private channel. Next time it could be a 0-day posted with a proof of concept.
4. **Follow up with houfu (redlines, 3.7M downloads).** He has publicly proposed a joint "redlines diff → adeu writer" story. That would cover our weakest area (diff and move detection) and give access to a much larger user base.
5. **Answer the token-cost critique.** Vespper's benchmark is the only independent number in the market, and it frames DOCX MCPs as about 2× the cost of python-docx. A response, for example a compact read mode, or our own Harvey LAB run with a compaction-aware harness, would be worth more than any listing.
6. **Decide whether n8n and LangChain are worth their maintenance cost.** Together they are under 5% of downloads.
7. **Get onto the high-traffic lists we're missing:** punkpeye/awesome-mcp-servers (the subagent says a previous PR was closed unmerged), mcp.so, the Docker MCP catalog, and the Cline marketplace.

---

## 9. Corrections to figures circulating elsewhere

Numbers from aggregators and from one subagent that the primary sources contradict. Do not reuse them.

| Claim | Source of claim | Primary value |
|---|---|---|
| 347 stars, 28 forks, 10 watchers, created 2026-01-14 | research subagent | **170 / 26 / 2, created 2025-12-30** (GitHub API) |
| PyPI `adeu` 11,263/month | directory subagent, pypistats `/recent` | **32,892** (pypistats daily series); pepy 37.9k |
| `langchain-adeu` 10,767/month | directory subagent | **918** (pypistats) to 3,044 (pepy) |
| `n8n-nodes-adeu` 971/week, 2,562/month | ncnodes via subagent | **37/week, 342/month** (npm API) |
| Lawve AI MCP listing | web subagent | URL returns 404 |
| `LegalQuants/adeu-demo` shows usage | web subagent | Webinar repo about `.env`, hooks and Actions; not an Adeu consumer |
| GitHub repo created 2026-03-22 | directory subagent | 2025-12-30 |

---

## 10. Gaps: what this research could not see

- **GitHub code search, the stargazer list, and npm "dependents" web pages** all need authentication or are behind Cloudflare. A logged-in search for `@adeu/core`, `from adeu import`, `langchain_adeu` and `@adeu/mcp-server` across GitHub would likely find more dependents. This is the highest-yield follow-up.
- **The hidden pinned consumers** (`@adeu/core@3.0.2`, `adeu==3.0.4`) can't be identified from public data. npm and PyPI don't expose downloader identity. BigQuery's `pypi.file_downloads` table could break 3.0.4 traffic down by installer and CI fingerprint.
- **Reddit, Product Hunt, AlternativeTo, PulseMCP and There's An AI For That** blocked automated fetches. Mentions there come from search snippets only.
- **LinkedIn** content was seen through search snippets only. Reaction counts are as snippeted.
- **The 2026-09-22 spike** (1,713 downloads in one day) has no identified cause.

## Sources fetched this session

- **Registries and package statistics:**
  - pypistats.org API (`/recent`, `/overall`, `/system`, `/python_minor`)
  - pepy.tech/projects/adeu and /langchain-adeu
  - pypi.org/pypi/adeu/json
  - libraries.io/pypi/adeu
  - api.npmjs.org downloads and versions endpoints
  - registry.npmjs.org search
  - api.deps.dev dependents
  - registry.modelcontextprotocol.io/v0/servers?search=adeu
  - skills.sh/dealfluence/adeu
- **GitHub API:**
  - repos/dealfluence/adeu
  - /forks
  - /issues (pages 1–2)
  - /releases
  - github.com/dealfluence/adeu/network/dependents
- **Third-party repositories:**
  - gregmos/PII-Shield `nodejs-v2/package.json`
  - akunikkola/claude-for-legal-finland `juristi/.mcp.json`
  - sarturko-maker/Claude-Plugin-MCP `pyproject.toml`
  - Deodat-Lawson/LaunchStack `services/`
  - NVIDIA/SkillSpector repo and `tests/fixtures/mcp_registry/mcp_registry.json`
  - dheerajjha/mcp-migrate
  - LegalQuants/adeu-demo
  - houfu/redlines `docs/competitive-landscape-2026-08.md`
- **Web pages:**
  - vespper.com/blog/docx-mcps-harvey-lab
  - hn.algolia.com item 49881505
  - adeu.ai/case-studies/ahti-climate
  - finland.dlapiper.com event page (2026-10-23)
- **Search results seen** (not opened): Artur Serov / Adeu / LegalQuants LinkedIn posts, ClaudePluginHub, agentmods.dev, flowy.sh, mcpmarket.com, geminicli.com/extensions, glama.ai.
