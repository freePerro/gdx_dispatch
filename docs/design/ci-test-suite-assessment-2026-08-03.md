# CI & Test Suite Deep Assessment — 2026-08-03

**Status:** **PARTIALLY ACTIONED.** §7 items 1, 2 (#264) and 6 (#265) done.
**Item 3 closed and item 7 MOSTLY closed, 2026-08-23.** **Still open:** item 4
(targeted `@pytest.mark.forked`), item 5 (a production-faithful `client`
fixture), and **the rest of item 7** — `gdx_dispatch/tests/load/locustfile.py`
still exists (never run, points at a placeholder domain) and the 31
`test_NN_*` sprint-numbered files are unrenamed. Named rather than glossed:
an adversarial review caught the first draft of this line claiming item 7 was
closed outright.

**Item 3 — re-scoped by verification.** Three of its four asks were already
done by the 2026-08-04 rewrite: the marker filter is preserved, `--forked` is
available via `FORKED=1`, and the `.venv` assumption is gone (`$PYTEST` >
`.venv` > `python3`, with a message naming the docker form). What remained was
`.test_durations`, and it was genuinely stale: **6,009 entries across 523 test
files, 38 of which no longer exist.** Regenerated from a full serial run
(`--store-durations`, which needs `PYTEST_FULL_SERIAL=1` to get past the
conftest guard) and then **pruned against the collected set** — `--store-durations`
MERGES rather than replaces, so the first pass left all 38 dead files in place
and grew the file to 7,281 entries. After pruning: **6,828 entries, 554
collectable files, 0 dead FILES.** The prune is file-level — `--collect-only -q`
prints `file: count`, not node ids — so ~67 entries for tests removed from
files that still exist remain. pytest-split ignores unknown keys, so they cost
nothing; the claim is stated per-file because that is what was measured.

**What this did NOT fix, stated plainly because the first draft of this note
claimed it did.** The local shard wall-clock spread — one shard 166s, another
542s — is **not** caused by stale durations. pytest-split balances correctly:
asked for group 4 it reports `estimated duration: 183.42s` against a 180s ideal
(1,259s ÷ 7). The spread is CPU contention from running seven docker containers
on one 14-core laptop; the shards sum to 2,305s of wall-clock against 1,259s of
recorded work, a 1.8× inflation. In CI each shard is its own runner, where
accurate durations do translate into balance — that is where the payoff is.
The suite's genuinely slow tests are network-retry paths: one
`test_phone_com_sync` case at 47.7s and five OAuth-callback cases near 20s each,
12 tests accounting for 15% of the recorded total.

⚠ **A claim in this doc's own §3 was wrong**, and it would have destroyed
coverage: *"`test_13_onboarding.py` (4 tests, fully subsumed by
`test_24_onboarding.py`'s 33)"*. **None of its four test names appears in
`test_24`** — `test_onboarding_returns_six_steps`,
`test_onboarding_step_structure`, `test_onboarding_graceful_on_missing_tables`,
`test_onboarding_percent_calculation`. It was NOT deleted. A second claim was
also wrong but harmless: `test_zz_probe_integration.py` is described as
untracked; it is tracked.

**Item 7 — the deletes, each verified before removal rather than trusted:**

| Removed | Evidence it was dead |
|---|---|
| `tests/_reproductions/` (9 files) | Named `bug_test_*.py`, which **cannot match** pytest's `test_*.py` pattern — never collected by any invocation. Bodies are stubs: `xfail(strict=False)` with the real assertion commented out. |
| `tests/_adversarial/` (2 files) | The runner and curator `pytest.ini` named — `adversarial_tests.py`, `tools/orchestrator/adversarial_curate.py` — **do not exist**. Nothing ran them by path or otherwise. |
| `tests/factories/` | Contained only `__pycache__`. |
| `tests/test_zz_probe_integration.py` | 0 bytes. |
| `.test_baseline` | Nothing reads it; its enforcing script `tools/pre_commit_test_gate.sh` is gone. |
| `pytest.ini` markers `auth`, `billing`, `infra`, `requires_pg` | **0 uses** of `@pytest.mark.<name>` each. `requires_pg` reads as live — the name appears 12 times — but every one is a local `_requires_pg = pytest.mark.skipif(...)` variable or prose, never the declared marker. |

**Also corrected, in memory rather than here:** a standing note claimed
`run_tests_split.sh` "prints FAIL but exits 0". It does not — planting a
failing test and running it with no pipe returns **exit 1**. The original
observation came from piping it to `tail`, which replaces the exit status.

Read-only assessment of whether the CI workflows and test suite are still relevant after the
last ~7 weeks of heavy feature work, or whether they're accumulating tech debt. Produced from
three parallel deep sweeps (backend suite relevance, test infrastructure, coverage-vs-recent-work)
plus live GitHub Actions run data. **Nothing was changed as part of this assessment.**

Companion doc: `testing-gaps-2026-07-24.md` (the gap list from ten days earlier — see §6 for
how it has aged).

---

> **Status update (2026-08-04):**
>
> - **§7 items 1 + 2 → PR #264, all checks green.** Lying tests fixed (tax module
>   now has real endpoint coverage + a `create_app()` reachability guard; the
>   health-check 503 fix surfaced nothing hidden in CI); UX gate + its
>   negative-test wired into the frontend job as blocking steps, with the 5
>   legacy native-confirm sites grandfathered as WARN pending #215;
>   `ai-code-review.yml` deleted.
> - **§7 item 6 → decided: kill, and built: PR #265** (stacked on #264 — merge
>   #264 first; #265 only gets its CI run once it retargets `main`). Removes the
>   8 control-plane modules, `modules/contractors/`, 7 templates, and 12 test
>   files (~108 tests). **Kept:** `api/public_router.py` (`/api/v1`) and
>   `core/api_keys.py` — the garagedoorxperts.com lead form and `/used-doors`
>   feed depend on them; the removed `core/public_api.py` was the dead bare-`/v1`
>   twin. `test_control_plane_removed.py` pins both directions. Five
>   `create_all`-only tables are left in place (no drop migration); 7 days of
>   prod access logs showed zero legitimate traffic to any removed surface.
> - **Still open:** §7 items 3 (`.test_durations` regen + `run_tests_split.sh`
>   fixes), 4 (targeted `@pytest.mark.forked` experiment), 5 (shared
>   production-faithful client fixture + e2e green-by-skip conversion), and
>   7 (hygiene deletes: `_reproductions/`, `factories/`, `_adversarial/`,
>   `load/`, `test_zz_probe_integration.py`, stale doc refs, sprint-file
>   renames).

