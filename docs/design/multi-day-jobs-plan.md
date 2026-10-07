# Multi-day jobs: one job, many visit days

**Date:** 2026-10-03
**Status:** PARTIALLY BUILT. PR 1 (§5.2 sync, and the arrival lookup from §5.4) MERGED #846, RELEASED v1.137.0, to §5.2's rules. §5.2a (revision 2, 2026-10-04) replaces those sync rules and adds `recompute_job_schedule`, as PR 1b against `main`: MERGED #868 (2026-10-05), RELEASED v1.138.0 (on prod and demo 2026-10-05; walked in a browser that night: the full write walk on the demo, a read-only look at prod), with one rule the build added (R2's second clause, below, accepted by Doug 2026-10-05); Doug ruled its open questions 2026-10-04 (a day closes only when someone closes it). PR 2a (§5.3a: the visits API, the job page's Visits card, and the "Partial Jobs — Need to Schedule" section Doug ruled 2026-10-05, D10–D12) MERGED #882 (2026-10-06), with its parked-row follow-up (queue rows carry the board's hours) MERGED #886, both RELEASED v1.139.0 (on prod and demo 2026-10-06; a read-only browser look at prod). Doug ruled D13 (2026-10-06): a partial job's queued hours are the hours still left; built with PR 3, which records the attested day hours it subtracts. PR 2b (the board draws each visit day as its own card, "Day k of n", and a drag moves that visit only; `GET /api/dispatch/visits`) built 2026-10-06 to the 2b build spec in §5.3a, MERGED #891, RELEASED v1.140.0 (on prod and demo 2026-10-06; a read-only browser look at both boards, where no multi-day job existed yet to badge). PR 3 (the "Is this job finished?" sheet, B3, B4, B6, B7, B8, billing, D13) built 2026-10-06 to §5.4a, open against `main`, not merged; B7 was narrowed in the build so "On my way" to an estimate or lead job does not start it, and B8 was widened to the phone's Stop and toggle (see B8). Its build spec is §5.4a (2026-10-06, audited 36 rounds, reshaped after round 32 to hours per person; the 0 h "Yes" rule simplified by Doug after round 36 and the spec frozen for the build). Doug ruled all decisions on 2026-10-04 (§8) and moved the day's stop into the closeout sheet (§5.4). The first draft was audited 2026-10-04 (§10); §5.2a went through three plan audits, was rewritten to Doug's rulings after the third, then through sixteen more rounds (2026-10-04), the sixteenth finding no defect, then revised the same day to Doug's ruling that arrival times are always recorded and audited five more rounds (20–24), the last finding no defect; then revised 2026-10-05 to Doug's rulings that a re-open onto a closed day, a mis-tap, and an old arrival with no time are all settled by asking the office, and audited rounds 25–41 on the arrival-undo rules, round 41 finding no logic defect and two wording fixes, applied; §5.2a ships as PR 1b against `main` (see *Packaging*), since #846 merged before it was built.

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
  one gets its own test in PR 2. (Superseded 2026-10-04: the recompute and
  its writer tests moved to PR 1b, and §5.2a's *Writers* table, re-traced
  that day, replaces this list.)

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
- **A drifted single-day job is moved, not duplicated** (added in PR 1
  after `/audit`). Before this plan, a visit whose day drifted from
  `scheduled_at` (an Appointments-page edit, `/uncomplete`, `/reactivate`)
  was pulled back by the next sync. That stays true for a job with no worked
  visit and every open visit on one shop day. A visit cancelled before
  anyone arrived is not a worked visit, unless the same tech holds an open
  visit on another day — a cancel-and-rebook, whose new day is kept
  (round-14 and round-15 audits). A cancelled visit beside *another* tech's
  visit on a different day stays ambiguous: it is read as drift, and the
  open visit is pulled back to the job's day. Otherwise a multi-day job
  never qualifies, so a helper's day-2 visit is not moved to day 1. (A helper
  on the job's crew still gets a day-1 visit of their own; whether a
  day-2-only helper belongs on the crew is PR 2's call.)
- **A closed visit is the last word on that tech's day — unless the office
  rebooks it.** Closed means completed, cancelled, **or arrived at**: the
  tech's "I'm here" stamps `arrived_at` and leaves the status `scheduled`, so
  an arrival is the only trace of a worked day until PR 3's sheet closes
  visits itself. The sync never moves, merges or retires a closed visit. An
  edit that keeps the date (title, time, crew) gives a tech whose day already
  holds a closed visit no new one, so a typo fix never turns a finished day
  back into a "scheduled" one. Two **rebooks** are the exceptions: setting
  the date again after clearing it books the day whatever it holds (a
  same-afternoon return), and moving the date onto a *cancelled* day books
  it. Moving the date onto a day the tech already worked books nothing: an
  open visit that would have moved there is retired into the worked one,
  with a `visit_merged` audit row (round-4 to round-13 audits).
- **Worked days with no trace are still movable.** On prod (2026-10-04), 16
  of 37 live visits on Complete jobs carry neither an arrival nor a closed
  status. The sync cannot tell those from an unworked visit: it moves them
  as it always did, and when the new date already holds an open visit for
  that tech it merges the untraced one away (soft-deleted, with a
  `visit_merged` audit row naming both). PR 3's sheet is what closes a day
  for real.
- **"I'm here" lands on today's visit, or on the job's only visit.** The
  arrival lookup takes this tech's (or an unassigned) open visit today —
  a not-yet-arrived one first, so a same-afternoon return is stamped — else
  the job's single live visit, if it is not closed (a visit the office
  marked "arrived" stays put), whoever holds it and whatever its day (a tech
  swap, an early arrival, a date written without the sync). That visit, and
  the job's date, then move onto the arrival time, with the old values on the
  arrival's audit row: an arrived visit is a worked day the sync never moves,
  so leaving it on Wednesday after a Monday arrival would freeze a phantom
  Wednesday (round-10 to round-13 audits). On a job with several visits and
  none today, **no visit is stamped** — any pick marks the wrong day worked;
  the tap still lands on the job assignment and the audit log. PR 2's office
  booking makes the unbooked day rare.
- **Known PR 1 trade-offs** (round-3 and round-8 audits). Re-measured
  on prod 2026-10-04 after arrivals began counting as closed: 0 jobs hold a
  closed visit beside an open visit off the job's day, 1 job holds more than
  one live visit, and no visit carries status "arrived" without a time.
  Dragging *one* tech's visit to another day on a two-tech job makes the
  job look multi-day: the next sync keeps the moved visit and adds a
  primary-day visit for that tech, where the old sync pulled it back. Under
  this plan's model, where the crew can differ by day, that is a multi-day
  job; PR 2's recompute does not change it, and PR 2's Visits card makes it
  visible and editable. Cancelling a tech's visit and rebooking it on
  another day keeps the rebooked visit and books nothing new on the
  cancelled day; if the office then moves the job to a *third* day, the
  rebooked visit stays where it is and the third day gets a new one, so
  that tech holds two (round-16 audit). That comes from the job's date
  staying on the cancelled day, which PR 2's recompute (next bullet) ends.
- **PR 2 must keep `scheduled_at` on a live visit day.** PR 1 can't tell
  "day 1's visit was deleted, day 2 remains" from drift, and would pull day 2
  back to day 1. PR 2's appointment DELETE and PATCH run
  `recompute_job_schedule` (§5.1), so the job's date moves to day 2 first.
  The mobile Today reorder (`reorder_mobile_today`) also writes
  `scheduled_at` from whichever visit is reordered, without the sync; PR 2
  must route it through the same recompute.
  Until PR 2 there is no booking flow designed for a second day; the
  Appointments page can already link a plain appointment to a job, and
  that is the live way in for the trade-offs above.
- **Tests:**
  - a 3-day job survives a title edit, a tech change, a time change and a
    **date move** on day 1;
  - a completed visit is never moved or retired;
  - rescheduling with no open visit inserts and doesn't move;
  - a collision merges;
  - clearing the date retires only open visits.

### 5.2a Revision 2: the state table (2026-10-04 — replaces §5.2's sync rules; built in PR 1b, MERGED #868, RELEASED v1.138.0)

**Why a revision.** §5.2 as built in #846
took 19 audit rounds, and nearly every code finding was the same kind of
bug: the sync was working out the office's intent (drift, or a deliberate
second day, or a cancel-and-rebook) from how the visits happened to be laid
out, because `Job.scheduled_at` could not be trusted to sit on a visit day.
§5.1's `recompute_job_schedule` is what makes it trustworthy. This revision
moves that helper from PR 2 into PR 1b, and rewrites the sync as a table that
can be tested whole. Doug asked for both on 2026-10-04.

**What already exists (do not rebuild):** `_visit_closed`
(`jobs.py`, #846), the `visit_merged` audit row, `_arrival_visit` and
`_stamp_arrival` (`mobile.py`, #846), `shop_day_of` /
`shop_tz_name_from_settings` (`core/pay_periods.py`). The writer inventory in
§5.1 was re-traced 2026-10-04 and is restated in the table below.

**Ruled by Doug, 2026-10-04 (after the third plan audit), and written into
the table below:**
- **A day closes only when someone closes it:** completion, a cancel, or PR
  3's "Done for today". A tap means the crew is there; it does not close the
  day. A visit never closes because its date passed. Earlier drafts guessed
  from the calendar (a "PAST" state and a nightly recompute), and every audit
  round found a new contradiction at that boundary. The accepted cost: an
  un-tapped, unfinished day leaves the job showing late on the board until the
  office resolves it.
- **The job's date stays on the day the crew is working** until that day is
  closed. When a day closes unfinished and another day is already booked, the
  date moves to it; if none is booked, the job **goes back to the office** to
  schedule. Nothing books a day automatically. The kick-back's surface is
  the Dispatch board's **"Partial Jobs — Need to Schedule"** section (§5.3,
  built in PR 2; Doug, 2026-10-05, D10). It replaces the earlier note that
  PR 3 must find one: the existing Dashboard queue
  (`/api/jobs/return-visits-unscheduled`, `jobs.py:3523`) lists only
  return-visit child jobs with no date and no tech (`jobs.py:3557-3563`), so a
  kicked-back multi-day job never appears there. A day nobody closes still
  leaves the job showing late on the board, which is the cost Doug
  accepted.
- **Finishing the job retires its remaining booked days** automatically, and
  **cancelling it retires its open visits**. Both are audited. "Remaining"
  means after the day it finished: an un-tapped visit on or before that day
  may be the day the work was done, so it is closed, not removed (F1).
- Q2–Q6 of the third draft were ruled as recommended; they appear below as
  rows C5, A2, R1, C6 and X1.

**Prod, read-only, 2026-10-04:** 5 live, unfinished jobs hold a visit that is
not completed or cancelled, and on all 5 `scheduled_at` already equals the
earliest one's start: **0 jobs break I. No backfill.** Separately, 35
completed jobs still hold 36 Current visits (19 of them ON SITE), because no
code closes visits at completion today. I does not cover finished jobs, so
these break nothing until a job is re-opened; R0 below cleans them up then,
audited, instead of a bulk data migration. 9 of those jobs have a null
`completed_at` (9 un-tapped visits), so R0 uses the re-open time as the
finish day and closes all 9 rather than retiring them; none starts after now
(prod, 2026-10-04), so no booked future day is affected. Techs skip "I'm here"
often (17 un-stamped visits on 16 of the 37 completed jobs with visits), which
is why a missed tap must not be read as anything but "not tapped".

#### Terms

| Term | Meaning |
|---|---|
| Visit | An `appointments` row with `job_id` set **and `deleted_at` null**: one tech, one shop day. A retired row is not a visit in any state below, and counts nowhere (N, I, R1–R4, E4/E5, the oracle). |
| OPEN | Not tapped (`arrived_at` null, status not "arrived"), not completed, not cancelled. The date does not matter. |
| ON SITE | Tapped (`arrived_at` set or status "arrived"), not completed, not cancelled. After PR 1b no writer can produce status "arrived" without `arrived_at` (*Arrival is always recorded*); the status-only half covers rows written before it. |
| CLOSED | Status "completed", or "cancelled" with `arrived_at` set (worked, then closed). |
| CANCELLED | Status "cancelled" and `arrived_at` null. |
| Current | OPEN or ON SITE: the day is not closed. |
| Live | Anything but CANCELLED. |
| Retire | Soft-delete (`deleted_at`), as #846 and today's sync do, with a `visit_retired` audit row carrying the row's tech and start. The row is kept. Not status "cancelled": the phone's Today query filters only `deleted_at` (`mobile.py:1176-1182`), so a cancelled visit would stay on the tech's screen. |
| Current day, N | The shop day of the earliest Current visit. None if there is none. Read from the **visits**, never from `scheduled_at`. |
| Target, T | The shop day of the new `scheduled_at` in an edit. Any day, past included. |
| Crew | `JobAssignment` techs, or `[assigned_to]` on a legacy job; with neither, one unassigned slot (`tech_id` null), as today. |

#### The invariant

> **I.** On a job that is not completed or cancelled and holds a Current
> visit, `scheduled_at` equals the earliest Current visit's `start_at`.
> Otherwise `scheduled_at` is whatever its last writer set, including null.

I does not depend on the clock, so it can only change when a row is written,
and every writer listed under *Writers* runs `recompute_job_schedule(db, job,
user, reason)` last. Recompute writes a `job_schedule_recomputed` audit row
(from, to, reason) when it changes the value, and does nothing on a completed
or cancelled job.

#### Design: a pure planner, a thin applier

The sync splits in two. `plan_visit_sync(visits, crew, edit)` is a pure
function: it reads visit rows as plain values and returns either a list of
actions (`insert`, `move`, `reassign`, `retire`, `close`, `copy_fields`) or a
**refusal** with a reason. `apply_visit_plan` writes the actions and their
audit rows (`visit_added`, `visit_moved`, `visit_reassigned`,
`visit_retired`, `visit_closed`, and `visit_updated` for a `copy_fields` that
changes a value; a copy that changes nothing is not planned). The table below specifies the planner, and
the whole-grid test runs it without a database.

#### Job edit (`update_job`, `create_job`, public API PATCH)

A date edit means **"move the current day to T."** The form shows
`scheduled_at`, which under I is the current day's start, so what the office
sees is what the edit moves. A date edit is detected by comparing the payload
with the stored `scheduled_at` **to the minute**, so re-saving an unchanged
form is E1. (An exact compare would not survive the browser: a JS `Date` keeps
milliseconds only, so a stored value with microseconds comes back "changed"
on every save. Every new time is truncated to the minute: A2's move to the
arrival time, E2/E4's new start, **and the job's own `scheduled_at` on every
path that writes it** (`update_job`, public PATCH, `/uncomplete`,
`/reactivate`; today a raw `setattr`, `jobs.py:1424-1425`), so P7 holds even
when no visit is written (E5). The planner's result carries the job's new
date, so the pure grid can check it. Recompute copies the visit's
`start_at` exactly, so I stays an exact equality even for a start the
Appointments page stored with seconds.)

**Refusals are checked before any write in the request**, and only on an edit to a live job that does not cancel it (a cancel and an edit on a finished job plan no move, so R1–R4 never apply; a re-open's R0 leaves no N, so none can fire; a re-open's own two questions, below, are also answered before any write). A refusal
fails the **whole request** with 409 and names the Appointments page (PR 2:
the Visits card) as the place to do it. Nothing is written: no visit, no job
field, no status flip on `/uncomplete` or `/reactivate`, no audit row.

**R0, on re-open.** `/uncomplete` and `/reactivate` first apply the clean-up
the job's finish would now have done (F1 for a completed job, X1 for a
cancelled one) to every Current visit it still holds, audited with the re-opening user and reason
`job_reopened`. Any visit a finished job holds is left over from before it
finished (P10), so this is F1 applied late, not a guess. F1's "finish day"
is the job's `completed_at`, or the re-open time when it is null. The new date
is then planned with no N. A date given on re-open is always a
date set, even if it equals the stored value, since R0 has just removed the
visits, and books each crew tech on T by E4. **When a crew tech already holds
a CLOSED visit on T, the re-open asks** (Doug, 2026-10-05) instead of
guessing: the request carries `rebook_closed_day` (true or false), and
without it the re-open returns 409 `needs_answer`, naming the techs and the
day. The question reads the rows as R0 leaves them (planned in memory), so
a visit R0 itself closes counts, and the wording claims no more than the
rows show: the Re-open dialog asks "Mike already has a closed visit on Tue
10/6. Book him on that day again?". Yes books them anyway — a job completed
by mistake and un-completed back onto the same day gets its visit back, so
the tech's next tap finds it (A1) — and is the one named exception to P2. No
leaves those techs as E5 (nothing booked), and the dialog says their tap
will find no visit. The planner plans R0 in memory first, so this question,
like the old-row question (*Arrival is always recorded*), is asked before
anything is written. R1–R4 cannot apply on a re-open: R0 leaves no N, and
all four need one.

