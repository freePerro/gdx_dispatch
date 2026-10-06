# Multi-day jobs: one job, many visit days

**Date:** 2026-10-03
**Status:** PARTIALLY BUILT. PR 1 (§5.2 sync, and the arrival lookup from §5.4) MERGED #846, RELEASED v1.137.0, to §5.2's rules. §5.2a (revision 2, 2026-10-04) replaces those sync rules and adds `recompute_job_schedule`, as PR 1b against `main`: MERGED #868 (2026-10-05), RELEASED v1.138.0 (on prod and demo 2026-10-05; walked in a browser that night: the full write walk on the demo, a read-only look at prod), with one rule the build added (R2's second clause, below, accepted by Doug 2026-10-05); Doug ruled its open questions 2026-10-04 (a day closes only when someone closes it). PR 2a (§5.3a: the visits API, the job page's Visits card, and the "Partial Jobs — Need to Schedule" section Doug ruled 2026-10-05, D10–D12) MERGED #882 (2026-10-06), not yet released; its parked-row follow-up (queue rows carry the board's hours) is in review. Not built: PR 2b (the board drawing each visit day) and PR 3 (the "Is this job finished?" sheet, B3, B4, B6, B7, billing). Doug ruled all decisions on 2026-10-04 (§8) and moved the day's stop into the closeout sheet (§5.4). The first draft was audited 2026-10-04 (§10); §5.2a went through three plan audits, was rewritten to Doug's rulings after the third, then through sixteen more rounds (2026-10-04), the sixteenth finding no defect, then revised the same day to Doug's ruling that arrival times are always recorded and audited five more rounds (20–24), the last finding no defect; then revised 2026-10-05 to Doug's rulings that a re-open onto a closed day, a mis-tap, and an old arrival with no time are all settled by asking the office, and audited rounds 25–41 on the arrival-undo rules, round 41 finding no logic defect and two wording fixes, applied; §5.2a ships as PR 1b against `main` (see *Packaging*), since #846 merged before it was built.

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

### 5.3a PR 2 build spec (2026-10-05; 2a built, in review)

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
  holding lane's total and the job drawer read the same fields. Prod, read-only, 2026-10-05: **0 jobs qualify**, so the section
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
   - MERGED #846, RELEASED v1.137.0, to §5.2's rules.
   1b. **Revision 2 (§5.2a), its own PR against `main`:**
     `recompute_job_schedule`, wired into every writer in §5.2a's table with
     one test each, and the sync rebuilt to §5.2a's table with the
     whole-grid property test. (Written 2026-10-04 to ride in PR 1; #846
     merged first.) MERGED #868, RELEASED v1.138.0.
2. **The office books days (B5).** Covers §5.3: the visits API, the Visits
   card, the board showing each day, and the "Partial Jobs — Need to
   Schedule" section (D10–D12). Split as 2a and 2b (§5.3a); 2a built, in
   review, not merged. (The
   recompute and its writers moved to PR 1b on 2026-10-04, §5.2a; the new
   visits API is one more writer and gets its own test.)
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
| D8 | What happens to the "needs a return visit" checkbox? | **Keep it, label it differently.** Shown on "Yes" only; wording in §5.4, confirmed 2026-10-04. |
| D9 | Does the customer see anything on a "No" day? | **Nothing.** It is one job; the logs are for us. |
| D10 | (2026-10-05) Where does a job go when a day closes unfinished and nothing is booked after it? | **Its own Dispatch section, "Partial Jobs — Need to Schedule".** Doug: "it needs to go into a spot in dispatch that says partial job need to schedule." Replaces the "Continuing" label and the Dashboard note. |
| D11 | (2026-10-05) Where on the board? | **Above "New Jobs to Schedule."** |
| D12 | (2026-10-05) Does a drop book the next day? | **No — the office picks the day.** Doug: "the next day does not always work for the tech." A drop books the date the board is showing. |

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
