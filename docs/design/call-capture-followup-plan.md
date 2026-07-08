# Call Capture & Follow-Through Plan

**Status: BUILT 2026-07-07 (v3 design) — branch feat/call-capture-followup, all 3 phases in one PR; verified on the Android emulator (light + dark), 48 backend + 911 frontend tests green**
**Date: 2026-07-07**

> Audit round 1 verdict: PR 1 buildable; PR 2 as originally written was "a bell with no
> clapper" — web push is NOT functional in prod (no VAPID keys, no PWA manifest, no
> subscribe CTA on office nav; iOS can't receive push in a plain browser tab at all).
> The audit's replacement (SMS via Phone.com) was ALSO wrong — Doug: the SMS path
> doesn't work yet; the integration is deliberately P2P-only (automated/A2P sends
> would get the number carrier-blocked; 10DLC registration still in carrier review).
> v3 digest channel = **email via `core/transactional_email.py`** (Outlook Graph
> through Doug's connected account first, SMTP fallback). Audit also code-verified the
> forwarded-call claim and the StickyNote/PlannerTask choices. Full critique in
> memory/critique_latest.md.

## Problem

Doug runs the app from a mobile browser. Phone calls come in on busy days and there is
no time to find or create a customer. Notes get lost, callbacks get forgotten. The
planner exists but (a) there is no fast way to get a call note *into* it from a phone,
and (b) nothing ever resurfaces an unhandled note.

Call topology (Doug-described + code-verified in audit round 1, 2026-07-07; live-prod
timing check remains in PR 1):
- Calls to the business number route through Phone.com — **including calls Phone.com
  forwards to Doug's cell**. These fire the inbound webhook and land in
  `phone_com_calls` with caller-ID → customer matching already applied
  (`modules/phone_com/customer_resolver.py`).
- Calls dialed **directly to Doug's cell** never touch Phone.com and are invisible to
  the app. Capture must not depend on a call log entry existing.

## What already exists (reuse, don't rebuild)

| Piece | Where | State |
|---|---|---|
| Inbound call log w/ customer match | `modules/phone_com/webhook_router.py`, `upserts.py` | Live |
| Unmatched-caller queue ("Cold Leads") | `GET /api/phone-com/cold-leads`, `PhoneComColdLeadsView` | Live |
| Planner tasks (title, desc, due, status, priority, customer_id, job_id, assigned_to) | `PlannerTask` @ `models/tenant_models.py:1727`, `routers/planner.py` | Live, in mobile bottom nav |
| Phone normalize/hash/match helpers | `modules/phone_com/customer_resolver.py` | Live |
| In-app notifications (bell/drawer) | `routers/notifications.py`, `NotificationsDrawer.vue` | Live |
| Web push | `core/push_subscriptions.py`, `routers/push.py` | **Code only — NOT functional in prod.** No `VAPID_*` keys (sends no-op as `skipped_no_vapid`), no PWA manifest (iOS push requires home-screen install), subscribe CTA only on tech `MobileTodayView`. Do not build on this without funding all three. |
| Outbound SMS via Phone.com | `modules/phone_com/router.py:750` (`POST /api/phone-com/messages`) | **Code exists but NOT usable for automated sends** (Doug-confirmed 2026-07-07): integration is P2P-only; A2P/automated traffic risks the number being carrier-blocked. 10DLC registration in carrier review — revisit when approved. |
| Transactional email | `core/transactional_email.py` — tries user's connected **Outlook account via MS Graph** first, falls back to tenant SMTP | Live (Doug's Outlook connection powers the inbox view today) |
| Celery beat schedule | `core/scheduler.py::build_beat_schedule()` | Live |
| Global search palette (`/api/search`) | `CommandPalette.vue`, `routers/search.py` | Live but **keyboard-only** — unreachable on mobile |

Two parallel task systems exist (`PlannerTask` via `/api/planner`, `InternalTask` via
`/api/tasks`). **Decision: captures target `PlannerTask`** (the planner is the mobile
bottom-nav surface). Merging the two systems is out of scope — separate cleanup later.

