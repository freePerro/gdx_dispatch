# Phase D — the SaaS residue the single-tenant refactor left behind

**Status:** `PARTIALLY BUILT` — **S1, S3 and S6 built.** Connect retired
entirely (owner decision 2026-09-01); the `x-tenant-tier` selector removed after
measurement proved it was **dead code, not the exploit this document first
claimed**. **S2, S4, S5, S7, S8, S9, S10 are NOT built** — S2 (public `/signup`)
is the one still live on prod.

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

## Inventory

| # | Artifact | Evidence | Reachable now? |
|---|---|---|---|
| ~~**S1**~~ | ~~`x-tenant-tier` header selects the caller's own rate limit~~ — **CORRECTED: it never worked.** Dead code; slowapi calls a `default_limits` provider with no arguments | **BUILT** — replaced with a static `DEFAULT_RATE_LIMIT` | Was **never** reachable |
| **S2** | Public `/signup` page whose submit target does not exist | prod: `GET /signup` → **200 unauthenticated**; `POST /signup` (the form's own target) → **405** | **Yes — publicly indexable** |
| ~~**S3**~~ | ~~Connect payment-intent takes the destination account from the request body~~ | **BUILT — deleted with the whole Connect surface.** Closes #421 | Gone |
| **S4** | 8 vendor-admin routes with zero UI callers | `/api/admin/health-scores/*` (3), `/api/admin/metrics/*` (2), `/api/admin/tenants/{tenant_id}/modules` (3) | Mounted, admin/owner-gated |
| **S5** | Churn scoring pointed at the owner | `core/health_score.py:3` — *"per-tenant engagement scores and triggers retention playbooks"* | Mounted; **not** beat-scheduled |
| ~~**S6**~~ | ~~Two parallel Stripe Connect implementations~~ | **BUILT — all 9 routes deleted**, both files removed, module key dropped | Gone |
| **S7** | Dead control-plane grants table | prod `tenant_module_grants` = 0 rows; `company_module_grants` = 111 rows (with duplicates) | Fallback path only |
| **S8** | SaaS billing state on a product that is not sold | prod `tenants.subscription_status` = `'trialing'` on both rows; `core/tenant.py:52` hardcodes `"active"` in the ambient dict | Inert |
| **S9** | Seed-tenant duplicates in prod data | `Example Garage Doors` / `00000000-…-0001` sits beside the real company row in **both** `tenants` and `companies` — it is the default in `single_tenant()` | Data, not code |
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

### S2 — a sign-up page for a product that is not sold

`router/index.js:163` routes `/signup` with `meta: { public: true }`.
`SignupView.vue:18` posts to `/signup`, which is not in the route table — prod
returns 405. A stranger gets a working-looking form whose button fails.
**Fix:** remove the route and the view. `OnboardingView` is **not** residue and
stays — `/api/onboarding/*` is a real first-run surface with 9 live endpoints.

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
| 2 | **S2** — remove `/signup` route + view; strike the two `mobile-all-platforms-plan.md` rows | prod probe after deploy: `GET /signup` → 404 |
| ~~3~~ | ~~**S3 + S6**~~ — **DONE.** Connect deleted; #421 closed | ✅ counterfactual guard verified; route table diff is exactly the 9 routes; no data change, so no migration |
| 4 | **S4 + S5 + S7** — remove the dead vendor-admin surfaces | route-table diff; absence assertions like `test_automation_sequences_retired.py` |
| 5 | **S8 + S9** — data cleanup, soft-delete only (invariant #2) | owner sign-off; these are prod rows |
| 6 | **S10** — the original Phase D: migrate ~20 `get_engine` call sites, drop the shim | mechanical; last because it is the largest and least urgent |

**Counterfactual gates required.** A green suite proves nothing here unless it
can fail for the defect: PR 1's test must send `x-tenant-tier: professional` and
assert the limit did *not* change; PR 4's must assert route **absence**, so a
re-added router is caught whatever file it lives in.

## Open questions for the owner

1. ~~**Connect: delete or keep?**~~ — **ANSWERED 2026-09-01: delete.** Done;
   see S3/S6 above. Issue #421 closed by that PR.
2. **S9 seed rows:** soft-delete the `Example Garage Doors` tenant/company rows,
   or leave them? They are referenced by nothing found so far, but they are
   prod rows in two tables and deserve an explicit decision.
3. **S5 health scores:** delete outright, or keep the endpoint unmounted for a
   future hosted offering? Deleting is recommended — it is recoverable from git,
   and a dead mounted route is worse than no route.
