# Working agreement — gdx_dispatch

## The rule above the rest

Answer with evidence, not assurance. Every "done", "green", or "deployed" claim
ships with the artifact that proves it — pasted output, a screenshot, or a
browser walk. No evidence in hand means the claim is "not yet verified", said
plainly.

**Name the falsifier before you trust your own call**, not just someone
else's. Say what would make the claim wrong, then go look for it. This applies
hardest to a severity judgement, an aggregate, or any conclusion that
authorizes an action you already want to take — that is exactly when the check
gets skipped. On 2026-09-12 an audit of #644's `live-defect` label was done
properly (prod queried, `audit_logs` checked, the falsifier named) and then
the identical label was applied to a fresh finding with none of that rigor,
because the finding was mine and I was pleased with it.

**Verify state, don't assert it from memory.** Branch, PR status, deployed
version, prod flags: check them live. A remembered value is a guess with a
timestamp.

**A question is not a work order.** "What would be best?", "is it really an
issue?", "what needs doing?" — answer, then stop. Doug widens scope
explicitly; a question is not that.

## What phase are we in

`PHASE.md` answers it, and it is the only file that does. It names the current
phase, the **exit condition** that ends it, and whether the **sweep budget** is
open. Read it at session start; `/start` opens with it and
`session_checklist.py` prints the live count. <!-- session_checklist.py lives in ~/.claude/hooks, outside this repo; link-ok -->

Doug declares the phase. Claude does not switch it, and does not call the exit
condition met without naming the evidence. Our own audits generate most of the
backlog, so starting a sweep while the budget is CLOSED is choosing more
backlog, not less. Fixing a `live-defect` is never rate-limited by any of
this — the budget governs sweeps, not repairs.

## Project map

- **Backend:** FastAPI + SQLAlchemy in `gdx_dispatch/` — `routers/` (HTTP),
  `services/`, `models/`, `core/` (auth, audit, payments), `tasks/` (Celery).
- **Frontend:** Vue 3 + PrimeVue SPA in `gdx_dispatch/frontend/` (vitest,
  Histoire, Playwright e2e in `frontend/e2e/`).
- **Plugins:** in-app plugin surface (`plugin_api/`, `plugin_host/`) plus a
  separate public plugins repo. The plugin-host has **no network egress in
  production** — plugins cannot pip-install at runtime. Proprietary pricing
  plugins are git-ignored on purpose: never commit, push, or merge them.
- **Single-tenant, forever.** One tenant per database; isolation is the
  connection. Never design for multi-tenancy.
- **Migrations:** Alembic in `gdx_dispatch/migrations/`. Every migration must
  run on both SQLite and Postgres; escape literal `%` as `%%`.
- **Deploy:** Docker images via the Release workflow; releases are cut by tag.
  The maintainer triggers merge and release.
- `ARCHITECTURAL_INVARIANTS.md` is the registry of load-bearing invariants —
  read it before touching mutation paths, deletes, or money code.
- **Subagents:** one per domain, checked in under `.claude/agents/` (14 as of
  2026-09-24). Which files each one owns is data, not prose:
  `gdx_dispatch/tools/agent_ownership.txt`, gated by
  `tests/test_agent_ownership.py` — a new router, view or module with no
  owner line is red. Rationale: the plan `domain-agents-plan` under `docs/design/`.

## Commands and harness facts

- Backend tests run through the docker-app image — never against a real DB.
  Frontend unit tests are vitest; e2e runs against a throwaway container,
  which needs `-e GDX_E2E_BYPASS=1`.
- Each gated commit needs its own dedicated `cd`; everywhere else use
  `git -C`. Check the current branch before every commit — an IDE or a
  parallel session can move it under you.
- **Playwright MCP autofills the production password.** Never open a prod
  login page with it — inject a token instead; headed snapshots have written
  that password to disk.
- **A row-refusing drop can take prod down.** The guarded table-drop
  migrations (088, 091, 092) count rows before any `DROP`, inside the
  migration transaction, and raise on a non-empty table — a refusal mutates
  nothing, so restoring the pre-update snapshot fixes nothing. The `app`
  entrypoint runs `alembic upgrade head` under `set -e` before serving
  `/health`, and `app` restarts `unless-stopped`, so a refusal crash-loops;
  `update.sh` cannot tell that from a slow migration and, after its 10-minute
  wait, says to keep waiting. Read `docker logs` for the refusal, then pin the
  previous `APP_VERSION` or export-and-empty the table and restart. Recount on
  prod **and** demo (which does not go through `update.sh`) immediately before
  deploying one.
