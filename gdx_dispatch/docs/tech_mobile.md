# Tech Mobile — Workflow, API, Permissions, Offline Sync

Sprint `sprint_tech_mobile` documentation. The mobile surface is a PWA
served from the same FastAPI backend at `/mobile` and `/mobile/jobs`
(tech) and `/mobile/dispatch` (dispatcher).

This is the contract a tech, a dispatcher, and an integrator can rely
on. Anything not listed here is internal and may change.

## Workflow (a tech's day, mobile-first)

1. **Morning** — open `https://<tenant>.example.com/mobile` on
   the phone. PWA prompts to install if not installed.
2. **Today's route** — `/mobile` shows the day's jobs in scheduled
   order. Each card carries customer name, address, ETA, alerts (gate
   code, dog warning, COD), service type, and a Navigate button.
3. **On my way** — tap "On my way" → state advances to `en_route`,
   audit-logged. Optional auto-fire if `tech_mobile.on_my_way_auto_fire`
   is `auto`.
4. **Arrive** — tap "I'm here" → geo-tagged `on_site` stamp; per-tech
   labor timer starts.
5. **Work the job** — capture photos (slot-tagged before/during/after if
   the tenant requires it), add notes, request parts via the parts
   modal (SKU autocomplete from tenant catalog), chat with dispatch
   via the per-job thread.
6. **Quote on-truck** — Phase 2.1: tap "Build quote" → pick a service
   → 3 tier cards (Good / Better / Best) → "Hand to customer" →
   customer picks tier + signs on tech's phone.
7. **Complete** — tap "Complete" → optional completion notes +
   customer signature → state goes to `done`.
8. **Close out** — Phase 2.2: tap "Close out" → financial summary +
   "Generate & email invoice" sends the invoice straight from the
   truck. Office reconciles payment.
9. **End of day** — Phase 4.2: today view's last card flips to
   "Day wrap" — jobs done, hours, parts requested, callbacks scheduled.

## Routes

| Route | Audience | Permission required |
|---|---|---|
| `/mobile` | tech | `mobile.use` |
| `/mobile/jobs` | tech | `mobile.use` |
| `/mobile/summary` | tech | `mobile.use` |
| `/mobile/dispatch` | dispatcher | `mobile.dispatch_view` |

## API surface (mobile-scoped)

All endpoints are gated on the `mobile` module (`require_module("mobile")`)
plus per-call ownership/permission checks. Tenant isolation is via the
DB connection (`get_tenant_db`) — there are no tenant-id filters in the
SQL.

### Today's route + state transitions
- `GET  /api/mobile/today` — today's jobs for the calling user
- `POST /api/mobile/today/reorder` — drag-reorder
- `POST /api/mobile/jobs/{id}/en-route` — advance state
- `POST /api/mobile/jobs/{id}/arrived` — geo-tagged arrival
- `POST /api/mobile/jobs/{id}/complete` — completion (with signature)

### Notes / photos / signatures (per-tech attribution)
- `POST /api/mobile/jobs/{id}/notes`
- `POST /api/mobile/jobs/{id}/photos` (multipart; slot kind = before/during/after)
- `POST /api/mobile/jobs/{id}/signature`

### Parts (Phase 1.3)
- `GET  /api/jobs/{id}/parts-needed`
- `POST /api/jobs/{id}/parts-needed`
- `PATCH /api/parts-needed/{id}`

### Quoting (Phase 2.1)
- `GET  /api/mobile/quotes/services` — Good/Better/Best preset catalog
- `GET  /api/mobile/quotes/decline-reasons`
- `POST /api/mobile/jobs/{id}/quote` — build quote from preset/custom
- `GET  /api/mobile/jobs/{id}/quote` — list quotes
- `GET  /api/mobile/quotes/{id}`
- `POST /api/mobile/quotes/{id}/accept` — customer accept + signature
- `POST /api/mobile/quotes/{id}/decline` — decline + reason

### Invoicing (Phase 2.2)
- `GET  /api/mobile/jobs/{id}/financial` — close-out summary
- `POST /api/mobile/jobs/{id}/invoice` — create + email invoice
- `POST /api/mobile/invoices/{id}/send` — re-send
- `POST /api/mobile/invoices/{id}/send-receipt` — receipt for
  office-recorded payment

### Day summary (Phase 4.2)
- `GET  /api/mobile/day-summary?date=YYYY-MM-DD` — jobs done, hours,
  parts requested, callbacks. Default date = today.

