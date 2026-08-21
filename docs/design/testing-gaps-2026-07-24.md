# Testing Gaps Assessment — 2026-07-24

**Status:** **PARTIALLY ADDRESSED** (verified 2026-08-21). Gap 1, the biggest —
the frontend<->backend contract-parity scan — shipped as
`gdx_dispatch/tools/frontend_contract_scan.py`.
**Still open:** Gap 2 (no ESLint config exists anywhere in `frontend/`), Gap 3
(no CI-gated browser smoke — `ci.yml` still runs `--ignore=tests/e2e`), Gap 4
(no upgrade-with-data migration harness), Gap 5 (no coverage measurement in CI),
Gap 6 (no response-shape ratchet), and every visual rung.

What kinds of testing the project is missing, ranked by which classes of
production bugs they would have caught. Method: inventoried every test
surface (backend pytest, frontend vitest, Playwright, schemathesis, locust,
Histoire, drift detectors), mapped each against what CI actually gates, and
cross-referenced against the bug classes that have escaped to prod.

**Headline:** the project is not short on test *volume* — 455 backend test
files, 134 vitest specs, plus e2e/schemathesis/load/a11y/visual suites that
all exist. The problems are two genuinely absent categories (frontend↔backend
contract parity, frontend static analysis) and a recurring theme of tests
that exist but never gate anything.

---

## Current state — what runs vs. what merely exists

**Gates every merge** (`.github/workflows/ci.yml`):
- Backend pytest, 7 pytest-split shards on real Postgres 16, `--forked`
- Ruff ratchet (`.ruff_baseline`) + hard F821/F823 gate
- Frontend `npm run build` + full vitest run
- Docker image build + boot smoke on fresh PG — incidentally covers
  empty-DB→head Alembic migration

**Exists but never gates** (opt-in markers, live-VPS-only, or manual):
- `gdx_dispatch/tests/e2e/` — 40 files: schemathesis fuzzing, tenant
  isolation, security, accessibility, console-clean, **visual regression**.
  All `@pytest.mark.e2e`, excluded by `pytest.ini` addopts, need a live VPS.
- `frontend/e2e/` — 13 Playwright specs, run only via manual `npm run e2e`
- `gdx_dispatch/tests/load/locustfile.py` — never run, no perf budgets
- `gdx_dispatch/tests/health/` — silent-failure drift detectors, opt-in
- `gdx_dispatch/tests/_adversarial/` — vLLM-generated, deliberately excluded
- Histoire stories — manual `story:dev` only

**Absent entirely:** contract-parity checks, ESLint/TypeScript/jsconfig,
coverage measurement (no pytest-cov, no vitest coverage), CI-gated browser
smoke, upgrade-with-data migration tests.

---

## Gap 1 — Frontend↔backend contract parity ★ biggest

Nothing verifies that every `api.get/post/patch/del`/`fetch` URL in
`frontend/src` resolves to a registered FastAPI route with the right method,
or that fields the frontend reads exist in the response. Vitest mocks the API
layer, so it structurally *cannot* catch this class; schemathesis only fuzzes
the backend against its own spec. This is the single most-escaped bug class
(dead buttons, silent 404s, wrong field names).

Ingredients already in the repo:
- `gdx_dispatch/tools/route_shadow_scan.py` already enumerates the full
  FastAPI route table (for shadow detection) — reuse the enumeration.
- The ruff-style baseline-ratchet pattern is already proven in CI.

Build: a static scan that extracts frontend call sites (regex over
`api.<verb>(...)` + raw `fetch`), normalizes path params, and matches against
the route table. Runs in seconds, no server needed. Baseline the known
misses, ratchet to zero, hard-fail on net-new.

## Gap 2 — Frontend static analysis: none exists

No ESLint config, no TypeScript, not even a jsconfig. Calling a method a
composable doesn't export (`api.delete` vs `del`), or reading a store field
that doesn't exist (`auth.token` vs `accessToken`), is invisible until a user
clicks the button.

