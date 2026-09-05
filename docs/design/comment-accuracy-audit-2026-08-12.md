# Comment accuracy audit — 2026-08-12

**Status:** **SWEEP COMPLETE, NEXT PASS OPEN** (verified 2026-08-21). The
audit's own fixes landed: `gdx_dispatch/tools/comment_drift_scan.py` and
`tests/test_comment_drift_scan.py` are on main, and
`core/job_display_state.py:247` now logs before returning Unknown, honouring
the contract its docstring promised.
**Open:** the 26 residual hits described below are still untriaged, and the
scanner is still **not** wired into CI — nothing in `.github/` runs it, so the
count is free to climb back toward 171.

Goal: verify that code comments say what the code actually does, and fix
whichever side is wrong.

## Scale

| Surface | Count |
|---|---|
| Python files (tracked) | 1,257 |
| Python LOC | 319,138 |
| `#` comments | 22,126 |
| Docstrings | 5,601 |
| **Total comment units** | **27,727** |
| Vue / TS / JS files | 501 |

Reading 27,727 comments by eye is not a verification method — it produces a
feeling of thoroughness, not evidence. So the audit mechanized the part that
*is* checkable: comments making **falsifiable claims** (a named symbol, module,
file, route, parameter, or status code). Those were machine-flagged, then every
hit was verified by reading the code, and the wrong side was fixed.

Prose that can't be checked mechanically ("this is subtle because…") was left
alone unless it sat next to a claim that failed.

## What was fixed

### Code was wrong (1)

**`core/job_display_state.py`** — the docstring promised it "never silently
returns Unknown" and the section header said "Never silent", but the final
branch returned `Unknown` with no log. The neighbouring branch logged; the
empty-`lifecycle_stage` branch did not. Since `lifecycle_stage` is `NOT NULL`
with a server default, an empty value means legacy or corrupt data — exactly
the surprise worth logging. **Added the missing log** so the code honours its
stated contract. Tests: 20/20 pass.

### Comments were wrong (30 files)

Grouped by what the comment claimed:

**Pointers to things that no longer exist**
- `core/unified_principal.py` — listed 5 principal variants; **3 were gone**
  (`pat_validation`, `scim_auth`, `oauth2_grants`). Also claimed "no router or
  dependency is wired to produce or consume it yet" — false: the 0.9-d
  dispatcher shipped and `routers/ai` + the MCP adapter/bridge consume it.
  Also cited `check_capability:191`; the short-circuit is at 219 (line number
  removed rather than re-pinned).
- `app.py` — documented a 7-middleware request flow; **3 of the 7 don't exist**
  (`TenantRoleMiddleware`, `ConsumerAuditMiddleware`,
  `CrossTenantAccessMiddleware`). Corrected to the real registration order.
- `app.py` `_check_encryption_at_rest` — named `core.database._FERNET` (doesn't
  exist) and `tenants.db_url_enc` (column dropped in migrations 081-083;
  `_decrypt_db_url` is now an identity no-op). Also cited a plan doc that isn't
  in the repo.
- `core/mcp_invoke.py` / `core/mcp_registry.py` — pointed at
  `routers/mcp_execute.py`, `routers/mcp_sse.py`, `routers/mcp_registry.py`.
  **None were ever built**; the real callers are `routers/ai` and the MCP
  adapter/bridge. Also referenced a `RouterPrincipal` type that exists nowhere.
- `core/spiffe/__init__.py` — TODO said "none of these components are wired".
  The middleware **is** wired (opt-in via `SPIFFE_ENABLE`). The claimed
  super-admin router was never built.
- `core/service_accounts.py` — "Minted by CLI
  (`tools/service_account_mint.py`)". **No such CLI, and no web UI** — see
  Finding 2 below.
- `core/modules.py` + `routers/settings.py` — both said module bootstrap happens
  via `tools/bootstrap_modules_for_tenant.py`. No such script; bootstrap
  actually happens in the `if not existing:` seed block 20 lines above.
- 7 routers — "Pattern mirrors `routers/proposals.py`", retired by migration 061
  and moved to `modules/proposals/`. Repointed at live exemplars.
- `modules/billing_terms` (`resolve_terms` → `resolve_effective_terms`),
  `core/tenant_ctx.py` (`_per_tenant_settings` → `_tenant_settings`),
  `core/spiffe/spire_trust_bundle.py` (`MAX_STALE_SECONDS` →
  `max_stale_seconds`), `routers/catalog.py` (`CatalogGroup` → `CustomCatalog`),
  `models/platform_extensions.py`, `models/tenant_models.py`, `routers/tours.py`, <!-- file deleted 2026-09-03 with the SaaS-residue purge; link-ok -->
  `routers/custom_fields.py`.

