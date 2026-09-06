# Tech weekly timesheet — self-service week view + own-entry corrections

**Date:** 2026-08-13 · **Status:** RELEASED v1.54.0 — G1 through G5 all on main (verified 2026-08-21): `timeclock.py:691` `include_breaks`; `:35 SELF_SERVICE_EDIT_WINDOW_DAYS = 14` + `:166 _enforce_self_service_limits` (422 without a note, two-sided window, dispatch/admin exempt); the shared `useWeeklyTimesheet` + `TimeEntryDialog.vue`; `TimeclockView.vue:184` My Timesheet; `MobileTimeclockView.vue:167` week section. _This header read "PLAN — nothing built" for shipped work until 2026-08-21._
**Ask (Doug):** "For each user … on the time clock area a time sheet for each week and a
way to edit the hours if need be like they forgot to clock in or out … have a week total
for them to see. The base for this should be there."

The base IS there. The office Timesheets page (v1.49.0, PR #302) already carries the
edit/add dialog, the shop-timezone machinery, and the break netting; the backend already
permits a tech to edit **their own** entries. This plan puts a navigable weekly timesheet
with a week total and self-correction onto the tech's own Time Clock pages (desktop
`/timeclock` and mobile `/mobile/timeclock`), reusing that machinery instead of
reinventing it.

---

## 1. What already exists (verified in code 2026-08-13)

### Backend — `gdx_dispatch/routers/timeclock.py` (needs almost nothing)

| Capability | Where | Note |
|---|---|---|
| List own entries in a date range | `GET /api/timeclock/entries?date_start&date_end` | Defaults to caller's own id (`_resolve_tech_id`); self path is deliberately uncapped |
| Add a missed shift | `POST /api/timeclock/entries` | Non-dispatch caller may only create for **self**; `entry_type="manual"`; audit-logged |
| Correct in/out times | `PATCH /api/timeclock/entries/{id}` | Own entry OR dispatch/admin; recomputes `minutes`; rejects out ≤ in; audit-logged (`timeclock_entry_updated`) |
| Break netting | `_break_minutes_by_entry()` | **Only populated on the `all_technicians=true` (office) path today** — gap #1 below |
| Auto-closed unknown shifts | `minutes = NULL` + note | PATCH with a real end time is exactly how these get fixed; the self view must render them honestly ("Unknown"), same as the office page |

### Frontend