- Cheapest: ESLint flat config + `eslint-plugin-vue`, baseline-ratcheted.
- Real fix: `openapi-typescript` is **already in devDependencies, unused** —
  generate API types from the OpenAPI spec, adopt incrementally via
  JSDoc + `checkJS`.
- Grow the source-assertion vitest idiom already present
  (`no_legacy_css_tokens.spec.js`, `no_applayout_in_views.spec.js`) for
  bannable patterns — e.g. the invalid PrimeVue-3 `'warning'` severity token.

## Gap 3 — No CI-gated browser smoke

CI stops at `curl /health`. Neither Playwright e2e nor backend e2e gate a
merge; both need a live VPS or a manual throwaway-container run (the
`/verifyplaywright` harness proves the disposable-stack approach works — it's
just not in CI). A ~5-minute compose-stack job (login → dashboard → one CRUD
chain → assert console clean) would catch wiring breaks that both pytest and
mocked vitest miss.

## Gap 4 — Migration tests only cover empty-DB→head

The CI build job boots the image on a fresh PG, which exercises head
migration on an *empty* schema. Missing:
- **Upgrade-with-data:** load release-N−1 schema + realistic rows, run
  migrations, assert the backfill did what it claimed (the migration-037
  `read_at` backfill class). This is the scariest untested path given weekly
  prod deploys.
- **Downgrades:** effectively untested (2 references in the whole suite).

Harness sketch: keep a small prod-shaped fixture dump per release tag;
a serial pytest loads it at N−1, upgrades to head, asserts row-level
outcomes for every data migration since.

## Gap 5 — No coverage measurement

Nobody can see the holes. 55 of 130 views have any spec; backend coverage is
unknown despite 455 files. Add pytest-cov + vitest coverage in report-only
mode first, then ratchet the number exactly like the ruff baseline. Do not
set an arbitrary threshold up front — measure, baseline, ratchet.

## Gap 6 — Response-shape drift

727 of ~1,006 route decorators declare `response_model`; the other ~280
return raw dicts that can drift silently against what the frontend reads
(the `total` vs `total_amount` class). Two moves:
1. Ratchet `response_model` adoption (count-based, same pattern).
2. Run schemathesis with response validation against the compose stack in CI
   (spot-check tier), instead of live-VPS-only.

---

## Visual correctness — how to actually test it

Asked separately: *can you test that the app looks right?* Yes — four rungs,
in increasing cost/flakiness. The repo already owns most of the machinery.

### Rung 0 — Static: catch *wrong code* before anything renders
The distinct question "can we detect code that is wrong for visual
correctness?" — yes, four mechanisms, all deterministic, all vitest-speed:

1. **Enumerable-value source assertions.** Where a visual API accepts a
   closed set of values, scan source and assert membership. The canonical
   case: PrimeVue 4 Tag severities are exactly
   `secondary/success/info/warn/danger/contrast` — a spec that scans `.vue`
   files for `severity` literals and status-map values and fails on anything
   outside the set would have caught every colorless-tag bug shipped to
   date. The repo already has this test idiom
   (`no_legacy_css_tokens.spec.js`, `no_applayout_in_views.spec.js`) —
   it's a one-file addition.
2. **Token-resolution checks.** Collect every `var(--p-*, --gdx-*)`
   reference across `.vue`/`.css` source; assert each is defined in the
   theme preset (in BOTH light and dark definitions). An undefined custom
   property doesn't error — it silently falls back and renders wrong.
   Stylelint's `value-no-unknown-custom-properties` plugin does this
   off-the-shelf, or a ~50-line vitest spec does it in-repo.
3. **Single-source-of-truth enforcement.** Ban hand-rolled severity/status
   maps in views — a spec that greps `views/**` for inline
   `status → severity` object literals and requires importing
   `utils/statusSeverity.js` instead. Divergent-map bugs (same status,
   different color per screen) become structurally impossible.
4. **Mount-and-assert-the-class.** For each entry in the status maps, mount
   the real `<Tag :severity>` in vitest and assert the rendered element
   carries a recognized severity class (`p-tag-warn` etc.). This tests the
   *mapping code* — an invalid token produces no severity class, which the
   assertion catches without ever comparing a pixel.

