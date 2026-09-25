# Domain subagents — one per piece of the app — Plan

**Date:** 2026-09-24
**Status:** PARTIALLY BUILT — the 14 agent files under `.claude/agents/`, the ownership map, its scanner and gating test, the `.gitignore` exception and the CLAUDE.md pointer were written 2026-09-24 and pass locally (§8). The same 14 agents plus a dispatcher were also built into a local Paperclip instance as the company "GDX Dispatch Code" the same day and smoke-run there (§10). Landing on branch `fix/paperclip-run-speed` together with the run-speed fixes of §12 (PR number added when opened). No agent has driven a real *change* yet; §9 names what "built" still owes.
**See also:** `gdx_dispatch/docs/decisions/ADR-012-parallel-worker-coordination.md` (2026-04-18, Proposed; the *role* axis — Architect / Worker / Reviewer — whose runtime was extracted from this repo on 2026-04-20). This plan is the *domain* axis. It does not supersede ADR-012 and ADR-012 does not name these files; the two compose. The user-level verifier `~/.claude/agents/agent-quality.md` is unchanged.
**Ask (Doug, 2026-09-24):** "let's break down this app into pieces that we can have custom agents that are able to work on it."

---

## 1. What already exists (do not rebuild)

| Piece | State | Where |
|---|---|---|
| Claude Code project subagents: one `.md` per agent with `name` and `description` frontmatter (optional `tools`, `disallowedTools`, `model`, `memory`, `isolation`, `skills`, `hooks`, …). A subagent's context includes *every* CLAUDE.md level, and every `PreToolUse` hook in the user's settings file runs inside it. Subagents may spawn subagents to depth 3. | platform feature, docs read 2026-09-24 | https://code.claude.com/docs/en/sub-agents |
| `agent-quality` — a verifier with no Edit/Write tool, user-level | shipped 2026-08-12 | `~/.claude/agents/agent-quality.md` |
| ADR-012: role-scoped skills, work-orders with `files_in` / `files_out`, worktrees, a merge queue | Proposed 2026-04-18; the `/architect` skill exists, the loop runtime was extracted | `gdx_dispatch/docs/decisions/ADR-012-parallel-worker-coordination.md` |
| The app's own feature map: 27 module keys plus 9 legacy aliases | shipped | `gdx_dispatch/core/modules.py` (`MODULES`, `LEGACY_MODULE_ALIASES`) |
| Tracked-set enumeration without the `git` binary (the docker image has none) | shipped 2026-09-12 | `gdx_dispatch/tools/tracked_files.py` |
| `.gitignore` ignored all of `.claude/` except `settings.json` <!-- link-ok --> | true until this change | `.gitignore` line 74 |

**Prior art searched 2026-09-24** (firecrawl: *Claude Code custom subagents per domain, agents directory, codebase ownership map, monorepo pattern*): six hits, all generic how-to guides for writing a subagent file (mindstudio.ai, hidekazu-konishi.com, lumadock.com, vibecoding.app, dotclaude.com, a DeepWiki course page). None decomposes a particular codebase and none guards the decomposition. Still building, because the value is entirely in the repo-specific map and its guard.

## 2. Decision

Fourteen **project-scoped** subagents, checked in under `.claude/agents/`, one per piece of the app. **Which files each owns is data, not prose:** `gdx_dispatch/tools/agent_ownership.txt`, read by `gdx_dispatch/tools/agent_ownership_scan.py` and gated in the default suite by `gdx_dispatch/tests/test_agent_ownership.py`.

