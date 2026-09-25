# Vacation balance tracking — Plan

**Date:** 2026-09-24
**Status:** PLAN — research complete, nothing built. Product-shape questions in §7 are owed before any code. Parent: `time-off-and-holiday-pay-plan.md` (MERGED #770, unreleased at the time of writing).
**Ask (Doug, 2026-09-24):** "does it track vacation hours used?" — it does not — "yes let's have the totals for vacation time tracked; do some research on it."

## 1. What already exists (do not rebuild)

Verified on `origin/main` @ `0bb21a0` (the #770 merge) on 2026-09-24.

| Piece | State | Where |
|---|---|---|
| A paid day off is a closed `TimeclockEntry` with `entry_type` `vacation` or `holiday`, `minutes` = the paid minutes, one per shop workday, idempotent per (person, day) | shipped in #770 | `core/time_off.py` `create_time_off_entries`, `existing_time_off_days` |
| Requests table (`time_off_requests`): tech requests, office approves / denies / revokes; approval writes the entries and remembers their ids; revoke soft-deletes exactly those | shipped in #770 | `routers/time_off.py`, `models/tenant_models.py` `TimeOffRequest` |
| Office direct entry (Add Entry → Vacation / Holiday, date range, hours per day) | shipped in #770 | `components/TimeEntryDialog.vue` |
| Holiday calendar + default hours per day off + counts-toward-overtime flag, on `AppSettings` | shipped in #770 | `holiday_calendar`, `time_off_default_minutes`, `time_off_counts_toward_overtime` |
| Time-off hours summed **per pay period** per person and per crew, split from worked hours: Timesheets card "+ X.XXh off", CSV `type` + `time_off_hours`, PDF, email body | shipped in #770 | `core/timesheet_hours.py:118-206`, `core/timesheet_export.py` `CSV_HEADER` |
| Tech-facing panel listing their own requests with status and the office's note | shipped in #770 | `components/TimeOffRequestPanel.vue` (mobile bottom sheet and desktop card) |
| `GET /api/timeclock/time-off/options` — what the request form needs, readable by any signed-in user | shipped in #770 | `routers/time_off.py:264` |
| Per-person schedule with a tenant default and a per-user override (`users.shift_start` / `users.workdays` NULL → `AppSettings.default_*`) | shipped | `routers/technicians.py:146-175`, `routers/users.py:353`, `views/UsersView.vue:323-348` |
| `users.hire_date` column | exists, **no reader and no writer**: not in any router, not in `UsersView.vue`; 0 of 10 active users on prod have one (read-only count 2026-09-24) | `models/tenant_models.py` `User` |
| Pay-period cadence (`weekly_mon`, `weekly_sun`, `biweekly`, `semimonthly`) and the period arithmetic | shipped | `core/pay_periods.py` |

**What does not exist, anywhere:** an allowance or entitlement per person, an accrual, a year-to-date used figure, a remaining balance, carryover, or any screen that answers "how much vacation has this person taken this year". A grep of every file #770 touched for balance / accrual / allowance / remaining / entitle / ytd found nothing time-off related. The parent plan's §6 not-built list does not mention balances, so this is an unconsidered gap, not a ruling.

**The one number that is already derivable** is *used*: sum `minutes` over live `timeclock_entries_router` rows with `entry_type = 'vacation'` for a `technician_id` whose `clock_in_at` falls in a date range, decided in shop-local time the way `existing_time_off_days` does. Revoked days are soft-deleted (`deleted_at`), so the live filter is the whole correctness story. Nothing computes it today.

## 2. Prior art (searched 2026-09-24)

Read, closest first:

- **QuickBooks Time — "Set up and configure time off accruals"** (Intuit help article `L8iHE6gYe`, read 2026-09-24). Accrual types: *Yearly* (lump sum), *Every Pay Period* (fixed hours per period or annualised), *Based on hours worked*, *Manual* (ledger only), *None*. Controls: *Max Balance Limit* (0 = unlimited), *Allow Negative Balance*, *Use it or lose it* (reset on hire date or a calendar date), *Don't begin accruals until* (per person), *Grant X hours on* (one-time opening balance). **Per-employee settings override the company policy.** Admins adjust balances through an *Accruals and Balances* screen or a spreadsheet; employees see their balance in the Time Off section. The article is the most relevant because QuickBooks Time is a timeclock first, like ours, not a payroll product.
- **ptoaccrual.com, "How PTO accrual works"** (read 2026-09-24). The vocabulary the whole category shares: per-period vs per-hour vs lump-sum; an *annual accrual cap* (earn no more than X per year, then reset) is a different thing from a *maximum-balance cap* (accrual pauses at a ceiling, resumes when time is used); a *carryover limit* governs the year boundary. Per-period rate = annual ÷ periods per year, and the guide warns that rounding the per-period rate and carrying it across a year drifts from the annual total, so keep full precision and round for display only.
- **myclockledger.com, "PTO accrual and balance guide"** (reviewed 2026-07-31 per the page; read 2026-09-24). The balance is a **ledger**: opening amount + earned + adjustments − used. "Keep adjustments separate so corrections remain auditable." Caps and waiting periods are policy inputs applied explicitly, never inferred.
- **TimeClock 365, "Accruals explained"** (read 2026-09-24). Same shape with a few extra knobs: negative-units limit, accrual lifetime (when unused time lapses), maximum-at-any-moment and maximum-per-year caps, tenure bonus keyed off employment start date, and a downloadable *time off balance* report that shows every accrual and lapse month by month.
- **Frappe HR (ERPNext) leave module** (docs.frappe.io/hr, read 2026-09-24). Open-source reference for the data model: a *Leave Allocation* (employee, leave type, from/to dates, new leaves allocated, optional carry-forward of unused leaves from the previous allocation) and a *Leave Ledger Entry* — "a unified ledger for all leave related transactions for an employee" — written by allocations, applications, encashments and expiry. The balance is the sum of the ledger.
- Gusto's admin article returned 403 and its developer docs page names only the policy types (vacation, sick, holiday) and the accrual bases "per hour worked, per calendar year, or unlimited"; nothing further was readable, so Gusto is not cited for detail.

**What every product above agrees on**, and what this plan adopts: the balance is derived from a ledger, not stored as a mutable number; the policy (rate, cap, carryover) lives at the company level with per-person overrides; the employee sees their own balance where they request time; the office can post an opening balance and an adjustment, each as an auditable line.

## 3. Minnesota law — what shapes the design

Read 2026-09-24; cite before acting, these are dated observations.

- **Vacation is contractual, not statutory.** Minnesota does not require an employer to offer vacation. If a policy exists, earned vacation is wages and the *policy* decides accrual, carryover and payout at separation (*Lee v. Fresenius Medical Care, Inc.*, 741 N.W.2d 117 (Minn. 2007): vacation pay is "wholly contractual"; Minn. Stat. §§ 181.13–181.14 govern only the timing of final wages). Source: aaronhall.com, "MN law on vacation payouts at termination" — a law-firm summary, not primary; the case cite is the primary. **Consequence:** the app can implement whatever rule Doug writes down; there is no state-mandated vacation formula to encode. The rule should be written down *because* the policy is the law here.
- **Sick time is statutory, and it is a balance tracker by law.** Minnesota Earned Sick and Safe Time, Minn. Stat. § 181.9446 (history: 2023 c 53 art 12 s 5; 2024 c 127 art 11 s 8), read at revisor.mn.gov 2026-09-24: an employee "accrues a minimum of one hour of earned sick and safe time for every 30 hours worked up to a maximum of 48 hours … in a year"; "the total amount of accrued but unused earned sick and safe time … must not exceed 80 hours at any time"; or the employer front-loads 48 hours and pays out the unused at year end, or front-loads 80 and does not. The DLI FAQ (dli.mn.gov/sick-leave-FAQs, read 2026-09-24) adds the recordkeeping rule: **at the end of each pay period the employer must provide the total ESST hours available and the total used**, "through an earnings statement or through an electronic system, including their company's online portal, timekeeping software or other accessible systems"; records kept three years, producible within 72 hours; no payout owed at separation, but hours are reinstated if the person returns within 180 days; an existing PTO plan satisfies ESST only if it meets every ESST minimum. DLI also links "FAQs about earned sick and safe time rules (effective July 6, 2026)" — **not read**; whatever the new rules say about the notice method is unverified here.
- **Minnesota Paid Leave (2026)** is a state-run insurance program: the employer files quarterly wage detail and pays premiums through the joint UI / Paid Leave system; the state determines eligibility and pays benefits (ui.mn.gov/employers/paid-leave, read 2026-09-24). Nothing for this app to track. Payroll here runs through an external payroll company (`gl-phase3-trust-switch.md`, Doug 2026-07-02), which handles the premium.

**Why sick belongs in a vacation-balance plan:** ESST is the *same shape* — a per-person balance with accrual per hours worked, a yearly cap, a carryover cap, and a per-pay-period "available / used" statement — and it is the one the law requires. A balance design that can only do "N hours a year, no accrual" would be rebuilt the day sick is added. #770 deliberately left `sick` out of `TIME_OFF_TYPES` (one constant plus a label to add). This plan builds the balance so that adding `sick` later means adding a policy row, not a second mechanism. It does **not** build sick; §7 asks.

## 4. Decisions proposed

### 4.1 Representation: a ledger per (person, type), balance = sum

Follow every product in §2 and Frappe's model. One new table, `time_off_ledger`:

| column | meaning |
|---|---|
| `id`, `company_id`, `technician_id` (a USER id, the timeclock convention), `entry_type` (`vacation`; later `sick`) | key |
| `kind` | `grant` (opening balance or annual lump), `accrual` (earned by rule), `used` (mirror of a paid time-off entry), `adjustment` (office correction), `carryover`, `expiry` |
| `minutes` | signed: grants, accruals, carryover positive; used, expiry negative; adjustment either |
| `effective_on` (shop-local date) | the day the line counts from; the year boundary and the "as of" balance both read this |
| `timeclock_entry_id` | set on `used` lines; the paid entry this line mirrors, so a revoke reverses exactly one line |
| `source` | `approval`, `direct_entry`, `holiday_post`, `office`, `rule` |
| `note`, `created_by`, `created_at`, `deleted_at` | audit |

Balance as of a date = Σ `minutes` over live lines with `effective_on ≤ date`. Used this year = −Σ `used` lines in the policy year. Never a stored, mutable "balance" column: that is the class of silent-write defect `ARCHITECTURAL_INVARIANTS.md` exists to stop, and it is what the money code in this repo already avoids (`balance_due` is recomputed, not edited).

**Rejected — deriving "used" from the timeclock at read time with no ledger.** It works for *used* alone (§1) and needs no migration. It cannot express a grant, an adjustment, a carryover or an expiry, so it is not a balance; and a `holiday` entry must not draw down a vacation balance while a `vacation` entry must, which a pure reader can do but an office correction ("give him back the day he worked on the 4th") cannot. Rejected for the ask as stated: "totals tracked", which means a number the office can stand behind.

**Rejected — a mutable `users.vacation_balance_minutes`.** No history, no audit, and a revoke has to know what to add back. Every product in §2 moved off this.

### 4.2 Policy: company default, per-person override — the schedule pattern

Mirror `shift_start` / `workdays`: the rule on `AppSettings`, a nullable override on `users`, "effective" = override else default. Policy fields, all per `entry_type`:

- `method`: `annual_grant` (N hours on the policy-year start) or `per_hours_worked` (M minutes per 60 worked, the ESST shape) or `per_pay_period` (N ÷ periods per year). **Recommendation: ship `annual_grant` and `per_hours_worked`; skip `per_pay_period`** — the cadence table in `core/pay_periods.py` makes it derivable later, and a garage-door shop's policy is either "two weeks a year" or the statutory sick rule.
- `hours_per_year` (annual grant, or the annual cap for accrual), `max_balance_hours` (ceiling; ESST: 80), `carryover_max_hours` (0 = use it or lose it), `policy_year_start`: `calendar` (Jan 1) or `anniversary` (needs `users.hire_date`, which nothing sets — §7).
- `allow_negative`: **recommendation: no**, and the approve endpoint refuses with the shortfall named. Prior art offers it; a shop of ten does not need to lend hours through software.

`per_hours_worked` accrues from *worked* minutes only (`core/timesheet_hours.py` already separates worked from time-off), so a vacation day earns no sick time — matches the statute's "hours worked".

### 4.3 Writes: three producers, one function

All ledger lines are written by one `core/time_off_ledger.py` function inside the caller's transaction, with `log_audit_event()` (invariant #1): <!-- proposed module, does not exist yet; link-ok -->

1. **Used** — `create_time_off_entries` gains a ledger write per created entry when the type has a policy; the revoke path deletes the mirrored line by `timeclock_entry_id`. Holiday posting writes **no** ledger line (a holiday is not drawn from vacation) unless a holiday policy is ever configured.
2. **Grant / carryover / expiry at the year boundary** — computed lazily on read *and* materialised by an idempotent function keyed on (person, type, year), run by the existing pay-period send or by the first read after the boundary — **not** by a background task: a write by a system identity into a pay record is the thing the parent plan refused for holidays, and the same objection applies. Run it when a human is looking.
3. **Office adjustment and opening balance** — a manager posts a signed line with a required note. This is how the day-one migration happens: the office types each person's current balance as a `grant` dated today. No import, no guess.

### 4.4 Reads and where they show

- **Tech** — `TimeOffRequestPanel.vue` head gains one line: *Vacation: 32.0h available · 8.0h used this year*; the request dialog shows the balance after the request and refuses (client-side, mirrored server-side) when `allow_negative` is off. Served by `/api/timeclock/time-off/options` widening or a new `GET /api/timeclock/time-off/balance` for the caller — readable by any signed-in user for their own row only.
- **Office** — Timesheets per-person card: *available / used YTD* beside the existing "+ X.XXh off"; the approve dialog shows the balance the approval would leave. A per-person ledger view (the QuickBooks Time "Accruals and Balances" idea) on the Users page, with the Post-adjustment form.
- **Export** — CSV gains `time_off_available_hours` and `time_off_used_ytd_hours` per person; PDF and email carry the same two numbers per person. This is exactly the ESST "available and used at the end of each pay period" statement, so when `sick` is added the export already satisfies DLI's electronic-system option; the payroll company can also put it on the stub.
- **Settings** — the existing "Time off and holidays" card gains the policy fields; the Users edit dialog gains the per-person override, beside shift start and workdays.

### 4.5 Migration `098_time_off_ledger` (money-adjacent)

Adds `time_off_ledger` and the policy columns on `app_settings` and `users`. **Existing rows untouched:** no timeclock row changes meaning; the #770 vacation entries already on any tenant are NOT auto-mirrored into the ledger (the office posts an opening balance that already accounts for them — simpler and honest, and there are none on prod). **Rollback:** `downgrade()` drops the table and the columns; paid time-off entries stay as ordinary closed entries, exactly as after a #770 downgrade. Both engines; guarded `has_table` / column checks; rerunnable; `%` escaped.

## 5. What this plan does not do

- Build `sick`. It makes adding it a policy row. §7 asks whether the shop's ESST is tracked by the payroll company today, which decides whether this app should own it at all.
- Compute overtime (parent plan §2.5 stands).
- Touch `tech_unavailability` or `routers/payroll.py` (both remain orphans on the ledger).
- Pay out anything. A balance is hours; the payroll company prices them.
- Read the July 6, 2026 ESST rules FAQ (§3).

## 6. Sibling sweep, declared now

Class: a reader or writer of paid time-off entries that does not know a ledger line mirrors each one. Surface: `core/time_off.py`, `routers/time_off.py` (approve, revoke, direct entries, holiday post), `routers/timeclock.py` PATCH/DELETE on entries (an office delete of a vacation row must reverse its ledger line), `core/timesheet_hours.py`, `core/timesheet_export.py`, `core/timesheet_delivery.py`, `tasks/payroll_timesheet.py`, `views/TimesheetsView.vue`, `views/UsersView.vue`, `views/SettingsView.vue`, `components/TimeEntryDialog.vue`, `components/TimeOffRequestDialog.vue`, `components/TimeOffRequestPanel.vue`. Counted in the build PR's body.

## 7. Questions for Doug (product shape — owed before code)

1. **The vacation rule itself, in words.** "N hours per year granted Jan 1", or earned per hour worked, or per pay period? Same for everyone or by tenure? Carryover, and how much? Use-it-or-lose-it date? This becomes the policy in §4.2 and should also be the written policy the *Lee* rule makes binding.
2. **Calendar year or anniversary year?** Anniversary needs `users.hire_date`, which nothing sets and nobody on prod has. If anniversary, the Users page gains a hire-date field first.
3. **Opening balances.** Are today's balances known (spreadsheet, payroll company)? The plan has the office post them as dated grants; say if an import is wanted instead.
4. **Sick / ESST.** Does the payroll company track ESST accrual and put available/used on the stub today? If yes, this app stays out of sick and only vacation gets a policy. If no, the shop has a compliance gap this design closes by adding a `sick` policy row — but that is a scope widening, so it is asked, not assumed.
5. **Negative balances.** Recommendation is refuse. Say if a manager should be able to approve past zero with a note.
6. **Who sees whose balance.** Techs see their own; office sees all. Should the approve/deny screen show the balance to the approver (recommended yes)?

## 8. Verification (planned)

Backend: ledger arithmetic on both engines, idempotent year-boundary lines, revoke reverses exactly one line, refuse-on-shortfall, export columns; every assertion reads rows back. Frontend: panel and card render the numbers from a mocked balance; the dialog refuses past zero. Browser walk on a throwaway container: admin sets a policy, posts an opening balance, tech requests past the balance and is refused, requests within it and sees available drop, office approves and the Timesheets card and CSV carry the two numbers; light and dark, desktop and 390×844. Prod walk after release. None of it done — nothing is built.
