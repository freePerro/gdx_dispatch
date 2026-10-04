# Multi-day jobs: one job, many visit days

**Date:** 2026-10-03
**Status:** PLAN. Nothing is built. Three PRs are proposed (§6). Doug ruled all decisions on 2026-10-04 (§8) and moved the day's stop into the closeout sheet (§5.4). The first draft was audited 2026-10-04 (§10); this revision has not been re-audited.

**Trigger:** Doug asked, "What happens if a job is not finished and turns into a
multi-day job? Can a tech or anyone go back to it?" The answer, traced on
2026-10-03, was: only by accident. Doug then ruled, in the same conversation:

- **One job, many days.** A job keeps one number and gets a visit for each
  day. It is billed once.
- **A "Done for today" button on the phone** (revised 2026-10-04, below). It records the day's work and
  leaves the job open.
- **Multi-day support is in phase.** `PHASE.md` was amended the same day:
  UI and workflow fixes count as hardening.

On 2026-10-04 Doug revised the second ruling: the closeout sheet itself asks
**"Is this job finished?"** instead of a separate button. "No" saves the day
as a daily log entry on the same job. The logs are internal: in the end it is
one job, and the customer sees one invoice (§5.4, §8).

Related plan: `job-closeout-billing-visibility-plan.md` §A6, "Multi-tech
and multi-visit jobs will underbill", which is still unresolved. This plan
resolves A6's *multi-visit* half (§5.5). Its *multi-tech* half (per-tech hours
at closeout) is touched only where a day attestation is per tech. A6 now
points back here.

---

## 1. What happens today (verified 2026-10-03, main @ 55605736)

A tech who can't finish has two options, and neither fits:

1. **Leave the job open.** The job keeps yesterday's `scheduled_at`.
   - It drops off the tech's Today screen. Today is date-only
     (`routers/mobile.py:1172-1219`), and the "in the area" list requires
     *no* date (`:1224-1241`).
   - It resurfaces only on the Dispatch "late open" card
     (`routers/dispatch_scheduling.py:170-200`) until the office drags it to
     a new day.
   - The phone won't take it forward on day 2. `dispatch_status` is still
     `on_site`, and "On my way" returns 400 because transitions are
     forward-only (`mobile.py:157-201`).
