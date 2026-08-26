# Estimate rejection visibility — plan

**Status:** PLAN — not built. Re-verified against main 2026-08-21: there is no
`GET /api/estimates/{id}/activity`, no "Failed Email" label anywhere in the
frontend, and `bounce_detect._match_estimates` still has none of PR 2's
parity work. Decisions in § Decisions taken are locked and still stand.

**Date:** 2026-08-18 (rev 2, same day — Doug's decisions folded in)
**Trigger:** EST-000085 showed a bare red "Rejected" tag and the office had no
way to see why or by whom. The answer existed the whole time — a complete
`audit_logs` row — but no UI surfaces it, and the word "Rejected" reads like a
customer decline when it actually means "the estimate email bounced."

**Decisions from Doug (2026-08-18):**
1. The `rejected` status displays as **"Failed Email"** (not "Undeliverable").
2. Fixing the address and re-sending must return the tag to the sent state.
   The status vocabulary's name for "delivered as far as we can know" is
   `sent` — in-app re-send already restores it (verified,
   [estimates.py:1965](../../gdx_dispatch/routers/estimates.py#L1965));
   what's missing is detecting a *manual* re-send from the operator's own
   mail client, which the app cannot see today → PR 3.

## What actually happened (prod forensics, 2026-08-18)

Verified directly against the prod DB:

- EST-000085 was emailed 2026-08-13 to a mistyped customer address
  (one letter off from the address now on file).
- The address on the customer record was corrected ~3.5 h later — *after* the
  send, *before* the sync saw the NDR. Exactly the case the
  conversation/time matching rung in the bounce detector was built for, and it
  worked: `matched_by: "conversation_time"`.
- 2026-08-14 06:00 UTC the bounce detector (PR #317, its **first prod flip**)
  set `status = rejected` and wrote audit action `estimate_email_rejected`
  (actor `bounce-detector`, details: failed recipient, NDR subject, Graph
  message id, match rung).
- `declined_at` / `declined_reason` are NULL by design on this path — it is
  not a decline. The customer never received or saw the estimate.

So: **the audit trail is complete; the product surface is silent.** Even the
person who requested the bounce feature could not tell, five days later, what
the red tag meant. Prod today: 9 estimates `declined` (real decisions) vs 1
`rejected` (this bounce) — the vocabulary distinguishes them, the UI doesn't.

## Code map (all five status-setting paths)

| # | Path | Route | Actor recorded | Reason | `declined_at` | Webhook | Office bell |
|---|------|-------|----------------|--------|---------------|---------|-------------|
| A | Office decline | `POST /api/estimates/{id}/decline` ([estimates.py:2430](../../gdx_dispatch/routers/estimates.py#L2430)) | staff user id | mandatory | yes | `estimate.declined` | no (office is actor) |
| B | Mobile tech decline | `POST /api/mobile/quotes/{id}/decline` ([mobile_quoting.py:692](../../gdx_dispatch/routers/mobile_quoting.py#L692)) | tech id + IP | mandatory | yes | **missing** | **missing** |
| C | Public link decline | `POST /api/proposals/{token}/decline` ([proposals/router.py:610](../../gdx_dispatch/modules/proposals/router.py#L610)) | `customer:public-link` | optional | yes | **missing** | yes |
| D | Portal decline | `POST /api/customer-portal/estimates/{id}/decline` ([portal.py:1344](../../gdx_dispatch/routers/portal.py#L1344)) | `portal:{user_id}` | optional | yes | **missing** | yes |
| E | Email bounce | `bounce_detect._match_estimates` ([bounce_detect.py:184](../../gdx_dispatch/modules/outlook/bounce_detect.py#L184)) | `bounce-detector` | n/a | no (status only) | **missing** (`email.bounced` not emitted for estimates) | **missing** |

Every path writes a proper audit row. Nothing else can set these statuses
(generic PATCH has no `status` field; `_ensure_editable` 409s finalized
estimates). Reopen is the only path that clears `declined_at`/`declined_reason`.

## The gaps

1. **`EstimateView.vue` renders none of it.** `declined_reason` and
   `declined_at` are fetched and stored in script state but never bound in the
   template. No actor is ever shown. No activity/history panel exists on the
   estimate. (The customer portal shows *more* than the office does —
   `CustomerPortalView.vue` renders "Declined: {reason}".)
2. **"Rejected" is a misleading label** for "email bounced." Same red
   `danger` severity as Declined; only the word differs, and the word lies.
3. **Bounce path parity:** for invoices the detector stamps
   `outbound_emails.bounced_at` and emits `email.bounced`; the estimate rung
   does neither. No office bell fires — the office discovers a bounce only by
   noticing a red tag some day.
4. **Path inconsistencies** (table above): mobile decline emits no webhook and
   no bell; public/portal declines emit no webhook.

## Plan

### PR 1 — Tell the story on the estimate (office desktop)

Backend:
- `GET /api/estimates/{id}/activity` — read-only, staff-auth. Serves the
  estimate's `audit_logs` rows through a curated action whitelist
  (`estimate_created`, `estimate_marked_sent`, `estimate_sent`/send actions,
  `estimate_accepted`, `estimate_declined`, `mobile_quote_declined`,
  `public_estimate_declined`, `portal_estimate_declined`,
  `estimate_email_rejected`, reopen, expiry, attachment upload). Humanize the
  actor server-side: staff id → display name, `portal:*` → "Customer
  (portal)", `customer:public-link` → "Customer (email link)",
  `bounce-detector` → "System — email bounce detector". Whitelist means noisy
  `patch_estimate {}` rows collapse into an "edited" line or are grouped.

Frontend (`EstimateView.vue`):
- **Status context strip** under the header, driven by status:
  - `declined`: "Declined {date} by {actor} — {reason}" (finally renders
    `declined_reason`; actor from the activity endpoint).
  - `rejected`: warning banner: "The estimate email to **{failed
    recipient}** bounced on {date}. The customer never received it." with two
    actions: **Fix customer email** (link to customer edit) and **Re-send**
    (the existing send composer — `/send` and `/mark-sent` already permit
    `rejected`, verified at [estimates.py:1862](../../gdx_dispatch/routers/estimates.py#L1862)
    and [:1501](../../gdx_dispatch/routers/estimates.py#L1501)). Failed
    recipient/date come from the `estimate_email_rejected` activity row.
- **Rename the displayed label** for `rejected` → **"Failed Email"**
  (Doug's wording, decided 2026-08-18) with `warn` severity (not `danger`)
  in `EstimateView.vue`, `EstimatesView.vue` list, and `statusSeverity.js`.
  Enum value stays `rejected` — display-only, no migration.
- **Regression tests for the recovery flip** (behavior exists, untested for
  this transition): `rejected` + successful `/send` → `sent`; `rejected` +
  `/mark-sent` → `sent`; failed `/send` from `rejected` → status unchanged.
  The banner's Re-send button is the UI path onto this.
- **Activity panel** on the estimate detail rendering the endpoint — the
  generic answer to "who did what, when" (invariant #1 made visible).

Tests: pytest for the endpoint (whitelist, actor mapping, cross-entity id
leak check); vitest for strip + label + panel. Browser walk on EST-000085's
real data in a throwaway container, light + dark.

### PR 2 — Bounce parity + proactive notification

In `bounce_detect._match_estimates`, mirror what the invoice rung already
does, plus tell the office:
- Stamp the matching `outbound_emails` row (`bounced_at`) when one exists
  (post-v1.68.0 sends will have one; the EST-000085-era send predates the
  table and has none — code must tolerate absence).
- Emit the `email.bounced` domain event for estimate bounces.
- Fire the office bell (`notify_estimate_decision`-style): "Estimate
  {number} email bounced — {recipient} undeliverable." Recommended default:
  on, not configurable (a bounce is always actionable).

### PR 3 — Recovery detection: a manual re-send heals the tag

Requirement (Doug, 2026-08-18): fixing the address and re-sending must return
the tag to `sent`. The in-app paths already do this (PR 1 adds the tests and
the button). The hole is a **manual re-send from the operator's own mail
client** — the app never sees it, so the estimate stays "Failed Email"
forever. EST-000085's original send was exactly this manual channel.

Mirror image of the bounce detector, in the same outlook sync:

- For each estimate in `rejected`, scan synced **Sent Items** for a new
  outbound message with `sentDateTime` **after** the bounce flip, matched by
  the same three rungs bounce_detect already uses, in the same confidence
  order: subject serial → customer email (the *current*, corrected address)
  → conversation/time. Exclude system-sender messages (NDRs/receipts).
- On match: `status = "sent"`, `sent_at =` the detected message's
  `sentDateTime`, `sent_via = "manual"`, re-apply send expiry; audit action
  `estimate_resend_detected` (details: recipient, graph message id,
  `matched_by`) by actor `resend-detector`.
- Only the `rejected` → `sent` transition — never touch draft / accepted /
  declined / expired. If the re-send bounces again, the bounce detector
  flips it right back (the cycle is safe: each flip is audited).
- Lesson from #317 applies: audit every `status.in_(...)` consumer again
  when a new transition into `sent` appears (nurture, expiry sweeps,
  Unsent-style derivations, close-rate reports).

### Follow-ups to file as issues (not bundled)

- Mobile decline (path B): emit `estimate.declined` webhook + office bell.
- Public/portal declines (C, D): emit `estimate.declined` webhook.
- Localized (non-English) NDRs still undetected — fail-quiet (known from
  #317, unchanged).

## Decisions taken

- **"Failed Email"** is the display label for `rejected` (Doug,
  2026-08-18); enum value unchanged, no migration.
- **Re-send restores `sent`** — the vocabulary's delivered-side state is
  `sent` (we only ever know the provider accepted; a later NDR revokes it).
  In-app paths already flip; PR 3 adds manual-send detection.
- No schema change: actor stays in `audit_logs` only; no
  `status_changed_by` column. The activity endpoint is the read path.
- Three PRs, merged in order; follow-ups filed, not bundled.

## Open (Doug)

- EST-000085 itself: **never re-sent** — mailbox-verified 2026-08-18: Sent
  Items holds exactly one send (Aug 13, to the mistyped address) and zero
  messages to the corrected address. The customer still hasn't seen it;
  address on file is correct; one Re-send away. (Doug action —
  customer-facing.)