- **`TimesheetsView.vue` (office)** — the donor. Contains:
  - the entry dialog (the app's ONLY `PATCH /entries` caller), incl. the `wasOpen` /
    never-reopen-a-closed-shift guard and the "say why, it's audit-logged" notes hint;
  - shop wall-clock conversion (`zoneOffsetMs`, `toShopWallClock`, `shopWallClockToIso`,
    `shopDayKey`) — display, picker input, and day bucketing all in tenant timezone;
  - Monday-based `startOfWeek` ("what a payroll period looks like");
  - the ±1-day fetch slack trick (server buckets by UTC day; boundary re-applied client-side
    in shop time — 2 of 39 real prod rows cross the UTC day line).
- **`TimeclockView.vue` (desktop self)** — has a "Weekly Summary" card + week totals already,
  but it is the part being **replaced**, because it has three real defects:
  1. **Sunday-based week**, disagreeing with the office page's Monday payroll week;
  2. buckets days by browser-local/UTC date string (`clock_in.split('T')[0]`), not shop time;
  3. "Break Hours" filters `entry_type === 'break'`, which never exists (`entry_type` is
     `clock`/`manual`; breaks live in their own table) — the column is **always 0.00**, and
     Net Hours = Work − 0 is fiction.
- **`MobileTimeclockView.vue` (mobile self)** — today-only card list, no week view, no editing.
  Mobile users are redirected `/timeclock → /mobile/timeclock` (router line ~512), so **without
  mobile parity the techs — the actual audience — never see any of this.**
- Routes/gates: `/timeclock` = any authenticated user; `/timesheets` = `scheduling.write`.
  No new gates needed — the backend enforces self-only.

---

## 2. Gaps to close

### G1 — `break_minutes` on the self path (backend, small)
`GET /entries` only nets breaks for the office (`all_technicians=true`). A tech's week total
computed without it pays out every lunch and **disagrees with the office page for the same
week** — the exact two-pages-two-answers bug class this repo keeps re-fixing.
**Fix:** add `include_breaks: bool = False` query param; when true, run the existing
`_break_minutes_by_entry()` on the self path too. Opt-in, so the existing self callers
(TimeclockView/MobileTimeclockView today-lists) are byte-identical unaffected.

### G2 — self-edit policy (backend, decision D2)
The API already lets a tech edit their own rows with no note and no time limit — it's just
never been exposed in UI. Before giving it a button:
- **Require a non-empty `notes` on PATCH/POST when the actor is not dispatch/admin** (server
  side, not just frontend). The office dialog already preaches "say why"; for self-service
  it should be enforced — the note is the tech's attestation of why the clock record changed.
- **Recommended: limit self-edits/creates to entries whose clock-in is within the last 14
  days** (dispatch/admin unlimited). Older weeks are paid weeks; retroactive self-service
  rewrites of them should go through the office. 422 with a plain message
  ("older shifts are corrected by the office").

### G3 — shared components (frontend refactor, no behavior change)
- Move `zoneOffsetMs` / `toShopWallClock` / `shopWallClockToIso` / `shopDayKey` out of
  `TimesheetsView.vue` into `composables/useTenantTimezone.js` (where `dateKeyInZone`
  already lives).
- Extract the entry dialog into `components/TimeEntryDialog.vue`:
  - office mode: roster picker for new entries (as today);
  - self mode: technician fixed to the caller, no roster fetch needed (the self page needs
    no names at all — it's all "me");
  - keep the `canSave` rules verbatim, especially **never let a closed shift be saved back
    to open** and the `showOnFocus=false` DatePicker fix (both were browser-found).
- `TimesheetsView.vue` consumes both — proof the extraction didn't change behavior.

### G4 — desktop "My Timesheet" card (replaces "Weekly Summary")
In `TimeclockView.vue`:
- **Week navigation:** `‹ prev · This week · next ›` (next disabled beyond the current
  week), Monday-based, computed from **shop-time today** — same helpers as the office page.
- **Day rows Mon→Sun** (skip future days, keeping the existing "don't render days that
  haven't happened" rule), each day showing its entries (In / Out / worked) with a pencil
  on each row → `TimeEntryDialog` in self mode. Auto-closed rows render the same
  "Unknown — set the real end time" tag the office sees; an open row on a **past** day is
  the "forgot to clock out" case and the dialog's `wasOpen` path already handles it.
- **"Add missed shift" button** → dialog in create mode (self), defaulting to the viewed
  week (reuse the office 8am–4pm anchor default).
- **Week total footer:** Worked (net of breaks) + Breaks, from `minutes − break_minutes` —
  the same arithmetic as the office `workedMinutes()`, so the two pages agree to the digit.
- **Data:** a *separate* `weekEntries` fetch (`date_start/date_end` = viewed week ±1 day
  slack, `include_breaks=true`), filtered by `shopDayKey`. Do NOT reuse the existing
  `entries` ref: Today's Entries + the clock card feed off it, and navigating to a past
  week must not blank today's list.

### G5 — mobile parity
`MobileTimeclockView.vue`: a "This Week" section under Today's Entries — same week nav and
week total, card-list style (no DataTable, per the file's own rule), tap an entry → the same
dialog (it's a PrimeVue Dialog; verify usability at 390px in the walk — big targets, and the
DatePicker time panel behavior that bit the office dialog). Same `weekEntries` fetch.

---

## 3. Build order

Two PRs (stacked or sequential; repo practice = bottom-up merge):

1. **PR A — backend + refactor (no visible change):**
   `include_breaks` param (G1) · self-edit note requirement + 14-day window if D2 approved
   (G2) · helper move + `TimeEntryDialog.vue` extraction with TimesheetsView consuming it
   (G3) · tests below.
2. **PR B — the feature:** desktop card (G4) + mobile section (G5) + frontend tests +
   browser walk.

No migrations. No new tables, columns, or gates.

## 4. Tests + verification

- **Backend (pytest, docker-app image per the no-prod-DB harness):**
  - `include_breaks=true` on self path returns netted rows; omitted → field absent
    (existing-caller contract);
  - non-dispatch PATCH/POST without notes → 422; with notes → 200 (if D2 adopted);
  - self PATCH on an entry older than the window → 422; dispatch on same → 200;
  - existing suites (`test_timeclock_office_timesheets.py` etc.) stay green — the office
    path must be untouched.
- **Frontend (vitest):** week bucketing by shop day across the UTC-evening boundary (reuse
  the real prod case: 10:02 PM CDT files under the right day); Monday week start; future-day
  skip; week total = Σ(minutes − break_minutes)/60.
- **Browser walk (verifyplaywright, throwaway container, both themes):** clock in/out →
  entry appears in this week; edit an entry's out-time → week total moves; add a missed
  shift on yesterday → appears with `manual` tag; prev/next week nav; confirm the *office*
  page shows the tech's correction with the same worked figure. Mobile viewport pass for
  the dialog. (Known env trap: local dev DB stuck at migration 045 — use the throwaway
  container path, not the resident dev stack.)

## 5. Decisions for Doug

- **D1 — Week start.** Recommend **Monday** everywhere (office page already chose it as the
  payroll week; the desktop card's Sunday week is one of the defects being replaced). Say if
  payroll actually runs Sun–Sat and both pages will follow.
- **D2 — Self-edit guardrails.** Recommend **note required + 14-day window** (server-enforced,
  office exempt). The permissive alternative (what the API silently allows today) is a
  payroll-integrity hole the moment it gets a button.
- **D3 — Office visibility of self-edits.** Recommend **no new review queue**: every change
  is already audit-logged with actor + note, manual entries carry the `manual` tag on the
  office page, and the exceptions card stays reserved for real anomalies (wallpaper rule).

## 6. Explicitly out of scope (adjacent, noted while reading)

- `POST /submit-day`'s docstring claims it stamps `submitted_for_payroll_at`; the code
  aggregates and stamps nothing, and nothing locks entries after "submit". Editing a
  "submitted" day is therefore possible and unflagged — real, but it's payroll-workflow
  design, not this feature. (Also a live comment-drift hit for the scanner.)
- Payroll table linkage: this remains the CLOCK record; the office page's scope note
  ("payroll figures are kept separately") applies verbatim to the tech view — carry a
  one-line version of it onto the new card.
- Break self-editing: the dialog edits clock times only (office limitation too). A tech
  who forgot to end a break still needs the office. Follow-up if it ever bites.
