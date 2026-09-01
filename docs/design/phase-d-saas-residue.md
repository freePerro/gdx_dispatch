# Phase D — the SaaS residue the single-tenant refactor left behind

**Status:** `PARTIALLY BUILT` — **S1, S2, S3 and S6 built.** Connect retired
entirely (owner decision 2026-09-01); the `x-tenant-tier` selector removed after
measurement proved it was **dead code, not the exploit this document first
claimed**; the public signup page removed together with the multi-tenant
workspace picker on the login page that this document's sweep had missed.
**S4, S5, S7, S8, S9, S10 are NOT built.** ⚠ Nothing here is deployed or
walked on prod yet.

## What already exists (do not rebuild)

The multi-tenant → single-tenant refactor is mostly **done**. Before touching
anything here, know what is already finished, because none of it needs redoing:

| Already done | Where it shows |
|---|---|
| **Phase A** — removed the multi-tenant resolver: subdomain / `x-tenant-id` lookup, trial-expiry checks, unknown-tenant 404 | `core/tenant.py::TenantMiddleware` docstring |
| **Phase C** — removed the per-request `_current_tenant_id` ContextVar and the PostgreSQL GUC machinery it fed | same docstring; `_lookup_tenant` is now a test-compat stub |
| **The Redis rate limiter was already de-SaaS'd** | `core/rate_limiter.py::_key_and_limit` — keyed per *caller*, and its comment states the intent explicitly: *"no per-request tier lookup"* |
| **Module grants moved to the data plane** | `company_module_grants` is the live table (111 rows on prod); the control-plane `tenant_module_grants` is the dead fallback (0 rows) |
| **The module toggle UI already points at the right API** | `SettingsView.vue` calls `/api/settings/modules/{key}/enable` \| `…/disable`, **not** the vendor-era `/api/admin/tenants/{id}/modules` |
| **Per-tenant SSO config was already removed by decision** | `unimplemented-endpoints-decision-list.md` item 6 — *"single-tenant, 9 users, no per-tenant SSO config wanted"* |
| **The ambient tenant is env-sourced, not looked up** | `core/tenant.py::single_tenant()` reads `GDX_TENANT_*`; no customer identifier is hard-coded in the OSS tree |

**Phase D is the named, unfinished remainder.** It exists today only as a code
comment — `core/tenant.py:17`:

> *"Kept as a compatibility shim for the ~20 import sites that still call
> `engine_registry.get_engine(tenant_id, db_url)`. **Phase D will remove those
> call sites and this shim.**"*

`grep -rln "Phase D" docs/` returns nothing about this refactor. A second
comment, `control/models.py:42`, cites **`SCOPE.md` — a file that is not in the
repo**. This document is the missing record; it does not invent scope, it writes
down what the code already says is outstanding and adds what a sweep found.

## The shape

> **Code whose subject is "a tenant as administered by a platform vendor"** —
> a surface that only makes sense if this application hosts multiple customer
> companies for a vendor who bills them, scores them, or takes a cut of their
> revenue.

This is deliberately narrower than "mentions a tenant". Ambient
single-tenant plumbing (`company_id()`, `request.state.tenant`) is **not** in
scope: it is the shim Phase D retires by call-site migration, tracked as S10,
not by deletion.

## The sweep

Declared before the fix, per the working agreement.

- **Pattern searched:** SaaS-billing and vendor-administration vocabulary —
  `subscription_status`, `platform_fee`, `trialing`, `churn`, `retention
  playbook`, `plan_tier`, `x-tenant-tier` — plus every route in the pinned
  route table taking `{tenant_id}` as a **path parameter**.
- **Surface searched:** all of `gdx_dispatch/**/*.py` excluding `tests/`; all
  1,366 lines of `gdx_dispatch/openapi_routes.txt`; `frontend/src/**` for
  callers; `docs/design/**` for rival plans.
- **Instances found:** 9 code/data artifacts (S1–S9) plus the shim (S10).
  The route half of the sweep is **exhaustive and small**: exactly **five**
  routes in the entire table take `{tenant_id}` as a path parameter, and all
  five are in this inventory. This is a bounded cleanup, not open archaeology.
