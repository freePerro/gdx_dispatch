# Time off and holiday pay — Plan

**Date:** 2026-09-23
**Status:** BUILT — every section below is implemented on `feat/time-off-requests` and browser-walked on a throwaway container 2026-09-23 (`frontend/e2e/time-off.spec.js`); PR #770 open, not merged, not released
**Ask (Doug, 2026-09-23):** "there is nothing that lets anyone put down a
vacation day or holiday pay." Shape decisions, all Doug's, same day:

1. A paid day off is a **timeclock entry type**, not a payroll-entry kind and
   not a promotion of the scheduling `tech_unavailability` rows.
2. **A tech can request it**; the office approves.
3. **Admin settings** get an area with the calendar options for holiday pay.
4. Whether paid time off counts toward overtime is a **per-company option**;
   for this shop it does **not**.

---

## 1. What already exists (do not rebuild)

| Piece | State | Where |
|---|---|---|
| Timeclock entries with a free-text `entry_type` (only `clock` and `manual` ever written) | shipped | `models/tenant_models.py` `TimeclockEntry`; `routers/timeclock.py` |
| Office Timesheets page: crew entries, per-tech cards, Type column, Add Entry dialog, submitted-day badge | shipped | `frontend/src/views/TimesheetsView.vue`, `components/TimeEntryDialog.vue` |
| Tech self-service timeclock (mobile + desktop) with a weekly timesheet and "Add missed shift" | shipped | `views/MobileTimeclockView.vue`, `views/TimeclockView.vue`, `composables/useWeeklyTimesheet.js` |
| Pay-period export: one hours authority, CSV + PDF + email built from the same `PeriodTimesheet` | shipped | `core/timesheet_hours.py`, `core/timesheet_export.py`, `core/timesheet_delivery.py` |
| Pay-period settings card in Settings → Feature Settings, saved through `PATCH /api/settings` | shipped | `views/SettingsView.vue` (~1114), `routers/settings.py` |
| Shop hours: `default_shift_start`, `default_workdays` bitmask (Mon=1 … Sun=64), per-user overrides on `users.shift_start` / `users.workdays` | shipped | `AppSettings`, `User` |
| `tech_unavailability` (vacation/sick/other, scheduling only) | API only, **no UI caller** | `routers/scheduling.py`, `routers/technicians.py` |
| In-app payroll summary with weekly overtime math | **orphan**: no frontend caller, reads job-level `time_entries`, revenue side 503s by design | `routers/payroll.py` |

Nothing links a day off to hours or pay today. The Pay Runs page is a shell
(its endpoints are `ui_compat` stubs and the page says so).

## 2. Decisions

### 2.1 Representation: a time-off entry is a closed timeclock entry

`entry_type` ∈ `{"vacation", "holiday"}`; `clock_in_at` is the person's shift
start on that shop-local day (their `users.shift_start` override, else
`AppSettings.default_shift_start`), `clock_out_at` = clock-in + paid minutes,
`minutes` = the paid minutes. **No new column on the timeclock table.**

Why not NULL stamps: `clock_out_at IS NULL` means "still clocked in" to
`/status`, the stale-shift sweep, the tech-locations lookup and the export's
`open_shift` flag. A synthetic closed span keeps every existing reader
honest: the Timesheets page buckets it into the right shop day, the weekly
timesheet shows it, the export lists it, and nothing flags it.

What changes for readers: break attribution (`break_minutes_by_entry`) must
skip time-off entries, or a lunch taken on a day that carries both a worked
shift and a posted holiday can land on the holiday row. The export gains a
type column and a time-off split (2.5).

### 2.2 Requests: a separate table, never a pending timeclock row

`time_off_requests` holds the workflow; the timeclock table holds only
approved paid time. A pending request in the timeclock table would need
every hours reader to filter a status column, and one missed reader pays a
day that was never approved.

Lifecycle: `pending` → `approved` | `denied` | `cancelled`;
`approved` → `revoked` (office; soft-deletes the entries it created, whose
ids the request records). Approval creates one entry per **workday** in the
range (the person's `users.workdays` override, else
`AppSettings.default_workdays`), skipping any day that already carries a
time-off entry for them, and reports what it skipped.