| Agent | Owns | Files | The rule that bites hardest |
|---|---|---|---|
| `platform-core` | app wiring, identity and RBAC, audit, database, every migration, settings/admin, scheduler, middleware, observability, scanners, CI | 137 | invariants 1–3, 5, 6 are `documented-only`; migrations run on SQLite and Postgres |
| `frontend-shell` | App/layout/router/stores, shared composables, theme, i18n, help, test harness, lockfile, Dashboard | 74 | jsdom has no media queries; lockfile only on npm 11 |
| `money-billing` | invoices, payments, Stripe, deposits, GL, tax, collections, reminders, statements, closeout-to-invoice | 52 | never sales tax; void is terminal; refunds credit-first |
| `jobs-dispatch` | jobs, closeouts, scheduling, dispatch board, planner, service calls/agreements, checklists, GPS/maps, warranties | 68 | billed labor from attested hours only; one closeout reader, one jobsite rule |
| `estimates-pricing` | estimates, proposals, change orders, catalog, door catalog/listings, pricing engine, labor pricing, margins | 57 | no Midland multiplier; commission is out of core; quantity as recorded |
| `customers-crm` | customers, contacts, locations, leads, segments, tags, loyalty, referrals, reviews, surveys, winback, portal | 34 | PII is encrypted — raw SQL matches nothing; portal is customer-facing |
| `comms-email-phone` | Outlook/Graph, Phone.com, inbound email, cell gateway, notifications, push, transactional email, team chat | 76 | webhook routes are public by design; every customer email through one layout |
| `back-office-books` | QuickBooks, bank feeds, vendor bills/statements (AP), expenses, budgets, overhead, forecasting, reports, exports | 90 | never a new QB sync; QB reads are metered |
| `people-time` | technicians, timeclock, time off, pay periods, timesheets, payroll, efficiency, commission boundary | 35 | one hours authority; PTO is a timeclock entry type (#770); balances are a PLAN |
| `mobile-tech` | `/api/mobile/*`, every `Mobile*` view and component, bottom nav, offline and photo queues, PWA | 42 | the surface owns, the domain's rules apply; verify on the Pixel 8 AVD |
| `inventory-purchasing` | stock, van inventory, parts to order, purchase orders, vendor master, on-order tracking, distributor portal | 25 | parts used must reach the invoice (unbilled-parts gate) |
| `documents-media` | documents, uploads, photos, PDF generation and templates, signatures, branding | 29 | object-level attachment authz; CodeQL recognizes only three path-check shapes |
| `plugins-host` | `plugin_api`, plugin-host container, admin install, proxies, storefront, browser stream, public plugins | 25 | no egress in prod; `pip --target` leaves old code in place |
| `ai-mcp` | AI routers, provider, LLM client, MCP server and its 44 tools, capabilities, recommendations, assistant UI | 71 | tool contract is theirs, semantics are the domain's; three access layers |

File counts are from the scan on 2026-09-24 (§8) and will drift; the scan is the source.

## 3. Why these seams

- **Ten verticals follow the code's own boundaries.** Routers, `modules/` packages and views already cluster by noun; the module registry in `core/modules.py` names most of the same clusters. Assignment was made per file from each router's docstring and prefix, not from its filename.
- **Three are cross-cutting surfaces, not verticals.** `platform-core` is what everything stands on (and owns every migration, whoever's table it changes, because portability is one rule). `frontend-shell` is the app the views render inside. `mobile-tech` is the technician's phone — a surface that renders every domain's rules. For mobile the rule is written down in the agent file: **the surface owns the file, the domain owns the rule, and the domain reviews.** `routers/mobile_invoicing.py` is mobile-tech's file under money-billing's rules.
- **`ai-mcp` mirrors the domains one tool at a time.** It owns the tool contract (descriptor, confirm colour, error schema); `invoices_void` does exactly what money-billing's void does. A tool that writes a table around a domain service is a defect in both territories.
- **Money and the books are separate on purpose.** `money-billing` is where a dollar is created or moved and where the three HARDENING money defects lived (#422, #445, #661). `back-office-books` is reconciliation after the fact, with two third-party integrations under the opposite rule (QuickBooks is being phased out; the GL here is the source of truth). Merging them would put "never schedule a new QB sync" and "the ledger chokepoint is terminal" in one file, where one of them gets skimmed.
- **Churn agrees.** Over the last ~200 commits the frontend, migrations, `core/mcp_tools`, `modules/outlook`, `modules/bank_feeds`, `modules/ledger`, `modules/quickbooks` and `routers/invoices.py` lead — each lands squarely inside one owner above.

## 4. Shape choices

- **Agents inherit CLAUDE.md** (documented upstream), so each body carries only what CLAUDE.md does not: the territory, the domain rules that bite, the neighbours, a verification recipe with the test patterns by name, and the report shape. Nothing from CLAUDE.md is duplicated; "where the two disagree, CLAUDE.md wins" is in every file.
- **No `tools:` restriction.** These are builders. The restricted one is the verifier, `agent-quality`, which keeps no Edit/Write on purpose. A restricted reviewer variant per domain is a possible follow-up (§7).
- **No `memory:` field.** The auto-memory index (2026-09-12, outside the repo under `~/.claude/projects/`) records why a private, drifting copy of a maintained file is worse than none. The map and CLAUDE.md are the memory; they are reviewed with the code.
- **No `model:` pin.** Inherits the session's model. Pinning is Doug's call (§7).
- **Map semantics:** fnmatch globs (so `*` crosses `/`), **last match wins**, and **no catch-all**. A catch-all would make the "every file has an owner" check unable to fail, and a check that cannot fail is decoration (the 2026-09-01 lesson from a sibling ratchet). Every `core/*.py` is listed by name.
- **The guard enumerates the tracked set**, not the filesystem, for the reason `tracked_files.py` records: a gitignored local path must not turn a guard green here and red in CI. `--worktree` exists for previewing an unstaged file and the test never uses it.
- **Four assertions plus a self-test:** no unowned file; no dead rule (a renamed file's rule goes red); every owner has an agent file; every agent file owns something; and the scanner is shown to report an unowned path.
- **`.claude/agents/` is un-ignored**; agent-memory directories stay ignored (`.git/info/exclude` already listed `agent-memory-local`).

## 5. Rejected alternatives

1. **Roles only (ADR-012's Architect / Worker / Reviewer).** Not rival — complementary. A Worker still needs to know which files are its slice; that is what the map answers. ADR-012's `files_in` / `files_out` per work-order was the same idea without a guard.
2. **One agent per `MODULES` key.** 27 keys plus 9 aliases; several are retired or folded (campaigns, the communications shell, proposals into estimates, change orders into estimates). 27 files nobody would keep current.
3. **Six big agents.** Rejected for the money/books reason in §3, and because Outlook plus Phone.com (14k lines of third-party surface) would have shared a file with the customer record.
4. **A catch-all rule per directory.** §4.
5. **`memory: project` on each agent.** §4.
6. **User-level `~/.claude/agents/`.** These are codebase-specific and should be reviewed with the code, which is the upstream recommendation; it only needed the `.gitignore` exception.
7. **Keeping the map inside `.claude/`.** The test runs in the docker image against the tracked set; the map lives in `gdx_dispatch/tools/` next to the other baselines it resembles.
8. **GitHub `CODEOWNERS` format.** Needs real GitHub accounts (ADR-012 §4); agents cannot appear in it.

## 6. How to use and keep it true

- Invoke with the `Agent` tool, `subagent_type: money-billing` (or any name in §2). Descriptions are written so the main session can pick the owner from a task statement; Doug can also name one.
- `python -m gdx_dispatch.tools.agent_ownership_scan --owner <name>` lists an agent's files; `--file <path>` names the owner of one path.
- **Cross-owner edits are allowed when mechanical and required by the change** (a caller signature) and are listed separately in the agent's report; anything more is handed to the owner by name.
- **Adding a router, view, module, composable or core file means adding a rule**, or `test_agent_ownership.py` is red. Renaming an agent means renaming its file and its rules. Deleting a file means deleting its rule (the dead-rule check).
- The three sharp edges every agent file repeats deliberately, because they are the ones a fresh context skips: enumerate every FAIL and SKIP by name; a mock proves arguments, not results; docs state, code proves.

## 7. Open questions (Doug's calls)

1. **Model pins.** Should small territories (`inventory-purchasing`, `documents-media`) run on a cheaper model, or does everything inherit?
2. **A reviewer variant.** A read-only twin per domain (no Edit/Write, like `agent-quality`) for adversarial review before a PR — or is `/audit` enough?
3. **Worktree isolation.** `isolation: worktree` per invocation keeps parallel agents off each other's files (ADR-012 §3); default it, or leave per call?
4. **`routers/games.py` and the `/admin/games` views** belong to no business domain; they are `platform-core`'s by default. What is the games surface, and who should own it?

## 8. Verification (2026-09-24, local, staged, not merged)

Tracked-mode scan, `python -m gdx_dispatch.tools.agent_ownership_scan --strict`, exit 0:

```
Owner                  files
  ai-mcp                 71
  back-office-books      90
  comms-email-phone      76
  customers-crm          34
  documents-media        29
  estimates-pricing      57
  frontend-shell         74
  inventory-purchasing   25
  jobs-dispatch          68
  mobile-tech            42
  money-billing          52
  people-time            35
  platform-core          137
  plugins-host           25
  (total)                815

clean
```

Guard and neighbours in the docker image (`docker-app`, no venv on the host):

```
pytest -rs gdx_dispatch/tests/test_agent_ownership.py gdx_dispatch/tests/test_doc_link_scan.py gdx_dispatch/tests/test_tracked_files.py
25 passed in 1.59s
```

`ruff check` on the scanner and the test: all checks passed (ruff 0.15.8). Frontmatter of all 14 files parsed: `name` equals the file stem, `description` present, no other keys; descriptions total ~1,000 words (~1.4k tokens against the documented 15k limit).

**Not verified:** discovery of the 14 agents by a fresh Claude Code session (they are read at session start; this session predates them); any agent driving a real change; the Postgres arm of anything (not touched).

## 9. What "built" still owes

- Drive one bounded change through one agent and record, here, whether it stayed inside its territory, applied the neighbour rule, and enumerated its tests. Until then the agent bodies are a design, not a measured tool.
- A PR, then `MERGED #N` on line 3.
- Rulings on §7.

## 10. Paperclip build — company "GDX Dispatch Code" (2026-09-24)

**Ask (Doug, 2026-09-24):** "could this be built into paperclip" → "Build it in paperclip under a new organization called GDX Dispatch Code."

Paperclip is the agent-orchestration server evaluated on 2026-09-24 (v2026.916.1, local instance under `~/paperclip-eval`, not a service; how to reach it is in the maintainer's memory notes, not here). Nothing about it lives in this repo except the "Unattended runs" section now in every agent file. The mapping:

| Paperclip object | What it is here | Binding |
|---|---|---|
| Company `GDX Dispatch Code` (issue prefix `GDXA`) | the organization | created with the CLI |
| Project `gdx_dispatch`, lead agent `dispatcher` | the repository | project workspace `local_path` → the maintainer's main checkout, `defaultRef: main` |
| Execution workspace policy | one git worktree per issue | `isolated_workspace`, strategy `git_worktree`, `baseRef: main`, branch `paperclip/{{issue.identifier}}-{{slug}}`, parent `~/github_gdx_dispatch/paperclip-worktrees` |
| 14 engineer agents, one per §2 row, `reportsTo: dispatcher` | the domain agents | adapter `claude_local`, `engine: "cli"`, `dangerouslySkipPermissions: false`, heartbeat off, one run at a time; instructions bundle mode **external** pointing at `.claude/agents/<name>.md` in the main checkout, so the file in this repo is the single source and Paperclip holds no copy |
| Agent `dispatcher` (role `pm`, `canAssignTasks`) | the router | managed bundle whose whole instruction is: resolve owners with `agent_ownership_scan --file`, reassign or split per owner, never assign merge/release/prod work, comment the scan output |

**Why `engine: "cli"` is not optional.** The 2026-09-24 eval showed Paperclip's default engine (ACP via `claude-agent-acp`) launches Claude with `--setting-sources=project,local`, so every user-scope hook (the commit gate, the verification gate, the credential guard) never loads, and its permission default is approve-all. The CLI engine spawns `claude --print` with HOME unchanged. Evidence from this build: run `d289131c` (GDXA-1) and run `ab6d99e4` (GDXA-2) each logged 5 `SessionStart` hook starts and 5 responses, including the session-checklist banner.

**Why the runtime protocol is in the agent files.** Claude Code CLI 2.1.280 refuses `--append-system-prompt` together with `--append-system-prompt-file`, and the external bundle already uses the file flag. A managed copy of each agent file was rejected as a drifting copy. So each file carries an "Unattended runs" section conditioned on `PAPERCLIP_TASK_ID`: work on the provisioned worktree's branch, push and open a **draft** PR when the issue asks for a change, never merge/release/touch prod or demo, report as the final issue comment, and stop (mark blocked) if the working directory is not under `paperclip-worktrees`.

**Smoke runs (read-only issues; nothing in the repo was modified by any of them):**

| Issue | Agent | Result | What it proved |
|---|---|---|---|
| GDXA-1 | money-billing | succeeded, 9 turns, 55 s, $0.84, report posted as a comment | hooks fire; the agent read its own file, counted its 52 files, and **noticed it was in the primary checkout on `main`** and refused to act on that |
| GDXA-2 | platform-core | succeeded, 12 turns, 109 s, $1.03, report posted | `git worktree add -b paperclip/GDXA-2-…` from `main`, session cwd = the worktree, clean tree; also found that the scanner and map were absent there |
| GDXA-3 | dispatcher → money-billing | dispatcher run cancelled by the server the moment it reassigned (by design), owner run succeeded, 8 turns, 54 s, $0.82, closed `done` | the dispatcher resolved both files to money-billing, said plainly that the scan was absent from its worktree instead of inventing output, commented, reassigned; **the reassignment did not start the owner's run** — a manual wake did; hooks fired in the owner's run (5) |

GDXA-3's gap, as first read: the issues route wakes the new assignee on an assignee change (reason `issue_assigned`), but when the change is made by an agent from inside its own run the server first cancels that run ("Cancelled before issue reassignment") and no run had been queued for the new assignee two minutes later; `agent wake` started it at once. **Corrected later the same day:** triage's reassignments of GDXA-8 and GDXA-9 did start dispatcher runs on their own, two to three minutes after the cancellation, once the cancelled run's workspace finalized. The wake is delayed, not absent; the two-minute reads were impatience. Two facts still stand: the reassigning agent cannot comment or verify after the PATCH because its run is gone, and an agent cannot wake another agent at all (`POST /api/agents/{id}/wakeup` with an agent token returns 403 "Agent can only invoke itself", found by the intake agent on GDXA-11). So the child-issue pattern remains the right delegation shape, every "wake it yourself" step was removed from the dispatcher, triage and intake instructions, and verification is "read the issue's run within a few minutes, escalate after five". The dispatcher's instructions now require an explicit `POST /api/agents/{id}/wakeup` after every reassignment, verified by reading back a queued run, and a read-only fallback to the maintainer's checkout for the scan until the map is on `main`.

GDXA-1's finding had a cause: the project policy was silently ignored because the instance experimental flag `enableIsolatedWorkspaces` was off (`gateProjectExecutionWorkspacePolicy` returns null without it). Enabled on the instance 2026-09-24; GDXA-2 ran after. GDXA-2's finding is a state fact, not a defect: the map, scanner and guard are staged in the main checkout but not yet on `main`, and a worktree is cut from `main`. It resolves when this plan's PR merges. Until then an unattended run cannot run the ownership scan.

**Facts the build established (all verified, see the run logs):**

- Paperclip's Claude adapter defaults to `claude-opus-5` when `adapterConfig.model` is unset, overriding the user's own default. The 15 agents have no pin (§7.1 stands).
- Assigning an issue starts a run immediately; the heartbeat setting does not gate that.
- Execution workspaces persist after a run as `active` records with the worktree on disk; closing is a Paperclip action (`workspace close-readiness` reports `ready` / `merged_by_ancestry` for GDXA-2, planned action "archive record"). Worktrees will accumulate under `paperclip-worktrees` until closed.
- Every run in the project provisions a worktree, including the dispatcher's routing runs.
- `issue get --json` does not embed comments; `GET /api/issues/{id}/comments` does.

**Not in this repo, deliberately:** company and agent ids, the instance path, the node binary. They are Paperclip state and would rot here; `paperclipai company list` and `agent list` are one call.

## 11. Working the found-not-filed ledger through Paperclip (2026-09-24)

**Ask (Doug, 2026-09-24):** "what would be the best way for it to work on the found not filed" → "Lets do this … maybe we need a triage agent before it goes to the dispatcher", and: a restated question is handled inside verification, not bounced back.

**The rule that shapes it:** the ruling stays with the maintainer. The ledger exists because intake was pure cost when Claude judged its own findings (CLAUDE.md, *One issue per session*), so nothing in Paperclip picks its own work from it. The flow is: the maintainer rules on an entry → it becomes a GDXA issue → **triage** re-verifies it and rules on the evidence → the **dispatcher** routes a `real` one by the ownership map → the owner fixes the class and opens a draft pull request → the maintainer lands it → the entry moves to "Ruled / closed" with the PR number.

**What was built** (all outside this repo, in the local Paperclip instance and beside it):

| Piece | What it does |
|---|---|
| Agent `triage` (role qa, reports to the dispatcher) | Managed instructions: research → verify → rule → record, the verdict vocabulary `real` / `not_an_issue` / `needs_decision` shared with the agent-quality verdict ledger (a local, untracked tool under the tools directory <!-- link-ok -->), one comment in a fixed shape (Verdict, Evidence, Falsifier, Recommendation, Found-not-filed). A `needs_decision` is resolved in verification: history, options and a recommendation, escalated with a Paperclip confirmation or questions interaction and `in_review`, never a bare question. Launched with `--disallowedTools Edit,Write,NotebookEdit`, so it is read-only by construction, not by promise (verified in the run log: neither tool in the session's tool list). Carries the catalog skill `issue-triage` the maintainer installed, for Paperclip's own status and escalation mechanics. |
| Bridge script (`gdx_ledger_bridge.py`, beside Paperclip, outside this repo <!-- link-ok -->) | `triage --section S [--limit N]`: open repo-code entries of a ledger section become `triage-only` GDXA issues assigned to `triage`, and the issue id is written into the entry's status bracket. `tagged`: entries the maintainer marks `-> GDXA` become `verify-then-fix` issues, where a `real` verdict is handed to the dispatcher. `sync`: the latest `Verdict:` comment and the issue status are written back into the bracket. Harness and hook entries are excluded by pattern; the script never selects on its own judgment. |
| Issue template | Mode, ledger section, the entry verbatim, and the instruction to re-verify against current code and name the falsifier before anything else. |
| Company budget | 5000 cents a month, set before the first pass; raise deliberately. |

**What is not routed to agents:** the ~42 entries that name the maintainer's hooks or the harness (outside the repo and outside any worktree), and product decisions, which an agent can only restate; those are the maintainer's. Ledger shape at the time: 473 entries, 379 open, about 148 naming repo code.

**Preconditions still owed:** landing this plan's PR, so worktrees contain the map and the scanner; and one small real change through the fix path, which nothing has exercised.

**First pass:** the two open repo-code entries of the "estimate Edit customer build (#766)" section → GDXA-4 and GDXA-5, `triage-only`.

### 11.1 First-pass results (2026-09-24)

| Issue | Verdict | Run | What the triage established |
|---|---|---|---|
| GDXA-5 (InvoiceDetail `openCustomerEdit` fallback nulls fields) | `real` | 44 turns, 275 s, $2.35 | Proven by execution in the image: the fallback's five-key shell becomes a PATCH that sets `notes`, `referral_source` to null and `customer_type` to Residential, and the server applies it on a 200 (data loss on 2xx). Four falsifiers checked and named. Class stated as a shape; all five `CustomerFormDialog` mount sites swept, one instance; fix specified as the `EstimateView` shape. |
| GDXA-4 (document pages name the customer with no link or edit) | `real`, entry corrected | 61 turns, 537 s, $3.71 | Verified by render, not by reading. The class survives but the entry mis-described one instance: `MobileJobDetailView` has had an edit affordance since 2026-07-17, so "no edit" was wrong when written. Re-stated the class as "no route to the customer record", listed the three one-line links, and found a partially orphaned endpoint on the way (carried to the ledger, not filed). |

Both verdicts were written back into the ledger brackets by `sync` (`GDXA-n: real (done) synced 2026-09-24`). Neither run modified a file; both ran in their own worktree with the read-only tool set and all five session-start hooks.

**A cap that cannot bite.** After five runs costing about $9 by the adapter's own `total_cost_usd`, the company's `spentMonthlyCents` is still 0: CLI-engine runs report cost in the run result but do not accrue to the company budget, so the 5000-cent cap is decoration until Paperclip counts them. Recorded on the ledger; the working control is the maintainer creating issues one section at a time.

### 11.2 The intake agent — the ledger work moves off the maintainer's session (2026-09-24)

**Ask (Doug, 2026-09-24):** "Instead of you controlling it can we have an agent that does the found not filed work and turns it into tasks."

The rule does not move: the ruling stays with the maintainer. What moves is who runs the mechanics. A third Paperclip agent, `ledger-intake`, does the transcription, syncing and proposing; the maintainer's ruling becomes one of three things, none of which is a Claude session deciding: a `-> GDXA` tag in the ledger, a section named in the issue that wakes the agent, or **accepting a proposal on the board**.

| Piece | Shape |
|---|---|
| Agent `ledger-intake` (role pm, reports to the dispatcher) | Runs in the maintainer's main checkout, the only place the git-ignored ledger exists. Read-only by construction (`--disallowedTools Edit,Write,NotebookEdit`); every ledger write goes through the bridge script, which is deterministic and idempotent, so the agent can never improvise an edit. Company-level issues only (no project), so no worktree is cut for it. |
| Wake protocol | `sync` → `adopt` → `tagged` → (a named section: `triage --section`) or else `list`, pick the one section with the most open un-queued repo-code entries, `propose --limit 3` as a `suggest_tasks` interaction with a board-only resolver, then `propose-fixes` for synced `real` verdicts without a fix issue. One report comment; `in_review` if a proposal is pending, else `done`. Accepting a proposal wakes the agent (`wake_assignee_on_accept`), which then runs `adopt` to write the new ids back. |
| Bridge modes added | `list`, `propose`, `propose-fixes`, `adopt`. The queued-id match was tightened to the bridge's own stamp formats after `sync` was seen to treat a bracket that merely mentioned an id ("carried from GDXA-4") as a queued issue. |
| What it will not do | Create triage tasks for a section nobody named or accepted; propose more than one section per wake; propose harness, hook or Paperclip entries; touch a worktree; file on GitHub. A script failure is pasted and the issue set `blocked` with the maintainer as unblock owner. |

**Cost stays visible:** the proposal summary states the expected cost (about $3 per triage run at the observed rate), and the company cap is known not to accrue CLI-engine runs (§11.1), so one section per wake is the control.

**First intake run (GDXA-6, 2026-09-24):** 22 turns, 110 s, $1.12. `sync`, `adopt` and `tagged` were no-ops and the ledger's modification time did not change. It picked the largest section (the 2026-09-13 live-defect follow-ups, 38 open un-queued repo-code entries) and posted two board-only `suggest_tasks` interactions: three `triage-only` tasks from that section, and two `verify-then-fix` tasks for the GDXA-4 and GDXA-5 `real` verdicts, with the expected cost stated in the report (about $9 for the triage tasks, more for the fixes). It set the issue `in_review`. Nothing was created. The accept path (board accepts → Paperclip creates the tasks → the agent wakes and runs `adopt`) is built and unexercised until the maintainer accepts something.

### 11.3 Triage feeds the dispatcher — one mode (2026-09-24)

**Ask (Doug, 2026-09-24):** "shouldn't the triage feed the dispatcher?" → "yes make those changes".

As first built, a `real` verdict in `triage-only` mode dead-ended, the intake agent then proposed a second task, and that task went back through triage in `verify-then-fix` mode before reaching the dispatcher: a double verification and a third acceptance for no gain. Now there is one mode. Triage re-verifies; `not_an_issue` closes with evidence; `needs_decision` escalates; **`real` reassigns to the dispatcher and wakes it**, the dispatcher routes by the ownership map, the owner fixes the class and opens a draft pull request. The maintainer's ruling stays at the two gates that already exist: accepting the triage batch on the board (or naming a section, or tagging), and reviewing the draft PR. Batch size is the spend control.

Changes made: the bridge's issue body and title lost the mode; `propose-fixes` was removed; `sync` now records `real -> routed to <agent>` and, once a comment names one, `-> PR #N`; the triage instructions always hand a `real` verdict off; the intake instructions no longer propose fixes. The pending fix proposal on GDXA-6 was rejected as superseded (Paperclip only lets the creator withdraw a task suggestion, and only question interactions can be cancelled). GDXA-5, the smaller of the two `real` verdicts, was reopened and handed to the dispatcher by hand as the first exercise of the fix path; GDXA-4 waits for the maintainer.

**The dispatcher delegates through child issues, not reassignment.** Routing GDXA-5 reproduced the GDXA-3 gap exactly: the dispatcher commented and reassigned, the server cancelled its run at the PATCH ("Cancelled before issue reassignment", in the Paperclip server's issues route), and no run started for the new assignee until a manual wake. An agent cannot wake after a reassignment it makes from inside its own run, because that run is already gone. Paperclip's own contract has the answer: create a child issue assigned to the owner (creation wakes the assignee for every actor, and a child inherits the parent's worktree), then block the parent on the child with first-class `blockedByIssueIds`, so the dispatcher is woken when the child closes and can close the parent. The dispatcher's instructions were rewritten to that pattern, with a verify-the-run-started step and an explicit wake as the fallback. `sync` follows the parent's blocker child for the owner, status and PR number.

**Result of routing GDXA-5 — the fix path works end to end.** money-billing, in the issue's worktree: applied the fix exactly as the verdict specified, wrote four regression pins and proved them red on the pre-fix view (4 failed / 46 passed) and green after (50 / 50), ran the full vitest suite (283 files, 2581 tests, 0 failed), walked the change in a throwaway container in light and dark (3 passed with the fix, 2 failed without), ran the backend matrix, then pushed the branch and opened **draft PR #771** (base `main`, mergeable, not merged) with the Class / Searched / Instances block (1 found / 1 fixed / 0 deferred, five sibling sites cleared by name) and the close-out shape in its report. It also raised, correctly and unbundled, that **SQLAlchemy 2.1.0 (PyPI 2026-09-24 20:12Z) breaks every CI shard at import**: the pin `sqlalchemy>=2.0.49,<3.0` resolves 2.1.0, whose default PostgreSQL driver is psycopg 3, which the repo does not install. Verified here: all seven `test (N)` checks on PR #771 fail in about two minutes each; main's last run (11:38Z) predates the release. Platform-core territory; one-line fix.

Two incidents on the way, both recorded on the ledger. The matrix's 14 failures were all guard tests that read the git index: inside the docker image a linked worktree's `.git` is a file whose target lies outside the `/app` mount, so `tracked_files` raises; the agent proved the identical 14 on a pristine tree before blaming nothing. And the agent's own wait loops never exited because `pgrep -f 'run_tests_split.sh'` matched the shell running the loop; the maintainer's session terminated the loops, after which the agent finished on its own. Steering a comment into a running CLI-engine run is unsupported (`steeringDisposition: unsupported`); a comment posted meanwhile is queued for the next wake.

**A defect of the change-over, found by the maintainer:** GDXA-7 closed `done` with a `real` verdict and nothing routed. The three accepted tasks were created from the proposal posted *before* the single-mode change, so their issue text still said "triage-only, set done, do not reassign", and triage obeyed the issue over its new instructions (GDXA-9 says so in its comment; it moved only when the maintainer ruled). Three fixes: GDXA-7 was reopened and handed to the dispatcher; the triage instructions now say a `real` verdict always hands off and any issue text to the contrary is stale; and `sync` prints an UNROUTED line for a `real` verdict closed on triage with no child and no PR, which the intake agent is instructed to route. The first such line named GDXA-4, and an intake wake was created to route it through that path rather than by hand.

**Children of one parent share its worktree.** GDXA-4 split into GDXA-15 (mobile-tech) and GDXA-16 (jobs-dispatch); both inherited the parent's execution workspace, so jobs-dispatch committed and opened PR #773 from a branch mobile-tech was still editing. Paperclip's inheritance is by design and is right for a single owner. For a split the dispatcher now creates each child unassigned, sets `executionWorkspaceSettings.mode` to `isolated_workspace`, then assigns, so the run starts with its own worktree. Unexercised until the next multi-owner split; PR #773 may carry both halves and should be reviewed as one class fix.

**The accepted batch ran without the maintainer's session.** The board accepted the intake agent's triage proposal at 21:50Z; GDXA-7, 8 and 9 were created and triaged. GDXA-7 (the pave script drops `public` and then crashes): `real`, and when the maintainer asked on the issue whether it was real, triage re-verified from the side that would let it withdraw, queried prod read-only, and answered that prod is worse than the rig. GDXA-9 (dead `action_url`s): `real`; the maintainer ruled "retire" on the issue; triage split the ruling into a dead surface to remove and a live surface whose targets need repointing, wrote a removal manifest and handed it to the dispatcher. The dispatcher, on its new pattern, created child **GDXA-10** assigned to ai-mcp by the ownership scan, blocked GDXA-8 on it, and confirmed the child's run started **without a manual wake**. ai-mcp was working GDXA-10 when this was written. The ruling stayed with the maintainer at every step: acceptance, the question, and "retire".

### 11.4 Raise now — an urgent finding gets worked, not parked (2026-09-24)

**Ask (Doug, 2026-09-24):** on learning that the CI break was raised in a report and then waited: "there should be a way this gets worked on if it happens."

CLAUDE.md already says a live defect is never rate-limited and that something urgent is raised the moment it is seen. Under a person, that is the conversation; unattended, there was no equivalent, so the money-billing agent's correct "raise now" section sat in a comment. Now: an agent that finds something stopping every pull request or affecting production or the demo today, outside its own issue, creates ONE issue titled `Raise now: …`, priority `critical`, assigned to the dispatcher, in the same project as its own issue (so the fix gets a worktree), with the evidence and the smallest fix it can see, and says so in its report. The dispatcher routes `Raise now:` issues before anything else, with the child at `critical`, and may downgrade the priority with a reason but never closes one unrouted. The maintainer's gates are unchanged: the fix still arrives as a draft PR.

The first one, GDXA-12, was opened by the maintainer's session for the SQLAlchemy 2.1.0 break, since the finding predates the rule. **Result:** the dispatcher routed it within minutes into child GDXA-14 for platform-core at priority critical (platform-core's concurrency was raised to two so it would not queue behind GDXA-13); platform-core pinned `sqlalchemy>=2.0.49,<2.1` with a comment explaining why the ceiling is 2.1 and what adopting 2.1 would take, and opened **draft PR #772** (one line changed, mergeable; its own CI run passed all 16 checks, the seven test shards included, which is the proof the pin is the fix). From the maintainer's word to the draft PR: about thirty minutes, no session in the loop after the issue was created. Landed as `8a5acb7` on 2026-09-25 00:20Z after the maintainer said "can we fix the pr's": marked ready, squash-merged with `--admin` after every check was enumerated by name (16 pass), branch deleted with nothing stacked on it; the failed jobs on #771 and #773 were re-run, which proved that a rerun reuses the original merge commit; #771 was then rebased onto the pinned main by cherry-pick and force-push, went 16 of 16 green, and the maintainer merged it at 00:40Z as `260f0ea`. GDXA-5's ledger entry moved to "Ruled / closed" with the PR number: the loop from ledger entry to merged fix closed for the first time, with the maintainer's hand on exactly the two gates the design named (the triage batch and the PR). The maintainer then merged #773 (jobs-dispatch's half of GDXA-4) at 00:56Z as `97ff664`; mobile-tech's half (GDXA-15) was still being worked on the same, now-merged branch, so its commit will need the cherry-pick recipe onto `main` and a fresh PR. ai-mcp's GDXA-10 fix opened as #774 (10 dead deep links repointed plus a guard) after the pin had landed, so its merge ref carried the pin and it went green without a rebase.

### 11.5 The release mechanic — PR mechanics leave the owner (2026-09-24)

**Ask (Doug, 2026-09-24):** "should we have another agent that just knows how to pr and handle the worktrees?" → "yes do the pr agent".

The split: worktrees are mechanics and get scripts; pull requests are a job and get an agent; the commit stays with the owner because the commit gate demands the manifest and the manifest is the confidence interval of whoever did the work.

| Piece | Shape |
|---|---|
| Agent `release-mechanic` (role devops, reports to the dispatcher, read-only tool set, up to three concurrent runs) | Woken by a child issue the owner creates as its last act (`PR: <commit subject>`), inheriting the owner's worktree. Refuses a dirty tree or a branch with no commit. Pushes, opens the draft PR with a body assembled from the owner's report (sweep block verbatim, tests by name, what was not verified, issue ids, attribution line, no private identifiers), comments the number on both issues, polls `gh pr checks` no faster than every 90 s and enumerates every check by name. Red is a question: reads the job log through the API, re-runs a failing test in isolation before calling it a flake, raises an upstream break as a `Raise now:` issue instead of fixing it, hands a real failure back to the owner as `blocked`. Re-runs failed checks after an upstream fix lands. Repairs a squash-merge CONFLICTING state with the CLAUDE.md recipe (`checkout -B` from `origin/main` and cherry-pick), never `rebase`. Green → `in_review`; merged → both issues `done` and the workspace archived. Never merges, never marks ready, never adds a commit of its own. |
| Owner hand-off | The 14 agent files' unattended-run section now ends at "commit with the manifest, post the report, create the release child". The dispatcher's owner contract says the same. |
| Worktree provision and teardown scripts, attached to the project's `git_worktree` strategy | Provision links the maintainer's `node_modules` into the worktree (git ignores it) and writes a notes file naming the two known limits (the 14 tracked-set guards cannot pass inside docker here; never `pgrep` your own command). Teardown removes the link and the notes. Self-tested in an existing worktree; unexercised by Paperclip until the next worktree is cut. |
| Intake janitor pass | On every wake, after sync/adopt/tagged: archive execution workspaces whose issues are all done and whose PR is merged or closed, via close-readiness, and list what was left and why. |
| Bridge `sync` | Follows the blocker chain three deep (triage issue → owner child → release child) for the owner, status and PR number. |

**Not yet exercised:** the release mechanic has had no run; the first owner to finish under the new hand-off will create its first issue. GDXA-10, 13 and 15 were already in flight under the old contract and will open their own PRs.

## 12. Run speed — why the first fix runs timed out, and the fixes (2026-09-24)

**Ask (Doug, 2026-09-24):** "with our paper clip gdx agents why are they so slow?" → "lets fix these."

Measured from the on-disk run logs (`run-logs/<company>/<agent>/<run>.ndjson`; a tool call's duration is its `tool_result` timestamp minus its `tool_use` timestamp), not inferred:

| run | wall clock | `/audit` calls | time in audits | time waiting on a matrix |
|---|---|---|---|---|
| ai-mcp, GDXA-10, run 1 (timed out) | 5392 s | 4 | 2738 s | 601 s |
| ai-mcp, GDXA-10, run 2 (timed out) | 5400 s | 5 | 2705 s | 912 s |
| platform-core, GDXA-7 (timed out) | 5400 s | 2 | 725 s | 2156 s |
| mobile-tech, GDXA-15 (timed out) | 5389 s | 4 | 928 s | 1163 s |
| money-billing, GDXA-5 (succeeded, PR #771) | 2775 s | 1 | — | 1451 s |
| jobs-dispatch, GDXA-16 (succeeded, PR #773) | 1176 s | 1 | — | — |

Four causes, each with its fix in this PR:

1. **The audit loop.** Owners re-ran `/audit` after every fix round ("second pass", "third/final pass", "fourth/final pass"); each call is a forked reviewer that runs its own docker probes, 7–18 min. Part of it was mechanical, found by this PR's own audit: the `/audit` skill writes its critique under the **main checkout's** project key, and the commit gate (`verification_gate.py`, user scope, outside this repo) read only the **committing session's** key. A Paperclip run is launched inside its worktree, so its key is the worktree's; the gate refused audited commits ("no critique at …/-paperclip-worktrees-paperclip-GDXA-7-…/memory/critique_latest.md" in run `09b0a422`, the same in GDXA-4 and GDXA-8) and the agent audited again. Fix: the gate now also accepts the critique under the main checkout's key (user-scope hook change, with tests beside it), and the agent files say audit once, on the final diff, after the matrix is green. Still true and not fixed here: the gate checks only that the critique is newer than HEAD and names a diff, never that the hash matches the staged diff, so a concurrent owner's critique can satisfy another's gate. <!-- verification_gate.py is a user-scope hook outside this repo; link-ok -->
2. **Concurrent matrices.** Four owners ran `run_tests_split.sh` (N=7) at the same time: 28 pytest shards on 20 cores beside the Android emulator, load average 28, 1.8 GB free. (The 22:54 shard seen then is not evidence of contention: this PR's own matrix, run alone under the lock, took 23:10 on the same shard 4; that shard is slow by itself.) Fix: the script takes a host-wide `flock` (`/tmp/gdx_matrix.lock`), prints that it is waiting and names the holder.
3. **Duplicate matrices.** The provisioned worktree notes told owners to prove the 14 tracked-set guard failures "on a pristine tree once", so runs copied the tree and ran the matrix twice. Root cause: a linked worktree's `.git` is a file pointing at `<main>/.git/worktrees/<name>`, outside the `/app` bind mount, so `tools/tracked_files.py` raises inside the container. Fix: the script mounts that gitdir read-only into a docker `PYTEST` (verified on a Paperclip worktree: `test_tracked_files.py` 4 failed → 8 passed, 2 skipped), and the notes now say run it once.
4. **Timeouts and context.** Four runs hit the 5400 s cap. A timed-out run records no session id, so its scheduled retry cannot `--resume` and redoes the audit and the matrix from nothing; ai-mcp reached a third 90-minute run on a ten-link repoint. `timeoutSec` is 10800 on all 14 domain agents (read back with `agent list`); it binds runs started after the change, and the two retries in flight were observed past 5400 s. mobile-tech read 41 emulator screenshots into context (peak 376 K tokens, a 32 MB run log); the section now caps screenshot reads at six per run, and the android skill says the same.

Not changed: the adapter model (`claude-opus-5`, §10). That is a cost decision and the maintainer's.