**Claims that were simply untrue**
- `routers/estimates.py` convert-to-job — comment said the estimate may be
  `'sent'` "if force=true". **There is no force parameter**; the check is
  `status in ("accepted",)` only. The docstring also called the endpoint
  "Idempotent if already linked" — it raises **409**. Both corrected. (The 409
  behaviour was left as-is: changing HTTP semantics is an API change, not a
  comment fix. See Finding 3.)
- `services/pricing_engine.py` — documented the volume-discount gate as
  `customer.class_volume_discount_enabled` and the basis as
  `cached_rolling_volume_paid_12mo`. Real: `settings.class_volume_enabled[class]`
  and `customer.cached_rolling_volume`. The doc sent readers to the wrong object.
- `routers/users.py` — documented a PAT lockout-bypass gap pointing at
  `core/pat_validation.py`. The **gap is real** (the `service_account`
  `actor_kind` branch in `_db_verify_user` returns `{}` before loading the user
  row) but the named module is gone and `/api/pats` no longer exists, so no new
  PAT can be minted. Rewrote to describe the actual, narrowed gap.
- Four modules described GDX as a "multi-tenant SaaS platform"
  (`api/public_router.py`, `core/onboarding.py`, `routers/users.py`,
  `core/stripe_payments.py`). The release is **single-tenant and self-hosted**
  (migration 001 baseline). `public_router.py` contradicted itself 85 lines later
  under a "SINGLE-TENANT INVARIANT" heading.

### Verified correct (not changed)

Worth recording, because these are load-bearing and now confirmed:
- `routers/branding_public.py` — "this public copy wins by include order".
  **True**: same `/api/settings` prefix, included at `app.py:1618` vs
  `settings.py` at 1619, so techs get the ungated Google-Maps key.
- `core/mcp_invoke.py` — the "do NOT rearrange" gate order
  (resolve → validate → capability → approval → execute → audit) matches the
  implementation, and the documented 400/404/403/202/500 mapping matches
  `mcp_error_schema.py`.
- `core/health_score.py` — all five signal windows and point caps <!-- file deleted 2026-09-03 with the SaaS-residue purge; link-ok -->
  (30+20+20+15+15 = 100) match the code.
- `core/stripe_payments.py` `Raises:` blocks — these document *propagated* SDK
  exceptions, which is correct even though nothing `raise`s locally.

## Non-comment bugs found along the way — all three RESOLVED

Code defects surfaced by the audit. Decisions taken with the maintainer 2026-08-12.

### 1. Settings integration cards were permanently broken — FIXED

Worse than first reported. Beyond the dead buttons, `GET /api/settings/integrations`
never returned `stripe` or `sms` objects at all, so `loadIntegrations()`'s
`if (result?.stripe)` guard never fired and **both cards displayed
"Not Connected" forever** — while Stripe was demonstrably charging real cards.
The Connect buttons then hit `/api/stripe-connect/onboard` (no such route; the
real Connect router was at `/api/stripe/connect` — **the entire Connect surface
was deleted 2026-09-01**, see `phase-d-saas-residue.md` S3/S6, so neither path
exists now) and `/api/settings/sms/configure`
(no such route), with a fallback to `/api/settings/integrations/{provider}/connect`
— which is POST-only *and* is a feature-flag flip returning JSON, not a page to
open. `sms` isn't even a valid provider (`twilio` is), so it would have 422'd.

**Decision: truthful status tile for Stripe, delete the SMS card.**
`list_integrations` now returns `stripe: {configured}` from the existing
`payments.stripe_configured()` helper (never the key itself — asserted in test).
The card is read-only, because Stripe is configured by `STRIPE_SECRET_KEY` on the
server and there is nothing here to connect. The SMS card was deleted as
redundant — `PhoneComIntegrationCard`, directly below it, is the working SMS
surface. `connectIntegration()` is now QuickBooks-only, the one provider with a
real OAuth redirect.

### 2. Service-account auth was unprovisionable — REMOVED

`ServiceKeyMiddleware` granted **admin-equivalent** access via `X-Service-Key`
but shipped with no way to mint a key: no web UI, and the CLI its own docstring
pointed at was never written. Nothing in the app ever sent the header.

