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
backlog — in the 30 days to 2026-09-07 we opened 99 issues and closed 54
(measured against the tracker 2026-09-07; an earlier 48/47 "parity" reading was
wrong — intake runs at roughly 1.8x closure) — so
starting a sweep while the budget is CLOSED is choosing more backlog, not less.
`live-defect` work is never rate-limited by any of this.

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

## Commands and harness facts

- Backend tests run through the docker-app image — never against a real DB.
  Frontend unit tests are vitest; e2e runs against a throwaway container,
  which needs `-e GDX_E2E_BYPASS=1`.
- ~15 known cross-file test flakes exist on main: they pass in isolation.
  Re-run in isolation before blaming your branch.
- Lint is a **ruff ratchet against a baseline**, not plain pass/fail — a
  branch can be "clean" and still over the ratchet.
- **CI (`ci.yml`) triggers on `pull_request` with base `main`, and on `push`
  to `main`.** It does *not* run on a PR whose base is another feature branch —
  which is why **mid-stack PRs still need the local matrix run and its results
  posted before merge**: the rule is right, the old reason ("runs only on
  main") was not. Note also the `push` trigger carries a paths filter
  (`gdx_dispatch/**`, `gdx_dispatch/docker/**` — note the prefix: a *root*
  `docker/` change matches nothing — `.github/workflows/**`, excluding
  `**/*.md`)
  while the `pull_request` trigger has none, so a docs-only PR runs the full
  suite and a docs-only push to main does not. Verified against
  `.github/workflows/ci.yml` 2026-09-01.
- Main is merge-protected; `--admin` merge is the sanctioned path, but only
  after enumerating every check's result by name.
- **Run the matrix with `gdx_dispatch/tools/run_tests_split.sh` (N=7), and
  never with `--network host`** — the host network breaks ~15 tests. The cost
  of leaving it off is that the Postgres arm goes **silently SKIPped** (24+,
  and CI too, #440): a green run has not exercised PG. Enumerate skips with
  `-rs` and read the categories, or the gap is invisible.
- `pytest.ini` already carries `-q`; adding another makes output useless. To
  read a CI failure use the `jobs/<id>/logs` API, not `gh run view --log`.
- **A foreground `sleep` is blocked and a background Bash dies at 600s.** Long
  runs go through `nohup`, watched by ONE `Monitor` or a single until-loop —
  never a poll loop.
- The frontend lockfile can only be regenerated on **npm 11**: 10.8.2 (what
  CI's `setup-node` 20 and `node:20-slim` ship) and 9.2.0 both crash in
  arborist with `Cannot read properties of null (reading 'edgesOut')`. Prove
  `npm ci` in `node:20-slim` before pushing a lockfile.
- The git-ignored local `docker/demo/` directory fails three scanner tests
  locally that are green in CI. A fresh worktree does not have it — prefer one
  for a clean read.
- Mako 1.4.0 shadows `tools/`, producing an ImportError that only appears in
  CI.
- `refresh.sh` does not rebuild the plugin-host; rebuild it by hand or you are
  testing stale plugin code.
- Compose `--env-file`: an empty `JWT_SECRET` crash-loops the container. The
  `verifyplaywright` path passes `--env-file` rather than cloning the
  environment, deliberately — cloning trips the credential guard.
- Each gated commit needs its own dedicated `cd`; everywhere else use
  `git -C`. Check the current branch before every commit — an IDE or a
  parallel session can move it under you.
- `ssh gdx-vps` reaches production over Tailscale. Real mobile verification
  runs on the local Pixel 8 AVD, where `10.0.2.2` is the host.

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
   Instances: <N> found / <N> fixed / <N> deferred → #NNN (reason)
   ```

   Deferring is allowed; it just has to be counted, and each deferred instance
   is filed `sweep-finding`, which spends sweep budget. Deferral used to be
   free — that is why #558, #560 and #637 are all sweeps spawned by sweeps.
   While the sweep budget is CLOSED, filing is **net-zero** (`PHASE.md`): a
   deferred instance is counted here but filed only if the same PR closes a
   `sweep-finding` or Doug approves — otherwise it goes on the close-out's
   *found, not filed* list.
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

Every doc is either **about the past** or **about the present**, and the two
have opposite maintenance rules. A design doc, an ADR, an audit records what was
decided and why; it stays true forever, because the past does not change. A
guide, a runbook, an invariant registry, a "what's left" tracker describes the
system as it is right now, and starts rotting the day it is written.

**Date-stamp the past. Fix or retire the present.** This is not a preference —
it predicts where the defects are. The 2026-09-01 doc audit found ten live
defects and **all ten came from present-tense docs**: guides, runbooks, an ADR
whose status was left behind by its own build commit, and two root trackers.
**Zero came from a completed design doc.** The 2026-08-18 corpus audit before
it found 14 of 52 plan headers that would have sent a reader to rebuild shipped
work — every one a plan that shipped and never had its status updated. No doc
in this repo has ever overclaimed; the record only ever undersells what exists.

- **Every doc carries a status line in its header block — plans, guides,
  runbooks and ADRs alike.** Line 3, or just below it when a `**Date:**` (and
  sometimes `**Branch:**`) block comes first — 5 of the 70 design docs are
  shaped that way, four on line 4 and one on line 7. Vocabulary: `PLAN` ·
  `PARTIALLY BUILT` · `MERGED #N` · `RELEASED vX.Y.Z` · `HISTORICAL`. It names
  what is *not* built when the answer is "some of it". A doc with no status
  line is incomplete. Measured 2026-09-01 over tracked files, `docs/design/`
  was at **64 of 65** and `gdx_dispatch/docs/` at **10 of 42** — and that gap
  was not a coincidence, it was exactly where the ten defects were.
  **Re-measured 2026-09-12: `docs/design/` is 70 of 70 and
  `gdx_dispatch/docs/` is 41 of 41.** The gap that produced those defects is
  closed; the rule is what keeps it closed. (This bullet had itself gone stale
  in the direction the paragraph above predicts — it undersold what exists.)
- **The status line ships with the code.** A PR that implements part of a plan
  updates that plan's status in the same PR. ADR-016 was edited *inside its own
  build commit* and still read "nothing built yet" while the feature sat in the
  sidebar — that is the failure this rule exists to stop.
- **Docs state; code proves.** Never cite a plan as evidence of current state —
  not its status line, and especially not its "what already exists" table. Two
  such tables in this repo were wrong, one of them on the day it was written.
  Re-verify against code, a PR number, or a release tag before acting.
- **Half-shipped is not shipped.** When a multi-part fix lands partially, the
  record names which parts. "M4 fixed" for a fix that landed in one of two
  files is worse than no note at all.
- **A finding names an instance; the fix owns the class.** Audit findings are
  numbered by where someone happened to look. Fixing M13 means fixing every
  place that shape lives, or recording which instances you are leaving and why.
- **A fix is done when its guard runs.** A test excluded from the default gate
  is not a regression net. Shipping the test and leaving it unrun is a silent
  no-op.
- **Check for a rival plan first.** Before writing a plan, grep `docs/design/`
  recursively (finished records live in `docs/design/archive/` since
  2026-09-06) for others naming the same files. If one exists, cite it or mark it
  superseded — in both docs. Two plans in this repo reached opposite decisions
  about the same money path without ever referencing each other.
- **Keep the past; retire the present.** A shipped plan stays — its rejected
  alternatives and audit findings are the part code cannot recover. Measured
  2026-09-07: **14** source files cite a design doc by filename, **11 of them
  migrations** — and those migrations are self-documenting (056 carries the
  whole money-rail argument inline and merely names the audit it came from),
  so the doc is provenance, not the record. This page previously claimed 56
  and 8, and claimed the doc was those migrations' *only* record of why a
  money column is locked; both were wrong. Deleting one still manufactures the
  dead references this repo audits for. A
  present-tense doc whose subject no longer exists is the opposite case: it
  carries no reasoning, only instructions for a system that isn't there. Give
  it a `HISTORICAL` status line saying what it described and that the thing was
  never built. Deletion is available for that class and that class only, and
  only when the doc holds no decision anyone could still need.
- **Open a plan with "what already exists (do not rebuild)."** The best doc in
  the corpus established that half its ask needed no code at all.

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

## Known sharp edges

- jsdom applies **no media queries** — only a real browser proves layout.
- `useDestructiveConfirm` really confirms in the app — issue #215 was fixed
  2026-08-05 (`useConfirm()` now resolves during `setup()`). The fallback
  still auto-accepts when no ConfirmationService is registered, which is
  every vitest environment: unit tests never exercise the dialog, so a
  destructive flow is only proven in a browser.
- **A green ratchet proves nothing unless it can fail for your defect.**
  Before citing a scanner or baseline as evidence, name the input that would
  turn it red. `tests/authz_sweep.py` counts any authenticated route as gated,
  so it can never fail for a missing permission check — `routers/payments.py`
  has 5 mutation routes, 0 permission gates, and a green sweep (it was 7 until
  the 2026-09-10 orphan sweep deleted two; re-counted 2026-09-12). (Several of
  those are public by design and token-scoped; see `.authz_ungated_baseline`
  before "fixing" one. The point is the sweep cannot tell you which.)
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
- There is no checked-in OpenAPI document; `/openapi.json` is served live. The
  route table is pinned in `gdx_dispatch/openapi_routes.txt` (generated by
  `python -m gdx_dispatch.tools.openapi_snapshot --write`, gated in the default
  suite) — and `app.openapi()` collapses duplicate (method, path) registrations
  and names the losing handler, so for exact handlers read the routers.
- Plugin `type: list` screens must return a bare JSON array, never
  `{items: [...]}`.
- Plugin manifest handling: warn-and-strip unknown fields, never raise —
  raising during manifest parse silently removes the plugin.
- **SQLite stores a `Uuid` column as 32 dashless hex.** Raw SQL comparing
  `id = :dashed_uuid` matches on Postgres and **never** on SQLite — and a
  gate that cannot match looks exactly like a legitimate refusal.
- **`create_all` tables diverge from the ORM**, so a schema sweep has to
  search the *database*, not the models. For the same reason test fixtures
  must build their schema from the ORM: hand-written DDL once hid a feature
  that could not be inserted at all.
- `.tenant_plane_redundant_filter_baseline` is **line-keyed** — deleting or
  adding lines above a recorded finding shifts it and reddens the scan even
  when nothing changed. Re-freeze with
  `gdx_dispatch/tools/tenant_plane_redundant_filter_scan.py`.
- **`# noqa:` is shared with the repo's own scanners.** Their codes
  (`RAW_ENC`, `T6`, `X1`) are not ruff rules, so ruff prints
  `Invalid # noqa directive` for each — and a scanner code placed *before* a
  ruff code voids the ruff suppression.
- CodeQL's `py/path-injection` recognizes only `realpath`/`normpath`/`abspath`
  followed by `startswith`. `Path.resolve().is_relative_to()` and
  `commonpath` are genuinely safe and still flagged.
- **`pip install --target` does not replace an existing package directory.**
  A plugin artifact upgrade leaves the OLD code in place while writing the NEW
  dist-info, so `/ready` is green and stale-detection is fooled. Remove the
  package dir and both dist-infos, then restart. The plugin tables also drift:
  `schema_reconcile` auto-ALTERs them at boot.
- **A mock proves which arguments were passed, never which value comes back** —
  run at least one real invocation. And a test asserting that source text is
  *present* proves nothing; asserting text is *absent* is fine.
- **Playwright MCP autofills the production password.** Never open a prod
  login page with it — inject a token instead; headed snapshots have written
  that password to disk.

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
(net-zero while the sweep budget is CLOSED; see `PHASE.md`). One exception:
something a real user can hit on prod today is raised the moment it is seen.
**That exception is self-triggering, so distrust it**: Claude both judges
whether it applies and benefits from it applying. "Reachable in principle" is
not "a real user hits it today" — a hole needing a hand-crafted request from a
staff account in a single-tenant app is a hardening gap, not a live defect. If
the exception is the only thing authorizing a filing, that is the signal to
ask Doug instead. (2026-09-12: #712 was filed on exactly that mistake and
closed back to the ledger.)
Doug can widen the scope of a session; Claude does not. Adopted 2026-09-10.

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