### Chat (Phase 4.1)
- `GET  /api/mobile/jobs/{id}/chat` — thread messages
- `POST /api/mobile/jobs/{id}/chat` — send message (body or quick-action)
- `POST /api/mobile/chat/{message_id}/read` — mark read (dispatch-side
  read receipts; techs aren't tracked)

### Dispatch (Phase 4.4 — `mobile.dispatch_view`)
- `GET /api/mobile/dispatch/today` — every assignment in the tenant for
  today, grouped by tech + status
- `GET /api/mobile/dispatch/threads` — active per-job chat threads,
  unread-first
- `POST /api/mobile/dispatch/jobs/{id}/reassign` — reassign to a
  different tech

## Permissions

| Permission | Granted to (default builtin) | What it gates |
|---|---|---|
| `mobile.use` | technician, dispatcher | mobile route + GET endpoints |
| `mobile.chat` | technician, dispatcher | per-job chat send/receive |
| `mobile.dispatch_view` | dispatcher | `/mobile/dispatch` + dispatch endpoints |
| `jobs.write` | technician, dispatcher, admin | state transitions, notes, photos |
| `inventory.read/write` | technician | parts modal SKU autocomplete + flag |
| `estimates.write/send` | sales (NOT tech directly) | mobile quote endpoints internally bypass — tech-mobile is a sealed surface |
| `invoices.send` | accounting | mobile invoice endpoints internally bypass |

The "internally bypass" rows mean: mobile_quoting / mobile_invoicing
routers do not call `Depends(require_permission(...))` for the
estimates / invoices permission keys. Instead they enforce
ownership-of-job + module gate. This is by design — mobile is a
single-surface workflow; granting techs `estimates.send` would let them
hit `/api/estimates` admin paths, which is wider than the truck
warrants.

## PII masking (Phase 4.3)

On unassigned jobs (browse mode where the tech is not the assignee),
mobile responses mask:
- customer address → street + zip only (no street number, no street name)
- customer phone → last 4 digits only
- customer email → first character + `***@<domain>`

On assigned jobs the tech sees full PII. Threshold is the
`assigned_to == current_user OR job_assignments.tech_id == current_user`
check used everywhere else in the mobile router.

## Per-tenant configurability

The setting catalog at `gdx_dispatch/core/feature_defaults.py` declares every
"policy choice" setting. Tenants override via the
`AppSettings.tenant_mobile_settings` JSON column. Reads cascade:
override → catalog default. The admin UI at
`/admin/feature-settings/tech-mobile` renders the catalog form.

Settings used by mobile (non-exhaustive):
- `tech_mobile.signature_required_completion`
- `tech_mobile.signature_required_quote`
- `tech_mobile.signature_surface` (`phone_handoff` / `customer_link`)
- `tech_mobile.photo_slot_tagging` (`required` / `optional`)
- `tech_mobile.on_my_way_auto_fire`
- `tech_mobile.drag_reorder_authority`
- `tech_mobile.estimate_validity_days`
- `tech_mobile.quote_decline_reasons` (list[string])
- `tech_mobile.quote_tax_shape`
- `tech_mobile.offline_mode_enabled`
- `tech_mobile.completion_lead_tech_only`

## Offline sync contract (Phase 3.1)

The mobile PWA backs reads + queued mutations with IndexedDB (Dexie).
On the wire:

- **Mutations**: every `useApi.postQueued()` / `patchQueued()` call
  writes to `sync_queue` first, then attempts the request. The request
  carries `Idempotency-Key: <uuid4>` (Stripe convention).
  - 2xx → row marked `synced`.
  - 4xx (non-409) → row marked `failed`. Retry is manual.
  - 409 → treated as `synced` (server-side dedup detected the replay).
  - 5xx / network failure → row stays `pending`. Drained on next
    `online` event or visibility change.
- **Reads**: today's jobs, parts requests, customers are mirrored to
  IndexedDB stores indexed by `synced_at`. Hydration on visibility
  change pulls deltas; conflicts resolve row-level last-write-wins.
- **Banner**: `/mobile` shows a sticky banner whenever
  `!isOnline || pendingCount > 0`. Tech can tap "Sync now" to force a
  drain when re-connected.

Server-side dedup is handled by
`gdx_dispatch/core/middleware/idempotency.py` — Stripe-shaped, Redis-backed,
JSON responses cached for replay TTL.

## Push notifications

Web Push API via VAPID. Sprint 1 wired `pywebpush` + the
`/api/push/subscribe` endpoint. Notification types currently in use:

| Type | Trigger | Recipient |
|---|---|---|
| `parts_status_change` | dispatch flips a part to `ordered`/`received` | requesting tech |
| `parts_critical_flagged` | tech flags a critical part | dispatchers |
| `chat_message` (Phase 4.1) | new chat message on a job | the other side of the thread |
| `job_assigned` (Phase 4.2) | job assigned to a tech | that tech |
| `day_summary_ready` (Phase 4.2) | last job of the day completes | that tech |

Per-user notification preferences are stored in `AppSettings.notification_preferences`.

## Rate limits

The global slowapi limiter is registered as middleware in `gdx_dispatch/app.py`.
Default tier limits apply to every request. Per-route stricter limits
(e.g. photo uploads at 30/min/user) are deferred to a follow-up — the
global default already throttles abuse.

## Audit log

Every mobile mutation writes an audit row via `log_audit_event_sync`:
- actor = `current_user.user_id`
- entity_type = `job` / `estimate` / `invoice` / `chat`
- action = `mobile_<verb>` (e.g. `mobile_quote_accepted`)
- details = JSON with the relevant payload subset

Audit search at `/api/audit-events` with `?action=mobile_*` filters all
mobile activity for compliance review.