### 2.3 Office direct entry

The office can record time off without a request (a tech phoned it in):
`POST /api/timeclock/time-off/entries` with the same range → workday
expansion. The Timesheets Add Entry dialog gets a Type selector
(Shift / Vacation / Holiday) in office mode.

### 2.4 Holiday pay: a calendar the office posts from, never a background write

`AppSettings.holiday_calendar` is a JSON list of `{date, name, minutes}`.
Posting a holiday (`POST …/holidays/post` with the date and the people)
creates one `holiday` entry per chosen person and records who posted it.
There is no beat task: a payroll write by a system identity is exactly what
*Actions must be auditable* forbids, and the office choosing the people is the
only honest way to decide who is owed the day. The Timesheets page shows any
holiday in the displayed range that has no entries yet, with the Post button
right there, so the calendar cannot be forgotten.

### 2.5 "Counts toward overtime": read by the export

The app does not compute overtime for the bookkeeper and this plan does not
start (`core/timesheet_hours.py` line 30: "this shop's overtime is not a fact
the clock knows"). The setting's readers are the payroll artifacts a real
person uses:

- CSV: a `type` column on every row; `worked_hours` stays clock hours; new
  `time_off_hours`. **No overtime column** — `test_csv_has_no_money_columns`
  forbids the word in the header, on purpose: the CSV is an hours file.
- PDF and the emailed body: per-person worked / paid-time-off split, and one
  sentence — "Paid time off does not count toward overtime." (or "counts") —
  rendered only when the period holds time off.
- Timesheets page: per-card and summary totals split worked vs time off, and
  the same sentence under the Time off tile (read from
  `GET /api/timeclock/time-off/options`, open to any signed-in user, because
  a dispatcher cannot read `/api/settings`).

`routers/payroll.py` (orphan, job-level table) is left alone; ledgered.

## 3. Data

### 3.1 Migration `097_time_off`

- `time_off_requests` (new; `create_table` guarded by `has_table`):
  `id` String(36) pk, `company_id` String(36), `technician_id` String(36)
  (a USER id, the timeclock convention), `entry_type` String(20),
  `start_date` Date, `end_date` Date, `minutes_per_day` Integer,
  `notes` Text null, `status` String(20), `requested_by` String(36),
  `reviewed_by` String(36) null, `reviewed_at` DateTime(tz) null,
  `review_note` Text null, `entry_ids` JSON (default `[]`),
  `created_at`, `updated_at` DateTime(tz), `deleted_at` DateTime(tz) null.
- `app_settings` (guarded `add_column`): `holiday_calendar` JSON
  `DEFAULT '[]' NOT NULL`, `time_off_default_minutes` SmallInteger
  `DEFAULT 480 NOT NULL`, `time_off_counts_toward_overtime` Boolean
  `DEFAULT false NOT NULL`.

Existing rows: untouched. No timeclock row changes meaning; no money column
is added or altered. Rollback: `downgrade()` drops the three columns and the
table. A downgrade after approvals leaves the created timeclock entries in
place (they are ordinary closed entries) and loses only the request history
and the calendar.

### 3.2 Model

`TimeOffRequest` beside `TimeclockEntry`; three columns on `AppSettings`.

## 4. API (`routers/time_off.py`, prefix `/api/timeclock/time-off`, module `timeclock`)

| Route | Who | Audit action |
|---|---|---|
| `GET /options` | any signed-in user: types, default day length, workday mask, request limits, the overtime statement, shop timezone | — |
| `GET /requests?status=&all_technicians=` | self; crew with `all_technicians` for dispatch managers | — |
| `POST /requests` | any signed-in role except viewer (self); managers may name a `technician_id` | `time_off_requested` |
| `POST /requests/{id}/approve` | dispatch manager | `time_off_approved` (entry ids in details) |
| `POST /requests/{id}/deny` | dispatch manager | `time_off_denied` |
| `POST /requests/{id}/cancel` | the requester while pending; managers | `time_off_cancelled` |
| `POST /requests/{id}/revoke` | dispatch manager, approved only | `time_off_revoked` (entries soft-deleted) |
| `POST /entries` | dispatch manager | `time_off_entries_created` |
| `GET /holidays?start=&end=` | any signed-in user | — |
| `POST /holidays/post` | dispatch manager | `holiday_posted` |

Settings (`holiday_calendar`, `time_off_default_minutes`,
`time_off_counts_toward_overtime`) ride the existing `GET`/`PATCH
/api/settings` (admin/owner) with validation and the existing
`settings_updated` audit row.

Gates are `require_role` dependencies, so the authz permission ratchet
counts them as authorized rather than growing its baseline. Self-service
limits mirror the manual-shift rules: a note is required, the range may not
start more than 14 days back or run more than 60 days, and a day may not
exceed the default day length.

`PATCH /api/timeclock/entries/{id}` refuses a non-manager edit of a
time-off entry: the tech changes it by cancelling and re-requesting, not by
stretching an approved day.

## 5. UI

- **Tech (mobile `/mobile/timeclock`, and desktop `/timeclock`)**: a "Time
  off" section with Request time off (bottom-sheet dialog: dates, hours per
  day, note) and My requests (status tags, cancel while pending). Week view
  tags vacation/holiday days.