Supporting cast: `eslint-plugin-vue` template rules catch misspelled
component names and bad `v-bind`s (which render as nothing, silently);
banning literal hex/rgb colors outside the token files keeps components
dark-mode-safe by construction. Honest limit: TypeScript/vue-tsc helps with
prop *names* but not much with open string-union prop *values* — the
enumerable-set assertions above are the reliable tool for those.

### Rung 1 — Layout & style invariants (cheap, deterministic, gate-safe)
Assertions in a real headless browser that don't compare images:
- No horizontal overflow (`document.documentElement.scrollWidth <=
  clientWidth`) per routed page, both viewports.
- Touch targets ≥ 44px (already exists:
  `frontend/e2e/mobile-touch-targets.spec.js` — promote to CI).
- **Computed-style contrast:** `getComputedStyle` on real painted DOM →
  WCAG ratio ≥ 4.5:1 for key text/background pairs, asserted in BOTH light
  and dark themes. This is what the dark-mode-banner class needs. The
  existing `primevue-cta-contrast.spec.js` is jsdom-only and explicitly
  cannot resolve painted colors — vitest 4 browser mode (Playwright
  provider, already have both deps) closes that gap at the unit layer.

### Rung 2 — axe-core sweeps (automated judgment, low flake)
`@axe-core/playwright` on every routed page catches color-contrast
violations, invisible-focus, unlabeled controls. `tests/e2e/
test_accessibility.py` already exists — the work is running it against the
compose stack in CI instead of live-VPS-manually, in both themes.

### Rung 3 — Screenshot diffing (the real "does it look right")
**Already fully written:** `gdx_dispatch/tests/e2e/test_visual_regression.py`
screenshots 9 major pages, auto-creates baselines, 5% tolerance,
`--update-snapshots` refresh flow. It just never runs. To make it a gate
instead of a flake factory:
- Render in the **same Docker image** always (fonts/antialiasing are the #1
  false-positive source; container = deterministic rendering).
- Seeded fixture data, not live data; mask genuinely dynamic regions
  (dates, KPI numbers) via Playwright `mask:` option.
- Disable animations (`animations: 'disabled'` in screenshot options).
- Capture light AND dark theme per page — dark mode is where regressions
  have actually shipped.
- Commit baselines; PR flow = intentional UI change → update snapshots in
  the same PR, so the diff is reviewable.
- Alternative surface: screenshot Histoire stories per component instead of
  full pages — smaller diffs, far less flaky, better failure localization.

### Rung 4 — AI visual judgment (advisory, not a gate)
Feed the Rung-3 screenshots to a vision model with "anything look broken,
overlapping, unreadable, or misaligned?" Catches the unanticipated (overlap,
clipped text, white-on-white) that pixel-diff only catches if a baseline
existed. Non-deterministic → report-only comment, never a required check.

**Recommended visual stack:** Rung 0 immediately (no infrastructure needed,
runs in the existing vitest job); Rung 1 + 2 as CI gates once the compose
stack exists; Rung 3 on the 9-page suite in both themes, gating, with masked
dynamic regions; Rung 4 opportunistic.

---

## Lower tier (real but not urgent)

- **Load/perf:** locustfile exists, never enforced; wire endpoint budgets to
  it on-demand rather than per-merge.
- **Plugins:** Midland/CHI plugin code is essentially untested and partially
  untracked — testing is moot until it's in git.
- **Offline sync:** one unit spec (`test_offline_sync_phase31.test.js`); no
  e2e for replay/conflict/dedupe semantics.

## Suggested order

1. **Gap 1** contract-parity static scan (≈1 day, kills the most-escaped class)
2. **Gap 2** ESLint + ratchet (≈1 day); openapi-typescript adoption follows
3. **Gap 3** compose-stack smoke job in CI (unlocks Rungs 1–3 above)
4. **Visual Rungs 1–2** as gates; **Rung 3** once the smoke job is stable
5. **Gap 4** upgrade-with-data migration harness
6. **Gap 5 + 6** coverage + response_model ratchets (mechanical, low effort)
