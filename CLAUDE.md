# Working agreement — gdx_dispatch

## The rule above the rest

Answer with evidence, not assurance. Every "done", "green", or "deployed" claim
ships with the artifact that proves it — pasted output, a screenshot, or a
browser walk. No evidence in hand means the claim is "not yet verified", said
plainly.

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
- CI (`ci.yml`) runs only on main. Mid-stack PRs need the local test matrix
  run and its results posted before merge.
- Main is merge-protected; `--admin` merge is the sanctioned path, but only
  after enumerating every check's result by name.

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
   opened, that router is in scope.
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

- **Every doc carries a status line on line 3 — plans, guides, runbooks and
  ADRs alike.** Vocabulary: `PLAN` · `PARTIALLY BUILT` · `MERGED #N` ·
  `RELEASED vX.Y.Z` · `HISTORICAL`. It names what is *not* built when the
  answer is "some of it". A doc with no status line is incomplete. Measured
  2026-09-01: `docs/design/` is at 68 of 69, `gdx_dispatch/docs/` at 1 of 42.
  That gap is not a coincidence — it is exactly where the ten defects were.
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
  for others naming the same files. If one exists, cite it or mark it
  superseded — in both docs. Two plans in this repo reached opposite decisions
  about the same money path without ever referencing each other.
- **Keep the past; retire the present.** A shipped plan stays — its rejected
  alternatives and audit findings are the part code cannot recover, and 56
  source files cite design docs by filename, **8 of them immutable migrations**
  whose only record of *why* a money column is locked is the doc they name.
  Deleting one manufactures the dead references this repo audits for. A
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
  has 7 mutation routes, 0 permission gates, and a green sweep. (Several of
  those are public by design and token-scoped; see `.authz_ungated_baseline`
  before "fixing" one. The point is the sweep cannot tell you which.)
- There is no checked-in OpenAPI document; `/openapi.json` is served live. The
  route table is pinned in `gdx_dispatch/openapi_routes.txt` (generated by
  `python -m gdx_dispatch.tools.openapi_snapshot --write`, gated in the default
  suite) — and `app.openapi()` collapses duplicate (method, path) registrations
  and names the losing handler, so for exact handlers read the routers.
- Plugin `type: list` screens must return a bare JSON array, never
  `{items: [...]}`.
- Plugin manifest handling: warn-and-strip unknown fields, never raise —
  raising during manifest parse silently removes the plugin.

## Domain rules that shape code

- Garage-door work in Minnesota is a construction contract: **never charge
  the customer sales tax.**
- Billed labor comes from **attested hours only** — elapsed clock time is
  not evidence and code may not invent hours.
- Taxonomy: a service call is a repair; a converted estimate is an
  installation.
- QuickBooks is being phased out: never schedule new QB syncs; backfills go
  into this system, not QB.

## Close every work turn with

- Commit status: committed? pushed? PR number? Anything intentionally
  uncommitted, and why.
- What was verified (with the evidence), and what was not.
- Remaining open items as a list — "nothing left" requires having looked.

## Planning defaults (standing answers — don't re-ask)

- **Scope:** build the full recommended rung. Ask only when the larger option
  adds a migration, changes money math, or alters customer-facing behavior.
- **Packaging:** separate focused PRs; stacked PRs merge bottom-up; tech debt
  discovered mid-feature gets filed as its own follow-up — never bundled,
  never silently dropped.
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