- **Load the `gdx-harness` skill before running tests or the matrix, reading
  CI, arming a monitor, merging or re-stacking a PR, or running Docker Compose
  or `ssh gdx-vps`.** The matrix runs only through
  `gdx_dispatch/tools/run_tests_split.sh`; the skill holds the rest of the
  harness facts (runner flags and lock, `--scans`, CI triggers, the lint
  ratchet, flakes, merge gotchas). Area sharp edges live in `.claude/rules/`
  and load when a file in that area goes through the Read or Write tool or a
  plain `cat`/`head` — not on pipes, `wc`, Bash edits (`sed -i`, heredocs,
  scripts), the Grep tool or a directory-wide search. Before editing in an
  area, Read a file there or open the rule directly: `frontend.md`,
  `plugins.md`, `database.md`, `scanners-and-tests.md`, `written-record.md`.
  The costliest: SQLite stores a `Uuid` as 32 dashless hex (raw `id = :dashed`
  never matches); a mock or text-present test proves nothing about behaviour;
  `pip install --target` leaves the old plugin code in place.

## Build pipeline (every non-trivial change)

0. **Search the world before building it.** Before any feature a reasonable
   person might already have built — an extension, a CLI, a library, a service
   — run an actual web search for prior art and read the two or three closest
   hits. Say what you found and why you are still building. "I didn't find
   anything" is only credible if you searched; a plausible-sounding claim that
   nothing exists is a guess wearing a fact's clothes.
   The same instinct applies *inside* this repo: before building, grep for the
   thing. Six of the seventeen endpoints on the 2026-08-12 decision list turned
   out to be parallel fakes of features that already shipped elsewhere, and one
   plan proposed rebuilding a GL posting that already existed.
1. **Plan → research → adversarial audit** (`/audit`) before writing code.
   When the change touches a third-party surface (Stripe, QBO, SimpleFIN,
   Phone.com, n8n, Hostinger) or library behavior you're inferring rather than
   reading (Alembic portability, PrimeVue, CodeQL guards), read current
   upstream docs first and **cite what you read — URL plus the version or
   date**. Skip it when the codebase is the authority. **Vendor docs state;
   the live response proves** — probe the real endpoint when you can reach one.
2. **Build → full test matrix.** Enumerate every FAIL and SKIP by name — never
   summarize as "tests pass". Check the lint ratchet against the baseline.
3. **Verify in a throwaway container + real browser:** the real role, real
   data, light and dark mode, desktop and mobile. Tests cannot see dead UI;
   a browser can.
4. **Sibling sweep — declare the scope before the fix, not after.** Name the
   defect as a *shape* (what the code does wrong), not as a finding number.
   Then name the surface you'll search — every file that could hold that
   shape — **before** you start. Report three things: the pattern, the files
   searched, the instances found. A sweep scoped to the file you were already
   editing is not a sweep. If the shape could exist in a router you have never
   opened, that router is in scope. Put the accounting in the PR body —
   `~/.claude/hooks/github_merge_gate.py` blocks a sweep PR without it:

   ```
   Class:     <the shape the code gets wrong, not a finding number>
   Searched:  <every file/glob that could hold that shape>
   Instances: <N> found / <N> fixed / <N> deferred (reason)
   ```

   Deferring is allowed; it just has to be counted. A deferred instance goes
   on the close-out's *found, not filed* list and into `FOUND_NOT_FILED.md` — <!-- untracked by design, see "One issue per session"; link-ok -->
   Claude does not file it on the tracker (see *One issue per session*).
5. **After deploy, walk it on prod** before calling it shipped. The walk is
   the finish line, not the release.

## Actions must be auditable

Every state-changing action must answer: **who did it, what changed, when.**

- Every create/update/delete in routers and services calls
  `log_audit_event()` (`gdx_dispatch/core/audit.py`) — this is invariant #1
  in `ARCHITECTURAL_INVARIANTS.md`. A new mutation endpoint without an audit
  call is incomplete, not done.
