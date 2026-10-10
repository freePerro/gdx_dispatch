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
- ~15 known cross-file test flakes exist on main: they pass in isolation.
  Re-run in isolation before blaming your branch.
- Lint is a **ruff ratchet against a baseline**, not plain pass/fail — a
  branch can be "clean" and still over the ratchet.
- **CI (`ci.yml`) triggers on `pull_request` with base `main`, and on `push`
  to `main`.** It does *not* run on a PR whose base is another feature branch —
  which is why **mid-stack PRs still need the local matrix run and its results
  posted before merge**. Note also the `push` trigger carries a paths filter
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
  of leaving it off is that the Postgres arm goes **silently SKIPped** (102
  tests, measured 2026-09-14): a green local run has not exercised PG.
  The runner and `ci.yml` both pass `-ra` (GDXA-128), so every shard names
  its skips — read the categories, or the gap is invisible. On a bare `pytest`
  use `-ra` too and never plain `-rs`: `-r` REPLACES pytest's `fE` default, so
  `-rs` deletes the `FAILED`/`ERROR` summary lines.
  CI runs the `DATABASE_URL` and `GDX_TEST_PG_*` Postgres tests, and under `CI`
  an unreachable Postgres fails them rather than skipping (#440). It does
  **not** run the 8 `TEST_DATABASE_URL` role tests or the
  `GDX_TEST_CONTROL_DB_URL` integration tests — those still skip green there.
- **After `git merge origin/main` and before the matrix, run
  `gdx_dispatch/tools/run_tests_split.sh --scans`** (~30 s, one docker
  process, gitdir mounted, `-ra`, non-zero on red, no wait for the matrix
  lock). It runs the tests that pin `.tenant_plane_redundant_filter_baseline`
  and `.duplicate_block_baseline` against the tree; those two line-keyed
  baselines were the matrix red on at least eight agent issues (GDXA-408).
  It does **not** cover the doc-link, PII, authz, route-shadow or OpenAPI
  baselines: green `--scans` is not "baselines clear", and the matrix still
  runs. The file list is derived by naming convention
  (`gdx_dispatch/tools/baseline_scan_tests.py`; `--scans --list` prints it):
  a test file named `*scan_refreeze*`, or one defining
  `test_the_committed_baseline_is_what_the_tree_scans`, joins without an edit
  here. On red,
  `run_tests_split.sh --refreeze-baselines` re-freezes both baselines: it
  admits shrinkage and line shifts and **refuses growth** (exit 2; the
  refusing scan's baseline is untouched, the other is still re-frozen; a
  scanner crash exits 1). It passes `--allow-new` only when you type it; do that only for a
  clone or filter you have read and mean to keep. A hand-rolled
  `docker run -v $PWD:/app` in a linked worktree cannot read the git index and
  fails these tests with `TrackedFilesUnavailable`, which looks like a stale
  baseline. Use the runner.
- **`run_tests_split.sh` takes a host-wide lock** (`/tmp/gdx_matrix.lock`): a
  second matrix waits and says so, because two at once are slower than two in
  a row (2026-09-24: four at once put the 20-core box at load 28 with 1.8 GB
  free). `MATRIX_LOCK=0` bypasses it. A docker `PYTEST` gets
  `--tmpfs /tmp` injected (`TMPFS_TMP=0` opts out): the tests' file-backed
  SQLite fsyncs on every commit, and with `/tmp` on the host disk shard 4 took
  ~14 min against ~1 min on tmpfs (2026-10-05); the full matrix is ~6 min.
  In a linked worktree the script also mounts the gitdir into a docker
  `PYTEST`, so the tracked-set guards pass there instead of failing 14 tests
  for want of the index.
- **A failed matrix sorts its reds against main** (`tools/matrix_vs_main.py`):
  NEW (probably yours), FLAKY ON MAIN, or ALREADY FAILING ON MAIN, from a baseline the
  maintainer's host records once per `origin/main` commit (a user timer outside
  this repo, ~/.cache/gdx_matrix_baseline). <!-- host paths, outside this repo; link-ok -->
  It is a report: the exit code is unchanged. With no baseline on the host, the
  reds say "unclassified", never "not yours"; against a baseline older than the
  merge-base they say "WAS RED ON MAIN … NOT proven", since main may have fixed
  the test since. A shard that dies without naming a test is never recorded as
  green. NEW is not proof: the baseline is a single run, so a flaky test can
  pass there and fail here. The script also prints the docker
  image's age and key library versions, and warns when the image predates the
  last `requirements.txt`/Dockerfile change: requirements.txt pins ranges and
  CI resolves them fresh each run, so an old image can disagree with CI.
- `pytest.ini` already carries `-q`; adding another makes output useless. To
  read a CI failure use the `jobs/<id>/logs` API, not `gh run view --log`.
- **A foreground `sleep` is blocked and a background Bash dies at 600s.** Long
  runs go through `nohup`, watched by ONE `Monitor` or a single until-loop —
  never a poll loop.
- **A monitor's poll command is proven once in the foreground before it is
  armed, and it never swallows its own failure** — emit the error as an event
  and exit, so silence can only mean "still waiting". On 2026-09-23 a
  `gh pr checks --json` loop written with `|| continue` sat silent for its
  whole 30 minutes: the distro's gh 2.46.0 had no `--json` on `pr checks`
  (added in gh 2.50.0). gh has come from GitHub's own apt repository
  (`/etc/apt/sources.list.d/github-cli.list`) since 2026-09-23; plain
  `gh pr checks` output carries the verdict in column 2 regardless.