- **⚠ The frontend half was NOT exhaustive, and this document was wrong to imply
  it.** The declared surface searched `frontend/src` only for *callers of
  backend routes*. That finds a route (S2) but never a Vue component or store
  whose purpose is multi-tenant. Two items were found this way only after an
  adversarial audit: `PlatformRecovery` (folded into S2) and **S11**. Treat the
  frontend inventory as incomplete until a proper sweep runs; S4 and S5 in
  particular have not had one.

## Inventory

| # | Artifact | Evidence | Reachable now? |
|---|---|---|---|
| ~~**S1**~~ | ~~`x-tenant-tier` header selects the caller's own rate limit~~ — **CORRECTED: it never worked.** Dead code; slowapi calls a `default_limits` provider with no arguments | **BUILT** — replaced with a static `DEFAULT_RATE_LIMIT` | Was **never** reachable |
| ~~**S2**~~ | ~~Public `/signup` page whose submit target does not exist~~ — **wider than filed: it was linked from a workspace picker on the login page** | **BUILT** — `/signup`, `SignupView`, `PlatformRecovery` and the "Wrong workspace?" link all removed | Gone |
| ~~**S3**~~ | ~~Connect payment-intent takes the destination account from the request body~~ | **BUILT — deleted with the whole Connect surface.** Closes #421 | Gone |
| **S4** | 8 vendor-admin routes with zero UI callers | `/api/admin/health-scores/*` (3), `/api/admin/metrics/*` (2), `/api/admin/tenants/{tenant_id}/modules` (3) | Mounted, admin/owner-gated |
| **S5** | Churn scoring pointed at the owner | `core/health_score.py:3` — *"per-tenant engagement scores and triggers retention playbooks"* | Mounted; **not** beat-scheduled |
| ~~**S6**~~ | ~~Two parallel Stripe Connect implementations~~ | **BUILT — all 9 routes deleted**, both files removed, module key dropped | Gone |
| **S7** | Dead control-plane grants table | prod `tenant_module_grants` = 0 rows; `company_module_grants` = 111 rows (with duplicates) | Fallback path only |
| **S8** | SaaS billing state on a product that is not sold | prod `tenants.subscription_status` = `'trialing'` on both rows; `core/tenant.py:52` hardcodes `"active"` in the ambient dict | Inert |
| **S9** | Seed-tenant duplicates in prod data | `Example Garage Doors` / `00000000-…-0001` sits beside the real company row in **both** `tenants` and `companies` — it is the default in `single_tenant()` | Data, not code |
| **S11** | `x-tenant-id` / `gdx_tenant_slug` still threaded through the frontend | `stores/auth.js::_tenantHeader()` derives the header from `window.location.hostname` — prod is a 3-part host, so **every login today sends `x-tenant-id: gdx`** to the resolver Phase A deleted. ~20 frontend files (`useApi`, `theme`, `analytics`, `useOfflineSync`, `useAuthedFile`, `useTour`, Documents/BankFeeds/VendorStatements/Exports, `errorCapture`) plus 4 backend readers that use it as a logging fallback (`performance.py`, `prometheus.py`, `ai_router.py`, `ai_usage_logger.py`) | Sent and ignored — **issue #581** |
| **S10** | `_SingleEngineRegistry` shim + ~20 `get_engine(tenant_id, db_url)` call sites | `core/tenant.py:12-33` — the original Phase D scope | Inert by design |

### S1 — CORRECTED: dead code, not an exploit. Now removed.

**This document originally called S1 "the one that is actually exploitable."
That was wrong, and the error was mine — read from code without running it.**

Measured against a running container: the first 429 arrives at request **#121
whether or not `x-tenant-tier: professional` is sent**. The header does nothing
and never did.

The mechanism, from slowapi's own source (`slowapi/wrappers.py`):

```python
if callable(self.__limit_provider):
    if "key" in inspect.signature(self.__limit_provider).parameters.keys():
        limit_raw = self.__limit_provider(self.key_function(self.request))
    else:
        limit_raw = self.__limit_provider()      # ← no arguments
```