## Design

### 1. Quick capture (mobile-first)

A capture button on the mobile bottom nav (center or replacing dead space in the 5-tab
bar — final placement during build) opening a bottom-sheet with:

- **One free-text box** (becomes task title; long text spills into description). This
  plus Save is the complete minimum flow — everything below is optional enrichment.
- **Optional phone field** — the PRIMARY link path. Live auto-match as you type/paste
  (normalize → hash → customer lookup, reusing `customer_resolver`); shows the matched
  name inline. No match = number is stored anyway. Needed for direct-to-cell calls,
  and honest about webhook timing (next bullet).
- **Recent inbound calls strip** — last ~5 inbound calls from `phone_com_calls`
  (number, matched customer name if any, minutes-ago, missed/voicemail badge). Tapping
  one links the capture to that call. Covers business-line calls *and* forwarded-to-cell
  calls (code-verified: `upserts.py` `normalize_status` handles real `dial_out +1XXX`
  forward payloads). **Timing caveat (audit round 1):** Phone.com sends call records
  at call *completion* with variable lag, and webhooks get missed (that's why the
  nightly reconcile exists) — the call you hung up on 10 seconds ago may not be in the
  strip yet. The strip is a convenience for capture-minutes-later; it is NOT the
  primary path and the UX must not imply the just-ended call will be there. A mobile
  browser cannot see the phone's own call state or call log (no web API exists), so
  there is no device-side alternative.
- Save → `PlannerTask` with `status=todo`, `due_date=today`, `assigned_to=creator`,
  `customer_id` auto-filled from the call row or phone match when available.

Target: **≤2 taps + typing, under ~10 seconds.** No customer form, ever, at capture
time. Creating the customer/lead properly happens later at a desk, from the task.

### 2. Data model (small)

Add to `PlannerTask`:
- `contact_phone` (String(40), nullable) — E.164, for direct-to-cell captures and
  unmatched callers.
- `phone_com_call_id` (String(80), nullable) — provenance link to the call row.
- `source` (String(20), nullable, e.g. `"quick_capture"`) — lets the needs-action view
  and digest distinguish captures if we ever want to.

Alembic migration required — additive, but the ADD COLUMN statements need
`IF NOT EXISTS`-style guards (the migration-010 pattern): on a fresh install
`create_orm_tables()` runs before alembic and already builds the new columns, so an
unguarded ADD COLUMN would fail there. (#41 removed the need for *table*-existence
guards, not column ones — audit round 1 catch.)

Task detail UI (mobile + desktop): when `contact_phone` is set and `customer_id` is
not, show **"Create customer from this"** → opens `CustomerFormDialog` pre-filled with
the phone; on save, backfill `customer_id` onto the task (and onto the matching
`phone_com_calls` rows — the cold-leads queue shrinks automatically since it keys off
`customer_id IS NULL`).

### 3. Backend endpoints

- `GET /api/planner/recent-calls` (or extend existing phone-com calls list with
  `direction=in&limit=5` if it already supports it — check at build time): thin
  read for the capture sheet. Must degrade gracefully when the Phone.com module is
  disabled (return empty list, hide the strip).
- `GET /api/customers/match-phone?phone=...` → `{customer_id, name} | null`, reusing
  `customer_resolver.match_phone_to_customer`. (Check whether the CustomerFormDialog
  duplicate-warning endpoint already does this — reuse if so.)
- `POST /api/planner/tasks` — accept the three new optional fields; server-side
  auto-match: if `phone_com_call_id` given, copy its `customer_id`; else if
  `contact_phone` given, run the matcher.

### 4. Follow-through (the part that makes it stick)

- **Needs-action default view** on the mobile planner: open (`status != done`) tasks,
  overdue + today first, oldest-first within a day. Captures never scroll away.