- Soft-delete, never hard-delete, on tables that carry `deleted_at`
  (invariant #2) — audit and billing chains must stay reconstructable.
- Money mutations (payments, deposits, voids, adjustments) additionally
  record the acting user and are never performed as an anonymous or
  system-default identity.
- No silent writes: an action that succeeds without a trace, or fakes a
  success response without doing the work, is a defect of the highest class.
- When reviewing or building a feature, ask: "could we reconstruct from the
  records who did this and why?" If not, add the trail before shipping.

## The written record

The doc rules (status lines, date-stamp the past and fix or retire the
present, rival plans) are in `.claude/rules/written-record.md` and load when
you read a `.md` file. These three apply to code too:

- **A finding names an instance; the fix owns the class.** Audit findings are
  numbered by where someone happened to look. Fixing M13 means fixing every
  place that shape lives, or recording which instances you are leaving and why.
- **A fix is done when its guard runs.** A test excluded from the default gate
  is not a regression net. Shipping the test and leaving it unrun is a silent
  no-op.
- **When something is to be deleted, delete it.** No tarball, no `archive/`
  copy, no "just in case" branch, no commented-out block. Every hedge becomes
  a second thing to maintain, to search, and to be misled by later — and the
  copy is always the one that goes stale unnoticed (Doug, 2026-09-12). This
  does not override *Keep the past* (in the doc rules): a shipped plan's reasoning still stays,
  because it is the record. A backup of something already decided dead is not
  a record, it is a hedge against a decision that has already been made.

## Can someone actually use it?

A feature exists only when a real person can find it, reach it, and finish it.
Code that works but can't be used is not shipped — this repo has had a send
endpoint no UI ever called, an approval link that wasn't clickable, and
buttons wired to stubs. Before calling anything done:

- **Name the user** — office staff at a desk, a tech on a phone in a garage,
  a customer opening an email on their phone — and walk *their* path, on
  their device, start to finish.
- **There is a visible way in.** The feature is reachable from where that
  user naturally already is. "They'd have to know the URL" means not done.
  If it serves techs, it exists on mobile; if it serves the office, desktop.
- **No orphans in either direction:** every UI control calls a real endpoint
  that does the work; every endpoint meant for users has a UI caller. A
  button that no-ops or an API nobody can reach are both defects.
- **No dead ends.** Every action lands the user somewhere sensible with a
  clear next step — never a click that goes nowhere.
- **Customer-facing surfaces get the phone test:** links clickable, pages
  readable on a small screen, no login walls where a token link should do.

## Domain rules that shape code

- Garage-door work in Minnesota is a construction contract: **never charge
  the customer sales tax.**
- Billed labor comes from **attested hours only** — elapsed clock time is
  not evidence and code may not invent hours.
- Taxonomy: a service call is a repair; a converted estimate is an
  installation.
- QuickBooks is being phased out: never schedule new QB syncs; backfills go
  into this system, not QB.
  QB API reads are metered, writes are free.
- **Commission is plugin-bound and out of core** — never fix it in core.
- The Midland operator/parts multiplier is an open question with the
  distributor; do not invent one.

## One issue per session

Name the issue and what "done" means before starting. Anything noticed on the
way goes on a **found, not filed** list in the close-out — not fixed, not
filed, not investigated mid-task — and Doug decides what becomes an issue
(net-zero while the sweep budget is CLOSED; see `PHASE.md`).
Doug can widen the scope of a session; Claude does not. Adopted 2026-09-10.

**Claude does not open GitHub issues. At all.** Not for a sweep finding, not
for a security hole, not under the old "a real user can hit it today"
exception — that exception is withdrawn as a licence to file (Doug,
2026-09-12: a week was spent cleaning up issues Claude posted, and the intake
was pure cost). Something urgent still gets **raised the moment it is seen —
to Doug, in the conversation**, which is faster than a ticket anyway.
Everything else lands in `FOUND_NOT_FILED.md`. Doug files what deserves <!-- untracked by design, see below; link-ok -->
filing.

The withdrawn exception was self-triggering, which is why it failed: Claude
both judged whether it applied and benefited from it applying. #712 was filed
on that mistake — a hardening gap needing a hand-crafted request from a staff
account in a single-tenant app, labelled a live defect — and closed back to
the ledger the same night. Treat any rule that authorizes an action Claude
already wants to take as the rule most likely being misread.

That list is also **appended to `FOUND_NOT_FILED.md` in the repo root**, which <!-- FOUND_NOT_FILED.md is deliberately untracked — local to the maintainer's checkout, never committed; link-ok -->
is a durable local ledger, not a GitHub issue: git-ignored through
`.git/info/exclude`, never committed, never pushed (Doug, 2026-09-12). Filing
on the tracker is net-zero while the budget is CLOSED, and the close-out list
was evaporating between sessions. Each entry carries the date observed, the
file, and why it matters; entries are dated observations, so re-verify against
the code before acting on one. When Doug rules, the entry moves to that file's
"Ruled / closed" section with the decision.

## Close every work turn with

- Commit status: committed? pushed? PR number? Anything intentionally
  uncommitted, and why.
- What was verified (with the evidence), and what was not.
- Remaining open items as a list — "nothing left" requires having looked.
- The session's *found, not filed* list, or "nothing found" — reported here
  **and** appended to `FOUND_NOT_FILED.md`. <!-- untracked by design, see above; link-ok -->

## Planning defaults (standing answers — don't re-ask)

- **Scope:** build the full recommended rung. Ask only when the larger option
  adds a migration, changes money math, or alters customer-facing behavior.
- **Packaging:** separate focused PRs; stacked PRs merge bottom-up; tech debt
  discovered mid-feature goes on the close-out's *found, not filed* list for
  Doug to rule on — never bundled, never silently dropped.
- **Releases:** feature releases take a minor version bump. The maintainer
  triggers merge and release; "release and update everything" means the full
  chain — release, then production, then demo (and dev when stated).
- **Do ask about:** product shape (which page/surface something lives on),
  pricing and money rules, and destructive data actions.
- **Don't ask about** anything discoverable with existing access (environment,
  credentials, infra state) — check first.

## Hard rules

- Public repo: no private identifiers (customer, vendor, or internal domain
  names) in commits or PR bodies.
- Money-touching changes state what happens to existing rows, and carry a
  migration plus a rollback path.
- Anything customer-facing gets checked for unauthenticated reachability and
  ID/token enumeration before merge.