A `default_limits` provider is called with **no arguments** unless its signature
has a parameter named exactly `key` — and even then it receives the *key* (the
client IP), never the request. `_tier_limit(*args, **kwargs)` had no `key`
parameter, so `request` was always `None` and it always returned `120/minute`.

**A second dead branch in the same function:** the E2E bypass
(`GDX_E2E_BYPASS=1` + `x-e2e-test: true` → `100000/minute`) could not fire
either. Verified — e2e traffic still 429s at 120. The *working* bypass in
`core/rate_limiter.py` is unaffected. Repairing this one needs a different
mechanism and is filed separately, not bundled here.

**Built:** `_tier_limit` replaced by the constant `DEFAULT_RATE_LIMIT =
"120/minute"` — which is exactly what it already returned, so behaviour is
unchanged (probed before and after: 429 at #121 in all four
header/no-header combinations). `tests/test_rate_limit_default_is_static.py`
is the guard, counterfactually verified: reintroducing a callable turns it red.
It also pins slowapi's calling convention, so if a future version *does* pass
the request, the test fails rather than letting this reasoning rot.

**The real lesson is the severity, not the code.** A dead branch that looks like
a client-controlled rate limit is a trap: the tempting "fix" is to repair the
signature, which would ship the very bypass this document wrongly claimed
existed.

### S2 — BUILT. And it was bigger than this document said.

As filed: `/signup` was a **public** route (`meta.public`) rendering a
"Start your free trial / 14 days free" form that POSTed to `/signup` and
expected a `checkout_url`. No such backend route exists — prod returned **405**.