## Verdict

The suite is **not bloat to mass-delete**. It is ~540 files / ~5,900 test functions, most of it
alive, and recent feature discipline is genuinely good — **68% of non-merge commits shipped with
tests**, and CI caught a real regression in the last week (the stale `_SW_JS_PATH` PWA relic
test on `feat/pwa-installable`).

The debt is real but concentrated in three categories:

1. **Tests that actively lie** (assert features don't exist that do; skip instead of fail).
2. **Dead scaffolding** (directories never collected, workflows that skip every run, docs citing
   deleted tools).
3. **Missing gates already identified on 2026-07-24 and not closed since** — the suite keeps
   growing in the kinds of tests it already has, and not in the kinds it identified as missing.

The most expensive single problem isn't a test at all: shard balancing runs on stale data and the
`--forked` workaround makes the suite ~4× slower, together burning ~60–80 Actions-minutes per push.

---

## 1. Inventory — what exists and what actually runs

### Workflows

| Workflow | Gates merges? | State |
|---|---|---|
| `ci.yml` (lint / 7 test shards / build / frontend) | **Yes** | Healthy; well-commented; see §5 for cost |
| `security.yml` (pip-audit + Bandit) | No — both end `\|\| true` | Report-only; artifacts uploaded daily; CodeQL + Dependabot are the real gates |
| `ai-code-review.yml` (Cerebras) | No | **Never worked** — see §3 |
| `release.yml` | n/a | Working (release-tag driven) |
| CodeQL, Dependabot, Dependency Graph | Yes (CodeQL) | GitHub-side, healthy |

### Test tree (`gdx_dispatch/tests/`)

| Dir | Files | Test fns | Runs in CI? | Verdict |
|---|---|---|---|---|
| root | 490 | ~5,250 | **Yes** | The real suite |
| `serial/` | 11 | 118 | Yes (sharded like anything else) | Alive; the name is a lie — see §4 |
| `contracts/` | 2 | 2 | Yes | Alive but 1/7 detector coverage; NOT schemathesis |
| `integration/` | 2 | 10 | Collected, **100% skipped** | Zombie — `GDX_TEST_CONTROL_DB_URL` never set in CI |
| `health/` | 1 | 10 | No (`-m "not health"`) | Opt-in by design, but its alert-bus consumer was removed |
| `e2e/` | 38 | 473 | No (`--ignore` + marker) | Opt-in, needs live VPS; riddled with green-by-skip (§2) |
| `_adversarial/` | 1 | 5 | No (`norecursedirs`) | **Dead** — curating tools deleted |
| `_reproductions/` | 7 | 9 | **Never collected** | **Dead** — `bug_test_*` names match no pytest pattern; 2 files import deleted modules |
| `load/` | 0 | 0 | No | **Dead** — one locustfile pointed at a placeholder domain |
| `factories/` | **0** | 0 | n/a | **Empty directory** — only stale `.pyc`; still cited by `PG_GATE_TRIGGER.md` |

Frontend: ~180 vitest specs (31 in `frontend/tests/`, ~149 colocated in `src/**/__tests__/`) — all
run in CI. 15 Playwright specs in `frontend/e2e/` — manual only.

---

## 2. Tests that are actively lying (fix first — they create false confidence)

- **`tests/test_39_tax_settings.py` is the worst file in the suite.** 4 of 5 tests assert the tax
  endpoints return 404 on the premise (docstring) that "no tax routes exist." But
  `modules/tax/router.py` is real (6 endpoints under `/api/tax`), mounted in `app.py`, and called
  from `routers/jobs.py`, `routers/invoices.py`, and `routers/change_orders.py`. The test fixture
  builds an **empty** `FastAPI()` — its one `include_router` is try/except-wrapped around
  `core.gdpr_router`, a module that no longer exists — so every request 404s by construction.
  Net: a live, revenue-affecting tax module has **zero endpoint tests** behind a green file that
  claims it isn't built.
- **~140 of 185 runtime `pytest.skip()` calls are green-by-skip chains in `e2e/`** — "Seed job was
  not created" ×11, "No estimate created", "Customer not created", etc. A regression that breaks
  job creation turns the entire downstream e2e chain green-skipped instead of red. (Moot in CI,
  which excludes e2e — but it poisons every manual e2e run.)
- **`test_01_gdx_scaffold.py:33`** — the skip condition ends in `or rv.status_code == 503`, making
  the DNS-specific clauses dead: *any* 503 from `/health` silently passes.
- **`tests/conftest.py:203-208`** resets state in `core/superadmin` — **a deleted module** — inside
  `except Exception: pass`, so this cross-test-pollution guard has been a silent no-op since the
  deletion. Six test files still reference superadmin. Seven sibling reset guards use the same
  except-pass pattern; the next module rename fails the same silent way.
- **`test_40_ai_quote.py`** — three tests whose entire assertion is `status_code != 404` against a
  self-built app that mounts exactly that router (structurally incapable of failing), plus one test
  pinning `/api/catalog/items`, a path that never existed (real surface is `/api/catalog/parts` etc.).
- **`test_03_sprint2_modules.py:168`** (`test_qb_webhook_deduplication`) is tautological — inserts a
  row, re-queries it twice, asserts identity. Never touches the webhook endpoint or any dedup code.
- **`test_circuit_breaker.py:229`** — skipped with reason "test expectation invalid"; a known-wrong
  test kept instead of fixed or deleted.

### The structural root

**138 test files (25%) build their own bare `FastAPI()`** with hand-rolled auth/tenant shims; only
21 use the real `create_app()`. They exercise handler functions but prove nothing about auth,
middleware, tenant resolution, or reachability. Compounding it, `app.py` wraps router imports in
try/except → empty-`APIRouter()` fallback — so a production router import failure silently 404s an
entire surface, **and no test anywhere would catch it**. The mechanical cause is fixture
availability: `tests/conftest.py` exports no shared production-faithful `client`/`auth_headers`
fixture, so 60 files each define their own local `client`.

---

## 3. Dead weight (safe deletes, zero coverage loss)

**`ai-code-review.yml` has never done anything.** Verified in run logs: `CEREBRAS_API_KEY` is not
set, so both working steps show `skipped` on every PR while a runner spins up, installs the SDK,
and reports success (6 green runs, 0-minute durations). Even with a key it would fail twice over:
the inline script has a Python bug (`messages=[{{...}}]` builds a set-of-dict → TypeError at
runtime), and the prompt reviews for **Flask** patterns from the pre-GDX codebase — blueprints,
`app_factory.py`, `_safe_commit`, `db/models.py` — none of which exist in this FastAPI app.

Also safe to delete (verified unreferenced):

- `tests/_reproductions/`, `tests/factories/`, `tests/_adversarial/` (+ the `pytest.ini`
  `norecursedirs` comment citing `adversarial_tests.py` / `adversarial_curate.py`, neither of which
  exists), `tests/load/` + the `load` marker + the `locust` dependency.
- `tests/test_zz_probe_integration.py` — untracked, **0 bytes**, created Jul 31; leftover probe.
- `test_13_onboarding.py` (4 tests, fully subsumed by `test_24_onboarding.py`'s 33),
  `test_15_admin_api.py` (2 pure existence checks), the two markdown-length tests in
  `test_12_secrets_and_ha.py`, the two skipped `pass`-body stubs in `test_marketing.py:448,453`.
- `tests/conftest.py:26-53,108-132` — the SS-12A debug heartbeat: four pytest hooks string-compare
  **every test in every shard** against `test_01_gdx_scaffold_hang_capture.py`, a file that does
  not exist.
- `tests/conftest.py:99-105` — runtime schemathesis-xdist unregister, superseded by
  `-p no:schemathesis_xdist` in `pytest.ini` (whose own comment says so).
- `.test_baseline` (2989) — its enforcing script `tools/pre_commit_test_gate.sh` was deleted;
  `RD_SYSTEM.md:49` still cites it. Same for `PG_GATE_TRIGGER.md` (whole file — its runner
  `run_pg_integration_tests.sh` doesn't exist) and `refresh_test_schema.sh` (cited by
  `tests/fixtures/pg.py:11`).
- Unused marker declarations: `auth`, `billing`, `infra`, `requires_pg` (0 uses each).
- 13 redundant `sys.path.insert(0, _REPO_ROOT)` lines (pytest.ini already sets `pythonpath = .`).

**Schemathesis is abandoned, not just idle**: the only consumer (`e2e/test_schemathesis.py`) points
at a placeholder domain and makes a network call **at import time** — which makes ci.yml's
`--ignore=tests/e2e` load-bearing, not redundant (the `-m` filter alone runs after import). The
package remains a hard dependency purely to be disabled.

---

## 4. The "duplicate" sprint files are mostly NOT duplicates

Symbol-level overlap of the suspicious `test_NN_<name>` pairs:

| Pair | Prod-symbol overlap | Relationship |
|---|---|---|
| 16 ↔ 21 sla_monitor | 0% | HTTP routes vs internal Redis pipeline |
| 16 ↔ 26 contractors | 0% | raw model CRUD vs real HTTP endpoints |
| 16 ↔ 33 integrations | 0% | webhook fan-out/HMAC vs service API surface |
| 16 ↔ 29 audit_dashboard | 0% | mini-app HTTP vs service fns + hash-chain tamper test |
| 16 ↔ 22 task_monitor | 0% | near-disjoint endpoint split |
| 16 ↔ 25 platform_analytics | 33% | module refactor; only one file followed |
| **13 ↔ 24 onboarding** | 20% | **genuine redundancy — 24 absorbed 13; delete 13** |
| **25 ↔ 40 ai_quote** | 33% | **real duplication on the same engine** |

6 of 8 are complementary layers with a bad naming convention. The fix is **renaming**
(`test_contractors_models.py` / `test_contractors_router.py`), not deletion.

Other keep/kill calls:

- **QuickBooks tests (219 fns across 11 files) are not legacy debt.** `test_gl_qb_pull_disable.py`
  is the guard that enforces the QB phase-out (money-mutating pulls fail loudly under the ledger
  flag). Keep until QB is actually gone.
- **The multi-tenant control plane — DECIDED 2026-08-03: kill it.** ~120 tests defend
  `platform_analytics`, distributor/wholesaler dashboards, `developer_portal`, `status_page`,
  `task_monitor`, `sla_monitor`, and public API v1 — all still mounted in `app.py`, all with
  **zero** Vue references, in an app that is single-tenant by decision. Either delete feature +
  tests together, or accept them as a maintained API-only surface. (`openapi.json` still
  self-describes as "Multi-tenant field service dispatch platform API".)
- **`serial/` is serial in name only.** No marker, plugin, or runner makes it serial; in CI it's
  safe *accidentally* because `--forked` isolates every test in a subprocess. The local runner
  omits `--forked`, recreating exactly the hazard the directory was meant to prevent.

Skip/xfail hygiene is otherwise fine: 13 `skip`, 11 `skipif`, 10 `xfail` static markers — trivial.
The 185 runtime skips (§2) are the real story.

---

## 5. CI mechanics — where the minutes go

A completed run costs **~109 runner-minutes** (lint 0.2 + build 1.8 + frontend 1.7 + seven test
shards at ~13–17 min each). Repo is public, so minutes are free — but wall time to green is
~17–20 min per push. Recent reliability is good: 10 runs → 1 real failure (a genuine catch), 1
concurrency cancellation (working as designed).

- **`--forked` costs ~4×.** It contains the unfixed #20 SIGSEGV (anyio/TestClient teardown race).
  The escape hatch written in `ci.yml` itself — mark only the crash-prone teardown tests
  `@pytest.mark.forked` — has **never been attempted** (zero tests carry the marker). PR #60 tried
  blunt removal and segfaulted; the targeted path is untried and worth ~45–60 min/push.
- **`.test_durations` is badly stale**: 29% of test IDs covered, 25 referenced files deleted,
  recorded total 124 *seconds* vs real ~105-minute shards. Shard balance is effectively random —
  a plausible contributor to the 2026-07-06 wedged-shard incident. Regenerate with
  `--store-durations` from a real CI run, or delete it.
- **`tools/run_tests_split.sh` (the documented local runner) is broken and dangerous**: it defaults
  to a nonexistent `.venv`, and `-o addopts=` wipes the marker filter — a bare run would collect
  `e2e/` and trigger the import-time network call. It also omits `--forked`, so local and CI
  isolation semantics differ.
- **CI's Postgres service is mostly idle**: `GDX_TEST_CONTROL_DB_URL` and `GDX_TEST_PG_*` are never
  set, so the savepoint-wrapped PG control-DB path, `tests/integration/`, and the whole
  `structure.sql` template apparatus skip on every run. Either wire them to the already-running
  service or delete the apparatus.
- Frontend: `dangerouslyIgnoreUnhandledErrors: true` in `vitest.config.js` (PrimeVue teardown
  timer, primefaces/primevue#7410) globally silences a real failure channel — the frontend's
  equivalent of `--forked`.

---

## 6. Missing gates — the 2026-07-24 gap list has aged badly

Since `testing-gaps-2026-07-24.md`: backend tests grew 455→490 files, vitest 134→180, and **zero
of the six identified gaps were closed**. Still absent:

- **No ESLint / jsconfig** — the `api.delete`-vs-`del` bug class cited as the poster child was
  fixed by hand, so the class is still open. (The fix itself is solid: `useApi.js` now guards it
  and 6 spec cases cover it.)
- **No coverage measurement** anywhere (no pytest-cov, no vitest coverage).
- **No contract-parity check** frontend↔backend (`openapi-typescript` sits unused in devDeps). *[2026-08-31: `api.d.ts` and `openapi-typescript` are gone — nothing imported them; the route table is pinned in `gdx_dispatch/openapi_routes.txt`.]*
- **Two gates are written but unwired**: `lint:ux` (`scripts/ux_gate.mjs`) gates nothing in CI,
  and its own regression test `ux_gate.test.mjs` is excluded by `vitest.config.js` and invoked by
  no script — the test-for-the-gate is unreachable and the gate is unplugged. One line each.
- **`test_authz_regression.py` is a frozen allowlist** — 18 hardcoded paths from the 2026-06-24
  sweep; nothing shipped since (bank_feeds, door_listings, phone_com, vendor statements, closeout,
  deposits) is listed. The machinery to make authz a full route-table sweep instead of a list
  already exists (`tools/route_shadow_scan.py`, used by `test_route_shadow_baseline.py`) and is
  unused for this. There is also a previously flagged auth-coverage concern on payment endpoints,
  tracked separately outside this doc — the frozen allowlist is exactly the kind of gate that
  should have caught it.

### Untested hotspots from recent work

- **`frontend/src/views/BankFeedsView.vue`** — 1,640 lines, the largest Vue file in the repo,
  referenced by zero specs. The backend half of bank feeds is well covered (8 test files, 1,000+
  assertions); the risk is concentrated entirely in this one file.
- **Deposits** — the one recent feature area that genuinely skipped tests: `ba1ac46` and `c05dcc9`
  touched 9 Vue views + `routers/invoices.py` + `routers/mobile_invoicing.py` with zero tests.
  (test:src ratio 0.26 — lowest of 14 feature areas measured; median ~0.8.)
- **Commit `64ec188`** (bank-statement permission split) — edited `core/permissions.py` +
  `modules/bank_feeds/router.py` + migration 053 with no test asserting the role split.
- **`useDestructiveConfirm` fails open** when PrimeVue's ConfirmationService isn't registered —
  the default in unit tests. All 13 delete-flow specs silently take the auto-accept branch; the
  cancel/reject path is untested repo-wide, and a regression that made confirm dialogs never
  appear (issue #215) would pass CI. Imported by 38 files; no dedicated spec.
- **50% of Vue files (102 of 202) are named by no spec** — including `ProposalsView.vue` (1,064
  lines), `CommunicationsView.vue` (1,017), `MonthlyBudgetView.vue` (743). Backend runners-up:
  `routers/ui_compat.py` (879 LOC, 80 routes, 2 test references), `routers/budgets.py` (1,164 LOC,
  1 test), `routers/communications.py` (828 LOC, 0).

---

## 7. Recommended action order

Nothing below was done — this is the proposed sequence, ranked by risk retired per effort.

1. **Fix the liars**: rewrite `test_39_tax_settings.py` against the real `/api/tax/*` endpoints;
   fix the dead `or … == 503` in `test_01_gdx_scaffold.py:33`; delete the superadmin reset
   (`conftest.py:203-208`); fix or delete `test_qb_webhook_deduplication`.
2. **One-line wins**: wire `lint:ux` into the frontend CI job; un-exclude `ux_gate.test.mjs`;
   delete `ai-code-review.yml` (or, if AI review is wanted, fix the key, the prompt, and the
   `{{…}}` bug together).
3. **Regenerate `.test_durations`** from a real CI run; **fix `run_tests_split.sh`** (preserve the
   marker filter, add `--forked`, drop the `.venv` assumption).
4. **Attempt targeted `@pytest.mark.forked`** for #20 — biggest CI-time payoff available
   (~45–60 min/push); the blunt-removal experiment failed but the targeted path is untried.
5. **Add a shared production-faithful `client` fixture** built on the real `create_app()` and
   migrate fake-app tests opportunistically — the root-cause fix for §2's structural problem.
   Convert e2e's setup-failure skips to failures while in there.
6. **Decide the multi-tenant control plane's fate** (~120 tests defending endpoints no UI reaches)
   — the only item needing a product decision.
7. **Hygiene deletes** from §3, plus the doc fixes (`RD_SYSTEM.md:49`, `PG_GATE_TRIGGER.md`,
   `pytest.ini:23-25`) and the sprint-file renames from §4.