**Decision: remove the auth path.** Deleted `core/service_accounts.py` and its
middleware registration; the `service_accounts` table stays in the baseline
schema. This also aligns with D17 (`service_accounts` → `access_tokens`).

The security-relevant part: it was the **only producer** of service-account
identity anywhere in the codebase, which made the `actor_kind == "service_account"`
branch in `_db_verify_user` unreachable — a branch that returned `{}`
("trust the principal") *before* the `users.active` check. That was
D-pat-lockout-bypass: a deactivated user's PAT kept working. It now **fails
closed** and logs, so a token still claiming that actor_kind is denied as
forged or leftover. The rollback valve (`AUTH_DB_VERIFY_ENABLED=0`) is checked
first so it can still recover a bad deploy. The D17 detector in
`tests/health/test_silent_failure_detectors.py`, a placeholder skip since it was
written, is now a live assertion on both facts.

**D-pat-lockout-bypass is closed.**

### 3. convert-to-job "not idempotent" — NOT A BUG, no change

Both callers ([EstimatesView.vue], [EstimateView.vue]) already catch 409
explicitly and surface "Already converted" + refresh the row so the button flips
to View Job. The 409 is a deliberate, correctly-consumed contract. The only
defect was the docstring calling it "Idempotent", which this audit fixed.

## The scanner

`gdx_dispatch/tools/comment_drift_scan.py` — the detectors, kept so this stays
checkable instead of being a one-off sweep.

```
python3 gdx_dispatch/tools/comment_drift_scan.py              # full scan
python3 gdx_dispatch/tools/comment_drift_scan.py --path core/ # one section
python3 gdx_dispatch/tools/comment_drift_scan.py --det D1,X1
```

| Detector | Catches |
|---|---|
| D1 | comment names a symbol that exists in no code anywhere |
| D2 | docstring documents a parameter the signature lacks |
| D3 | handler docstring states a method/path its decorator contradicts |
| X1 | `gdx_dispatch.a.b.c` that no longer resolves |
| X2 | `path/to/file.py` that doesn't exist, or `:LINE` past EOF |

Two design points that make it usable rather than noise:

- **The index is built from code only** — comments and docstrings are stripped
  before indexing, so a stale claim can never vouch for itself. (The first
  version indexed comment text too and reported 2 findings across the whole
  repo.)
- **Historical narrative is not drift.** "X was removed", "port of Y",
  "used to live in Z" are accurate records of intentional absence. The scanner
  reads a whole comment block or docstring as one thought, so a "Removed."
  three lines down still exempts the name above it.

Tuning took it from **171 raw hits → 35**, with 21 contract tests
(`tests/test_comment_drift_scan.py`) pinning both directions: it must catch real
drift, and stay silent on historical notes, third-party vocabulary
(`WeasyPrint`, `DataError`, `Stripe.js`), prose sections under `Args:`, negated
route prose, and `--json /tmp/out.json` usage lines.

**Correction from the pre-commit adversarial review.** The historical-narrative
exemption was first written at *block* scope, which reported 9. That was too
coarse and self-defeating: one "was"/"removed"/"legacy" anywhere in a comment
exempted every claim in it, and since a corrected comment is itself written as
history, every comment this scanner exists to protect became exempt from it.
Measured with the check disabled entirely: 52. The exemption is now scoped to
the *sentence* carrying each reference — 35 — which recovers genuine dead
cross-references (`router_sql_live_audit.py:16`, `quickbooks/client.py:102`,
`tenant_models.py:521`) while still exempting real narrative. The remaining 26
over the original 9 are unreviewed and are the next pass, not a claim of
cleanliness.

Of the residual hits, the originally-triaged 9 are known-acceptable: roadmap
items (`TaxProvider` plugin), dated incident records, and `drift_scanner.py`'s
deliberate "until `platform.py` exists this check is inert" gate. The rest are
untriaged.

## Coverage and what's left

Swept mechanically across the **entire** Python tree and frontend:
dangling symbols, ghost parameters, route contracts, module-path rot, file-path
rot, HTTP status claims, documented exceptions, and numeric/unit drift.

The numeric sweep (73 raw hits — timeouts, day windows, point caps, size limits)
came back **essentially all accurate**, which is a genuinely good sign about the
codebase.

Not covered, and not mechanizable: free prose describing *why* code works the
way it does. Spot-checking during this audit found that class largely sound —
the drift concentrated in **pointers** (names, paths, modules), which is exactly
what the scanner now guards.

Suggested next step: run the scanner in CI, or at minimum before a release, so
the count stays near zero instead of rebuilding to 171.