**What the original sweep missed:** `/signup` was not orphaned. It was linked
from `PlatformRecovery.vue` — a **multi-tenant workspace picker** redirecting to
`https://<slug>.example.com/login`, i.e. the subdomain resolver **Phase A
deleted from the backend**. Its documented trigger (a login reply of "Unknown
tenant") can no longer occur — that string appears nowhere in the backend — but
it had a *second* entry point: a **"Wrong workspace?" button rendered
unconditionally on the login form**. Confirmed live in the shipped production
bundle. So the front door offered every user a picker for workspaces that do not
exist, and a "Create one" link to a form that 405s.

**Why the sweep missed it.** This document's declared surface was
"`gdx_dispatch/**/*.py` excluding tests; the route table; `frontend/src` **for
callers**". The frontend was searched only for *callers of backend routes* —
which finds `/signup` (it is a route) but not a Vue component whose whole
purpose is multi-tenant. **SaaS-era UI was never in the searched surface.** That
is a gap in the inventory, not a judgement call: S4 and S5 should be re-checked
against a proper frontend sweep before either is called complete.

**Built:** the `/signup` route, `SignupView.vue`, `PlatformRecovery.vue`, the
`showRecovery` machinery, the "Wrong workspace?" link, their two specs and the
orphaned CSS. Removing `/signup` alone was **not** an option — it would have
turned the picker's "Create one" into a dead router-link, strictly worse than
before.

**No dead end:** the router's `/:pathMatch(.*)*` catch-all means a stranger
hitting `/signup` now gets the real Not Found page, not a blank.

`OnboardingView` is **not** residue and stays — `/api/onboarding/*` is a real
first-run surface with 9 live endpoints.

Guard: `src/router/__tests__/saasSignupRetired.spec.js`, counterfactually
verified — restoring both the route and the view fails 3 of its 5 assertions.

**The prod probe must be the BUNDLE, not the URL.** An earlier draft of this
document proposed `GET /signup → 404` as the post-deploy gate. That gate can
never fail: `app.py` serves the SPA from a catch-all
`@app.get("/{full_path:path}")`, so `/signup` returned **200 before this change
and returns 200 after it** — the same 200 this document originally cited as
*evidence of the defect*. `POST /signup` stays 405 either way (the catch-all is
GET-only). Neither probe can tell fixed from unfixed. Use one that can:

```
curl -s https://<host>/login | grep -oE '/assets/index-[A-Za-z0-9._-]+\.js' \
  | head -1 | xargs -I{} curl -s https://<host>{} \
  | grep -c 'Wrong workspace\|PlatformRecovery'
# must be 0; measured 1 and 1 on prod at 1.113.2, before this shipped
```

**⚠ That probe is valid for the LOGIN page only, and the first version of it was
half-theatre.** It originally also grepped for `Start your free trial`. Measured
on prod *before* the fix shipped: **0 occurrences** — because Vite code-splits
lazy routes, and `SignupView` / `PluginsAdminView` live in chunks that
`index.html` never references, so the eager bundles cannot contain their
strings. That half of the probe would have reported success whether or not the
change deployed.

It works for `Wrong workspace` / `PlatformRecovery` precisely because
`LoginView` is **eagerly imported** and compiles into `index-*.js`.

| marker | chunk | can the curl probe fail? |
|---|---|---|
| `Wrong workspace` | eager `index-*.js` | ✅ yes |
| `PlatformRecovery` | eager `index-*.js` | ✅ yes |
| `Start your free trial` | lazy | ❌ reads 0 either way |
| `No plugin packages installed yet` | lazy | ❌ reads 0 either way |

**For a lazily-loaded view there is no HTTP substitute for opening the page** —
the asset directory is not listable, so you cannot enumerate the chunks from
outside. Load `/signup` and the Plugins admin page in a browser and read them.
That is why the walk is load-bearing here, not ceremonial.

### S3 / S6 — Stripe Connect, and issue #421

Issue #421 asks whether the endpoint is wanted at all. Two facts the issue did not have:

1. **Its stated mitigation is false.** The issue reasons that no
   `stripe_connect_account_id` is configured, so the path is inert. But
   `_get_tenant_account_id` returns the client's `body.account_id` *before* the
   config lookup, so the missing config is never consulted. Prod's key is
   `sk_live_`. The containment is Stripe rejecting an unconnected
   `transfer_data.destination` — nothing in our code.
2. **There are two Connect surfaces**, on two different tables, neither with a
   UI caller. Fixing one endpoint leaves the shape in the other eight routes.

**DECIDED 2026-09-01 — delete.** The owner's call. Connect is a platform
construct and this deployment is single-tenant, so there was never a second
party for it to serve; and taking a cut of another company's card payments makes
you a payment facilitator, with KYC and liability attached. If hosted instances
ever happen, that billing model should be designed deliberately then — most
likely a hosting subscription, not a per-transaction cut — rather than inherited
from 2026-era scaffolding that already carried this defect.

**Built:** all 9 routes removed (5 on `routers/stripe_connect.py`, 4 on
`routers/payments.py`), both `stripe_connect` modules deleted, the
`stripe_connect` module key dropped, tests retired, and the pinned route table
regenerated — a 9-line deletion and nothing else.
`tests/test_stripe_connect_retired.py` is the guard, and it was
counterfactually verified: restoring the router turns all five absence
assertions red while the three live-payment-path assertions stay green.

**Deliberately NOT removed** (named so the next reader doesn't think it was
missed): the `connected_account` parameter threaded through `core/payments.py`,
the two `stripe_connect_account_id` columns, and
`tasks/stale_intent_sweep.py::_connected_account_for`. The threading is
*provably* dead — `_stripe_extra()` reads a key that `single_tenant()` never
puts in the dict — but it lives in the live money path that six real card
payments went through, so unthreading it is its own change with its own risk.
No migration and no data change ship with this deletion.

## Rival documents — cited, and one corrected

Per the working agreement, plans naming these files were checked before writing.

- **`gl-phase2-reconciliation.md:124` is WRONG and this document supersedes that
  claim.** It states *"GDX does not currently process payments through Stripe…
  the Stripe code surface exists but is not live"*, and on that basis
  **downgrades Phase 1 §12's orphaned-charges defect from live money leakage to
  a dormant defect**. Measured on prod: **6 `card` payments totalling
  $7,650.02, 2026-07-22 → 2026-08-26, all six carrying `pi_` PaymentIntent
  references.** Stripe is live money here. The §12 defect should be re-rated to
  live, and that document's line 124 corrected — never deleted.
- `soc2-readiness-gap-analysis.md` (**untracked local draft — not in the repo
  as of this writing**) inventories rate limiting as *"slowapi global +
  `core/rate_limiter.py` tenant middleware"*. Accurate, but it does not note that
  the slowapi half takes its limit from a client header. S1 closes that gap; if
  that draft is ever committed it should reference this document.
- `comment-accuracy-audit-2026-08-12.md:119` audited `health_score.py`'s signal
  windows for comment accuracy. It judged the comments, not whether the feature
  belongs. No conflict — S5 is a scope question that audit never asked.
- `mobile-all-platforms-plan.md:89,107` only names `SignupView` in font-size and
  `min-height` sweeps. If S2 deletes the view, those two rows become stale and
  must be struck in that document in the same PR.

## Sequencing

Separate focused PRs, per the packaging default. Each PR updates this document's
status line in the **same** PR.

| PR | Scope | Gate |
|---|---|---|
| ~~1~~ | ~~**S1**~~ — **DONE.** Was dead code, not an exploit | ✅ counterfactual guard verified; behaviour probed identical before/after |
| ~~2~~ | ~~**S2**~~ — **DONE.** Also removed the workspace picker it was linked from | ✅ counterfactual guard; frontend 2300 passed. ⚠ owed after deploy: the **bundle probe** below (login page — valid) **and a browser load of `/signup`** (lazy chunk — the curl probe cannot see it) |
| ~~3~~ | ~~**S3 + S6**~~ — **DONE.** Connect deleted; #421 closed | ✅ counterfactual guard; route-table diff is exactly the 9 routes; no data change. **Post-deploy gate (measured against a container running this code, not guessed):** `GET /api/stripe/connect/status` **401 → 404** and `POST /api/stripe/connect/payment-intent` **401 → 405**. Both answered 401 on prod at 1.113.2 (mounted, auth-gated). The POST is **405, not 404** — the SPA catch-all matches the path for GET only, so a POST to a deleted route is a method mismatch. Falsifiable, and needs no credentials. |
| 4 | **S4 + S5 + S7** — remove the dead vendor-admin surfaces | route-table diff; absence assertions like `test_automation_sequences_retired.py` |
| 5 | **S8 + S9** — data cleanup, soft-delete only (invariant #2) | owner sign-off; these are prod rows |
| 6 | **S10** — the original Phase D: migrate ~20 `get_engine` call sites, drop the shim | mechanical; last because it is the largest and least urgent |

**Counterfactual gates required.** A green suite proves nothing here unless it
can fail for the defect. What each PR actually shipped, stated accurately —
an earlier draft of this line described a PR 1 gate that was never built:

- **PR 1 (S1).** The doc previously said its test "must send
  `x-tenant-tier: professional` and assert the limit did not change." **No such
  test exists.** `tests/test_rate_limit_default_is_static.py` asserts the
  limiter's *configuration* — that no `default_limits` provider is a callable —
  because slowapi cannot feed a provider the request at all. The behavioural
  proof (429 at request #121 with and without the header, before and after) was
  done by hand against a container and is **not** automated. A change that
  disabled the limiter entirely would pass the shipped guard.
- **PR 4 (S4/S5/S7).** Route **absence** on the live table, in the shape of
  `test_stripe_connect_retired.py`. ⚠ One trap: `app.openapi()` collapses
  duplicate `(method, path)` registrations into a single line, so removing one
  half of a shadowed pair changes **nothing** in `openapi_routes.txt`. Absence
  must be asserted per *handler* (file/symbol) as well as per path, or the gate
  silently passes. This is how an unreachable duplicate of
  `/api/jobs/{job_id}/costing` survived in `labor.py` until 2026-09-01.
- **PR 5 (S8/S9).** Data. The gate is a row count before and after, plus
  soft-delete only — never a response body.

## Open questions for the owner

1. ~~**Connect: delete or keep?**~~ — **ANSWERED 2026-09-01: delete.** Done;
   see S3/S6 above. Issue #421 closed by that PR.
2. **S9 seed rows:** soft-delete the `Example Garage Doors` tenant/company rows,
   or leave them? They are referenced by nothing found so far, but they are
   prod rows in two tables and deserve an explicit decision.
3. **S5 health scores:** delete outright, or keep the endpoint unmounted for a
   future hosted offering? Deleting is recommended — it is recoverable from git,
   and a dead mounted route is worse than no route.