- **Office (`/timesheets`)**: a Time off requests card (pending first,
  approve / deny with a note, revoke on approved), holidays in range that are
  not posted yet with a Post button, Type column values, totals split.
  Add Entry dialog gains Type → Vacation/Holiday with date range + hours.
- **Admin (Settings → Feature Settings)**: "Time off and holidays" card: default
  hours per day off, "Paid time off counts toward overtime", holiday table
  (date, name, hours, posted count, Post, Remove) with Add row.

## 6. Deliberately not built (Doug to rule if wanted)

- A `sick` type. The type list is one constant; adding it is a one-line
  change plus a label.
- Overtime math anywhere. See 2.5.
- Automatic holiday posting by a scheduled task.
- Touching the orphan `routers/payroll.py` summary.
- Any change to `tech_unavailability`; a PTO approval does not create a
  scheduling block. Ledgered as a product question.

## 7. Tests (as built)

- `test_migration_097_time_off.py`: chain, SQLite rerunnable and reversible
  with seeded rows kept and defaults populated, fresh-install path; Postgres
  arm under `DATABASE_URL` (CI).
- `test_time_off_core.py`: workday expansion (bitmask, per-user override,
  empty mask), stamps across the 2026-03-08 DST change and an evening shift
  start, idempotent creation, calendar validation.
- `test_time_off_router.py`: request → approve writes exactly the workday
  entries and one audit row; deny / cancel / revoke (revoke leaves a worked
  shift on the same day alone); tech vs manager vs viewer gates; range,
  backdate and overlap refusals; office direct entry idempotent; holiday post
  skips people already holding the day; `PATCH /entries/{id}` refuses a tech
  on a time-off row; `/options`.
- `test_time_off_export.py`: CSV split and header words, break attribution on
  a holiday-plus-shift day, PDF context and email sentence on/off, a real
  WeasyPrint render, settings round trip and refusals.
- Frontend: `TimeOffRequestPanel.spec.js` (panel + dialog),
  `HolidayPostDialog.spec.js`, `TimeEntryDialogTimeOff.spec.js`,
  `TimesheetsTimeOff.spec.js`, `SettingsTimeOff.spec.js`,
  `useWeeklyTimesheetTimeOff.spec.js`. AppBottomNav untouched: the tech's
  way in is the existing Clock tab.

## 8. Verification

Throwaway container, real browser (Playwright MCP): tech requests on the
Pixel viewport, office approves on desktop, the entries appear on
Timesheets with the split totals, the CSV carries the new columns, a
holiday is posted from Settings and again from the Timesheets notice; light
and dark. Then the prod walk after release.