2. **Close out and tick "needs a return visit."**
   - The job is marked **completed**, and an invoice is auto-drafted from
     day 1 (`routers/jobs.py:2514-2517`, `:2679`).
   - A child job is created with `parent_job_id`. The job page labels that
     child a **callback** (`jobs.py:3518-3543`, "warranty cost vs new
     revenue"), and the operations report counts it against first-time fix
     (`routers/reports.py:1360-1382`).

The office can't book Mon–Wed up front either:

- The board places a job by `Job.scheduled_at` alone
  (`DispatchView.vue:1171-1180`).
- `_sync_job_appointment` keeps **one appointment per (job, tech)** and
  soft-deletes any second one on the next edit (`jobs.py:466-474`).
- AppointmentsView already redirects job-linked bookings away for exactly
  that reason (`AppointmentsView.vue:555-569`).

Prod (read-only, 2026-10-03): 45 live appointments; 1 job has ever had more
than one live appointment (completed); 0 jobs `in_progress`.

## 2. Prior art (searched 2026-10-03)

- **Jobber** puts a one-off job's visits on one job, "invoiced once the last
  visit is complete". You pick a date range (one visit per day) or specific
  days, up to 20 visits. Revenue is attributed to the final visit.
  https://help.getjobber.com/hc/en-us/articles/115009379047-Create-a-One-Off-Job
- **ServiceTitan** recommends booking one job "and add an appointment for
  each day of work". Each new appointment copies the previous one with the
  next work day preselected. Its alternative is **Pause** at the end of each
  day, after which the office reschedules.
  https://help.servicetitan.com/how-to/how-to-handle-multiday-jobs ·
  https://help.servicetitan.com/residential-s-r/docs/pause-multi-day-jobs

Both match Doug's ruling. This plan builds the same shape on our tables
because the app is self-hosted and single-tenant, and there is nothing to
buy.

## 3. What already exists (do not rebuild)

| Need | Already there | Where |
|---|---|---|
| A visit row per tech per day | `appointments`: `job_id`, `tech_id`, `start_at`/`end_at`, `status` (scheduled/confirmed/en_route/arrived/completed/cancelled), `en_route_at`/`arrived_at`/`completed_at`, soft delete. No uniqueness on (job, tech). | `models/tenant_models.py:1605-1642`, `routers/appointments.py:40-47` |
| Visit CRUD and state, audited | POST/PATCH/DELETE `/api/appointments`, `/{id}/on-my-way`, `/arrived`, `/complete`, `/cancel`; every one calls `log_audit_event_sync` | `routers/appointments.py:414-725`, `:255-281` |
| Today shows a job on each visit day | Today merges appointments for the tech's local day first, then dated jobs | `mobile.py:1171-1219`, card time `:973-979` |
| Per-day parts | Live parts capture writes one row per event (`source='mobile'`); closeout never replaces them; the final invoice picks up every unbilled row | `mobile.py:3228`, `core/closeout_billing.py:289-319`, `tests/test_parts_used_live_capture.py` |
| Per-day notes | Timestamped, attributed `JobNote` rows | `mobile.py:2715`, `tenant_models.py:1268` |
| Per-day photos | `job_photos` with kind `progress` | `core/job_photos.py:28` |
| Explicit per-day labor rows | Office labor entry with its own `clock_in`/`clock_out`; costing sums every row | `routers/labor.py:245`, `job_costing.py:182-189` |
| An end-of-day attestation pattern | `POST /api/timeclock/submit-day`: an audited attestation that writes no rows | `routers/timeclock.py:1655-1712` |
| Bill once, net deposits | Final invoice nets paid deposits through `apply_deposits_to_final` | `modules/deposits/service.py:391-470` |
| A queue for "needs a date" | Dispatch late-open card; Dashboard return-visits queue | `dispatch_scheduling.py:170`, `jobs.py:3263` |

## 4. What blocks it (each item must change)

| # | Blocker | Where |
|---|---|---|
| B1 | The sync keys on (job, tech), so a second-day appointment for the same tech is retired on the next job edit. Which duplicate survives depends on unordered row order. | `jobs.py:460-479` |
| B2 | Arrival loads *the* appointment with `scalar_one_or_none()`, with no tech or day filter. Two live appointments raise `MultipleResultsFound`. This **already applies to any multi-tech job**: latent today (0 hits in 30 days of logs, 1 job ever had more than one appointment), certain under this plan. | `mobile.py:2242-2247` |
| B3 | `dispatch_status` only moves forward, so day 2 can't go back to en route | `mobile.py:157-201` |
| B4 | En route and arrival stamp JobAssignment only when the column is NULL, so they record day 1 only | `routers/job_assignments.py:338-343` |
| B5 | The board and its lanes read `Job.scheduled_at` only, so a 3-day job shows on one day | `DispatchView.vue:1171-1180`, `dispatch_scheduling.py:60,129,194` |
| B6 | A closeout keeps **one** `hours_worked` per job. A re-closeout restates it, other open timers close at 0, and `_close_labor_entry` never moves `clock_in`. | `jobs.py:2395-2460`, `:1901-1929` |
| B7 | The phone never sets `in_progress` (en route and arrival write `dispatch_status` only) | `mobile.py:2114-2117`, `:2227-2233` |

## 5. Design

### 5.1 Model: a visit is an appointment row

- A **visit** is one `appointments` row for one tech on one shop day. A
  three-day, two-tech job has six rows.
- No new table.
- **Per-tech, per-day state lives on the visit row, not the job.**
  `Job.dispatch_status` is one column per job (`tenant_models.py:373`), and
  `JobAssignment` holds only first-day stamps. The appointment row already
  carries `status`, `en_route_at`, `arrived_at` and `completed_at` per tech
  per day, so it becomes the source for "where is this tech on this job
  today". No migration is needed.
- **`Job.dispatch_status` becomes a roll-up**, not a per-tech gate:
  - `on_site` if any tech's visit today is arrived;
  - `en_route` if any is en route;
  - `assigned` once every tech on today's visits has closed the day;
  - `done` only at closeout.

  One tech's daily log never resets a job another tech is still
  working.
- **`Job.scheduled_at` stays a stored column with one rule:** it equals the
  start of the earliest *open* visit (not completed or cancelled), or the job
  has no visits. That keeps the board, late-open, Today and reports working
  off one column, and old readers stay correct for single-day jobs.
- **Every writer is enumerated and routed through one helper.** A single
  helper, `recompute_job_schedule(db, job)`, enforces the rule. These writers
  call either it or the scoped sync:

  | Writer | Where |
  |---|---|
  | create, update | `jobs.py:1021`, `:1294`, through the sync |
  | uncomplete | `jobs.py:4208` |
  | reactivate | `jobs.py:4272` |
  | mobile route reorder | `mobile.py:1487` |
  | public API PATCH | `api/public_router.py:546` |
  | the visits API, appointment PATCH and DELETE, the daily-log closeout | new |

  A writer missing from this list keeps a second source of truth, so each
  one gets its own test in PR 2.

### 5.2 The sync stops eating other days (fixes B1)

- **Closed visits are history.** The sync's appointment query excludes
  `status IN ('completed', 'cancelled')`. Today it loads every live row
  regardless of status and rewrites `start_at` in place (`jobs.py:446-489`).
  Without this exclusion, rescheduling a job whose only visit was closed for
  the day would drag day 1's record to the new date.
- **Only the primary day's open visits move.** `_sync_job_appointment`
  touches only open visits on the same shop day as the job's
  `scheduled_at` before the edit. Open visits on other days are left alone.
  If no open visit remains on that day (every day so far is closed), the sync
  **inserts** new visit rows for the new date and never moves a closed one.
- **Collisions.** Moving the primary day onto a day where the same tech
  already has an open visit for this job merges into the existing visit:
  the existing row survives and the moved one is retired, with an audit row.
  It never leaves two visits for one tech on one day.
- When `scheduled_at` is cleared, it still retires every *open* visit
  (today's "unschedule" meaning). Closed visits stay.
- Duplicate cleanup applies only within one (tech, day, open).
- **Tests:**
  - a 3-day job survives a title edit, a tech change, a time change and a
    **date move** on day 1;
  - a completed visit is never moved or retired;
  - rescheduling with no open visit inserts and doesn't move;
  - a collision merges;
  - clearing the date retires only open visits.

### 5.3 Office: book and move days

- **API:**
  - `POST /api/jobs/{id}/visits` accepts `{days: [date…] | {from, to,
    skip_weekends}, start_time, duration_minutes, tech_ids}`. It creates one
    appointment per tech per day, recomputes `scheduled_at`, and audits each
    visit as `visit_added`, with job, day and techs.
  - `GET /api/jobs/{id}/visits`.
  - Moving or removing a single day reuses `PATCH`/`DELETE
    /api/appointments/{id}`. Both are already audited, and both now
    recompute `scheduled_at`.
- **Job page:** a **Visits** card lists Day 1…N with techs, time and state
  (booked / on site / done for the day). It has "Add day(s)", the same
  shape as ServiceTitan's "next work day preselected".
- **Dispatch board (fixes B5):** loads appointments for the visible window
  alongside jobs. A multi-day job shows on each visit day with a "Day 2 of 3"
  badge. Dragging a card moves **that visit only**. Single-visit jobs keep
  today's path (the job-level patch).
- **Late-open card:** a job whose last visit was closed for the day and that
  has no later visit gets a **"Continuing — book next day"** label. It keeps
  the same card and doesn't start a new queue.

### 5.4 The closeout sheet asks "Is this job finished?" (fixes B2, B3, B4, B7)

- **Arrival (B2):** finds the appointment by `job_id` + this tech + today's
  shop day, not `scalar_one_or_none()` over the whole job. This fix ships
  first, in PR 1, because it also guards today's multi-tech jobs.
- **Starting the day (B7):** en route or arrival on a `scheduled` job sets
  `lifecycle_stage='in_progress'` and `started_at` if null. This also closes
  the "phone never reaches In Progress" gap from the 2026-10-03 lifecycle
  audit.
- **One sheet, one question.** `MobileJobCloseoutDialog` opens with
  **"Is this job finished?"** (Yes / No) before anything else. There is no
  separate button. The sheet is already mounted on the phone
  (`MobileJobDetailView.vue`) and the dispatch board (`DispatchView.vue`),
  and #842 (open) mounts it on the desktop job page, so the question reaches
  every surface that can close out.
  - **Yes:** today's closeout, unchanged — completion, autodraft invoice,
    the tenant's requirement gates.
  - **No:** a **daily log** entry for this tech on this job. The job stays
    open. Nothing is shown or sent to the customer (Doug, 2026-10-04: the
    logs are for us). The "No" sheet asks for:
    - **Hours worked today**, required and attested, for this tech (Doug,
      2026-10-04). Billed labor comes from attested hours only (CLAUDE.md
      domain rule). This holds even on a tenant whose
      `require_hours_on_complete` is off.
    - A note for the office. Optional, but encouraged ("what's left").
    - Parts, optional. Parts and photos go through the existing live
      capture. The `require_parts_on_complete`, signature and invoice
      gates apply to the final "Yes" only.
  - The job page lists the daily log entries by day (tech, hours, note),
    for the office.
- **The day's hours go on the timer that already exists.** Arrival already
  opens a per-tech job timer in `time_entries` (`mobile.py:2270-2285`;
  `user_id` = the tech, `clock_in` = today's arrival). A "No" closeout
  **closes that timer** with `duration_minutes` = the attested hours and
  labels it a day row (§8 D2). It does not create a second row.
  - **Pay period:** `clock_in` is today, so payroll (`payroll.py:245-262`
    sums closed rows by `user_id` and `DATE(clock_in)`) files the hours on
    the day they were worked. Day 2's arrival finds no open timer and opens
    a fresh one dated day 2, which removes the hazard of day 3's hours
    landing on a day-1 `clock_in`.
  - **Paid time:** this adds no new category. Payroll exposure is identical
    to today's single-day closeout, which also attests onto this same timer
    (`jobs.py:2397`). Whether payroll should count job timers at all is a
    pre-existing question, out of scope (§9).
  - **No timer open** (arrival was skipped): the endpoint creates the row
    with `clock_in` = start of today's visit, `user_id` = the tech, and
    audits that it did so.
- **On submit:**
  - The tech's visit for today becomes `status='completed'` with
    `completed_at` set.
  - The tech's timer closes as above.
  - The job stays `in_progress`.
  - `Job.dispatch_status` is re-rolled-up (§5.1). It returns to `assigned`
    only when **no** tech still has an open, arrived visit today.
  - The phone's day-2 gate (B3) reads **this tech's visit row for today**:
    "On my way" is allowed when today's visit is `scheduled`/`confirmed`,
    whatever the job-level status. It still never goes backwards within a
    visit.
  - En route and arrived stamp the appointment row for (job, tech, today)
    (B4). The JobAssignment first-day columns stay as they are, for legacy
    readers.
  - `recompute_job_schedule` runs: `scheduled_at` advances to the next open
    visit, or the job is flagged as continuing if there is none.
  - **No invoice, no closeout, no completion.** Audited as `job_day_closed`
    with job, visit, tech, hours, timer row id, note and next visit.
- **On the last day, the tech answers "Yes"** and gets the normal closeout.
- **The return-visit checkbox stays, relabelled, and only on "Yes"** (Doug,
  2026-10-04: keep it but label it differently). It still creates a
  separate job (`MobileJobCloseoutDialog.vue:853-858`), so it must stop
  reading as the way to continue unfinished work. It shows only after
  "Yes", for genuinely new work. Proposed label: **"Needs a follow-up job
  (new work)"**, with the hint "Not finished? Answer No above instead." The
  exact wording is Doug's to confirm at PR 3 review.

### 5.5 Billing: once, at final closeout (resolves A6's multi-visit half)

- **Install lane** (flat matrix price): days don't change the price. The
  final closeout bills as today.
- **Hourly lane:** billing today reads **only** `closeout.hours_worked`
  (`core/closeout_billing.py:240`) and never touches `time_entries`, so
  "sum the days" needs an explicit mechanism. Two candidates (D1, ruled (a)):
  - **(a) Recommended: a separate "Previous days" labor line.**
    `build_closeout_lines` gains one read: the job's day rows (§8 D2),
    summed as **man-hours** across techs and days. They are emitted as their
    own `service_labor_line` with `techs_on_site=1` (the rows are already
    per tech, so multiplying again would overbill). The existing
    final-day line (`hours_worked × techs_on_site`) is unchanged.
    - The customer sees two labor lines, and the invoice shows where every
      hour came from.
    - Re-closeout follows the existing rule: an untouched autodraft is
      rebuilt (`release_untouched_autodraft`); an already-touched or issued
      invoice is not restated (A7 in the related plan).
  - **(b) A running total.** The final closeout's `hours_worked` is
    pre-filled with the day sum for the tech to confirm. No change to
    `closeout_billing`, but it carries A6's failure mode: one edited number
    hides the breakdown.
- Either way, closeout's labor-row picker (`jobs.py:2395-2436`) must skip
  day rows, so they are never restated or closed at 0.
- **Not yet traced:** hourly multi-day jobs that have an **accepted
  estimate** skip the autodraft (`closeout_billing.py:499-507`) and go
  through the office estimate→invoice path. PR 3 must check whether that
  path reads closeout labor at all before claiming those jobs bill
  correctly.
- **This changes money math.** D1 was ruled (a) on 2026-10-04.
- **Parts:** unchanged. Every unbilled live-captured row across all days is
  picked up.
- **Deposits:** unchanged. Netted on the final invoice.
- **What happens to existing rows:** none change. Existing jobs have at most
  one visit per tech, no day rows, and bill exactly as today. There is no
  backfill.

### 5.6 Audit trail

Who, what and when are recorded at every step:

- `visit_added` and the existing `appointment` update, delete and cancel
  rows: who booked, moved or removed which day.
- `job_day_closed`: who stopped for the day, the hours attested, what's left.
- `job_closeout`: unchanged.

A three-day job can be reconstructed day by day from `audit_logs` plus the
appointment rows.

## 6. PRs (stacked, merged bottom-up)

1. **Visits survive and arrival is safe (B1, B2).** Covers §5.2 and the
   arrival lookup.
   - No UI and no migration.
   - It fixes a latent crash on multi-tech jobs by itself, so it is worth
     shipping even if the rest waits.
   - Tests: the sync tests from §5.2, including the date move,
     completed-visit, insert-not-move and collision cases; arrival with two
     techs on one job; arrival with a visit on another day.
2. **The office books days (B5).** Covers §5.3: the visits API, the Visits
   card, the board showing each day, `recompute_job_schedule` wired into
   every writer in §5.1 (one test per writer), and the continuing label.
3. **"Is this job finished?" and summed billing (B3, B4, B6, B7).**
   Covers §5.4 and §5.5.
   - D2 picked the column, so this PR adds an Alembic migration
     (`time_entries.appointment_id`, nullable) that runs on SQLite and
     Postgres and has a downgrade. Existing rows stay NULL.
   - If #842 has not merged by then, the desktop surface waits for it.

Each PR updates this doc's status line in the same commit (CLAUDE.md, "The
status line ships with the code").

## 7. Verification (per PR)

- Full matrix through `run_tests_split.sh`, with every FAIL and SKIP
  enumerated, and the ruff ratchet checked against the baseline.
- A throwaway container plus a real browser:
  - **Office (desktop):** book a 3-day, 2-tech job, see it on three board
    days, move day 2.
  - **Tech (Pixel 8 AVD):** day 1 On my way → I'm here → Closeout, "No",
    hours; day 2 shows on Today, On my way works; day 3 Closeout, "Yes".
  - **Office (desktop):** answer "No" from the job page; the daily log shows
    on the job; the relabelled follow-up option appears only after "Yes".
  - **Invoice:** one invoice, every day's parts, labor per D1.
  - Light and dark mode.
- After deploy, walk it on prod with a real job.

## 8. Decisions (ruled by Doug, 2026-10-04)

| # | Question | Ruling |
|---|---|---|
| D1 | Hourly-lane labor on a multi-day job: (a) a separate "Previous days" invoice line built from the attested day rows, or (b) the final closeout's hours pre-filled with the day total? | **(a) Separate line** (recommendation accepted). |
| D2 | How a day row is marked: a nullable `time_entries.appointment_id` column, or a note label? | **The column** (recommendation accepted). |
| D3 | After a "No" day with no next day booked: the office books it, or the tech picks the day? | **The office books it** (recommendation accepted). |
| D4 | Does the customer get an "on my way" text each day? | **Yes, each day** (recommendation accepted). |
| D5 | Can the crew differ by day? | **Yes** (recommendation accepted). |
| D6 | A separate "Done for today" button, or the closeout sheet asking "Is this job finished?" | **The closeout sheet asks.** Doug's idea: "No" makes it a daily log of the job. |
| D7 | What does a "No" day require? | **Hours.** Parts optional. |
| D8 | What happens to the "needs a return visit" checkbox? | **Keep it, label it differently.** Shown on "Yes" only; wording proposed in §5.4, to confirm at review. |
| D9 | Does the customer see anything on a "No" day? | **Nothing.** It is one job; the logs are for us. |

## 9. Out of scope (found, not filed)

These are in `FOUND_NOT_FILED.md`, "2026-10-03 — job lifecycle audit": <!-- untracked by design, see CLAUDE.md; link-ok -->

- the cancel lifecycle;
- the Re-open dialog never offering un-complete;
- the PATCH completion path skipping `completed_at`;
- the return-visit child being labelled a callback for genuine
  continuations made before this ships.

None is fixed here. B7 is the only lifecycle item this plan absorbs, because
day 2 can't work without it.

Also out of scope, both raised by the 2026-10-04 audit:

- **Does payroll pay job timers on top of the day clock?**
  `routers/payroll.py:245-262` sums every closed `time_entries` row per
  `user_id` and `DATE(clock_in)`. This plan adds no new paid category (day
  rows are the same arrival timers a single-day closeout already attests),
  but it doesn't answer that question.
- **On the final day, a tech who doesn't do the closeout has their timer
  closed at 0** (`jobs.py:2448-2460`). That is A6's multi-tech half. "Done
  for today" gives that tech a way to attest first, but the closeout still
  zeroes anyone who doesn't.

## 10. Audit findings

`/audit` 2026-10-04: an adversarial subagent read the plan against the code.
It found five problems. Each was re-checked by hand, and all five are
accepted and folded in above.

1. **The foundational assumption was wrong: `dispatch_status` is
   per job, not per tech** (`tenant_models.py:373`). The first draft reset
   "the tech's" status, which would have reset a whole two-tech job while
   one tech was still on site.
   → §5.1: per-tech, per-day state moves onto the visit row, and the job
   status becomes a roll-up. §5.4: the day-2 gate reads the tech's visit.
2. **The existing arrival timer was ignored.** It would have stayed open
   across days, so final-day hours landed on a day-1 `clock_in` (the wrong
   pay period). A second day row would also have been paid by
   `payroll.py:245-262`.
   → §5.4: "Done for today" closes the existing timer. There is no second
   row, and each day opens its own.
3. **The sync moved closed visits.** It ignores status and rewrites
   `start_at` (`jobs.py:446-489`), so rescheduling a job with no open
   visit would drag day 1's record forward.
   → §5.2: closed visits are excluded, rescheduling inserts rather than
   moves, collisions merge, and the cases are added to PR 1's tests.
4. **"Derived `scheduled_at`" was only partly derived.** uncomplete,
   reactivate, mobile reorder and the public PATCH write it directly.
   → §5.1: every writer is enumerated, routed through one helper, and
   tested per writer in PR 2.
5. **Billing doesn't read `time_entries`.** Hourly billing reads only
   `closeout.hours_worked` (`closeout_billing.py:240`), and the first
   draft's D1 hid that a mechanism had to be chosen.
   → §5.5 and D1: the two concrete mechanisms are spelled out, the
   `techs_on_site` double-count is handled, and the accepted-estimate path
   is flagged as untraced.

The audit found these claims accurate: B1, B2, B3 and B4; Today merging
appointments first; and `CLOSEOUT_LABOR_NOTE = "Closeout-attached"`. The
revised plan has not been re-audited.