- The frontend lockfile can only be regenerated on **npm 11**: 10.8.2 (what
  CI's `setup-node` 20 and `node:20-slim` ship) and 9.2.0 both crash in
  arborist with `Cannot read properties of null (reading 'edgesOut')`. Prove
  `npm ci` in `node:20-slim` before pushing a lockfile.
- The git-ignored local `docker/demo/` directory fails three scanner tests
  locally that are green in CI. A fresh worktree does not have it — prefer one
  for a clean read.
- Mako 1.4.0 shadows `tools/`, producing an ImportError that only appears in
  CI.
- The local stack's `refresh.sh` does not rebuild the plugin-host; rebuild it <!-- ~/gdx-local/refresh.sh, outside this repo; link-ok -->
  by hand or you are testing stale plugin code.
- Compose `--env-file`: an empty `JWT_SECRET` crash-loops the container. The
  `verifyplaywright` path passes `--env-file` rather than cloning the
  environment, deliberately — cloning trips the credential guard.
- Each gated commit needs its own dedicated `cd`; everywhere else use
  `git -C`. Check the current branch before every commit — an IDE or a
  parallel session can move it under you.
- `ssh gdx-vps` reaches production over Tailscale. Real mobile verification
  runs on the local Pixel 8 AVD, where `10.0.2.2` is the host.
- **ast-grep is `~/.local/bin/ast-grep`** (pip `ast-grep-cli` 0.45.3, user <!-- user-site binary, outside this repo; link-ok -->
  site). Invoke it as `ast-grep`, never `sg`: `/usr/bin/sg` is the Linux
  group-switch command (a symlink to `newgrp`). The pip `sg` launcher was
  deleted on purpose and a pip upgrade recreates it — delete it again if it
  reappears; nothing checks for it. ast-grep has no `vue` language: `.vue`
  files are reached only through the root `sgconfig.yml` mapping them to
  `html`, after which `-l js` patterns match inside `<script>` blocks. Without
  that file every `.vue` file is skipped silently (`--inspect summary` should
  say `isProject=true`). The same file makes the repo an ast-grep project
  with no rule directories, so a bare `ast-grep scan` at the root exits 0
  having checked nothing — pass rules explicitly. A `severity: info` rule prints as `note[…]`, not
  `info[…]`, so grepping for `info[` finds nothing — count findings with
  `--json`.

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

Every doc is either **about the past** or **about the present**, and the two
have opposite maintenance rules. A design doc, an ADR, an audit records what was
decided and why; it stays true forever, because the past does not change. A
guide, a runbook, an invariant registry, a "what's left" tracker describes the
system as it is right now, and starts rotting the day it is written.

**Date-stamp the past. Fix or retire the present.** This is not a preference —
it predicts where the defects are. The 2026-09-01 doc audit found ten live
defects and **all ten came from present-tense docs**: guides, runbooks, an ADR
whose status was left behind by its own build commit, and two root trackers.
**Zero came from a completed design doc.** No doc in this repo has ever
overclaimed; the record only ever undersells what exists.

- **Every doc carries a status line in its header block — plans, guides,
  runbooks and ADRs alike.** Line 3, or just below it when a `**Date:**` block
  comes first. Vocabulary: `PLAN` · `PARTIALLY BUILT` · `MERGED #N` ·
  `RELEASED vX.Y.Z` · `HISTORICAL`. It names what is *not* built when the
  answer is "some of it". A doc with no status line is incomplete. Measured
  2026-09-12 over tracked files: `docs/design/` is 70 of 70 and
  `gdx_dispatch/docs/` is 41 of 41. Every doc complies today; the rule is what
  keeps it that way.
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
  alternatives and audit findings are the part code cannot recover, and
  deleting one manufactures the dead references this repo audits for. A
  present-tense doc whose subject no longer exists is the opposite case: it
  carries no reasoning, only instructions for a system that isn't there. Give
  it a `HISTORICAL` status line saying what it described and that the thing was
  never built. Deletion is available for that class and that class only, and
  only when the doc holds no decision anyone could still need.
- **Open a plan with "what already exists (do not rebuild)."** The best doc in
  the corpus established that half its ask needed no code at all.
- **When something is to be deleted, delete it.** No tarball, no `archive/`
  copy, no "just in case" branch, no commented-out block. Every hedge becomes
  a second thing to maintain, to search, and to be misled by later — and the
  copy is always the one that goes stale unnoticed (Doug, 2026-09-12). This
  does not override *Keep the past*: a shipped plan's reasoning still stays,
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
- **`# noqa:` is shared with the repo's own scanners** (`RAWENC1`, `T6`,
  `X1`). ruff ignores a code it doesn't know, but it parses every `noqa`
  comment and accepts only uppercase letters followed by digits. A code
  outside that grammar gets `Invalid # noqa directive` and voids every ruff
  code listed *after* it. So does `noqa:` prose in a plain comment, unless a
  valid `noqa` comes before it on the line. The scanner code was `RAW_ENC`
  until GDXA-302 (2026-10-06), and it warned on every site. Check with
  `ruff check --no-cache`, because cached files re-emit no warnings.
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
- **A CONFLICTING PR runs no `pull_request` CI at all — silently.** GitHub
  cannot build the merge ref, so no run is created: not queued, not failed,
  nothing. A PR showing zero checks is DIRTY, not pending (cost a full CI
  cycle on #757/#758, 2026-09-20). Check `gh pr view N --json mergeable`
  before reading absent checks as anything.
- **With squash-merges, a stacked PR always goes CONFLICTING the moment its
  parent merges** if they touch the same files (the baselines guarantee they
  do). Not a merge mistake — the child still carries the parent's pre-squash
  commits. The fix is always the same two minutes: `git checkout -B <branch>
  origin/main && git cherry-pick <child's own commit>` and force-push; do
  NOT `git rebase origin/main`, which replays the whole pre-squash stack
  into conflicts (2026-09-20, three rounds of #753–#758).

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