- **Morning digest via EMAIL** (channel history: web push killed in audit round 1 —
  not functional in prod, iOS tabs can't receive it; SMS killed by Doug 2026-07-07 —
  Phone.com path is P2P-only, automated sends risk carrier-blocking the number until
  10DLC clears). New Celery beat task, once daily ~7:00 AM: if there are open planner
  tasks for the recipient, send an email via **`core/transactional_email.py`**
  (Outlook Graph through the configured sender's connected account first, SMTP
  fallback) — subject like "GDX: 3 call notes open, 2 overdue" with task titles +
  a link to `/mobile/planner` — plus an in-app `Notification` row. The phone's mail
  app provides the device notification for free: no VAPID, no manifest, no
  home-screen install.
  - MVP recipient = a configurable digest email (env `PLANNER_DIGEST_EMAIL`,
    unset ⇒ no-op); sender identity = the owner's connected Outlook account.
    Per-user digests later if wanted.
  - Beat task must handle `(False, None, reason)` from the sender **loudly** (log +
    error-sink), not silently — the whole audit lesson is channels that no-op.
  - Nothing to send → send nothing (no empty digests).
  - This is the **first staff-facing scheduled reminder** in the app — new pattern,
    keep it minimal.
  - **Acceptance test: the digest email arrives in Doug's inbox on his actual
    phone.** A browser-viewport check structurally cannot verify a beat task.
  - Upgrades parked: SMS when Doug's 10DLC registration clears carrier review;
    web push only if ever funded fully (VAPID keys + PWA manifest +
    install-to-home-screen + subscribe CTA on the office nav).
- Cold Leads stays the backstop for calls nobody captured.

### 5. Same-pass bonuses

- **Mobile search button**: add a search icon to the mobile topbar dispatching the
  existing `gdx:open-command-palette` window event (`App.vue:86`). Makes the freshly
  wired global search usable on the phone at all.
- **StickyNote removal** (Doug-confirmed dead code 2026-07-07): delete model
  (`tenant_models.py:1013`), export in `models/__init__.py`, `/api/sticky-notes`
  routes in `routers/notes.py`, tests in `tests/test_notes.py`, reference in
  `tools/qa_tier1.py`, types in `frontend/src/types/api.d.ts`. Migration to drop
  `sticky_notes` (guarded `to_regclass`-style since baseline/ORM history varies).

## PR slicing

1. **PR 1 — Capture**: model columns + migration, endpoints, capture sheet + bottom-nav
   button, task-detail "create customer from this" backfill. (The core value.)
2. **PR 2 — Follow-through**: needs-action default view + morning digest beat task +
   email/notification wiring. Done = the email in Doug's inbox on his real phone.
3. **PR 3 — Cleanup**: StickyNote removal + mobile search button. (Independent,
   mergeable any time.)

Each PR: browser-verified on mobile viewport, light + dark, per the usual manifest
discipline. PR 1 verification: live check that a **forwarded-to-cell call** appears in
`phone_com_calls` on prod, and note the observed webhook lag (code-verified already;
this confirms behavior + timing with real data). PR 2 verification: the digest email
received on a real phone (see §4).

## Open decisions (defaults chosen — flag if wrong)

1. **Digest recipients**: MVP = one configurable digest email (Doug's) in settings,
   sent via the owner's connected Outlook account. Per-user digests — later if wanted.
2. **Digest time**: 7:00 AM. Beat schedules run in server time — confirm the container
   TZ vs shop-local at build time; hardcode the right cron hour first, make it
   configurable later.
3. **Capture due-date default**: today (so it's overdue tomorrow and gets loud).
   Alternative: no due date + pinned "unprocessed" section.
4. **Voicemail auto-capture** (auto-create a task from each voicemail w/ transcript
   snippet): parked — revisit after PR 2 ships; the digest's cold-leads count covers
   most of the value with zero noise risk.
5. **Bottom-nav placement**: add a center "+" vs. put capture inside the Planner tab
   header. Default: center "+" on the bottom nav (visible from every screen — the
   whole point is zero navigation).
