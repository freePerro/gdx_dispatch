# Public Estimate Approval Page (QuickBooks-style) — Plan

**Date:** 2026-08-13 · **Status:** AUDITED — adversarial critique applied, see §7
**Branch:** `feat/public-estimate-approval` off `main` (PR #317 merged)

## 1. Problem

Estimates go out by email with a PDF attached, but the customer has no way to
approve or pay the downpayment from the email. The portal path
(accept/decline + one-motion deposit) exists but requires a CustomerUser +
login, which the send flow deliberately never links to (the Tier-9.8 deferred
note in `routers/estimates.py` ~L1565: a bare portal link dead-ends for
first-time recipients). Doug's direction: most customers don't want a portal —
they want a single public page: "Would you like to accept?" — like QuickBooks.

## 2. What already exists (verified in code; corrected per audit)

| Piece | Where | State |
| --- | --- | --- |
| `Estimate.public_token` — `token_urlsafe(48)[:64]`, `unique=True, nullable=False` | mint sites: `routers/estimates.py:767,2247`, `mobile_quoting.py:430`, `mcp_tools/estimates_create_draft.py:113`, `proposals/service.py:48`; column `proposals/models.py:58` | ✅ all five mint sites strong (QB sync mints *invoice* tokens only — never estimates) |
| Public GET `/api/proposals/{token}` — sent_at + deleted_at gated, explicit projection, records `estimate_viewed_by_customer` | `modules/proposals/router.py:67` | ✅ live, but payload gaps + a shipping `stripe_payment_link` leak (§3.1) |
| Reserved SPA route `/proposals/:token` (exact-path `/proposals` redirect; backend SPA catch-all serves deep links, `app.py:~2048`) | `frontend/src/router/index.js:353` | ✅ route free |
| Accept side-effects: job auto-create + one-motion deposit + public `/pay` URL | `routers/portal.py:1183-1275`, `modules/deposits/service.py` | ✅ reusable for NON-tier estimates; **portal has no tier path** |
| Tier accept service (`accept_tier`) | `modules/proposals/service.py:154` | ⚠ NO status gates — must NOT be delegated to from a public endpoint |
| Public `/pay/{token}` Stripe page (backend HTML) | `core/payments.py:623` | ✅ E2E-verified (v1.22.1 real charge) |
| Email button "View & Accept Estimate" behind unused `portal_url` param | `core/email_sender.py:100,113-119` | ✅ built, never passed |
| Public SPA page pattern (`meta: {public, noShell}`, bare `fetch`) | `CustomerPortalView.vue`, router:169 | ✅ precedent |
| Public branding endpoint | `routers/branding_public.py` (`GET /branding`) | ✅ |
| `GDX_PUBLIC_BASE_URL` | `docker-compose.yml:37`, `.env.template:43` | ✅ plumbed; eyeball prod value at deploy |

## 3. Build

### 3.1 Backend — enrich `GET /api/proposals/{token}`

File: `modules/proposals/router.py`.

- **Totals — two shapes, because the math differs (audit §1):**
  - Non-tier estimate: `totals` from `compute_estimate_totals(est, db)` →
    `{subtotal, discount, tax, tax_rate_pct, total}`. Matches PDF + email.
  - `proposal_mode` estimate: **NO single total.** `est.total` can be the
    *highest* tier (mobile builder) and `compute_estimate_totals` is
    tier-blind — any single number would show the best-tier price to a
    customer picking good. The page shows **per-tier prices only**; omit
    `totals` entirely from the payload in proposal_mode.
- **Line items**: public-safe projection of `EstimateLine` (description,
  quantity, unit_price, line_total, sort order). `hide_line_prices` is
  **tri-state**: per-estimate column wins, else tenant default — use
  `effective_hide_line_prices(estimate.hide_line_prices,
  get_features(tenant_id).hide_line_prices)` and STRIP prices server-side,
  copying `portal.py:1077-1107` verbatim.
- **Remove `stripe_payment_link` from the tier projection** — it's a payment
  path that bypasses the deposit invoice, already leaking today (audit §4).
  No consumer exists (this endpoint's page hasn't been built until now).
- **Deposit context**: `deposit_pct` (from `get_features(tenant_id)`), and on
  an `accepted` estimate with `balance_due > 0`, include `deposit:
  {invoice_number, amount, balance_due, pay_url, status}` (mirrors the
  portal's accept-then-abandon recovery at `portal.py:1031-1041`).
  `tenant_id` from `request.state.tenant` (same as `core/payments.py`).
- **Status masking**: serialize `rejected` (email bounced — internal) as
  `sent`, exactly like `_serialize_portal_estimate` (`portal.py:1020-1023`).
- Keep the explicit-projection discipline; extend the leak-regression test to
  cover `stripe_payment_link`.
- Estimate image attachments: out of scope v1 (the emailed PDF embeds them).
- Known limitation (audit §5): link-scanner prefetch (Outlook SafeLinks) can
  stamp `estimate_viewed_by_customer` — pre-existing behavior of this route,
  unchanged here; "viewed" stays a hint, never evidence.

### 3.2 Backend — `POST /api/proposals/{token}/accept` (public)

- **Lookup**: same triple filter (token, `deleted_at IS NULL`,
  `sent_at IS NOT NULL`) — **with `.with_for_update()`** so two concurrent
  accepts serialize instead of double-creating jobs/deposits (audit §2;
  no-op on SQLite tests, real row lock on Postgres). Wrong/unsent/deleted →
  uniform 404.
- **Body**: `{tier_id?: UUID}`. If `proposal_mode` with tiers, `tier_id` is
  REQUIRED (422 if missing) and must belong to this estimate (404).
  Non-tier estimates ignore/refuse tier_id (422 if sent).
- **Status gate**: acceptable from `sent` and `rejected` (bounce self-heal).
  `declined`/`expired` → 409 with customer-readable detail.
  **Re-click idempotency**: `accepted` → **200** with current state +
  existing deposit summary + `already_accepted: true` (deliberate divergence
  from portal's 409 — an emailed link gets double-clicked; tests pin it).
  Expiry beyond the status flag stays the nightly task's job (`rejected` is
  already in the sweep since #317).
- **Effects** — tier path is NEW code with gates, not `accept_tier`
  delegation (audit §6.3):
  1. `status='accepted'`, `accepted_at`, `updated_at`; `accepted_tier_id`
     when a tier was chosen.
  2. Audit event `public_estimate_accepted`, actor `"customer:public-link"`,
     details incl. client IP + tier.
  3. Job auto-create via `routers.estimates._create_job_from_estimate`
     (lazy import, portal pattern), only when `estimate.job_id is None`,
     inside the row lock — failure logs + audits but never un-accepts.
  4. One-motion deposit when `deposit_pct > 0` via
     `create_deposit_invoice(source="public_accept")` (already idempotent
     per estimate). **Deposit base + cap (audit §3):**
     - non-tier: base = `compute_estimate_totals(...)["total"]` (unchanged
       service cap applies);
     - tier: base = selected `tier.total_price`, and the service grows an
       optional `cap_total:` override so the cap compares against the tier
       price instead of the tier-blind lines total — otherwise an
       office-built tier priced above the lines silently skips the deposit.
       Existing callers (office/portal/mobile) pass nothing and keep
       today's behavior exactly.
     Tier price is the contract price as presented — no extra tax math on
     tiers (documented; MN construction contracts charge no customer sales
     tax anyway). Response carries `deposit` incl. `pay_url`; failures
     logged, never block the accept.
- **Response**: §3.1 payload + `deposit` (+`already_accepted` when so).

### 3.3 Backend — `POST /api/proposals/{token}/decline` (public)

- Same lookup + gates (row lock included). Body `{reason?: str, max 2000}` —
  optional (standing decision: customer declines never require a reason).
- `status='declined'`, `declined_at`, `declined_reason`, `updated_at`; audit
  `public_estimate_declined`. Re-click on `declined` → 200 `already_declined`.

### 3.4 Rate limiting — deliberately NO new wiring (audit §2)

The strict `_AUTH_PREFIXES` bucket was planned and is **dropped**: the per-IP
counter is shared across paths (SPA assets are proxied through the app, and
SafeLinks prefetches burn it), so it would 429 the paying customer, while
spoofable first-hop XFF means scanners sail through anyway. The actual
defense is the 64-char token (unguessable, unique, non-null), uniform 404s,
and the existing general limiter. Documented here so the omission reads as a
decision, not a miss.

### 3.5 Email — put the link in both send paths

- **Server send** (`routers/estimates.py /send`): explicit conditional —
  ```py
  base = os.getenv("GDX_PUBLIC_BASE_URL", "").rstrip("/")
  proposal_url = f"{base}/proposals/{est.public_token}" if base else ""
  ```
  (a bare f-string yields a relative, dead-in-email href — audit §6.7).
  Pass to `build_estimate_email_html(portal_url=proposal_url)` — the button
  already exists. Delete the Tier-9.8 deferred NOTE.
- **Composer path** (`/email-compose`): add `estimate_link` to the template
  ctx; when the base URL exists and the rendered body doesn't already carry
  the link, append `"Review & approve online: {url}"` (mirror the invoice
  compose "Pay online:" pattern). Default body template gains the
  `{{estimate_link}}` line.

### 3.6 Frontend — `ProposalPublicView.vue`

Route: `{ path: '/proposals/:token', name: 'proposal-public',
meta: { public: true, noSidebar: true, noShell: true } }` (exact-path
`/proposals` redirect untouched).

Bare `fetch` (no auth interceptors), PrimeVue, mobile-first, light + dark.

States:

- **Loading / invalid token**: friendly "This link is invalid or no longer
  available — reply to the email or call us."
- **Open (`sent`, incl. masked `rejected`)**: company header (public branding
  endpoint), estimate number/label, description, jobsite address; then:
  - non-tier: line-item table (respecting stripped prices) + totals block;
  - proposal_mode: good/better/best tier cards, each with ITS OWN price;
    selection required before Accept; **no combined total anywhere**.
  Buttons: **Accept** (confirm dialog quoting the amount being agreed to —
  the totals `total` for line estimates, the *selected tier's* price for
  proposals) and **Decline** (dialog, optional reason).
- **Accepted**: green confirmation; when `deposit.pay_url` → "Pay $X deposit"
  (full-page nav to `/pay/{token}`) + pay-by-check note (portal copy);
  "Pay later" leaves the page.
- **Declined**: "You declined this estimate. Changed your mind? Reply to the
  email or call us."
- **Expired**: "This estimate has expired — contact us for a current quote."

### 3.7 Tests

Backend (pytest, docker-app image harness; extend `tests/test_proposals.py`
fixtures):

- GET: uniform 404 (wrong token / draft / deleted); non-tier totals
  tax-inclusive; proposal_mode payload has NO totals; lines present;
  tri-state hide_line_prices strips prices (estimate override beats tenant
  default); leak regression extended with `stripe_payment_link`; deposit
  context present on accepted-with-balance.
- Accept: happy path flips status + creates job + deposit with `pay_url`
  (pct set); no deposit at pct=0; **office-tier shape** (estimate WITH lines
  + tier priced ABOVE the lines total) still creates the deposit at
  pct × tier price — the cap-override regression (audit §3); tier required
  in proposal_mode (422) / foreign tier 404; `accepted_tier_id` recorded;
  re-accept → 200 idempotent + same deposit (no second invoice); declined
  and expired → 409; draft/unsent → 404; accept from `rejected` works;
  job not duplicated when `job_id` already set.
- Decline: reason optional + recorded; re-decline → 200; declined blocks
  accept.
- Email: `/send` html contains `/proposals/{token}` when base set; base
  unset → **no `/proposals/` href at all** (pins the truthy-relative-URL
  bug); compose body contains the link.
- Deposit service: `cap_total` override honored; absent → old behavior.

Frontend (vitest): render states (open line-estimate, open tiers, accepted +
deposit, declined, expired, invalid); tier selection gates Accept; confirm
dialog shows selected tier price; accept POST payload carries tier_id;
decline with/without reason.

## 4. Explicitly out of scope

- E-signature capture (accepted_at + audit trail + client IP is the
  evidence — QuickBooks-equivalent).
- SMS delivery; estimate nurture sequencing.
- Portal changes (both paths coexist).
- Fixing the pre-existing SafeLinks view-stamp behavior of the public GET.
- Setting `estimate_deposit_pct` in prod (page + accept work at pct=0 —
  just no payment ask).

## 5. Deploy notes

- No migration. No new env. Verify `GDX_PUBLIC_BASE_URL` on prod at deploy.
- Post-deploy walk: send a real estimate to a personal inbox, click through
  email → page → accept → deposit → /pay (stop before charging, or $1 test).
- Confirm `estimate_deposit_pct` value with Doug before announcing the flow.

## 6. Risks / traps carried in

- **Money math**: line estimates use `compute_estimate_totals`; proposal_mode
  shows per-tier prices only — never a single computed total (tier-blind
  totals + max-tier `est.total`).
- **Draft leak**: every estimate has a token from birth — the `sent_at` gate
  keeps drafts dark; every new endpoint repeats the triple filter.
- **`_create_job_from_estimate`**: lazy import from `routers.estimates`
  (portal pattern) to dodge the import cycle; guarded by `job_id is None`
  inside the row lock.
- **Idempotency divergence from portal** (200 vs 409 on re-accept) is
  deliberate; tests pin it.
- **`cap_total` override**: new kwarg, default None → existing callers
  unchanged; only the public tier accept passes it.

## 7. Audit response (2026-08-13, adversarial subagent)

Verdicts adopted, all eight factual findings accepted:

1. Tier money math was the foundational flaw → per-tier prices only in
   proposal_mode; no `compute_estimate_totals` on tiered payloads (§3.1).
2. `accept_tier` has no gates → public tier accept is new, gated code (§3.2).
3. Deposit cap would silently skip office-built tier deposits →
   `cap_total` override in the service + regression test (§3.2, §3.7).
4. Rate-limit prefix change would throttle customers, not scanners →
   dropped, documented (§3.4).
5. `stripe_payment_link` already leaks in the public GET → removed + leak
   test extended (§3.1).
6. Naive f-string renders a dead relative email link when base unset →
   explicit conditional + pinning test (§3.5, §3.7).
7. Concurrent double-accept race → `with_for_update()` row lock (§3.2).
8. §2 table corrected (QB sync mints invoice tokens; five real estimate
   mint sites; hide_line_prices is per-estimate with tenant default).