| # | A date edit is refused when | Why |
|---|---|---|
| R1 | T is on or after another Current day of the job (a day ≠ N holding a Current visit) | Moving day 1 onto or past day 2 would merge two booked days or leave the job dated on a day the office did not type. (Doug, Q4.) |
| R2 | Anyone who will hold an OPEN visit on N **after this request's crew change** (so a tech added in the same save counts, and so does a tech outside the crew whose visit E2 moves) already holds a Live visit on T **other than the visits on N that this edit moves** (so a time change within N is not refused) | It would put two live visits for one tech on one day. The Jobs form sends crew and date together on every save (`JobsView.vue:1193-1220`). |
| R2, second clause (added in the build, 2026-10-05) | The same save's crew change empties N (C5–C7), so E4/E5 books T instead of E2 moving N, and every tech left already holds a Live visit on T (E5), so nothing lands on T | The job would keep a Current visit on a later day and recompute would date it there: a 200 that leaves `scheduled_at` on a day the office did not type (P7). Found by the whole-grid test, not by an audit round; refused as `double_booked`. Doug accepted the refusal 2026-10-05. |
| R3 | A tech holds two or more OPEN visits on N | Which one moves is a guess. (Prod, 2026-10-04: 0 such pairs; the Appointments page can create one.) |
| R4 | **Any** Current visit of the job is ON SITE (on N or a later day — an un-tapped Monday leaves N on Monday while Tuesday's crew taps in), and the edit changes the date, the time, or clears it | The crew is on site. Moving the others would leave the arrived crew behind, and recompute would put the old value back (a 200 that did nothing). The 409 says to close that day first. Until PR 3's "Done for today", the office closes it with **Complete** on the Appointments page — which PR 1b must make reachable (see *Appointments page*, below). |

With R1–R4, **a successful date edit leaves `scheduled_at` equal to the value
typed, truncated to the minute** (P7), backdating included: a visit moved to a past day is still OPEN,
so it is still the current day.

| # | Edit | Job state | Per tech t | Result |
|---|---|---|---|---|
| E1 | No date or crew change (title, customer, a re-save) | any | any | Copy title and customer onto every OPEN visit. **Never inserts, never retires.** |
| E2 | Date or time changed, not refused | N exists | — | **Every OPEN visit on N moves** to the new `scheduled_at`, whoever holds it: a crew tech, nobody, or a tech outside the crew. All move to the same start, as today. No visit on N is ON SITE here (R4). A CANCELLED visit on T stays beside it as history. |
| E3 | Date or time changed, not refused | N exists | — | Visits on N that are not OPEN (CLOSED or CANCELLED) stay where they are. |
| E4 | Date set or changed | no N | t has no Live visit on T | Insert an OPEN visit for t at the new start. Covers create, a date set from null, and a return after every day closed. |
| E5 | Date set or changed | no N | t has a Live visit on T | Nothing for t (a time fix after the day closed books no second visit). |
| E6 | Date cleared, not refused | any | any | Retire every OPEN visit, all days (today's "unschedule"). R4 refuses a clear while any visit is ON SITE, so no Current visit survives a clear. |

#### Crew change (`update_job` crew fields, `POST`/`DELETE /api/jobs/{id}/assignments`, the Re-open tech, `/start`)

Every crew path shares one helper. A single-tech input (`assigned_to`, the
Re-open dialog's tech) means "the crew is now [t]", as `update_job` already
treats it (`jobs.py:1296-1300`), and decomposes into a row per tech added and
removed, **removals first**, one tech at a time, each row reading the crew
as the previous step left it ("crew was empty" in C1 and "crew now empty" in
C6 mean at that step, not before the request): swapping A for B on a one-tech job is C6 then C1,
so A's visit is handed to B (two `visit_reassigned` rows) rather than retired
and re-inserted.

**A visit held by a tech outside the crew** (the Appointments page books any
technician on a job visit, `appointments.py:414-447`, without a crew check;
prod 2026-10-04: 4 such visits, 0 on a live job) belongs to the Appointments
page. Rev 2 drops #846's retire-if-not-in-crew step: the sync never retires a
visit for being outside the crew. It moves with N (E2), counts for R2, R4 and
I like any other, and is cleared by E6, F1 and X1 like any other.

| # | Edit | Job state | Result |
|---|---|---|---|
| C1 | t added, crew was empty | N holds an unassigned OPEN visit, and t holds no Live visit on N | Reassign it to t. No insert. |
| C2 | t added | N exists, t holds no Live visit on N, and C1 does not apply | Insert an OPEN visit for t on N at `scheduled_at` (N's earliest start, by I), default duration. |
| C3 | t added | t already holds a Live visit on N (OPEN, ON SITE, or a day already closed) | Nothing. |
| C4 | t added | no N | Nothing, unless the same edit sets a date (then E4). |
| C5 | t removed, others remain | t holds an OPEN visit on N | Retire it. t's visits on later days stay. (Doug, Q2.) |
| C6 | t removed, crew now empty | t holds an OPEN visit on N | Reassign it to unassigned, so the job keeps its booking (Doug, Q5) — unless N already holds an unassigned Live visit, in which case retire it as C5 (the job is still booked). |
| C7 | t removed | t holds no OPEN visit on N (none, or ON SITE) | Nothing. An arrived tech's day is not undone by a crew edit. |

**On a completed or cancelled job** the planner plans only E1's field copy
(title and customer onto any OPEN visit left over, audited `visit_updated`).
A date or crew edit changes the job's fields, as `update_job` allows today
(it refuses only a stage change, `jobs.py:1365-1370`), and changes no visit's
day, time, tech or status: no N is read, nothing is inserted, moved,
reassigned, closed or retired. A finished job's visits change day, tech or
status only through a re-open (R0) or the Appointments page.

**A request that cancels the job** plans X1 and E1's field copy only. A
crew or date change in the same payload changes the job's fields and no
visit. This is the ordinary UI path, not an edge case: the Jobs form sends
stage, crew and date on every save (`JobsView.vue:1230-1232`) and offers
Cancelled in its status list (`:645`); `update_job` reads them from one
`updates` dict (`jobs.py:1360-1450`). No visit moves, as on a finished job: there is no point booking a day X1 is
about to retire.

Order within one request: R0 on a re-open, then refusals, then crew (C1–C7),
then the date (E2–E6), then E1's field copy, then `recompute_job_schedule`.
A cancel runs X1 then E1's copy; recompute is a no-op on it.

#### Job finished or cancelled

| # | Event | Result |
|---|---|---|
| F1 | Job completed (`/complete`, `/close-without-work`, `/closeout`) | Every ON SITE visit, and every OPEN visit on or before the finish day, is **closed**: status completed, `arrived_at` left as it is (null stays null — no arrival is invented), `visit_closed`, reason `job_completed`. Every OPEN visit after the finish day is **retired**, `visit_retired`, reason `job_completed`. (Doug: finishing retires the remaining booked days.) |
| X1 | Job cancelled (`update_job` stage field, and the public API PATCH once it applies the same stage rules — see *Writers*). Triggered by the **requested** stage in the payload **when it is cancelled and differs from the stored stage** (what is left after `update_job` drops a matching stage, `jobs.py:1361-1364` — the Jobs form re-sends the stage on every save, so re-saving a cancelled job is an edit on a finished job, not a cancel), not by reading the job back: `update_job` writes the stage with raw SQL that is a no-op on SQLite (`jobs.py:1430-1447`), so a read-back would hide X1 from the SQLite test run. | Every OPEN visit, all days, is retired (`visit_retired`, reason `job_cancelled`). Every ON SITE visit gets status cancelled with `arrived_at` kept, `visit_closed`, reason `job_cancelled`: that is CLOSED, and keeps the record that the crew was there. That holds because every ON SITE visit X1 can reach carries an `arrived_at` (writers enforce it, and a cancel or reactivate holding a pre-existing status-only row asks the office first — see *Arrival is always recorded*). (Doug, Q6, and Doug 2026-10-04: arrival times are recorded even on the manual path.) |

Recompute is a no-op on both (a finished job's date is history).

**What this deletes from #846:** `single_day_job` and `worked_appts`
(drift handling — I makes drift impossible on live jobs), `_rebooked`,
`closed_on_target` and both of its rebook carve-outs, `previous_scheduled_at`
as the primary day, the retire-into-worked merge, and crew reconciliation on
every edit (E1 never inserts). **Both of #846's known trade-offs go away:** a title
edit no longer gives a dragged helper a visit on the job's day (E1), and a
cancel-and-rebook moves the job's date onto the rebooked day once the rebook
is saved (Appointments writers run recompute), so a later date move takes the
rebooked visit with it (E2).

**Behaviour that changes from today, on purpose:**
- A title edit never re-creates a visit the office deleted on the
  Appointments page. Today's sync re-creates it.
- On a job booked on two or more days, the job form cannot move day 1 onto or
  past day 2 (R1). On any job, the date or time cannot be changed or cleared
  from the job form while a crew member is tapped in on the current day (R4).
  A single-day job that nobody has tapped into moves freely, as today.
- A tap no longer moves the job's date; the date stays on the working day
  until it closes.
- Adding a tech from Job Detail gives them a visit (C1/C2), and removing one
  retires it (C5). Today `job_assignments.py` touches no visit.
- Completing or cancelling a job clears its remaining booked days off the
  techs' Today screens (F1/X1 retire them). Visits that are closed instead —
  the finished day under F1, an on-site visit under X1 — stay on that day's
  Today screen as they do today, because Today filters only `deleted_at`.
  **Not fixed here:** Today also lists any job assigned to the tech whose
  `Job.scheduled_at` is today, with no stage filter (`mobile.py:1199-1218`),
  so a job cancelled or finished before its booked day still shows on that
  day through the job's date. That is today's behaviour too, independent of
  visits; it is on the found-not-filed ledger (prod: 0 cancelled jobs dated
  today).

#### Arrival (`mobile_job_arrived`)

| # | State | Result |
|---|---|---|
| A1 | This tech (or nobody) holds a Current visit today | Stamp the un-arrived one first, own before unassigned. (As #846's rule 1, `mobile.py:2218-2228`.) |
| A2 | Otherwise, the job holds exactly one Current visit, it is OPEN, it is this tech's or unassigned (Doug, Q3), and this tech holds no Live visit today (a re-tap after the office closed today must not pull another day onto it) | Stamp it. If it is on another day (a late or early arrival), move it to the arrival time (`visit_moved` on the arrival's audit row). Two changes from #846's rule 2 (`mobile.py:2232`): the tech check, and cancelled or closed rows no longer block it (#846 required the job's only row of any status). |
| A3 | Otherwise | Stamp nothing. The tap still lands on the assignment and in the audit log. |

Then `recompute_job_schedule`. A tap on N leaves N current (ON SITE is
Current), so the date does not move. A2 moving a visit to today makes today
the current day.

#### Writers (each runs `recompute_job_schedule` last, each gets a test)

| Writer | Where (2026-10-04) | Today | Under rev 2 |
|---|---|---|---|
| create, update | `jobs.py:1144`, `:1465` | through the sync | planner (R, C, E, X1), then recompute |
| crew add, remove | `job_assignments.py:138`, `:208` | no visit touched | C1–C7, then recompute |
| `/uncomplete`, `/reactivate` | `jobs.py:4469`, `:4533`; tech at `:4471`, `:4535` | write `scheduled_at` when a date is given and `assigned_to` when a tech is given; no sync | R0, the status flip, the crew change, the date (E4; onto a day a crew tech already closed only when the request answers `rebook_closed_day: true` — see R0), and recompute **always**. `/reactivate` picks `scheduled` vs `service_call` from `scheduled_at` **after** recompute; a re-activate sent with no date that leaves no Current visit clears the stale `scheduled_at` (a typed date is kept, even when `rebook_closed_day` was answered No) (the old value goes in the `job_reactivated` audit row as `prior_scheduled_at`) and comes back a `service_call` (Doug ruled 2026-10-05). |
| `/start` | `jobs.py:1644` | sets `assigned_to` to the token's **user** id (`_user_id`, `jobs.py:182-184`) when the job has none — a users.id in a technicians.id column | resolve the user's technician id the way arrival does (`_get_technician_id`, `mobile.py:2326`); C1 with it, then recompute. With no technician row, no crew change. (No frontend caller found; API and MCP reach it.) |
| `/complete`, `/close-without-work`, `/closeout` | `jobs.py:1738`, `:1835`, `:2330` (stage flip at `:1716`, `:2774`) | visits untouched | F1 |
| mobile reorder | `mobile.py:1487` | writes `scheduled_at` and `start_at` by hand | recompute after its writes |
| arrival | `mobile.py:2268` | rule 2 also writes `scheduled_at` | A1–A3, then recompute (A2 stops writing `scheduled_at` itself) |
| public API PATCH | `api/public_router.py:546` | raw `UPDATE jobs`, no sync; `status` (`str \| None`, unchecked) is written straight to `lifecycle_stage`, so an API key can cancel or complete a job, or set a finished job back to scheduled, past every guard `update_job` has (`jobs.py:1361-1376`) | normalizes the stage as `update_job` does (`_lifecycle_stage_for_write`, `jobs.py:1188`: "Canceled" → cancelled, "Complete" → completed) and applies `update_job`'s stage rules before planning: a matching stage is dropped, a change on a finished job and a change to completed are 409 (refused before planning), a change to cancelled is a Cancel (X1). Then a date change goes through the planner (R1–R4 can 409 it); recompute. **API behaviour change**, approved by Doug 2026-10-05: the public API PATCH follows `update_job`'s stage rules. |
| Appointments POST, PATCH (a `job_id` change recomputes both jobs), DELETE | routes at `appointments.py:414`, `:507`, `:566` | no job update | recompute; DELETE and a `job_id` change refused on a visit with `arrived_at` set; a PATCH to cancelled on a status-only "arrived" visit is 409 `needs_answer` (*Arrival is always recorded*) |
| Appointments confirm, on-my-way, arrived, complete, cancel; undo-arrival on a visit and on a job (both new) | routes at `appointments.py:588`, `:617`, `:646`, `:675`, `:703` | no job update | recompute; cancel on a status-only "arrived" visit is 409 `needs_answer`; undo-arrival per *Undo arrival* (the job route, from the Job Detail crew row: 409 when the tech has no unmatched tap) |

#### Appointments page (PR 1b, so R4's way out exists)

Today a tech's tap sets `arrived_at` and leaves status "scheduled" (prod: all
19 tapped visits on finished jobs are status `scheduled`, none `arrived`).
The page picks its one action button from status alone
(`AppointmentsView.vue:101`, `stateTransitions` at `:328-333`), so a tapped
visit offers **Confirm**, never **Complete**, and the page never shows the
tap. The only click path to Complete runs through **Arrived**, which
overwrites the tech's real `arrived_at` with now (`appointments.py:661`).
PR 1b changes the row: a visit with `arrived_at` set that is not completed or
cancelled shows "On site since h:mm" and offers **Complete**. Verified in a
browser in PR 1b, light and dark, before the R4 message may point at it.

#### Arrival is always recorded (PR 1b; Doug, 2026-10-04)

"Arrived" is a time, not just a status: **every path that marks a visit
arrived records `arrived_at`**, the manual ones included.
- **Office Arrived button** (`POST /api/appointments/{id}/arrived`,
  `appointments.py:646`): takes an optional arrival time, defaulting to now,
  and **keeps an `arrived_at` the tech already stamped** instead of
  overwriting it (today `:661` overwrites it). A time supplied for a visit
  that already has one is refused with 409 naming the recorded time — a
  correction goes through the PATCH, which audits old and new — rather than
  dropped behind a 200. The page asks for the time
  (prefilled with now) so the office can enter when the crew actually got
  there.
- **Appointments PATCH** (`:507`): `arrived_at` becomes an accepted field.
  - A payload that **changes** status to "arrived" (stored status is not
    already "arrived") on a visit with no `arrived_at`, and omits
    `arrived_at`, is stamped now. Re-sending `status: "arrived"` to a row
    already "arrived" stamps nothing.
  - **The PATCH never clears a recorded arrival:** `arrived_at: null` is
    refused with 422, pointing at *Undo arrival*, whenever the stored
    `arrived_at` is set (a tech's tap, status "scheduled", included) or the
    payload sets status "arrived". Today the PATCH has no `arrived_at` field
    and copies `status` straight through, so this rule closes a hole the new
    field would otherwise open. (The POST, `:414`, always creates status
    "scheduled" — `AppointmentIn`, `:149-160`, has no status field — so it is
    not an arrival path.)
  - Changing a recorded `arrived_at` to another time is a correction: the
    audit row carries the old and the new value.
  - A payload that touches neither field (a title, notes or tech edit) is
    allowed on any row, old status-only rows included — except a `job_id`
    change, below.
- **A recorded arrival does not stop counting either.** Appointments DELETE
  (`:566`) and a PATCH that changes or clears `job_id` are refused with 409 on
  a visit with `arrived_at` set: either would drop an on-site or worked day
  off its job (out of N, R4, F1 and X1), and recompute would move the job off
  the crew that is there. The 409 points at *Undo arrival* for a mis-tap and
  at Cancel (`:703`, which leaves it CLOSED with its arrival kept) for a
  duplicate the crew really did work.
- **Undo arrival — a wrong arrival is undone by asking** (Doug, 2026-10-05,
  for a mis-tap; the same action covers an arrival the office entered by
  mistake and an old row with no time; the shape below — which tap, later
  taps, what was not put back — approved by Doug 2026-10-05). One action,
  asked first ("Undo Mike's arrival at 8:14? Reason:"), reason required:
  - **On a visit** — `POST /api/appointments/{id}/undo-arrival`, from the
    Appointments page row of any visit with an arrival (`arrived_at` set, or
    status "arrived" — the old status-only rows), **whatever wrote it** (a
    tap, the office Arrived button, the PATCH), and whether the visit is
    open or closed. It clears `arrived_at` and sets a status of "arrived"
    back to "scheduled". The visit's state follows from the Terms: an
    ON SITE visit becomes OPEN; a completed one stays CLOSED (the day is
    still closed, now with no arrival); a cancelled one becomes CANCELLED —
    which is how a mis-tapped visit that was then cancelled stops reading as
    a worked day.
  - **Which tap — the match rule.** Each `arrived` record belongs to one
    tech (the record's tech id; for an old record, the actor user's
    technician, `_get_technician_id`), so two techs' records never match
    each other's stamps. A visit's tap is the not-yet-undone `arrived`
    record of the visit's job that names the visit — PR 1b records always
    carry the stamped visit's id; an old record names it only in
    `visit_moved.visit_id` — or, failing that, whose `arrived_at` equals the
    visit's **original** arrival (the value the tap stamped: the `old` of
    the first PATCH correction's audit row if the time was corrected since;
    one `arrival_time` stamps both, `mobile.py:2347-2349`, `:2384`). A tap
    no visit matches is an **unmatched tap**: an A3 tap, a second press
    that stamped nothing (the tap writes an `arrived` record every time;
    prod 2026-10-05 has two from one user 1.3 s apart), a later real tap
    after a mis-tap, or a tap whose visit's time the office Arrived button
    overwrote before PR 1b (`appointments.py:661` records no old value;
    prod 2026-10-05: 0 `appointment_arrived` rows, but one can occur until
    PR 1b deploys). These look alike in the rows, so **the office is asked**
    rather than the code guessing.
  - **On a tap no visit holds** — `POST /api/jobs/{id}/undo-arrival` with
    `tech_id`, from the crew row on Job Detail. The dialog lists that
    tech's unmatched taps on the job (day and time) and the request names
    the wrong ones (`tap_record_ids`, at least one; 422 if one is not in the
    list). An empty list is 409 "no tap to undo here", pointing at the
    Appointments page when the tech's taps all stamped visits (one
    arrival, one place to undo it).
  - **On a visit whose tap is not found** (no match): the dialog shows the
    tech's unmatched taps (any tech's, for an unassigned visit) and the
    request may name the visit's own (`tap_record_id`, 422 if not in the
    list); a named tap is undone with the visit, as if matched. With none
    named, no tap is undone (the visit is cleared and the job side
    recomputed as below) and `{field: "tap", reason: "no_record"}` is
    reported.
  - **Later taps the same day are asked about too.** When a tap being
    undone has later unmatched taps of the same tech on the job on the
    same shop day, the dialog lists them ("Mike also tapped at 8:00:01 and
    13:02 — were these wrong too?") and the request names the ones that
    were (`also_undo`, 422 if one is not in that list). Named ones are
    undone with it; the rest stay taps. A visit is never restamped from
    another tap: if the crew really arrived at 13:02, the dialog offers to
    record that time on the visit through office Arrived (an arrival no
    tap wrote, audited `source: manual`).
  - **What is reverted.** PR 1b extends the tap's `arrived` audit payload
    (today `arrived_at`, the actor, and `visit_moved` with `visit_id`,
    `visit_start_from` and `job_scheduled_at_from`, `mobile.py:2255-2261`,
    `:2378-2392`) with the tech id, the stamped visit's id and the pre-move
    `end_at`. The `arrival_undone` row names every record and visit
    arrival it undid, so nothing undone counts again.
    A tech's **surviving taps** are their `arrived` records on the job not
    yet undone; the job's **surviving arrivals** are every tech's surviving
    taps plus the `arrived_at` on any visit no tap wrote. Both are taken
    after this undo.
    - **The visit side:** a visit A2 moved goes back to its old `start_at`
      and `end_at` (for a record from before PR 1b, `end_at` is the old
      `start_at` plus the visit's current length, which the move kept,
      `mobile.py:2262-2264`).
    - **The job side is recomputed from what survives, on every undo,
      whatever the arrival's source** — never restored from a recorded
      "before" value, which an earlier undo can make stale:
      - each tech with a tap undone in this request: their
        `JobAssignment.arrived_at`, if it equals the time of any of their
        tap records, becomes the earliest of their surviving taps, or null.
        Only taps write it (`stamp_tech_state`), and a manual time is not
        attested hours, so only taps refill it; a later real tap keeps
        counting in labor variance (`labor_variance.py:189-192`);
      - `Job.arrived_at`, if it equals the time of any tap record on the job
        or any value a visit arrival undone now has held (its current value,
        or an old or new value in its PATCH correction rows), becomes the earliest of the job's
        surviving arrivals, or null — so a value an earlier undo copied
        from an office-entered arrival goes when that arrival is undone;
      - on a job that is not completed (a cancelled one included — nothing
        on the cancel path resets it, and a reactivate would otherwise bring
        back an `on_site` with nobody there), a `dispatch_status` of
        `on_site` with **no** surviving arrival and no visit ON SITE becomes
        `assigned` if the job has `assigned_to`, else `unassigned` (the rule
        create uses, `jobs.py:1126`, `:4378`); with an arrival surviving it
        is left and reported `other_arrival`. Any other value is left (an
        undo never moves a job *up* a stage). A job that was `en_route`
        before the mis-tap reads `assigned` afterwards; the tech's next On
        my way puts it back. On a completed job it is not a revert target,
        so leaving it is not a skip: today's completion routes write `done`
        (`jobs.py:1719`, `:2777`), and while older completions left other
        values (prod 2026-10-05, read-only: 47 `done`, 185 `unassigned`, 8
        `assigned`), none is `on_site`. The public PATCH can complete a job
        without writing `done` until PR 1b refuses that change.
      - A stamp that equals no record's time was not written by anything
        the undo can see; it is left and reported `no_record`.
  - **The move is not restored (the rest of the undo goes ahead) when** the
    visit's day or time was changed after the tap — restoring would undo
    someone's edit — or the tech now holds another Live visit on the old day
    (P2), or the visit is completed or cancelled or its job is — a closed
    day stays where it was worked (P10), and a finished job's visits are
    not moved — or the tech has a surviving tap on the day the visit was
    moved to (the crew did come that day).
  - **A skipped revert is never silent:** the 200 response carries
    `not_reverted: [{field, reason}]` (`reason` one of `edited_since`,
    `double_book`, `closed`, `crew_came`, `other_arrival`, `no_record`), the `arrival_undone` audit
    row carries the same list, and the page shows it after the undo
    ("Arrival undone. The visit stays on Wed 10/7 — it was moved since.").
  - **Not touched: the job time entry a tap auto-opened.** Hours are
    attested through the timesheet path, never edited by a dispatch action;
    the dialog names the clock-in ("Mike's clock-in at 8:14 stays — fix it
    on Timesheets").
  - **Audit:** `arrival_undone`, with every reverted field's old and new
    value (status included), the source of the arrival, the reason and who
    undid it, so the arrival is never lost from the record. Then recompute.
- **Audit:** the row records who entered the time and whether it came from a
  tap or was entered by hand (`source: tap | manual`), and the time entered.
  A manual arrival time is a record of arrival only; it is not attested
  hours and feeds no billed labor (labor reads `JobAssignment` stamps,
  `labor_variance.py:189-192`).
- **Rows already written** with status "arrived" and no `arrived_at` — **the
  office is asked** (Doug, 2026-10-05). Prod 2026-10-05 (read-only): **0**
  such rows, and no appointment has ever had status "arrived" — the
  handling below guards rows written before PR 1b deploys. No time is invented and none can be recovered
  exactly: only the PATCH can have written them, and its audit row stores
  field names, not values, so the trail gives at most an upper bound
  (PATCH `{status: "arrived"}` then `{status: "arrived", notes: "x"}` writes
  two `appointment_updated` rows that both list `status`). So:
  - The Appointments page shows such a row as "Arrived — time not recorded"
    and offers **Enter arrival time** (the PATCH, audited `source: manual`)
    and **Undo arrival** (reason required; no tap record exists, so no tap
    is undone; the job side is recomputed as for any undo).
  - Anything that would turn one CANCELLED and lose the record without
    anyone deciding returns 409 `needs_answer`, naming the visit and those
    two actions, before anything is written: a job cancel (X1), a reactivate
    (R0 applying X1), the Appointments page's Cancel on the row (`:703`,
    offered today for any status but completed and cancelled,
    `AppointmentsView.vue:110`), and a PATCH to status "cancelled". These
    guard visits only: an appointment with no `job_id` is not a visit, is no
    job's worked day, and cancels as today. A
    completion, or an un-complete (F1), needs no answer: F1 closes the row
    as completed, which keeps it worked.

**Writers of a new job's date (corrected 2026-10-05, ruled by Doug):** this
paragraph first listed the return-visit child job (`jobs.py:4363`) and the
public API create (`api/public_router.py:500`) as "not writers of I", on the
reading that they make a job with no visit. Both accept `scheduled_at`, so both
made a *dated* job with no visit, and with §5.2's sync gone nothing ever booked
it. As built, every writer that creates a dated job books it through one
helper, `book_new_job` (`services/visit_sync.py`, E4): `create_job`, the
return-visit child and the public create. The other six job constructors
(`onboarding`, `service_triggers`, `service_calls`, the estimate conversion,
the close-out's return-visit child, `/follow-up`) set no date.

**Not writers of I:**
`ensure_assignment_for_legacy_job` (`job_assignments.py:347`, called on the
mobile taps) adds an assignment row for the tapping tech on a job with
`assigned_to` set and none, which changes the crew without a C row; it touches
no visit, so I is unaffected (prod 2026-10-04: 0 of 25 live jobs are in that
state; on the found-not-filed ledger); a later date edit on such a job is E4. `delete_job` retires
the job.

#### Tests: the whole table, not case by case

1. **Planner, whole grid (pure, no DB).** Four shop days D0 < D1 < D2 < D3.
   Per tech, the state on each day is one of {none, OPEN, ON SITE, CLOSED,
   CANCELLED}, plus "two OPEN" on one day for R3, plus an OPEN or ON SITE
   visit held by a tech outside the crew. Crew of zero (one unassigned
   slot), one, or two techs, with an extra unassigned OPEN visit on N as a
   starting state for every crew size (the two-tech half on three days, to keep
   the run in seconds). The public API PATCH's stage values run through the
   same five request kinds. Edits: re-save, title, time change on N, date move to each of
   D0–D3 and a fresh D4, clear, set from null, add tech, remove tech, remove
   the last tech, complete, cancel, re-open with and without a date
   (including onto a day the job already closed, answered yes, answered no,
   and unanswered), date and crew edits on a
   completed and on a cancelled job, a title change combined with a date
   move, a cancel of a completed job (refused), a cancel combined with each date and
   crew change, **and every crew change combined with every date change in
   one request** (the Jobs form sends both on every
   save). Every combination runs, and the
   properties are checked on each result.
2. **Row pins.** One test per R0–R4, E, C, F, X and A row, asserting its exact
   result. Every property is shown able to fail: each has a named mutant (R4
   deleted, ON SITE counted as closed, recompute skipped in one writer, F1
   not retiring) that turns it red, run once and recorded in the PR.
3. **Writers.** One DB test per row of *Writers*, asserting I afterwards and
   the audit rows written. Each writer that can refuse also gets a refused
   request asserting that the job, its visits and its audit rows are
   unchanged — P8 means nothing at the planner, where a refusal has no
   actions by its type. The arrival paths (office Arrived, PATCH, the
   mobile tap) each get a test that marking a visit arrived leaves
   `arrived_at` set, that office Arrived keeps a tech's existing stamp, that
   a PATCH clearing a recorded `arrived_at` is refused (on a tapped
   status-"scheduled" visit and on an "arrived" one), that re-sending
   "arrived" to an old status-only row stamps nothing, that a time
   correction audits the old value, that DELETE and a `job_id` change on a
   visit with `arrived_at` set are refused, that office Arrived with a
   time on a stamped visit is refused; that Undo arrival requires a reason
   and works on an arrival from each source (tap with a record, pre-PR-1b
   tap, office Arrived, PATCH, old status-only row) on an open, a completed
   and a cancelled visit, with the resulting state per the Terms; that from
   a tap record it puts back a visit A2 moved, reverts the assignment, job
   and dispatch stamps, leaves the time entry alone and audits every old
   value; that the crew-row undo lists only the tech's unmatched taps,
   undoes the ones named (with a PR 1b record and a pre-PR-1b one, the
   latter undone exactly like the former — `on_site` → `assigned`; Doug
   ruled 2026-10-05, see below), and is 422 for a
   record outside the list and 409 with an empty list, nothing written
   either way; that a double tap with the second named in `also_undo`
   leaves `JobAssignment.arrived_at` and `Job.arrived_at` null and
   `dispatch_status` `assigned`, while an
   8:00 mis-tap with an unnamed 13:00 same-day tap refills both to 13:00,
   clears the visit (no restamp) and, if the 8:00 tap had moved the visit,
   leaves it on that day (`crew_came`); that undoing the 13:00 tap later
   then takes both refilled stamps to null and `dispatch_status` to
   `assigned`; that after an A3 tap is undone with an office-entered
   arrival surviving, undoing that arrival takes `Job.arrived_at` to null
   and `dispatch_status` to `assigned`; that a mis-tapped job cancelled,
   then undone, then reactivated without a tech reads `assigned`, not
   `on_site`; that `Job.arrived_at` copied from an office arrival later
   corrected by PATCH still goes when that arrival is undone; that an
   `en_route` job left on
   `en_route` is not touched; that a same-day tap that
   stamped another visit is never listed, nor is a later day's tap; that
   with two techs' A3 taps, undoing one leaves `Job.arrived_at` on the
   other and `dispatch_status` on site; that undoing a day-1 tap of a tech
   who really tapped on day 2 moves their `JobAssignment.arrived_at` to the
   day-2 tap, whether it stamped an unassigned visit (A1) or none (A3), and
   to null when day 2 was only entered by the office; that a tap whose
   time was corrected by PATCH is still matched and fully reverted; that a
   pre-PR-1b tapped visit whose time office Arrived overwrote, on a
   two-visit job where the tech's first tap stamped the other day, lists
   only the unmatched day's tap, undoes it when named (leaving the first
   day's arrival and stamps), and undoes no tap, reporting `tap: no_record`,
   when none is named; that an undone tap is not counted by a later undo;
   that the move is skipped (rest applied, `not_reverted` in the response
   and audit) when the visit was edited since, the restore would
   double-book, the visit or job is finished, or the crew came that day;
   and that a job cancel, a reactivate, the row's
   Cancel and a PATCH to cancelled on a status-only "arrived" row each
   return `needs_answer` with nothing written.

**Properties, after every edit and every writer.** Every request is one of
five kinds, and the kind decides which properties it is checked against:
- **Re-open** (`/uncomplete`, `/reactivate`): P1–P10, judged against the rows
  R0 leaves (a live job with no N), with P2's and P5's R0 exceptions.
- **Refused before planning**: a stage change on a completed or cancelled
  job, which `update_job` already 409s ("Use Re-open", `jobs.py:1365-1370`)
  before the planner runs. Checked against P8.
- **Cancel** (a payload whose requested stage is cancelled and differs from the stored stage, on a live job): X1 and E1's copy
  only, so P10, P1, P4 and P6; it must move, insert and reassign nothing.
- **Edit on a finished job** (not a re-open): E1's copy only; P1, P4 and P6,
  and it must change no visit's day, time, tech or status.
- **Everything else** is an edit on a live job: P1–P10.

A `needs_answer` refusal (a re-open's two questions, a cancel holding an old
status-only row) is judged like any refusal, whatever the request's kind:
P8 only — nothing planned, nothing written. The grid covers the re-open
question (answered yes, no, and unanswered); the old-row question is pinned
by the writer tests, since the grid's ON SITE state is the stamped one.
- P1. A date or crew edit leaves every ON SITE, CLOSED and CANCELLED visit
  unchanged. (Only F1, X1 and R0 close an ON SITE visit.)
- P2. No action increases the number of (tech, shop day) pairs holding two or
  more Live visits, except a re-open onto a day the job already closed that
  the office answered yes to (R0). (Pre-existing ones, which the Appointments page can create,
  are left alone.)
- P3. I holds exactly (no truncation in the oracle), checked by an
  independent oracle (a naive earliest-Current start computed from the final
  rows), never by calling `recompute_job_schedule`. The grid includes starts
  with seconds and microseconds.
- P4. E1's field copy never inserts, retires, closes or reassigns a visit
  (checked on the copy's own actions, so a cancel's X1 retirements do not
  count against it).
- P5. OPEN visits on days other than N keep their day, time, tech and status
  (E1's field copy may change their title and customer) through a date move, a time
  change or a crew edit. (A clear, E6, and a re-open's clean-up, R0, retire
  or close them by design.)
- P6. Every visit inserted, moved, reassigned, closed, retired or given new
  field values has its audit row.
- P7. A date edit that is not refused leaves `scheduled_at` equal to the
  value typed, truncated to the minute. (Prod holds such values: 2 jobs
  carry JS milliseconds, from PrimeVue's DatePicker; the grid types values
  with seconds.)
- P8. A refused edit plans no action and changes no job field.
- P9. A date or time change (not a clear, E6) that is not refused leaves every holder of an OPEN visit
  on N **after the request's crew change** (crew, unassigned, or outside the
  crew) with an OPEN visit on T, and no OPEN visit left on N.
- P10. After F1, X1 or R0 the job holds no Current visit (R0: before the new
  date is planned), and F1 leaves at least one visit on or before the finish
  day if the job had one (no worked day is removed).

**Built differently from the earlier text, ruled by Doug 2026-10-05 (accepted):** the test list said a
pre-PR-1b record undone from the crew row reports `dispatch_status` as `no_record`. No rule in
*Undo arrival* produces that — `dispatch_status` is recomputed from what survives — so the
build treats it like a PR 1b record (`on_site` →
`assigned`, nothing in `not_reverted`), pinned by
`test_crew_row_undoes_the_named_tap[pre_pr1b_record]`.

#### Packaging

Written to be folded into #846 before it merged. #846 merged 2026-10-04
(`a64aecac`) and shipped in v1.137.0, on prod and demo by 2026-10-05, so
§5.2's sync rules are live. Revision 2 now ships as its own PR against
`main`, replacing #846's sync; a recompute-only PR first would still mean
rewriting that sync. #846's arrival half stays (A2 narrowed per Q3). That
PR is **PR 1b** throughout §5.2a; "PR 1" elsewhere in this doc is #846.
Taps recorded under v1.137.0 are "pre-PR-1b" records in *Undo arrival*'s
sense. Code citations in §5.2a are to `43cabb13` (#846's head, 2026-10-04);
`mobile.py` has moved about 100 lines on `main` since, so PR 1b re-resolves
each one against `main` before building.

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
- **"Partial Jobs — Need to Schedule" (Doug, 2026-10-05, D10–D12).** This
  replaces the earlier "Continuing — book next day" label, which kept the
  job on its old card and started no queue.
  - **What lands there:** a job that is not finished (`lifecycle_stage` not
    completed or cancelled), has at least one visit closed for the day, and
    has no Current visit after it. Today the office closes a day with
    Complete on the Appointments page (PR 1b); after PR 3 the closeout
    sheet's "No" closes it too. A query over existing rows: no migration.
  - **Where:** its own section on the Dispatch board's day view, **above
    "New Jobs to Schedule"** (D11) — between the tech columns and that
    queue, so both stay drag targets without scrolling (the ordering note at
    `DispatchView.vue:257`).
  - **What a drop does:** dropping a card on a tech books a new visit for
    that tech on **the date the board is showing**. The office picks the
    day; nothing assumes the next day, because the next day does not always
    work for the tech (D12). The earlier days are untouched, and the card
    leaves the section as soon as a later day is booked. The Visits card's
    "Add day(s)" is the other way to book it, for any date.
  - **Who:** the office, on desktop. Not on the tech's phone.

### 5.3a PR 2 build spec (2026-10-05; 2a MERGED #882, RELEASED v1.139.0; 2b MERGED #891, RELEASED v1.140.0)

PR 2 ships as two PRs against `main`. **2a**: the visits API, the Visits
card, and the Partial Jobs section. **2b**: the board shows a multi-day job
on each visit day, with a "Day 2 of 3" badge, and dragging that card moves
the visit. They are split because the board today is built only on
`Job.scheduled_at` (`DispatchView.vue:1171`, `fetchJobs` :1807) and reads no
appointment at all. Teaching it visits is the largest piece, and 2a stands
on its own without it.

**What already exists (re-traced on `main` @ 294ee9ce, do not rebuild):**

| Need | Already there |
|---|---|
| A day-2 visit shows on the tech's phone | Today unions the tech's appointments for the local day (`mobile.py:1312-1332`) |
| A day-2-only helper can open the job | `job_belongs_to_user` grants access through any live appointment (`core/job_access.py:120-130`) |
| Move, remove, complete and undo one visit | `PATCH`/`DELETE /api/appointments/{id}`, `/complete`, the undo-arrival routes. Each runs `recompute_job_schedule` through `_record` (`appointments.py:266`) |
| The phone's route reorder keeps I | `reorder_mobile_today` already recomputes (`mobile.py:1639`), so §5.2's "PR 2 must route it" is done |
| A refusal shown as a message | `utils/visitRefusals.js` (`refusalOf`) |

**Decisions in this spec.** None adds a migration, changes money math or
changes anything a customer sees.

- **Booking a day does not touch the crew.** A tech booked only on day 2
  holds a visit and is not added to `JobAssignment`. D5 lets the crew differ
  by day. Access and Today already follow the visit (table above). Adding
  them to the crew would make the next crew-field edit plan C1–C7 against
  them. This is §5.2's open "day-2-only helper" call.
- **No day before shop today can be booked** (409 `past_day`). An OPEN visit
  on a past day would become N, and the job would show late at once with no
  one ever having been booked to do it.
- **A tech who already holds a Live visit of this job on a requested day**
  fails the whole request (409 `double_booked`, naming the tech and the day).
  So does the same (tech, day) pair named twice in one request. Nothing is
  written. Silently skipping would answer 200 for a day that was not booked.
- **A finished job books nothing** (409 `job_finished`). Re-open it first.
- **The refusal messages from 1b are unchanged.** They name the Appointments
  page, which still works. The Visits card is a second way to do the same
  thing, not a replacement.
- **A Partial Jobs drop is an ordinary board drop.** It goes through the
  board's existing path (`assignJob`, `DispatchView.vue:1606`): `PATCH
  /api/jobs/{id}` with the tech as the crew and the board's date. The
  timeline sends the slot's time; the tray sends midnight, date-only, as it
  does for every job (`:1731-1736`). The job has no Current visit, so the
  planner books it by E4 (§5.2a: "a return after every day closed"), and the
  crew becomes [that tech]. That is what every board drop means, and it is
  what puts the card in that tech's column. A board that places a job by crew
  (`:1310-1313`) would show a booked visit for a tech outside the crew in the
  wrong column (plan audit 2026-10-05, finding 1). One edge: when that tech
  already holds a Live visit on that day, E5 books nothing, and the job
  stays partial. The section re-reads after every drop, and a card that is
  still there gets a warning toast naming the tech and the day, so the 200
  never looks like a booking. The same is true of a drop at the very
  minute the job already holds (the last worked day's start): no date
  changed, so the planner reads it as E1 and books nothing, and the same
  warning shows.
- **The Visits card's Move and Remove get job-scoped routes.** They do not
  reuse `PATCH`/`DELETE /api/appointments/{id}`. Those routes depend only on
  `get_current_user` (`appointments.py:555`, `:661`) and check no day, tech
  or stage. Moving an OPEN visit onto a day where the same tech already holds
  one would create R3's `two_open_visits`, and from then on every job-form
  date edit is refused (`visit_sync.py:340-350`). The new routes run the same
  refusals as the POST. The old routes are left as they are: that gap
  predates this plan and goes on the found-not-filed ledger.

**API (2a).**

- `GET /api/jobs/{id}/visits`, gated as `GET /api/jobs/{id}` is (any
  signed-in user of the jobs module, `jobs.py:3601`; there is no
  `jobs.read` permission). Returns every visit
  (Terms) oldest first: id, tech id and name, start, end, status,
  `arrived_at`, `completed_at`, its state (open / on_site / closed /
  cancelled), its shop day, and `day_index` / `day_count`. The day count is
  over the distinct shop days that hold a Live visit, so a CANCELLED visit
  shows but is not a day of work.
- `POST /api/jobs/{id}/visits`, gated by `jobs.write` and a dispatch role (as
  `job_assignments.py:168-180`). Body: `days: [date]` (1–20, Jobber's cap,
  §2), or `range: {from, to, skip_weekends}` expanding to at most 20;
  `start_time` (`HH:MM`, shop time); `duration_minutes` (optional; default is
  the job's `default_duration`); `tech_ids` (optional; default is the crew,
  and with no crew one unassigned slot, as E4 books). It plans with a new
  pure `plan_add_visits(visits, tz, today, requests)` beside the planner, so
  the refusals above are tested without a database. It also refuses
  `no_days` (an empty list) and `too_many_days` (over 20); a range over 20,
  or reversed, and an unknown tech id are 422. Then it applies the plan
  with `apply_visit_plan` (one `visit_added` audit row per visit, reason
  `visits_booked`) and calls `recompute_job_schedule` last. One transaction:
  a refusal writes nothing. Returns the visit list as GET does.
- `GET /api/dispatch/partial-jobs`, gated by `jobs.read_all` as late-open is.
  Membership: the job is not deleted, its stage is not completed or
  cancelled, it holds at least one CLOSED visit, and it holds **no Current
  visit**. A job with a Current visit is still booked: it has a date and is
  on the board, late or not. Each row carries the job card fields late-open
  carries, plus `holding_area_id`, `last_worked_day` (the shop day of its latest
  CLOSED visit), `worked_by` (`[{tech_id, name}]`, the techs who worked
  that day) and `closed_days` (`{day: [tech_id]}`, every day with a closed
  visit and who closed it — a visit can close early on a later day).
  Like every board queue row since the 2026-10-06 follow-up, it also carries
  `scheduled_duration_hours` and `effective_duration_hours` as `GET /api/jobs`
  gives them, and a parked row is normalized like a day-list job, so its
  holding lane's total and the job drawer read the same fields. Until PR 3
  that number is the whole job's estimate; D13 makes it the hours still
  left. Prod, read-only, 2026-10-05: **0 jobs qualify**, so the section
  starts empty and there is nothing to backfill.
- `PATCH /api/jobs/{id}/visits/{visit_id}` and
  `DELETE /api/jobs/{id}/visits/{visit_id}`, with the same gate as the POST.
  The PATCH body is shop-local, as the POST's is: `day`, `start_time`,
  `duration_minutes` (15–1440), and `tech_id` (absent keeps the tech, null
  makes the visit an unassigned slot). The server converts to an instant in
  the shop's zone, so the browser does no zone arithmetic on a write.
  Both act on an OPEN visit only (409 `visit_not_open`); a visit not on this
  job is 404 `visit_not_found`. An ON SITE or
  closed day is changed through Complete or Undo arrival. The move refuses
  `past_day` (a late visit may still change tech on its own day),
  `double_booked` (the tech it lands on already holds another
  Live visit of this job that day), `bad_length` and `job_finished`. The
  remove is a Retire (`visit_retired`, reason `visit_removed`) and refuses
  `last_open_visit`: removing the job's last booked day would leave it dated
  on a day no one is coming, so the office moves it or clears the job's
  date instead. Both are planned by pure functions beside
  `plan_add_visits`, end with recompute, and answer 200 with the visit list.
- **Late-open leaves partial jobs out.** Otherwise the same job would sit
  in two queues. The same membership predicate is shared by both queries
  (one helper), so the two cannot drift.

**Dispatch board (2a).** A "Partial Jobs — Need to Schedule" section in the
day view, ordered between the tech grid and "New Jobs to Schedule" (D11).
The `order` values move as `DispatchView.vue:257-269` asks, with that note
updated: Partial Jobs is 2, New Jobs 3, the holding areas 4. It is hidden when empty. A job parked in a holding area other than Ready to
Schedule is left out, as "New Jobs" leaves it out (`:1250-1266`): a day-1
job waiting on parts sits in its holding area, not in this queue. The
holding area's lane draws it from the partial list, because the day's job
list (`/api/jobs?date=`) never loads a job dated on the day it was last
worked; parking and Release change only its holding area. (Audit,
2026-10-05: without that, the server's late-open exclusion and this filter
together left a parked partial job on no screen at all.) Each card shows the customer, the job,
the last worked day and who worked it. It is draggable onto a tech's
timeline or tray, and the drop is the board's own (Decisions, above). A
drop on a day that has passed books nothing and says so, because a visit
booked there would be late the moment it existed. A drop at the job's own
stored instant (a tray drop on the day it was worked at midnight) is not
sent either: the server reads an unchanged date as E1 and would swap the
crew without booking a day (audit, 2026-10-05). Nor is a drop on a tech who
closed a visit of the job that day (`closed_days`): that is E5, which books nothing but still
writes the date and the crew, because a time fix on a closed day is allowed
on purpose (`test_e5_a_time_fix_after_the_day_closed_books_no_second_visit`).
A drop on "New Jobs to Schedule" only un-parks it. A
refusal shows the server's message as a toast (`refusalOf`), and the card
stays. The client-side
"New Jobs" list leaves out partial job ids, so a partial job with no tech
cannot show in both.

**Job page (2a).** The Schedule tab's "Appointments" card (`JobDetailView.vue:479`) becomes the
**Visits** card. It fetches `GET /api/jobs/{id}/visits` and drops the
±15-day client-side filter (`:2058-2075`), which hid any visit outside the
window. Each row shows "Day k of n", the date, the tech, the time and the
state. Dispatch roles get these actions, each on an existing endpoint:
- Move (OPEN): date, time, length and tech, through the new visit `PATCH`.
- Remove (OPEN): through the new visit `DELETE` (a retire).
- Complete (ON SITE): through `/complete`.
- Undo arrival (ON SITE or CLOSED with an arrival): the existing dialog.

"Add day(s)" opens a dialog with a multi-date picker or a range with "skip
weekends", a start time, a length, and the techs, defaulting to the crew
(or, on a job with no crew rows, its `assigned_to`, as the Schedule dialog
does).
It posts to the new route. Technicians see the list with no actions.

**What 2a does not show.** A day booked with "Add day(s)" on a later date,
or for a tech outside the crew, is on the Visits card and on that tech's
phone, but the board still draws the job once, on `scheduled_at` and by
crew, until 2b teaches it visits. 2a's walk checks the phone and the card
for those days, not the board.

**Tests (2a).**
- The pure planners (add, move, remove): each refusal; crew default; unassigned slot;
  range expansion with and without weekends; the 20 cap.
- The route: one `visit_added` row per visit; a refusal writes nothing;
  recompute moves `scheduled_at` when a day earlier than N is added; a
  technician gets 403.
- Partial jobs: membership (closed + no Current is in; closed + a Current
  visit is out; finished is out; cancelled-only is out), and late-open
  excludes exactly those jobs.
- Vitest: the Visits card's actions per state and role; the Partial section
  drop's payload and toast.
- A browser walk on a throwaway container (§7): a day closed on the Visits
  card lands the job in Partial Jobs; a drop onto a tech **outside the old
  crew** books it, and the card shows in that tech's column; a drop onto
  the tech who already closed that day warns and stays; "Add day(s)" books
  days 2 and 3, and they show on the tech's Today for those dates. Light
  and dark.

**2b build spec (2026-10-06, traced on `main` @ 946022e7).**

*What already exists (do not rebuild).*

| Need | Already there |
|---|---|
| Move one visit: day, time, length, tech, with the refusals | `PATCH /api/jobs/{id}/visits/{visit_id}` (2a, `job_visits.py`), shop-local body, recompute last |
| The day index and count of a visit | `_visit_list` (`job_visits.py:98`): days are the distinct shop days of Live visits |
| The card fields a board queue row carries | `_board_row` (`dispatch_scheduling.py:183`), used by late-open and partial jobs |
| The job's crew | `visit_sync.job_crew` |
| A refusal shown as a message | `utils/visitRefusals.js` (`refusalOf`) |
| Hours per tech column | `technicianColumns` and `sumDurationHours` (`DispatchView.vue:1325`, `:1488`) |

*What is missing.* No endpoint returns a window's visits with card fields:
`GET /api/jobs?date=` is a job list keyed on `scheduled_at`, and the visits
GET is per job. The board reads no appointment at all.

*Decisions.* None adds a migration, changes money math or changes anything a
customer sees.

- **Which jobs the board draws by visit.** Every job with a Live visit,
  except one the job card already draws exactly: all its Live visits on one
  shop day, every one starting at `scheduled_at`, and their techs exactly
  the crew (`job_crew`, as a set), or, for a job with no crew, exactly one
  unassigned slot, which is what booking a crew-less job writes (`_book`:
  `crew or [None]`; plan audit round 2, finding 1). That
  job keeps today's card, today's job-level drag and today's duration
  prompt, so a one-day job, alone or with a crew, behaves as it does now.
  Anything else is drawn by visit: two or more days; a crew tech whose
  visit was removed, or a visit moved to another time, on the Visits card
  (2a's Move and Remove change appointments only, never the crew); a tech
  outside the crew; a crewed job's day with no tech. (Plan audit 2026-10-06, finding 1:
  a rule of "two days or an outside tech" left the removed and moved cases
  drawn at the wrong time and in a column with no visit.) A job with no
  Live visit at all keeps its card. Visit length is not compared: a crew
  job's visit length is not the job's estimate by design (man-hours).
- **One card per Live visit**, on the visit's shop day, in the visit's
  tech's column, at the visit's start, as tall as the visit. A CANCELLED
  visit is not drawn. A CLOSED day of an open job is drawn, marked done for
  the day, because a day's column shows the day's work, done or not
  (`DispatchView.vue:1360`). Every card carries "Day k of n". A visit with
  no tech on the board's day goes where a dated job with no tech goes: the
  red "Scheduled — Not Assigned" lane when it is on, "New Jobs to Schedule"
  when it is off. One exception: that lane reads job rows with no
  `assigned_to` (`dispatch_scheduling.py:136`), so a crewed job's
  unassigned day is not in it, and that card goes to New Jobs whatever the
  setting says. Otherwise it would be on no screen (audit finding 3). A
  crew-less job is in the lane as a job row (unless it sits in a holding
  area: the lane also needs `holding_area_id IS NULL`, a gap that predates
  2b and goes on the ledger), so its no-tech visit cards stay out of New
  Jobs while the lane is on, as its job card does today.
- **Week view** draws from the same merged list: one card per visit on its
  day, naming its tech, with the badge, and no job copy. Week cards are not
  draggable today and stay that way.
- **The job-level copy is hidden** for a job the visits response names, so
  the job is never drawn twice. A job the response does not name keeps its
  card, so a gap in the visits read shows the old card instead of nothing.
- **Hours.** A visit card's column hours are the visit's length, not the
  job's estimate: a day counts what that day was booked for. (Booking a
  dated job gives the first visit the estimate's man-hours over the crew,
  with no cap, `appointments.py:145-148`, and "Add day(s)" defaults to the
  last visit's length, so a long install shows long days until someone
  books them shorter. That is the booking's truth, and the board shows it.) The
  holding-area totals keep the job's hours and count the job once.
- **Drag moves that visit only**, through the visit PATCH, for an OPEN
  visit. A visit card keeps the job's `id` (the drawer, Open Job and the
  closeout read it), so the drag cannot be told apart by the id it puts on
  `dataTransfer`. The drag start records the dragged card itself, and every
  drop handler (timeline, tray, holding area, New Jobs) checks it first; a
  handler that does not move visits refuses a visit card rather than fall
  through to `PATCH /api/jobs/{id}`, which acts on the job's day N, not the
  dragged day: a date moves day N's open visits, a cleared tech empties the
  crew and changes day N's visits (C5, C6), and a cleared date retires every
  open day (audit finding 4; reason corrected in rounds 8–10).
  An ON SITE or CLOSED card is not draggable: those change through Complete
  and Undo arrival, as on the Visits card.
  - New Jobs cards also carry controls that are not drops (round 8, finding
    1). On a visit card, "Assign to tech..." sends the visit PATCH with that
    tech and the visit's own day and time, and "Pick a date and time" (the
    job's schedule dialog) is not shown: the day's Move is on the Visits
    card, reached by Open Job.
  - A holding lane's Release acts on the job, as parking does, so on a visit
    card it is kept and sends `holding_area_id: null` by job id. Today it
    returns silently when the job row is not loaded, which on a later day
    it usually is not (round 9, finding 1). After it, the board re-reads
    (`fetchJobs`, both lists), or the visit card would sit in the lane until
    the next poll (round 10, finding 1). That holds on both branches of
    `releaseFromHoldingArea`: a partial job goes through
    `setPartialHoldingArea`, which today re-reads only the partial list, so
    on a visit card it too ends with `fetchJobs` (round 11, finding 1).
  - A parked job keeps its techs on later days (C5 and C6 clear day N
    only), so such a day's card is drawn in its tech's column as well as
    the job being listed in its holding lane. That is intended: the tech is
    still booked that day (round 11, question).
  - Timeline drop: the visit moves to that tech, that day and that time,
    and keeps its length. The browser sends the shop day and `HH:MM` of the
    slot's instant (the timeline computes it in the browser's zone, as it
    does for every job drag) read in the shop zone, and no
    `duration_minutes`; the server builds the instant. The PATCH's `duration_minutes` becomes optional: absent,
    the visit keeps its own length. The 15–1440 bound applies only to a
    length someone types, so a booked visit over 24 hours can still be
    dragged (audit round 3, finding 1: with the length required, such a
    drag was a 422 with no refusal code). The Visits card's Move sends the
    length only when it was changed, compared in whole minutes
    (`Math.round(hours * 60)` against the stored minutes; the input re-reads
    its two-decimal display on blur, so an hours comparison calls 2000
    minutes changed; round 6, finding 2), for the same reason (round 4,
    finding 2): it prefills the visit's length and always sent it. Its
    hours input's max becomes the larger of 24 and the stored length, since
    PrimeVue's InputNumber clamps to the max on blur and would otherwise
    turn a tabbed-through 30 into a sent 24 (round 5, finding 3); a changed
    length over 24 is refused in the dialog before anything is sent.
  - A visit over 24 hours is one appointment spanning more than one shop
    day. The board draws it on its start day only, as it is stored. That
    the booking writes such rows at all (uncapped man-hours over the crew,
    and Add day(s) defaulting to the last length) predates 2b and goes on
    the found-not-filed ledger.
  - Tray drop: the visit moves to that tech and the board's day at midnight,
    which is how the tray keeps any job "without a time" (`onTimelinePlaceTray`).
  - A drop that changes nothing sends nothing.
  - A drop on a holding area or on "New Jobs to Schedule" is refused with a
    toast naming the Visits card. Parking or unassigning is a job-level act,
    and from one day's card it would act on day N instead.
  - No duration prompt: a visit has a length.
  - A refusal (`past_day`, `double_booked`, `visit_not_open`, ...) is a toast
    through `refusalOf`, and the board re-reads.
  - A drop target added later must opt in: the refusal is the default, so a
    handler that forgets visits refuses instead of moving every day.
  - Assigning a tech to a visit does not touch the crew (2a's decision), so a
    crew-less multi-day job whose days all get techs this way stays in the
    red lane by its job row. The same is true of the Visits card's Move
    since 2a; it is recorded, not fixed, here.
- **The card is the visit.** The board builds a visit card from the item
  once, in one place, by overwriting the job's fields every filter reads:
  `scheduled_at` ← `visit_start`; `technician_id`, `assigned_tech_ids`,
  `assigned_to` and `lead_tech_id` ← the visit's tech (none for an
  unassigned day); `scheduled_duration_hours` and `effective_duration_hours`
  ← the visit's length (the timeline sizes a card from the first, capacity
  sums the second), with the job's own number kept as `job_duration_hours`. The overwrite runs
  after `normalizeJob`, which falls back to `assigned_to` when the tech
  fields are empty (`DispatchView.vue:1274`) and would otherwise put a
  crewed job's unassigned day under its lead (audit round 4, finding 1). It keeps the job's `id`, title, customer and status, and
  adds `visit_id`, `visit_state`, `day_index`, `day_count`, `job_has_crew`
  (from the server: the overwrite erases `assigned_to`, the only field
  that told a crewed job from a crew-less one) and `card_key` (job and
  visit). `matchesDate`, the timeline and the columns then need no visit
  branch. Three places do: New Jobs keeps a no-tech visit card of a crewed
  job whatever the lane setting (its last line otherwise drops every dated
  no-tech row when the lane is on; round 5, finding 1); every list keys on
  `card_key || id` instead of `id` (the tech timeline's tray and blocks, New
  Jobs, week view, the holding lanes), because a crew's two cards on one day
  share the job id; and a holding lane lists a job once, totalled at
  `job_duration_hours`.
- **A Partial Jobs row stays.** A partial job with two or more closed days
  is drawn by visit on those days, as closed, not-draggable cards, and its
  queue row is unchanged. A cancelled job's arrived visits are closed days
  and are drawn the same way; its job card is drawn today too.
- **The drawer** shows "Day k of n" and the visit's state for a visit card,
  and stays open across the poll while that visit is still drawn: it
  re-points by `card_key`, then by job id, since a job's cards share the id
  (round 6, finding 4).
- **Who.** The visits read is gated `jobs.read_all`, as partial jobs and
  late-open are. A user without it gets today's board. Prod's technician
  role carries `jobs.read_all` (`core/job_access.py:256-266`), so the test's
  403 covers the builtin role only; the read is read-only either way.

*API.* `GET /api/dispatch/visits?date_from=&date_to=` (or `date=`), shop
days inclusive, at most 366 days (422 otherwise, and for a reversed range;
the board does not ask past that and keeps job cards, as it does for a user
without the permission).
It selects the Live visits starting in that window whose job is not deleted,
then, over those jobs' full visit lists and crews, keeps the jobs the rule
above draws by visit. Each item is `_board_row` plus `holding_area_id`,
`job_has_crew`, `visit_id`, `visit_tech_id`, `visit_tech_name`,
`visit_start`, `visit_end`, `visit_state`, `visit_day`, `day_index`,
`day_count`, and the effective site (`address`, `site_label`, from
`resolve_job_sites` in one batch, as `GET /api/jobs` does), which the drawer
and the New Jobs card read. The item is the whole card: a later day's job row is usually
not loaded at all, since the day list fetches on `scheduled_at` (round 6,
finding 1). Also returned:
`job_ids` (the jobs drawn by visit) and `timezone`. The board fetches it
inside `fetchJobs`, both reads in parallel with the same date scope captured
once, so each of its callers (the date and view watchers, the
after-drop and after-closeout re-reads, the poll, Refresh) gets both and the
two lists never describe different windows (round 7, finding 1). Both are
stored together after `Promise.all`, and only by the latest call: a slower
answer for the previous date is dropped (round 8; the job read alone had no
such guard). The visits read fails on its own: a failed read is an empty
visits list (or, on a poll, the last good one), so the job list still draws
and every job shows its job card (round 9, finding 4). Like the partial and
late-open reads, it is skipped without `jobs.read_all` and re-run when
permissions load (a new `watch(permissionsLoaded)`, like the partial
read's at `DispatchView.vue:962`, that calls `fetchJobs`, so the latest-call
rule holds; round 11, finding 2); until
then the board shows job cards, as it does for a user without the
permission (round 9, finding 3). The drawer
re-points after both land, against the drawn list (visit cards and the job
rows left after the hide), by `card_key`, then by job id; today it searches
the raw job rows, which have no visit cards (round 7, finding 2).

*Not in 2b.* The phone's dispatch view (`MobileDispatch`) keeps its job
list; the phone's Today already shows visits. The "Scheduled — Not
Assigned" lane and late-open keep their job rows.

*Tests (2b).*
- Route: the visit PATCH with no `duration_minutes` keeps a 30-hour
  visit's length; one item per Live visit in the window with its day index and
  count; a one-day crew job whose visits match is left out, and so is a
crew-less one-day job with its one unassigned slot; the same job
  with one crew visit removed, or one moved to another time, is in; a tech
  outside the crew, or a crewed job's day with no tech, is in; a cancelled visit is neither drawn nor counted; a
  deleted job is out; the window is cut on the shop day, not UTC; a
  technician gets 403; a range over 366 days or reversed is 422.
- Vitest: a 3-day job draws one card per day in its tech's column with
  "Day k of n" and hides the job copy; a timeline drop and a tray drop send
  the visit PATCH with the shop day and time; a refusal toasts; a queue or
  holding drop is refused with no request; a CLOSED card is not draggable;
  a crewed job's unassigned visit shows in New Jobs with the red lane on,
  and a crew-less job's does not; week view
  draws each day; a matching one-day job still drops through
  `PATCH /api/jobs/{id}`; a crew's two cards on one day render with
  distinct keys; a parked multi-day job is in its holding lane once, at
  `job_duration_hours`; Release on a parked partial job's visit card
  re-reads the visits; capacity
  counts the visit's hours; the drawer shows the same day's "Day k of n"
  after a poll; changing the date fires both reads with the new date. The Visits card's Move leaves out `duration_minutes`
  when the length is unchanged (a 2000-minute visit tabbed through), sends
  it when changed, and refuses a changed length over 24 hours in the
  dialog. A New Jobs visit card's tech dropdown sends the visit PATCH, and
  the card shows no "Pick a date and time". Release on a visit card in a
  holding lane sends the job PATCH and the card leaves the lane.
- Browser walk on a throwaway container: book days 2 and 3 for different
  techs, see each day on its column with its badge, drag day 2 to another
  tech and time, check the Visits card agrees, try a past-day drop.
  Desktop and phone width, light and dark.

### 5.4 The closeout sheet asks "Is this job finished?" (fixes B2, B3, B4, B7)

> **Superseded in part by §5.4a (2026-10-06).** Its "No timer open" rule
> (below) would pay the tech; PR 3 follows #529 instead, and a caller with
> no timer pays nobody. Its "the tech's visit" and "the tech's timer" are
> the crew's visits and each person's timers (R-P4, R-P5). Where the two
> differ, §5.4a is what PR 3 builds.

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
  - The visit closes and `recompute_job_schedule` runs: `scheduled_at`
    advances to the next booked day, or, if none is booked, the job goes
    back to the office to schedule (Doug, 2026-10-04; §5.2a).
  - **No invoice, no closeout, no completion.** Audited as `job_day_closed`
    with job, visit, tech, hours, timer row id, note and next visit.
- **On the last day, the tech answers "Yes"** and gets the normal closeout.
- **The return-visit checkbox stays, relabelled, and only on "Yes"** (Doug,
  2026-10-04: keep it but label it differently). It still creates a
  separate job (`MobileJobCloseoutDialog.vue:853-858`), so it must stop
  reading as the way to continue unfinished work. It shows only after
  "Yes", for genuinely new work. Proposed label: **"Needs a follow-up job
  (new work)"**, with the hint "Not finished? Answer No above instead."
  Doug confirmed the wording on 2026-10-04.

### 5.5 Billing: once, at final closeout (resolves A6's multi-visit half)

> **Superseded in part by §5.4a (2026-10-06).** The earlier days do not go
> through `service_labor_line`: R-P1 bills them at the plain hourly rate,
> with no first-hour price and no 1 h floor, on a line named "Labor —
> earlier visits". Where the two differ, §5.4a is what PR 3 builds.

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

### 5.4a PR 3 build spec (2026-10-06; PLAN, not built; hours per person since audit round 32)

PR 3 builds §5.4 and §5.5 against `main` @ f55d7d3e. The line citations in
§4, §5.4 and §5.5 date from 2026-10-03/04 and have drifted. The citations
below were re-traced on 2026-10-06 and replace them.

**Doug's rulings for this PR (2026-10-06):**

- **R-P1, money: "Previous days" bills at the plain hourly rate.**
  - The day rows' man-hours are summed across days and techs, then rounded
    up to the half hour once.
  - There is no first-hour price and no 1-hour minimum on this line.
  - The first-hour price appears once, on the final day's line, as it does
    today.
  - Why it needed a ruling: `service_labor_line`
    (`core/billing_lanes.py:114`) charges the first-hour price and a 1.0
    man-hour floor (`billed_man_hours`, `:87`). Reusing it would have charged
    a three-day repair the first-hour price twice.
- **R-P2: D4 ("on my way" text each day) is moot.**
  - No on-my-way text is sent anywhere. `mobile_job_en_route` returns
    `customer_notified: False` (`mobile.py:2335-2338`).
  - An `on_my_way` template and an auto-fire flag exist, but nothing fires
    them.
  - PR 3 builds none. The missing text goes on the found-not-filed list.
- **R-P3: the daily log shows in two places.** On the desktop job page, and
  on the phone's job detail, so the next day's tech sees what's left. The
  customer never sees it (D9).
- **R-P4: "No" closes the crew's day, not just the submitter's.** One
  submit closes every checked visit of that day and records every checked
  person's hours. Without the crew close, a helper's day stays open: the job
  never advances, and the helper's hours never bill.
- **R-P5, pay identity: the crew's hours never land on a helper's pay row**
  (Doug, 2026-10-06).
  - Payroll's hours read sums `time_entries` by `user_id`
    (`payroll.py:243-270`), and `mobile_day_summary.py:101-118` shows those
    hours to the tech as their own.
  - So a helper's own open timer closes **at 0**, exactly as the final
    closeout closes a colleague's timer (`jobs.py:2570-2585`). It carries
    `day_closed_at` but no minutes, so it is not a day row.
  - The hours typed for that helper go on a **separate day row with
    `user_id` NULL**. Under #529 that row is costing evidence, not pay.
  - Only the submitter's own timer becomes their day row, with their
    `user_id`, as the final closeout does for its caller.
  - Elapsed clock time is never written as hours: the timer closes at 0
    because it was not attested, the rule in `_close_labor_entry`
    (`jobs.py:2029`).
- **R-P6: the office can answer "No" from the board and the desktop**
  (Doug, 2026-10-06). It sees the same visits and people as the phone. Every
  day row a desk user creates has `user_id` NULL, and every timer it
  consumes closes at 0 (R-P5).

**The sheet's shape: hours per person, visits as checkboxes** (Claude,
2026-10-06, after audit round 32; Doug may overrule). Hours belong to
people, not to visits.

- Visits carry no hours. Closing a visit is schedule bookkeeping only.
- Each person who tapped in that day has one hours row, whichever visits
  they were on.
- Someone who didn't tap in goes on an "added helper" row.

Rounds 13–32 asked "which person do a visit's hours belong to?", and each
answer opened the next edge case: the owner, `resolved`, `_visit_users`,
the anchor visit, the Techs stepper, the `hours: 0` rule,
`arrived_by_user_id`, and the timer→visit link. Under this shape that
question is never asked, so all of that machinery is gone. Billing, D13 and
tech efficiency read sums per job and per day, so they are unchanged. The
record of what was replaced is under "Audit rounds 13–33" below.

**What already exists (re-traced 2026-10-06; do not rebuild):**

| Need | Already there |
|---|---|
| One closeout endpoint for every surface | `POST /api/jobs/{id}/closeout`, `closeout_job` (`routers/jobs.py:2198`) |
| One closeout sheet on every surface | `MobileJobCloseoutDialog.vue`, mounted on the phone (`MobileJobDetailView.vue:958`), the board (`DispatchView.vue:772`) and the desktop job page (`JobDetailView.vue:1355`, #842) |
| Visit close, with recompute | The visit write in `complete_appointment` (`appointments.py:772-790`) and `recompute_job_schedule` (`visit_sync.py:922`). `_record` (`appointments.py:266`) commits on its own, so day-close does **not** call it (below) |
| The tech's visit for today | `_arrival_visit` (`mobile.py:2343`), rows A1–A3. A visit's `tech_id` is the technician id, not the user id |
| The day's timer | Arrival opens `entry_type='job'`, `user_id` = the user, `tech_id` = the technician id or else the user id, `clock_in` = now (`_create_time_entry`, `mobile.py:767-790`), guarded by `_find_open_time_entry` (`:725`) |
| How a labor row is closed and rated | `_close_labor_entry` (`jobs.py:2029`), `_labor_rate_for` (`jobs.py:1879`) |
| Who a desk-attested row is paid to | Nobody. #529 (Doug, 2026-08-28; `jobs.py:2536-2554`): job rows are costing evidence, the day clock (`timeclock_entries`) is the paid time, and a desk-attested row keeps `user_id` NULL |
| Partial = a closed visit and no Current one | `partial_clause` (`services/visit_sync.py:713`); the queue is `GET /api/dispatch/partial-jobs` (`dispatch_scheduling.py:275`) |
| Starting a job | `start_job` (`jobs.py:1443-1500`) sets `lifecycle_stage='in_progress'`, `status='In Progress'`, `started_at` if null |
| Autodraft line ownership | `AUTODRAFT_LINE_SOURCE` (`closeout_billing.py:52`). Every machine-made line carries it |
| The hourly rate, and labor taxability | `service_rates` (`billing_lanes.py:74`); the tenant's `labor_taxable` flag (`closeout_billing.py:204`) |
| Day 2 on Today | `/api/mobile/today` lists a job by the tech's visit for the day (`mobile.py:1317-1334`) |

**Migration `106_day_close_markers`** (D2). It adds one nullable timestamp
column to each of two tables.

- **`time_entries.day_closed_at`: the day-close marker.** Day-close sets it
  on every timer it touches and every row it writes. Nothing else writes it.
  - A **day row** is a row with `day_closed_at` set and `duration_minutes`
    > 0: one person's attested hours for one shop day.
  - A timer that day-close closed at 0 also carries the marker. So it is
    never picked, restated or listed again, and it adds 0 to every sum.
- **`appointments.day_closed_at`: the visit's close marker.** Day-close
  stamps the body's raw `closed_at` on every visit it closes. Nothing else
  writes it.
  - It makes a visits-only submission replayable (the replay key, below).
  - It lets a second closer be told who closed the visit first
    (`already_closed`).
- **The ORM models `TimeEntry` and `Appointment` gain the columns.**
  - `time_entries` is created by `create_all` before alembic runs
    (`entrypoint.sh:46-51`).
  - So the migration is guarded with `has_table` and a per-column check, as
    105 is. It is a no-op where `create_all` already built the columns.
- Batch mode for SQLite.
- **Downgrade:** drops both columns. Rolling back loses only the markers;
  the hours rows stay, as plain closed rows.
- **Existing rows:** NULL, and no backfill runs. Before this PR nothing was
  day-closed.

No column links a timer to a visit, and no column records who arrived. Both
existed only to answer the per-visit question, and money no longer asks it.

**"No": `POST /api/jobs/{id}/day-close`** (new).

Body:

- `day`: **required**, an ISO date: the shop date the sheet was read for
  (`open_day.date`). Missing or malformed is a 422.
- `visits`: zero or more visit ids to close, each at most once. They must be
  this job's, and every one must be on `day`.
- `people`: zero or more `{user_id, hours}`, each `user_id` at most once,
  with **0 < hours ≤ 24**.
  - Each names a person with a candidate timer on `day` (below).
  - A person the sheet showed and the user unchecked is simply not sent,
    and their timers are left alone.
- `added`: zero to ten `{hours}`, with 0 < hours ≤ 24. Each is a person who
  worked and never tapped in, so they have no timer and no `user_id` the
  server can know.
- At least one of `visits`, `people` and `added` is non-empty.
  - A visits-only body closes visits with no hours. That is the closer's
    attestation that nobody worked them, the same thing a "No" on an
    untapped visit meant before.
  - A people-only body closes the timers of a day whose visits are already
    closed: a person unchecked earlier answering for themselves.
- `closed_at`: **required**, an ISO timestamp with a zone (missing,
  malformed or naive is a 422): the tap time, set by the client.
  - It is **the submission's key**. Day-close stamps the raw body value as
    `day_closed_at` on every row and visit it writes or consumes.
  - Only the visits' `completed_at` uses a clamped copy, no later than
    server `now`. A clamped key would differ on every replay from a phone
    whose clock runs ahead (round 16).
  - **The first line of defence is the existing `Idempotency-Key`.** The
    queue sends one on every replay (`useOfflineSync.js:478`), and the
    middleware caches 2xx responses in Redis for 24 h
    (`core/middleware/idempotency_keys.py:21`). `closed_at` covers a Redis
    outage, and a replay after the cache expired.
  - A row that was sent once with no clear answer is parked after 23 h as
    "may have sent" (`useOfflineSync.js:234-239, 412`). A row never sent
    replays at any age.
  - **The queue action is a new `job.day_close`,** in no supersede group
    (`useOfflineSync.js:130`). Two "No"s for two days are both real, and
    neither retires the other.
- `note`: optional, ≤ 2000.

The phone, the board and the desktop send the same body. The server never
looks up "today" or the caller's tech id for a close.

**Why the body names the day, the visits and the people:** the sheet sends
it through `api.postQueued`, and the offline queue replays every write
except payments (`useOfflineSync.js:697-702`). A "No" tapped offline on
day 2 and replayed on day 3 must close day 2, not day 3.

- **Who may call it:** the same permission as closeout. The audit records
  the actor, every visit and every person's hours.
- **"Current", never "Live".** The glossary's Live includes closed visits
  (`is_live`, `visit_sync.py:70`). Every visit test in this PR uses
  `is_current`.
  - **Everywhere in this PR, "Current" also means not deleted.** Remove and
    crew change C5 retire a visit by setting `deleted_at` alone
    (`visit_sync.py:891`). Its status stays scheduled, and `is_current`
    never reads `deleted_at` (`:54-67`). The PR adds a `deleted_at IS NULL`
    test beside every `is_current` call it makes (round 18).
- **Concurrency.** The **job row** is read `with_for_update()` first, before
  the replay check. That one lock serializes every day-close and closeout on
  the job.
  - Postgres then serializes a tech and a dispatcher who tap "No" at the
    same moment. The second caller re-reads and is refused with
    `already_closed` (below).
  - SQLite ignores `FOR UPDATE`. It is the test and dev database, never
    production. So this PR makes no concurrency claim for it, and the
    concurrency tests are Postgres-only.
- **Candidate timers on day D** (D is always the body's `day`):
  - `entry_type='job'` on this job, not deleted, `clock_in` on shop day D,
    and `day_closed_at` NULL.
  - Each is either open, or Stop-marked: the `MOBILE_STOP_LABOR_NOTE` prefix
    with 0 or NULL minutes.
  - This is its own query, **not** a call to `_stopped_job_timer_for`.
    There is no 24 h window, because a forgotten day can close days later.
  - **`clock_in` after the job's latest finish**, if it has one: the
    newest of `job_closeouts.created_at` and the `created_at` of its newest
    `job_completed` or `job_closed_without_work` audit row. Those two routes
    write no closeout row, and a re-open clears `jobs.completed_at`
    (`jobs.py:4420`), so the audit row is the durable record (round 35).
    A "Yes" 0-closes only running timers
    (`_open_job_timers`, `jobs.py:2516-2588`), so a helper's Stop-marked
    timer survives a finished job unmarked. Without the bound, re-opening
    the job would make that billed day a candidate again, and
    `earlier_day_open` would force a "No" that bills it twice (round 34).
  - A timer whose `clock_in` is on another shop day is never a candidate. A
    day-2 "No" replayed on day 3 cannot touch day 3's timer.
- **The people of day D** are the distinct `user_id`s of the candidate
  timers. Which visit a timer was opened on is never asked.
- **Refusals** (409 with a code):
  - `job_finished`: the job is completed or cancelled.
  - `visit_not_open`: a listed visit is not this job's, is deleted, or is
    not Current.
  - `day_moved`: a listed visit is not on the body's `day`. The message is
    "The visit moved; reopen the sheet."
    - Without it, take a queued "No" whose visit the office moved to
      tomorrow before the replay. It would close tomorrow's visit (round 23).
  - `person_not_open`: a `people` entry's user has no candidate timer on D.
- **Two refusals carry `already_closed`** when the cause is that someone
  else closed it first:
  - `visit_not_open` on a visit whose `day_closed_at` is set;
  - `person_not_open` for a user whose day-D timers carry `day_closed_at`.

  The payload holds D's day rows, each with its person, hours, and who
  closed it. The sheet, or the failed list for a queued send, shows
  "Already closed by <name> with N h. Tell the office if your N h on <date>
  differ." A visit the office Completed by hand has no marker, so its
  refusal carries no `already_closed`, and the failed list asks for the
  hours again.
- **A person who tapped in after the sheet was read** is not in `people`,
  so their timer is left alone. Nothing is lost:
  - their timer stays a candidate on D, so `open_day` returns D again with
    them on it;
  - "Yes" is blocked by `earlier_day_open` while D is a past day.

  There is no `crew_changed` refusal. Leaving the timer alone is the same
  outcome as unchecking that person, which the sheet already allows.
- **Check order**, so every test has one expected code:
  1. Shape 422s, which read the body only, never server time or stored
     state. That is what lets a lost-response replay pass them exactly when
     the original did (round 27). They cover:
     - types, and hours bounds for `people` and `added`;
     - `added` holding at most 10;
     - duplicate visit or user ids;
     - the empty body;
     - `note` ≤ 2000;
     - `day` and `closed_at` present and valid.
  2. Take the job-row lock, then the replay check (200 no-op).
  3. `job_finished`.
  4. `visit_not_open`, then `day_moved`, then `person_not_open`.

  The first failing check answers.
- **Replay.** **This submission landed** when a time row or a visit on this
  job carries `day_closed_at` equal to the body's `closed_at`. The response
  is then a 200 no-op, even if the job has since been finished.
  - The replay check runs before every check that reads stored state.
    Otherwise a lost response would replay into `visit_not_open`, and the
    failed list would ask for hours that were recorded (round 23).
  - The offline queue resends the stored body, so a replay carries the same
    `closed_at`. Two different submissions on one job would need the same
    tap millisecond, and the job-row lock would still serialize them.
  - The key is per submission, not "a day row exists on D". Otherwise a
    queued "No" whose visit the office closed by "Yes" meanwhile would
    no-op as 200, the queue would file it as synced, and the hours would
    vanish (round 15).
- **The client must send `conflictIsError: true`.** The offline queue files
  any 409 as synced unless the call sets it (`useOfflineSync.js:509`), as
  closeout does (`MobileJobCloseoutDialog.vue:478`). Without the flag, a
  refused "No" reports success and its hours vanish.
  - A refused replay stays in the queue's failed list, showing the date and
    the hours.
  - The case that matters is a day-2 "No" queued offline, while the job was
    finished from another device. The replay is refused as `job_finished`.
    The tech sees "Day not recorded: the job was already finished. Tell the
    office: N h on <date>."
  - The office adds those hours to the invoice by hand. No UI hand-adds or
    edits a `time_entries` row today: `POST /api/jobs/{id}/time-entries` and
    `PATCH /api/time-entries/{id}` have no frontend caller. So the tech's pay
    for that day needs an admin (round 26; the missing UI is on the
    found-not-filed list).
  - The server does not add hours to a finished job behind the invoice's
    back.
- **One transaction, one commit, no `_record`:**
  1. **Visits.** Each listed visit is set to `status='completed'` with
     `completed_at` (the clamped `closed_at`) and `day_closed_at` (the raw
     value).
     - This is the same write as `complete_appointment`, factored into a
        shared helper that does not commit.
     - It is audited `visit_closed`.
  2. **People.** For each `people` entry P with hours h:
     - **If P is the submitter** (`_user_id()`, `mobile.py:249`), one of
       their candidate timers becomes their day row: the open one with the
       newest `clock_in`, else the newest Stop-marked one.
       - It is closed through `_close_labor_entry(timer, closed_at, h × 60,
         _labor_rate_for(db, tech))`.
       - It keeps its `user_id`, and gets the note (set, not appended) and
         `day_closed_at`.
       - Their other candidate timers close at 0, with the marker.
       - This holds whatever visit the timer was opened on, and for an A3
         arrival with no visit. It matches the closeout, which restates the
         caller's own timer whatever the visit (`jobs.py:2525`).
     - **Otherwise,** every candidate timer of P closes at 0 with no rate if
       it is open, and gets `day_closed_at` either way. Each 0-close is
       logged as `closeout_unattested_timer_closed` is today. One new row
       carries P's hours:
       - `user_id` NULL and `entry_type='work'`, the closeout's synthetic
         row shape (`jobs.py:2549-2556`). So no `entry_type == "job"`
         reader, such as `_stopped_job_timer_for` (`jobs.py:1999`), ever
         sees it.
       - `tech_id` = P's earliest candidate timer's `tech_id`, so the
         daily log can name P.
       - `clock_in` = that timer's `clock_in`; `clock_out` = `clock_in` +
         h.
       - It also gets the rate and `day_closed_at`.
       - It is audited `day_row_created`.
  3. **Added helpers.** Each `added` entry gets a new row of the same shape,
     except:
     - `tech_id` = the submitter's user id, as the closeout's synthetic row
       does (`time_entries.tech_id` is NOT NULL, `tenant_models.py:449`);
     - `clock_in` = the earliest listed visit's `start_at`, else the
       earliest candidate timer's `clock_in`, else noon of D shop-local;
     - the note "Added helper (not tapped in)".
  4. **Unlisted timers are left exactly as they are.** That covers an
     unchecked person, and a person who tapped in after the sheet was read.
     The final closeout 0-closes any timer still open, as it does today
     (`jobs.py:2570-2585`).
  5. **`Job.dispatch_status`** is rolled up from the job's remaining Current
     visits on that day: `on_site` if any is arrived, else `en_route` if any
     has `en_route_at`, else `assigned`.
  6. **`recompute_job_schedule`:** `scheduled_at` advances to the next
     Current visit, or the job lands in Partial Jobs (D10).
  7. **Audit `job_day_closed`:** job, day, the visits, each person's user id
     with their hours and day-row id, the added helpers' hours and row ids,
     the actor, the note, and the next visit.
- **Never touched:** the closeout, the invoice and the job's
  `lifecycle_stage`.
- **What it bills and pays, derived:**
  - Billed man-hours = Σ `people` hours + Σ `added` hours. Every one of
    them is exactly one row.
  - The submitter is paid their own hours, if they have a candidate timer.
    Nobody else is paid by this submission.
  - A person is one row however many visits they were on, so one
    submission bills each person once. A second submission naming the same
    timers finds them marked and is refused `person_not_open`.
  - **Across submissions the guard is the sheet, not the server.** A person
    closed by the lead who taps in again has a new, unmarked timer, and a
    second stint is real work, so the server cannot refuse it. The sheet
    shows what is already logged for them (`logged`, below) and asks for
    only the time since. Typing the whole day again bills the first part
    twice (round 33).
  - **The other over-bill path is an `added` entry for someone who did
    tap in.** The sheet guards it with a hint, and the person's own row is
    right above. It cannot be closed by the server, which has no way to
    know who an added helper is.
- **A submitter with no timer** (a desk user, or a tech who never tapped
  in) has no "You" row. A tech's own hours then go on an `added` row and
  pay nobody. That is today's single-day closeout rule for a caller without
  a timer (`jobs.py:2549`, #529). The sheet says so: "You didn't tap I'm
  here today, so these hours aren't added to your pay. Tell the office."

**`update_appointment` refuses moving an arrived visit to another day**
(409 `visit_arrived`, "Undo the arrival first").

- It refuses a `start_at` on a different shop day from the stored one, on a
  visit with `arrived_at` set.
- The edit form resends every field on every save
  (`appointments.py:612-613`), so the test compares with the stored value,
  not "named in the request". A notes edit on an arrived visit succeeds.
- Every other mover already takes only OPEN visits: Move
  (`visit_sync.py:665-666`, via `_find_open`, `:624`) and crew changes
  C1/C6 (`:395-412`).
- Why: see "Audit rounds 13–33" (round 33).

**The day row's note** lives on that `time_entries.notes`.

- The closeout picker matches only the exact `"Closeout-attached"` marker
  (`jobs.py:1917`) and the `"Timer stopped on mobile"` prefix (`:1947`).
- The picker also excludes every `day_closed_at` row explicitly (below).

**Daily log: `GET /api/jobs/{id}/day-log`.**

- It lists each day row (`day_closed_at` set, minutes > 0), newest first,
  with these fields:
  - the shop-local date;
  - the person's name, from the user id the `job_day_closed` audit row
    records for that row, or "Added helper" for an `added` row;
  - hours, and the note;
  - who closed it, from the audit row.
- Permission: the same as reading the job.
- UI:
  - a "Daily log" card on `JobDetailView.vue`;
  - a section on `MobileJobDetailView.vue` (R-P3);
  - both hidden when the list is empty.

**Closeout ("Yes")** changes only as follows.

- **The lock, and the earlier-day refusal.** `closeout_job` reads the job
  row `with_for_update()` **first, before the timer step** (`jobs.py:2517`).
  That is the same lock as day-close.
  - It is not taken just before billing. If it were, a racing "No" would
    commit between the two, and the timer step would overwrite the day row's
    duration.
  - Then it refuses `earlier_day_open` (defined under "The sheet", below).
  - A "No" racing a "Yes" therefore commits entirely before the closeout's
    timer step, or after the closeout commits. In the second case it is
    refused `job_finished`, or is the 200 no-op if that same submission
    already landed.
- **A 0 h "Yes" is allowed only if today was already closed with "No" and
  nobody still has a timer running on this job today** (Doug, 2026-10-06, after
  round 36; replaces the round 34–36 versions, which asked what was left on
  the whole job and were patched three times).
  - `closeout_job` refuses `hours <= 0` when the tenant's
    `require_hours_on_complete` is on (`jobs.py:2282`), and prod has it on.
  - "Today was closed": the job has a `job_day_closed` audit row whose
    `details.day` is the tap's shop day. Read in Python from the job's
    audit rows (compared as `entity_id == str(job.id)`), not through a JSON
    operator, so SQLite and Postgres agree.
  - "Nobody still has a timer running today": the job has no candidate
    timer on the tap's shop day (the definition above: open or
    Stop-marked, `day_closed_at` NULL). This catches a helper who tapped in
    again after the "No", and a person the "No" left unlisted, whose timer
    a "No" never touches; a 0 h "Yes" would otherwise close either at 0.
  - Otherwise the flag refuses 0 exactly as today. That covers the cases
    the earlier versions missed: day 2 closed and day 3 worked with a
    timer, day 3 worked without a tap-in, and a helper who taps in again
    after today's "No". The hours field starts at **0**
    (`MobileJobCloseoutDialog.vue:302`, `hours = ref(0)`), so any wider
    skip restates a running timer at 0 (`jobs.py:2562`) and loses the day.
  - Falsifier, accepted: work attested after today's "No" with no timer at
    all (a visit completed by hand). The flag misses that today too.
- **The other two ways a job finishes** (round 35). `/complete`
  (`jobs.py:1597`, no SPA caller, reachable over the API) and
  `/close-without-work` (`jobs.py:1698`, the job page's button at
  `JobDetailView.vue:2770`) write no closeout row.
  - Both refuse 409 `earlier_day_open`, the sheet's own predicate, checked
    after the job-row lock. Close-without-work 0-closes every open timer
    (`:1743-1745`), so without the gate a forgotten day's timer is lost by
    that door. A single-day no-show is untouched: its one visit is the
    latest Current visit, on the same day, and not day-closed.
  - Neither route locks today (`jobs.py` has no
    `with_for_update`), so both gain the same locked job select as
    closeout, before reading timers (round 36). Without it a "No" that
    commits between close-without-work's timer read and its write has its
    new day row overwritten with 0.
  - Both write their audit row with `audit_or_rollback`
    (`core/audit.py`) **before** `db.commit()`, not
    `log_audit_event_sync` after it (`jobs.py:1668-1680`, `:1746-1765`).
    The completion and the row that bounds candidate timers then commit
    together or not at all. The bound query compares
    `audit_logs.entity_id` (a String) to `str(job.id)`, the dashed form
    both routes write, never to a `Uuid` value.
  - **The office's way out:** the Close-without-work dialog
    (`JobDetailView.vue:2767-2779`, today a bare `catch {}`) shows the
    `earlier_day_open` refusal inline: "<weekday, date> is still open.
    Close that day first", with a button that opens the job page's
    "Is this job finished?" sheet. `/complete` has no SPA caller, so its
    409 body carries the same code and date.
  - Neither checks `later_day_started`. Each acts on the server's now, and
    `clock_in` is never later than now, so no later day can have started.
  - Both stay allowed on a job with day rows. Those rows are billed by the
    suggestion (see Billing: the earlier-visits line no longer needs a
    closeout), so "customer cancelled the rest" still invoices days 1–2.
  - Close-without-work's dialog shows "Already logged: N h on earlier days
    (still billed)" when the job has day rows, so the office is not told
    nothing is billable.
  - Their audit rows bound the candidate timers (above).
- **The labor picker** (`jobs.py:2515-2588`): `_owned_closeout_labor_entry`
  and `_stopped_job_timer_for` add `day_closed_at IS NULL`. So neither a day
  row nor a timer day-close consumed at 0 is ever restated or re-targeted.
  - `_open_job_timers` needs nothing: a `day_closed_at` row is always
    closed.
  - **The caller's target timer is unchanged** (`jobs.py:2525`, newest open
    first). A "No" leaves no open timer behind for any person it named:
    their own "No" turns it into their day row, and any other "No" closes
    it at 0.
  - So a target timer is either today's, or one the caller started after
    their own close. Its hours are paid on that timer's own day (round 11).
- **The sheet's hours label** (`MobileJobCloseoutDialog.vue:895`).
  - On a job with day rows, the label becomes **"Hours worked today"**.
  - Above it: "Already logged: N h (billed separately)". This counts every
    day row, including a helper's from earlier today, so "previous days"
    would be wrong.
  - When a day row from today exists, the techs-on-site hint reads "Don't
    count techs whose day is already logged". Techs on site still defaults
    to 1 (`MobileJobCloseoutDialog.vue:305`); hours starts at 0 (`:302`).
  - Without this, a tech could enter whole-job hours and bill the previous
    days twice.

**The sheet** (`MobileJobCloseoutDialog.vue`).

- It opens on **"Is this job finished?"** (Yes / No) before any other
  section.
- **Yes:** today's form. The return-visit checkbox is relabelled **"Needs a
  follow-up job (new work)"**, with the hint **"Not finished? Answer No above
  instead."** It is shown on Yes only (D8).
- **No** shows four parts.
  - **"Visits done for <day>"**: one checkbox per Current visit of
    `open_day.date`, showing its tech name (or "Unassigned") and time, all
    checked by default.
    - Unchecking leaves that visit open. The sheet then warns: "This job
      stays on the board until every visit is closed."
  - **"Hours"**: one row per person in `open_day.people`. Each row has a
    checkbox, checked by default, and a required hours field.
    - The caller's own row comes first, labelled "You", and its field
      starts blank.
    - Every other row shows the "You" value until edited. On a desk
      sheet, with no "You" row, every row starts blank.
    - **A person who already has day rows on that day** (a lead's earlier
      "No" closed them, and they tapped in again) shows "Already logged for
      <day>: N h (by <closer>). Enter only the time since." Their field
      then starts blank, never with the "You" value (round 33).
    - Unchecking a person leaves their timer open, with the warning
      "<name>'s time stays open until they answer this sheet or the job is
      finished".
  - **"+ Add a helper who didn't tap in"** adds an hours row. The hint
    reads: "Only for someone with no row above — anyone who tapped I'm here
    already has one."
  - **Note for the office** (optional, placeholder "What's left?"), and a
    line pointing to the existing live parts capture. This PR adds no parts
    path to "No".
  - It submits through `api.postQueued` with `day`, `visits`, `people`,
    `added`, `closed_at`, `note` and `conflictIsError: true`.
- **Where the sheet gets its rows, on every surface:**
  `GET /api/jobs/{id}/day-log` also returns
  `open_day: {date, visits: [{id, tech_name, start_at, state}], people:
  [{user_id, name, mine, logged: [{hours, closed_by}]}]}`.
  - `people` are the people of `open_day.date`.
  - `logged` is that person's day rows on that date, read from the
    `job_day_closed` audit rows (step 7 lists each person's user id with
    their hours, day-row id and the actor). It is never matched by
    `tech_id`. A timer's `tech_id` is the user id when the user has no
    technician record (`mobile.py:784`), and an `added` row carries the
    submitter's user id, so a `tech_id` match would show a submitter their
    own added helpers' hours (round 34).
  - `open_day.date` is the **oldest past worked day**: a day with a Current
    visit someone arrived on (ON_SITE), **or with a candidate timer**. If
    there is none, it is today.
  - The timer counts because the phone's Start button needs only an
    en-route job (`MobileJobDetailView.vue:1608-1610`), and `mobile_clock_in`
    never stamps a visit. A tech who tapped On my way, started the clock and
    forgot to close leaves an OPEN visit and a running timer. Without the
    timer clause, that day would read as a rain-out, and a later "Yes" would
    0-close its timer (round 16).
  - A past day with neither is a no-show, which is not this sheet's to
    close. PR 1 already routes it: it stays Current, the job sits on the
    late-open card, and the office reschedules it there.
  - So a forgotten day comes first. `partial_clause` (`visit_sync.py:727`)
    counts any not-completed visit as Current, whatever its date, so an
    unanswered day 2 keeps the job on the late-open card. Nothing would
    otherwise reach that day again.
  - **An earlier worked day blocks "Yes".** Suppose `open_day.date` is
    **before today** (shop-local), **and** either it is before the job's
    latest Current visit's shop day, or a day-close closed a visit on it
    (`appointments.day_closed_at` set on a visit of that day). Then a
    worked past day is still open, and it isn't the final day.
    - The second arm is a past day whose visits a "No" closed while a
      person was unchecked: their timer is still open. A "Yes" would 0-close
      it and their hours would bill nowhere (round 33).
    - It is keyed on a day-close, not on "no Current visit" (round 34). A
      job with no visit at all, or one whose only visit the office
      Completed by hand, can carry a running timer from an A3 arrival. Prod
      had 11 such timers on 2026-10-06. That job's next-morning "Yes" stays
      allowed, exactly as today.
    - "Before today" is what keeps an early finish open. On day 3 of 4,
      with day 4 booked, `open_day` is today, so Yes is allowed. Yes then
      retires day 4, per F1.
    - **Yes is disabled**, and the sheet reads "Close <weekday, date> first:
      answer No for that day".
    - The server enforces the same rule: `closeout_job` refuses with 409
      `earlier_day_open`. The closeout already sends `conflictIsError`.
    - **"Today" is the tap's shop day, not the server's.** `CloseoutPayload`
      gains an optional `tapped_at` that the sheet sets. It is clamped to no
      later than server `now`, and is server `now` when absent.
      - Without it, take an early finish on day 3 of 4, queued offline and
        replayed on day 4's morning. It would see day 3 as "before today"
        and be refused.
      - `tapped_at` feeds only this refusal. Everything else the closeout
        stamps still uses server `now`, as today.
    - **A later day already started refuses too.** The closeout refuses
      with 409 `later_day_started` if, on a shop day after `tapped_at`'s
      day, any Current visit is ON_SITE **or** the job has a candidate
      timer (round 34: Start needs only en route, and `mobile_clock_in`
      never stamps a visit, the same gap `open_day`'s timer clause closes).
      The message is "Day not recorded: the crew has already
      started <date>; tell the office."
      - Otherwise, a replay landing after the next crew arrived would close
        their visit through `_finish_visits` (`jobs.py:1559`) and 0-close
        their timers, and their day would bill nowhere.
    - A single-visit job closed out the next morning is not blocked: its
      past day *is* its last Current day.
    - This works from the late-open card (desk) and from the phone.
  - The sheet reads `open_day` when it opens, and submits the ids it read
    then. A replay therefore names the original day.
  - The dialog needs no surface-specific prop, and a dispatcher gets real
    rows (R-P6).
  - Offline on the phone, the rows come from the last cached read.
- **Nothing to submit:** with no Current visit up to today and no people,
  "No" shows "No visit is booked on this job today — the office books the
  next day" (D3), and submits nothing.

**The timer of a forgotten day (B8).** Arrival
(`mobile_job_arrived`, `mobile.py:2512`) reuses an open timer only if its
`clock_in` is on today's shop day. Otherwise it opens a new one, and leaves
the old one open for that day's "No".

- Today it reuses any open timer on the job (`_find_open_time_entry` has no
  date bound, `mobile.py:725`), so day 3's work would post under day 2's
  `clock_in`.
- **Manual clock-in changes the same way.** `mobile_clock_in` (`:2816`)
  refuses with 409 only when an open timer on today's shop day exists. An
  older one doesn't block today's.
- Stop (`:1001`) and the toggle (`:849`) need nothing: they already take the
  newest open timer (`ORDER BY clock_in DESC`).
  - **Corrected in the build (2026-10-06, final-diff audit):** wrong once
    today's timer is stopped, since the newest open timer is then the
    forgotten day's, which the toggle showed running for 24 h and Stop
    0-closed. Both now read today's timer only, as arrival and clock-in do.
- On a single-day job nothing changes: closeout ends every timer, so no
  timer survives to a later day.

**The phone on day 2 (B3, B4, B7).**

- **The visit payload:** the mobile job payload gains `today_visit`, the
  tech's visit for today by the A1 rule (`{id, state, en_route_at,
  arrived_at}`, or null).
- **The buttons** (`MobileJobDetailView.vue:903-925`) read `today_visit`
  when it is present:
  - On my way when it is OPEN and not en route;
  - I'm here when it is en route;
  - Complete when it is ON_SITE.
  - With no `today_visit`, they read `job.dispatch_status` as today.
- **B3:** `_validate_forward_transition` (`mobile.py:186-204`) runs against
  the visit's progress when a `today_visit` exists. The job's status is then
  rolled up from it, as in day-close step 5. Within a visit it still never
  goes backwards.
- **B4:** en route stamps `en_route_at` on today's visit if it is null.
  Arrival already stamps it (`_stamp_arrival`). The JobAssignment first-day
  columns are unchanged (`job_assignments.py:380-385`).
- **B7:**
  - The stage write in `start_job` (`jobs.py:1465-1490`: `started_at` if
    null, `lifecycle_stage='in_progress'`, `status='In Progress'`) is
    factored into `_mark_job_started(job, now)`.
  - En route and arrival call it when the stage is before `in_progress`.
  - No new stage path is invented. There is no stage-write scanner, so the
    tests are the guard.

**Billing (§5.5, D1 (a), R-P1)**, in `build_closeout_lines`
(`core/closeout_billing.py:138`), `service` lane only:

- `prev = Σ duration_minutes/60` over the job's day rows: `day_closed_at`
  set, `deleted_at` null, `clock_out` set. A consumed timer adds 0.
- If `prev > 0`, there is one line, **"Labor — earlier visits"**:
  - It is not called "previous days", because a helper's day closed by "No"
    earlier the same day is on it too.
  - quantity = `prev` rounded up to the half hour;
  - unit price = `service_call_hourly_rate`;
  - `taxable` = the tenant's `labor_taxable` flag, the same as the final-day
    labor line. This PR changes no tax behavior;
  - `source = AUTODRAFT_LINE_SOURCE`, so a re-closeout's
    `release_untouched_autodraft` owns and rebuilds it.
- The final-day line (`:264-269`) is unchanged.
- The earlier-visits line is built **outside** the final-day line's
  `hours_worked > 0` check. A job whose final day attests 0 h still bills
  the previous days.
- **Invoice prefill:** `GET /{id}/closeout-billing-suggestion`
  (`jobs.py:3055`) gains an `earlier_visits_line` (or null) beside
  `labor_line`. It is computed from the day rows **before** the
  `closeout is None` early return (`jobs.py:3103`), and returned on that
  path too. A job finished by Close-without-work or `/complete` has no
  closeout, and would otherwise never put its earlier days on any invoice
  (round 35).
  - It is non-null only when the job is **completed**. Mid-job it is null,
    so a hand-made mid-job invoice cannot be offered the same days again
    at the end, and nothing mid-job changes from today.
  - Every reader's `has_closeout` guard moves below its
    `earlier_visits_line` use (round 36): `InvoiceCreateView.vue:790`
    (`if (!s?.has_closeout) return;`) and `LaborPickerDialog.vue:297`
    and `:314`. `InvoiceDetailView.vue:1278` only fetches; its consumer is
    the picker. A guard left in place would drop the line exactly on the
    no-closeout jobs this exists for.
  - Each reader adds it as a second line: `InvoiceCreateView.vue:802`, and
    the picker (`LaborPickerDialog.vue:296/313`), which serves both
    `InvoiceCreateView` and `InvoiceDetailView.vue:1278`. A reader left
    unchanged would silently drop the previous days.
- **The mobile invoice path** (`mobile_invoicing.py:849`) calls
  `build_closeout_lines` and inherits the line.
- **Accepted-estimate jobs:** the estimate prices them.
  - The autodraft skip (`closeout_billing.py:523-532`) is unchanged.
  - So is office invoice creation from an estimate (`create_invoice`,
    `invoices.py:1198`, which reads no closeout labor).
  - Day rows add no labor to an estimate-priced invoice. Neither does a
    single-day closeout today. This settles §5.5's "not yet traced".
- **Existing rows:** none change. No job has a day row until a "No" is
  submitted, so every existing and every single-day job bills exactly as
  today.
- **Rollback:**
  - Revert the PR and run the downgrade.
  - Day rows and visits lose `day_closed_at`.
    - A submitter's row reads as an ordinary closed job timer.
    - A `user_id` NULL row reads as a closeout's synthetic `'work'` row.
    - Their minutes stay on them, and the user-less rows stay unpaid.
  - A draft rebuilt after the rollback drops the earlier-visits line. An
    issued invoice is never restated.
  - A Stop-marked timer that day-close consumed at 0 loses its marker. For
    24 h after it was stopped, `_stopped_job_timer_for` could pick it for a
    "Yes" again. That restates the same tech's own timer, as it did before
    this PR, so the window is bounded and no row is double-billed.

**Tech efficiency** (`routers/tech_efficiency.py:7`, a missed reader). The
denominator becomes `closeout.hours_worked` plus the job's **wall-clock
days**.

- Per shop day, that is the longest day row (D13's `worked`).
- The exception is **the closeout's own shop day**
  (`job_closeouts.created_at`, shop-local). That day counts
  `max(hours_worked, longest day row that day)` instead of adding
  `hours_worked` on top.
- Otherwise a multi-day job inflates the ratio.

- Why max, not a sum, on the closeout's day: `hours_worked` is wall-clock
  for that day too.
  - A helper who left early (a 4 h row beside an 8 h closeout) reads as
    8 h, not 12.
  - A crew "No" of 6 h followed by a 0 h "Yes" the same day reads as 6 h,
    not 0.
- A next-morning closeout is the known approximation. Its shop day differs
  from the work day, so a same-work-day helper row is added. That costs
  only this report's ratio, not money.
- The ratio compares the job's estimate (wall-clock) with time on site
  (wall-clock), so it is not keyed by tech.
- The `closed_in_window` filter `jc.hours_worked > 0` (`:92`) becomes "that
  sum > 0". Otherwise a job whose final day attests 0 h, which the
  earlier-visits line deliberately bills, would drop out of the report.

**D13: queued hours.** For a partial job's row, `_duration_fields`
(`dispatch_scheduling.py:105`) returns `effective_duration_hours`. That
becomes `max(0, scheduled_duration_hours − worked)`.

- `worked` sums, per shop day, the **longest** day row that day
  (`day_closed_at` set).
- The job's hours are wall-clock: tech efficiency compares them with
  per-tech `hours_worked`. So two techs working 8 h on one day used 8 h of
  the job, not 16.
- `scheduled_duration_hours` itself is unchanged.
- A job with no estimate stays no-est.

**Pay identity (R-P5).** Payroll's `_fetch_tech_hours` reads `time_entries`
by `user_id` (`payroll.py:243-270`). Day-close writes hours onto a
`user_id` row only through the submitter's own timer, carrying the one
number they typed for themselves. So:

- A tech's own "No" pays their own day, whatever visits they were on, and
  as an A3 arrival too.
- A person left unchecked keeps an open timer, and their own "No" or "Yes"
  pays it.
- A desk "No" pays no one, exactly like a desk "Yes" today: the techs'
  timers close at 0, and their rows have `user_id` NULL.
- B8 keeps a forgotten day from costing the next one, because each day keeps
  its own timer.

**Tests (each must fail with its fix reverted):**

- **day-close, money:**
  - Solo tech T, one visit, "No" with T 8 h: T's open timer becomes the day
    row with T's `user_id`, 8 h, the rate and the note. The visit is
    completed with `day_closed_at`. T is paid 8, and the job bills 8.
  - T's Stop-marked timer that day is restated, not duplicated, including
    when the day is closed 3 days later.
  - **Same-day return:** T arrives on V1, and later on V2 (two timers, or
    one). One "You" row. T 8 h pays 8 and bills 8. Each of T's other
    candidate timers closes at 0 with the marker.
  - **Round-32 fixture A:** T and U both tap unassigned V1. The office books
    U a same-day V2, and U taps it. `open_day.people` is {T, U}, one row
    each.
    - U submits T 8, U 8: U's timer is the day row (8 h, U's `user_id`).
      T's timer is consumed at 0, and one `user_id` NULL 8 h row has T's
      `tech_id`. U is paid 8, T 0, and the job bills 16.
    - The same body submitted by T mirrors it.
  - **Round-32 fixture B:** the office Completes T's V1 mid-day. T taps
    again and lands on unassigned V2. `open_day.people` is {T}, one row.
    T 8 pays 8 and bills 8.
  - A3: a helper H with no visit is a person like any other. The lead's "No"
    with H 6: H's timer is consumed at 0, and a `user_id` NULL 6 h row is
    written.
  - A desk "No" with T 8 and U 8: both timers are consumed at 0, and there
    are two `user_id` NULL rows. Nobody is paid, and the job bills 16.
  - `added` [4]: one `user_id` NULL 4 h row, with `tech_id` = the
    submitter's id and the "Added helper" note. The insert succeeds on
    SQLite and Postgres.
  - A tech submitter with no timer: no "You" row, and their `added` hours
    pay nobody.
  - Unchecking U leaves U's timer open and unmarked. U's own people-only
    "No" later pays U.
  - A person who tapped in after the sheet was read is not in the body.
    Their timer is left open, `open_day` returns that day again with them on
    it, and a past day blocks Yes (`earlier_day_open`).
  - A visits-only body closes the visits and writes no rows.
  - Every listed person's timers are consumed, and every unlisted one is
    untouched (one fixture with three people, one unchecked).
- **day-close, replay and refusals:**
  - A lost-response replay with the same `closed_at` is a 200 no-op. That
    holds after the job was finished, and for a visits-only body, which is
    matched through `appointments.day_closed_at`.
  - A replay whose `closed_at` is ahead of server time is the 200 no-op.
  - A queued "No" replayed after the office's "Yes" closed its visit is
    `job_finished`, not 200.
  - `visit_not_open`: a closed visit, a deleted visit, and another job's
    visit. A visit closed by another day-close carries `already_closed`. One
    the office Completed by hand does not.
  - `day_moved`: the office moves a listed visit to tomorrow before the
    replay. The visit stays open tomorrow, and the timer stays open today.
  - `person_not_open`: a user with no timer on D. A second closer naming a
    person already closed carries `already_closed`.
  - A 422 for each of these:
    - a duplicate visit id, and a duplicate user id;
    - hours 0, 24.5 and −4 in `people` and in `added`;
    - eleven `added` entries;
    - an empty body;
    - a 2001-character note;
    - `day` missing and malformed;
    - `closed_at` missing, malformed and naive.
  - Day bound: a day-2 "No" replayed on day 3 with a day-3 timer open closes
    day 2's visits and timers. The day-3 timer is untouched.
  - Concurrency, Postgres only:
    - two sessions submitting the same body produce one set of rows;
    - a people-only "No" from U and the lead's "No" naming U leave one row
      for U's day.
  - Atomicity: a failure in step 2 leaves the visits open.
- **day-close, state:**
  - Roll-up: `assigned`, `en_route` and `on_site` cases. The roll-up
    ignores the closed visits.
  - Recompute to the next visit, and to Partial Jobs.
  - The `job_day_closed` audit row names every person and their hours.
  - Day-close NULL rows are `entry_type='work'`.
  - The queued action is `job.day_close`. A second queued "No" for another
    day is not retired by the first.
- **`open_day`:**
  - It returns a forgotten earlier day (an arrived visit, or a running
    timer with no arrival) before today. After that day closes, it returns
    today.
  - A rained-out past day (no arrival, no timer) does not block.
  - `people` lists each user with a candidate timer once, whatever visits
    they were on.
  - A day-2 timer started with Start (no arrival) whose OPEN visit the
    office moved to day 5 gives `open_day` day 2 with no visits and that
    person. A people-only "No" closes it, and
    then "Yes" is allowed.
- **Closeout:**
  - `earlier_day_open`:
    - Yes is refused while a worked past day is open and isn't the final
      day.
    - A single-visit job closed out the next day is not refused.
    - An early finish on day 3 of 4 with day 4 booked is not refused.
    - Tapped on day 3 and replayed on day 4 with `tapped_at`, it is not
      refused. The same replay after day 4's crew arrived is
      `later_day_started`.
  - The picker skips `day_closed_at` rows: a day row is never restated or
    zeroed, and a consumed Stop-marked timer is never picked.
  - The closeout's target timer is unchanged (newest open first). No test
    expects a skip.
  - Same day: the helper answers "No" for themselves, then the lead
    answers "Yes". The lead's own timer carries the lead's hours with their
    `user_id`, and the helper's row bills on the earlier-visits line.
  - The same with the lead's Yes the next morning: the lead's own timer is
    still the target.
  - The lead answers "No", clocks in again, and taps "Yes" offline. It
    replays after midnight: the new timer is the target and carries the
    lead's `user_id`.
  - Closeout lock: the job-row lock is taken before the timer step.
    Asserted on the statement order, Postgres only.
- **Round 33:**
  - `update_appointment` moving an arrived visit to another shop day is
    409 `visit_arrived`. A notes edit resending the same `start_at`
    succeeds, and so does a tech change.
  - The lead's "No" closes U at 4 h. U taps I'm here again, and U's
    sheet shows "Already logged for <day>: 4 h (by Lead)" with a blank
    field. U 4 then bills 8 in total.
  - A "No" closes every visit of a past day with U unchecked: "Yes" is
    `earlier_day_open` until U's day is closed.
- **Round 34:**
  - A job with no visit, and an A3 timer from yesterday: the next morning's
    "Yes" is not `earlier_day_open`.
  - So is a job whose only visit the office Completed by hand while a timer
    ran.
  - A day-3 "Yes" queued offline and replayed on day 4, after day 4's crew
    tapped On my way and Start but not I'm here, is `later_day_started`.
  - A submitter with no technician record adds a helper: their own
    `logged` does not include the helper's hours.
  - A finished job with a helper's leftover Stop-marked timer is re-opened
    and booked a new day: that timer is not a candidate, `open_day` does
    not return its day, and "Yes" is allowed.
- **Round 35:**
  - Close-without-work and `/complete` on a job whose earlier day has an
    open timer and a later Current visit: 409 `earlier_day_open`, nothing
    written. A single-day no-show from yesterday still closes.
  - Close-without-work on a job with day rows: allowed, and the suggestion
    returns `earlier_visits_line` with no closeout.
  - Close-without-work leaves a helper's Stop-marked timer; the job is
    re-opened and booked a new day: that timer is not a candidate.
- **Round 36:**
  - A Close-without-work job with day rows: the invoice create page and
    the labor picker both show the earlier-visits line. The suggestion
    returns it null on an in-progress job.
  - Postgres: a "No" racing Close-without-work either lands before it
    (its day row survives at its hours) or is refused `job_finished`.
  - An audit write that fails inside Close-without-work rolls the
    completion back.
  - Close-without-work refused `earlier_day_open` shows the inline message
    and its button opens the sheet.
- **The 0 h "Yes"** (`require_hours_on_complete` on; replaces the round
  34–36 tests):
  - Today closed by "No", no timer left today: allowed, and bills only the
    earlier-visits line.
  - Day 2 closed by "No", the caller's day-3 timer running: refused; a 5 h
    "Yes" bills 5 h on the final-day line beside the earlier-visits line.
  - Day 2 closed by "No", day 3 worked without a tap-in: refused.
  - Today closed by "No", then a helper taps I'm here: refused.
  - Today closed by "No" with a person left unlisted, timer running:
    refused.
  - A job with no "No" anywhere: refused as today.
- **B8:**
  - Arrival on day 3 with day 2's timer still open opens a new timer. Day
    2's "No" then closes only day 2's timer.
  - `mobile_clock_in` with yesterday's timer still open opens today's.
- **Billing:**
  - Day rows of 3 h and 4.2 h give one 7.5 man-hour line at the hourly rate,
    with no first-hour price and no 1 h floor.
  - That line carries `source=autodraft`, and a re-closeout rebuilds it.
  - The final-day line is unchanged.
  - An accepted-estimate job is unchanged.
  - A job with no day rows is byte-for-byte unchanged.
  - The suggestion's `earlier_visits_line` is shown by each of its three
    readers.
- **The migration:** both columns, up, down and up again, on SQLite and on
  Postgres (in CI). Run it both on a database `create_all` built and on one
  it did not.
- **Phone:** the day-2 en-route gate opens from `today_visit`. B7 moves the
  stage through `_mark_job_started`.
- **D13:** two techs with 8 h each on one day subtract 8.
- **Tech efficiency:**
  - An 8 h closeout plus a same-day 4 h helper row gives a denominator of
    8, not 12.
  - A same-day 6 h "No" plus a 0 h "Yes" gives 6.
- **Sheet:**
  - The Yes and No branches.
  - The relabel shows on Yes only.
  - Visits are checkboxes with no hours field.
  - There is one hours row per person, "You" first. A desk sheet has no
    "You" row and starts blank.
  - "+ Add a helper" adds a row, and the body carries `added`.
  - The request carries `conflictIsError: true`. A mocked 409 lands in the
    failed list, not as synced.
  - The "Hours worked today" label shows when day rows exist.

**Audit rounds 1–12 (2026-10-06)** shaped what is still above:

- **Round 1:**
  - the body names the day it closes;
  - no `_record` inside the transaction;
  - R-P4;
  - the final-day hours label;
  - `_mark_job_started`;
  - #529 for desk rows;
  - the `labor_taxable` flag;
  - the missed suggestion readers and tech efficiency.
- **Round 2:**
  - visits closed by id, with R-P6;
  - `conflictIsError`;
  - the Stop-marker match and the shop-day bound;
  - R-P5 (crew hours never land on a helper's pay row);
  - OPEN visits listed.
- **Round 3:**
  - the NOT NULL `tech_id` on synthetic rows;
  - `open_day`, oldest day first.
- **Round 4:** `is_current` everywhere; wall-clock tech efficiency; the
  job-row lock.
- **Round 5:** `open_day` forces only a worked past day; the timer query
  is its own, with no 24 h window.
- **Round 6:**
  - B8;
  - the closeout lock moved before the timer step;
  - `earlier_day_open`.
- **Round 7:** the before-today bound on `earlier_day_open`;
  `mobile_clock_in` bound to the shop day.
- **Rounds 8–11:** the closeout's target timer stays "newest open first",
  with no skip. Every "No" consumes the closed day's timers, so a skip could
  only ever fire on a timer started after the caller's own close, and would
  then pay nobody. Tech efficiency takes `max()` on the closeout's own day.
- **Round 11:** `tapped_at`.
- **Round 12:** `later_day_started`.

**Audit rounds 13–33 (2026-10-06): the per-visit model, replaced.** From
round 13 the sheet put hours on **visits**: one hours field per visit, plus
a Techs stepper on unassigned visits. Money then needed to know which
person each visit's hours belonged to, and each round answered that and
found the next case:

- **Round 13:** a timer→visit link (`time_entries.appointment_id`), because
  "helper" matched every arriver on an unassigned visit and double-billed a
  desk "No".
- **Round 14:** hours per tech per day, with `hours: 0` on sibling visits
  for a same-day return, and a legacy unassigned-visit fallback.
- **Rounds 15–16:**
  - `_visit_owner`, grouping a tech's visits into one row;
  - a per-visit `techs` count;
  - `closed_at` as the replay key;
  - a worked day including a running timer.
- **Rounds 17–21:**
  - stale links (the office Completes, Moves, Removes or reassigns a
    visit), with the `crew_changed` refusal, `unchecked_helpers`, and the
    own-visit fallthrough;
  - an `update_appointment` guard against changing an arrived visit's tech
    or day (`visit_arrived`).
- **Rounds 22–24:**
  - `appointments.day_closed_at`;
  - the body's `day` and `day_moved`;
  - a guard against reopening a day-closed visit (`visit_day_closed`).
- **Rounds 26–29:**
  - an explicit check order, and shape checks that read the body only;
  - an `hours: 0` rule needing every user on the visit to be counted
    elsewhere;
  - a no-user row prefilled blank;
  - one `_visit_users` helper.
- **Round 30:** `appointments.arrived_by_user_id`, because a solo tech's
  second tap on an unassigned visit left it with no owner and split the
  tech across two rows.
- **Round 31:** the submitter's visits are those they are on, whoever owns
  them; a grouped row's hours sit on an "anchor" visit.
- **Round 32** found that rows were grouped by owner while pay followed
  `_visit_users`.
  - Two unassigned arrivers plus a same-day V2 put one person on two rows.
    With the defaults that paid 16 and billed 24 for an 8 h day.
  - An office Complete mid-day brought back round 20's double row through
    `arrived_by_user_id`.
  - Four membership rules now existed, each round patching the last pair.

The fix was to stop asking the question: one hours row per person, with
visits as checkboxes. What carried over, and what was withdrawn:

- **Kept:**
  - `closed_at` as the per-submission key;
  - `appointments.day_closed_at`;
  - `day` and `day_moved`;
  - the check order's principle (shape 422s read only the body; replay
    first);
  - `already_closed`;
  - "Current" meaning not deleted;
  - the running-timer clause of `open_day`.
- **Withdrawn, because they existed only to map hours to visits:**
  - `time_entries.appointment_id` and arrival's link;
  - `appointments.arrived_by_user_id`;
  - `_timer_visit`, `_visit_owner`, `resolved`, `_visit_users`;
  - the anchor and the Techs stepper;
  - the `hours: 0` rule;
  - `crew_changed`, `unchecked_helpers` and `helper_not_open` (now
    `person_not_open`).
- **`visit_day_closed` is withdrawn.** It stopped a reopened visit from
  billing a second per-visit row. A reopened day-closed visit now carries no
  hours, and its people's timers are already consumed, so closing it again
  writes nothing.
- **`visit_arrived` keeps only its day half** (round 33). Round 32's draft
  withdrew it whole, judging it by money alone. But `visit_state` reads any
  `arrived_at` as ON_SITE (`visit_sync.py:59`), and `update_appointment`
  moves `start_at` without clearing it. So a day-2 arrived visit dragged to
  day 5 would read as worked on day 5:
  - `later_day_started` would refuse every "Yes" until then;
  - `open_day` would treat day 5 as a worked past day.

  Changing an arrived visit's tech stays allowed: no refusal and no money
  reads it now.
- The remaining over-bill path, an `added` entry for someone who did tap
  in, is stated under "What it bills and pays".

**Audit round 33 (2026-10-06)** checked the reshape against the code. It
found one submission bills each person once, and four problems, all folded
in above:

1. Withdrawing `visit_arrived` whole let an arrived visit be moved to
   another day, where it reads as ON_SITE and trips `later_day_started` →
   the day half is kept.
2. §5.4's "No timer open" rule and §5.5's `service_labor_line` still
   contradicted §5.4a → marked superseded in both.
3. A person closed by the lead who taps in again could be billed twice by
   their own "No" → the sheet shows what is already logged for them.
4. `earlier_day_open` had no rule for a past day with no Current visit →
   defined.

**Audit round 34 (2026-10-06)** confirmed round 33's fixes against the
code, plus read-only prod queries. It found five problems, all folded in
above:

1. Prod has `require_hours_on_complete` on, so the 0 h "Yes" every forced
   "No" ends in was refused → skipped when the job has a day row.
2. Round 33's "no Current visit" arm blocked a single-day job's
   next-morning "Yes" when an A3 timer ran on a job with no visit → keyed
   on a day-closed visit instead.
3. `later_day_started` missed a later day started with Start but no
   arrival → it counts candidate timers too.
4. `logged` matched by `tech_id`, which collides → read from the audit
   rows.
5. A helper's Stop-marked timer outlived a finished job, and a re-open
   could bill that day twice → candidates start after the latest closeout.

**Audit round 35 (2026-10-06)** confirmed round 34's fixes (the
`later_day_started` timer arm, the closeout bound across re-open, audit-row
durability, the day-closed arm of `earlier_day_open`). It found two
problems, folded in above:

1. The 0 h "Yes" skip fired on any job with a day row, so a crew answering
   "Yes" on day 3 with the hours field left at 0 lost the day → skipped
   only when no candidate timer remains. Also corrected: the field starts
   at 0, not 1.
2. Close-without-work and `/complete` were outside §5.4a: they could
   0-close a forgotten day's timer, never billed earlier days (the
   suggestion returned early without a closeout), and left no closeout to
   bound candidates → both refuse `earlier_day_open`, the suggestion
   computes the earlier-visits line without a closeout, and the bound uses
   their audit rows.

**Audit round 36 (2026-10-06)** confirmed the server half of round 35's
suggestion change and the 0 h narrowing's logic. It found five problems,
folded in above:

1. Every suggestion reader returns before the new line when there is no
   closeout → the `has_closeout` guards move, and the line is returned
   only on a completed job.
2. A final day worked without a tap-in has no timer, so the narrowed skip
   still fired. Doug then ruled the rule be made simple instead: 0 h only
   when today was closed by "No" and no timer is still running today.
3. The spec cited a job-row lock the two routes do not take → both gain
   one.
4. The bounding audit row commits after the completion → written with
   `audit_or_rollback` before the commit, compared as a string.
5. Close-without-work's 409 had no message or way out → an inline message
   and a button to the sheet.

## 6. PRs (stacked, merged bottom-up)

1. **Visits survive and arrival is safe (B1, B2).** Covers §5.2 and the
   arrival lookup.
   - No UI and no migration.
   - It fixes a latent crash on multi-tech jobs by itself, so it is worth
     shipping even if the rest waits.
   - Tests: the sync tests from §5.2, including the date move,
     completed-visit, insert-not-move and collision cases; arrival with two
     techs on one job; arrival with a visit on another day.
   - MERGED #846, RELEASED v1.137.0, to §5.2's rules.
   1b. **Revision 2 (§5.2a), its own PR against `main`:**
     `recompute_job_schedule`, wired into every writer in §5.2a's table with
     one test each, and the sync rebuilt to §5.2a's table with the
     whole-grid property test. (Written 2026-10-04 to ride in PR 1; #846
     merged first.) MERGED #868, RELEASED v1.138.0.
2. **The office books days (B5).** Covers §5.3: the visits API, the Visits
   card, the board showing each day, and the "Partial Jobs — Need to
   Schedule" section (D10–D12). Split as 2a and 2b (§5.3a); 2a MERGED
   #882 with its follow-up #886, RELEASED v1.139.0; 2b MERGED #891, RELEASED
   v1.140.0. (The
   recompute and its writers moved to PR 1b on 2026-10-04, §5.2a; the new
   visits API is one more writer and gets its own test.)
3. **"Is this job finished?" and summed billing (B3, B4, B6, B7).**
   Covers §5.4 and §5.5.
   - D2 picked a column, so this PR adds an Alembic migration (106:
     nullable `day_closed_at` on `time_entries` and `appointments`, §5.4a)
     that runs on SQLite and Postgres and has a downgrade. Existing rows
     stay NULL. (D2 named `appointment_id`; audit round 32 replaced the
     per-visit hours model, and with it the timer→visit link.)
   - If #842 has not merged by then, the desktop surface waits for it.
     (#842 MERGED 2026-10-04, so nothing waits.)
   - D13: a partial job's queued hours become the hours still left, the
     job's `scheduled_duration_hours` minus the attested day hours this PR
     records, floored at 0. They are never booked visit lengths or timer
     clock time, which are not evidence of hours worked. A job with no
     estimate stays no-est.

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
| D2 | How a day row is marked: a nullable `time_entries.appointment_id` column, or a note label? | **The column** (recommendation accepted). PR 3 marks day rows with `time_entries.day_closed_at` instead of `appointment_id` (§5.4a, after audit round 32). |
| D3 | After a "No" day with no next day booked: the office books it, or the tech picks the day? | **The office books it** (recommendation accepted). |
| D4 | Does the customer get an "on my way" text each day? | **Yes, each day** (recommendation accepted). |
| D5 | Can the crew differ by day? | **Yes** (recommendation accepted). |
| D6 | A separate "Done for today" button, or the closeout sheet asking "Is this job finished?" | **The closeout sheet asks.** Doug's idea: "No" makes it a daily log of the job. |
| D7 | What does a "No" day require? | **Hours.** Parts optional. |
| D8 | What happens to the "needs a return visit" checkbox? | **Keep it, label it differently.** Shown on "Yes" only; wording in §5.4, confirmed 2026-10-04. |
| D9 | Does the customer see anything on a "No" day? | **Nothing.** It is one job; the logs are for us. |
| D10 | (2026-10-05) Where does a job go when a day closes unfinished and nothing is booked after it? | **Its own Dispatch section, "Partial Jobs — Need to Schedule".** Doug: "it needs to go into a spot in dispatch that says partial job need to schedule." Replaces the "Continuing" label and the Dashboard note. |
| D11 | (2026-10-05) Where on the board? | **Above "New Jobs to Schedule."** |
| D12 | (2026-10-05) Does a drop book the next day? | **No — the office picks the day.** Doug: "the next day does not always work for the tech." A drop books the date the board is showing. |
| D13 | (2026-10-06) What does a partial job's "queued" total in its holding lane mean? | **The hours still left.** Doug: "queued hour means the hours still left." Until then it is the whole job's estimate (#886). It is built with PR 3, because the attested day hours it subtracts are first recorded there. |

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
