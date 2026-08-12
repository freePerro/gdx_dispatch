# Unimplemented endpoints — build, remove, or leave?

Created 2026-08-12 from the C6 fix. These endpoints used to answer `{"ok": true}`
to a write they never performed; the Vue showed "Saved" and discarded the user's
edit. They now log at WARNING and return **501**.

**Nothing here is broken by the 501.** None of it ever worked. The change makes
the failure visible instead of silent.

## How to see what people actually try to use

Every refusal logs one line:

```
ui_compat_not_implemented feature=<name> method=<VERB> path=<path> tenant=<id> user=<sub>
```

```bash
ssh <prod-host> 'docker logs <app-container> 2>&1 | grep ui_compat_not_implemented' \
  | sed 's/.*feature=\([^ ]*\( [^ =]*\)*\) method=.*/\1/' | sort | uniq -c | sort -rn
```

That count is the evidence for the decisions below: an endpoint nobody hits in a
month is a candidate for deletion, not for building.

## The list

Ordered by how expensive the missing behaviour looks. "UI" = a Vue view calls it
today, so a user can reach the 501.

| # | Endpoint | UI caller | What's missing | Suggested |
|---|---|---|---|---|
| 1 | `POST/PATCH /api/pricing[/{id}]` | `PricingView` | No `PricingEntry` model. The real pricing router is settings/markup/vendor-lists — a *different* concept. "Pricing entry" was never designed. | **Decide first**: is a pricing entry a catalog item? If the Pricing page duplicates the catalog, remove the page. |
| 2 | `POST /api/payroll/run-current-period` | `PayrollView` | `PayrollEntry` exists, but "run a period" is a calculation (gather hours → rates → entries), never written. | **Build** if payroll is run in GDX; otherwise remove the button. |
| 3 | `POST /api/communications/bulk-sms` | `SegmentsView` | Real single-send exists (`POST /api/communications/send`). Bulk = loop + rate-limit + opt-out + audit. | **Build on top of the working single-send.** Check DNC list per recipient. |
| 4 | `POST /api/customers/{id}/recurring-jobs` | `CustomerDetailView` | `RecurringJobSchedule` exists but needs `job_template_id` + a `frequency` enum; the Vue sends free-text `title` + `interval_days`. **Incompatible models.** | **Decide the model first**, then either repoint the Vue at `/api/recurring` or widen that model. |
| 5 | `POST /api/customers/{id}/portal-account` | `CustomerDetailView` | Portal has login/password endpoints but no provisioning. (`DELETE` on the same path is also broken — C2.) | **Build** — customers can't be onboarded to the portal without it. |
| 6 | `PATCH /api/sso`, `POST /api/sso/test-connection` | `SsoView` | Real SSO is OAuth redirect flows (`/auth/sso/google`), not config CRUD. No `SsoConfig` model. `GET /api/sso` is also a permanent blank (C5). | **Remove the page** unless per-tenant SSO config is genuinely wanted. |
| 7 | `POST/PATCH /api/scheduling[/{id}]` | `SchedulingView` | No `ScheduleEntry` model. Real scheduling is calendar + appointments + tech-unavailability. | **Probably remove** — likely duplicates the calendar. |
| 8 | `PATCH /api/booking/{slot_id}` | `BookingView` | No `Booking` model. Real booking is request/approve/decline. Editing a slot isn't in that model. | **Remove the edit affordance.** |
| 9 | `POST/PATCH /api/equipment-tracking[/{id}]` | `EquipmentTrackingView` | ⚠️ `EquipmentAsset` exists but its router was **deliberately unwired 2026-05-03** to kill the parallel `equipment_assets` table. Implementing resurrects exactly what that consolidation removed. | **Repoint the Vue** at the canonical equipment API. Do NOT implement here. |
| 10 | `POST /api/reviews/{id}/responses` | `ReviewsView` | `CustomerReview` has no response column. Needs a migration. | **Build** if replying to reviews matters; small migration. |
| 11 | `PATCH /api/jobs/{id}/parts/{part_id}` | `JobCostingView` | `JobPart` exists; only POST was built. Note `GET`/`DELETE` on the same resource are also broken (C2). | **Build the full CRUD** — the parts panel is non-functional without it. |
| 12 | `POST /api/jobs/{id}/apply-template` | `JobDetailView` | `JobTemplate` exists with checklist/duration/parts. Applying = copy onto the job. | **Build** — cheap and the model is ready. |
| 13 | `POST /api/marketing` | — | No model. | Remove. |
| 14 | `POST /api/uploads` | — | Real uploads go through the documents/photos routers. | Remove. |
| 15 | `POST /api/estimate/calculate`, `/api/estimate/save` | — | Portal estimate flow; the real estimate surface is `/api/estimates`. | Remove. |
| 16 | `POST /api/billing/change-plan`, `/api/billing/cancel` | — | SaaS-plan billing — a multi-tenant concept that went with the platform collapse. | Remove (single-tenant, self-hosted). |
| 17 | `POST /api/ai/quality/feedback` | — | No store. `GET /api/ai/quality/*` are also permanent blanks (C5). | Remove or build with the AI-quality page. |

## Verified NOT an issue

`POST /api/role-permissions/migration-banner/ack` returns `{"pending": False}`
and does nothing — correctly. The per-tenant feature-flag table it acknowledged
was removed in the single-tenant collapse, so there is never a pending
migration to ack. `GET` on the sibling path always reports `pending: False`.
The scanner still flags it (C6=1); that is a known, accepted true-negative.

## Already fixed, for reference

`PATCH /api/service-agreements/templates/{id}` was in this class and is now
genuinely implemented in `routers/service_agreements.update_template` — the
model and the sibling GET/POST already existed, so it only needed writing.
It also accepts the Vue's legacy `price` field alongside `default_price`.
