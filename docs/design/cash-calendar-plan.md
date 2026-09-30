# Cash calendar — "what's coming up" on one screen

**Status:** PARTIALLY BUILT — the calendar (Forecasting page), the Dashboard summary card, the floor and operating-account settings (migration 102) and the endpoint are built in the PR that adds this doc. NOT built: recurring detection from the bank feed (it still reads QuickBooks bank data), a "not seen in the bank" staleness signal, a date field on the create-recurring-payment form, and planned one-off payments (payroll runs, a distributor payment).
**Date:** 2026-09-29
**Owner:** Doug

## The ask

Doug, 2026-09-29: the parts of a cash forecast are in the app, but nothing lines them up by date against the bank balance, so "is any day in the next two weeks tight?" had to be rebuilt by hand from raw bank transactions. Wanted: a dated list with a running balance, a selectable window (next 7 / 14 / 30 days), and a warning on the first day the operating account would go under a chosen floor. Placement, decided by Doug the same day: **the full calendar on the Forecasting page, a summary card on the Dashboard linking to it.**

## What already exists (do not rebuild)

Verified against `main` at `63859af1` and read-only against production, 2026-09-29.

| Need | Already there | Where |
|---|---|---|
| Bank balances, synced daily | `bank_feed_accounts.balance` / `balance_as_of` (SimpleFIN) | `modules/bank_feeds/models.py` |
| Open customer invoices with due dates | `invoices.balance_due`, `due_date` | `models/tenant_models.py` |
| Scheduled jobs with a value | `_scheduled_jobs_projection` (estimate totals) | `modules/forecasting/service.py` |
| Recurring payments projected forward | `_observed_stream_projection` + `_combined_recurring` | `modules/forecasting/service.py` |
| Open vendor bills, net of payments | `vendor_invoices.due_date` + `payments.open_balance` | `modules/vendor_invoices/` |
| A window picker | Forecasting's 30/60/90 select (revenue only) | `views/ForecastingView.vue` |
| Backward-looking cash on the Dashboard | the "Cash & Risk" card (AR aging, collected, open A/P) | `views/DashboardView.vue` |

The archived bank-feed plan anticipated this: "cash curve for the dashboard later" (`docs/design/archive/simplefin-bank-feed-plan.md`). No other plan in `docs/design/` covers a dated cash view; grep for "cash calendar", "running balance", "runway" and "cash curve" found only that line.

Prior art (web search 2026-09-29): standalone cash-flow calendars (cashflowcalendar.app, the Maxprog and Pantana CPA write-ups, Melio's roundup of forecasting apps) converge on the same three things — a running daily balance, a cushion/floor, and flagging the days under it. They need the data synced into them; here it is already in the app, so this is a view, not a data source.

## Prerequisite fixed first (separate PR, #828)

Forecasting's "Expected revenue" summed recurring **payments** as revenue, and a past-due stream was moved onto today and counted twice in a window. Both had to be right before a calendar could use the same projections. #828 gives every recurring item a direction and rolls past dates forward on their cadence. This PR is stacked on it.

## Decisions

1. **One endpoint, read-only:** `GET /api/forecast/cash-calendar?days=N`, N in 1..90, `accounting.read` (same gate as every forecasting GET). No new data is stored except two settings.
2. **The window is exactly N dates**, today through today + N − 1. The rest of the forecasting module counts both ends (a 30-day window is 31 dates); the calendar does not copy that.
3. **Starting balance = the operating accounts.** Chosen in settings. Until chosen: every account with syncing on, not inactive, with a non-negative balance — and the page says it is a default and names them. Excluded accounts are listed with the reason (syncing off; negative balance, which looks like a loan or credit line; no balance yet). On production this default correctly drops two stale test accounts and a credit line, but includes savings and payroll accounts, which is why the page asks.
4. **Rows:** customer invoices on their due date (face balance); jobs scheduled in the window (stages scheduled, service call, in progress) at their **accepted, non-deleted** estimate total **less what is already billed on the job** — sent/overdue invoices (each its own row or past-due line) and paid ones (already in the bank); a draft invoice is not billed and is not subtracted — marked "Estimate" and labelled "Customer · Job N · title" (most production jobs have no number and titles repeat); recurring payments and QuickBooks templates by direction, marked "Estimate"; vendor bills on their due date at their open balance. Estimate-visit jobs (stage "estimate") are not money in. The calendar does not reuse the revenue forecast's jobs projection, which sums every estimate on a job, draft and declined included, and is discounted by a realization rate — a dated cash row cannot be.
5. **Two running balances.** `balance_after` counts every row; `balance_if_nothing_comes_in` counts only money out. The floor warning is given for both, because a customer paying on the due date is a hope and a bill's due date is not.
6. **Money out first within a day** — a day's low point is after its bills.
7. **Nothing undated is forced onto a date.** Customer invoices already past due, **finished jobs not yet billed** (completed jobs whose billing is not resolved by the app's shared rule, `core/billing_predicates.job_billing_resolved` — no finalized non-deposit invoice and not marked "not billable" by the office, the same set the Ready for Billing list shows — valued at its latest draft invoice (what will be billed, change orders included — they go onto the invoice, never the estimate — and already net of deposits), else at the accepted estimate less billed deposits, else listed as "not priced yet" — never dropped while its billing is unresolved. Past five, the list links to all of them in Billing. On production (read-only, 2026-09-29): 9 jobs — 2 with an accepted estimate ($6,301.38, neither has a draft), 3 valued by closeout draft ($859.06), 4 not priced. Requiring an estimate would have dropped every repair: they are priced at closeout. A deposit does not count as billed, or an installation awaiting its final invoice would vanish the day it completes. Not "accepted minus everything billed": a final invoice comes from the closeout, and a paid job would show the estimate-to-invoice gap as owed), and vendor bills past due or with no due date, are listed beside the calendar with totals (five shown, the rest a click away) and never enter the running balance. On production that is 25 of 36 open invoices and all 28 open bills (17 have no due date), so putting them on "today" would have invented a crash, and dropping them would have hidden real money.
8. **No floor by default.** A NULL floor means none chosen; the page says so and offers to set one. Doug's example was $1,000, but a guessed floor is an invented rule.
9. **Settings are owner/admin** (the existing `PUT /api/forecast/settings`, audited as `forecast_settings.update`). `clear_cash_floor` clears the floor explicitly so 0 stays a real floor; an empty account list returns to the default. Unknown account ids are refused with 400.
10. **"Today" is the shop's day**, from `app_settings.timezone` (America/Chicago on production) via the bank-feed module's `tenant_zoneinfo`, and a job's UTC `scheduled_at` is converted to that zone. The container clock is UTC, which is already tomorrow after ~7pm; the browser walk caught the calendar starting on Wednesday on a Tuesday evening.
11. **Dashboard card:** fixed at 14 days, self-gated on `accounting.read` (renders nothing otherwise), placed above "Cash & Risk", links to `/forecasting`, where the calendar is the first thing on the page. It names what is **not** in its numbers (bills with no future due date, customers past due, finished jobs not billed) whenever any is non-zero, and says "None among dated items" rather than "No tight days" when undated bills exist: on production no open bill has a future due date (all 28 are past due or undated, about $98k before payments), so without that line the card would read as all-clear while the money sat outside it.

## Rejected

- **Weighting each invoice by the aging collection rate.** Rows would show amounts nobody owes (e.g. $1,471.55 of a $1,549 invoice). The second, money-out-only balance answers the risk question honestly instead.
- **Putting overdue items on today.** See decision 7.
- **Defaulting the floor to $1,000.** See decision 8.
- **Reusing the revenue page's 30/60/90 window.** Revenue and cash answer different questions; the calendar has its own 7/14/30/60.

## Known limits (not built here)

- The finished-jobs list has no age limit: every completed job ever whose billing is unresolved is listed. The office's existing "not billable" dismiss (`jobs.not_billable_at`) removes a job from it; work billed outside the app still needs that dismiss. Whether to also cap the list by age is open (Doug to rule).

- Recurring streams still come from the QuickBooks bank mirror, which on production stops at 2026-08-14 while the SimpleFIN feed is current. All four active production streams have past expected dates; the calendar projects their future dates on cadence. Moving detection onto `bank_feed_transactions` is the next piece of work, and a "not seen in the bank" signal belongs with it (an attempt to infer staleness from stream dates alone was cut from #828 after audit: it fired on payments that had cleared).
- The create-recurring-payment form sends no date, so a stream made in the UI lands on today.
- A QuickBooks recurring template appears once, on its next date, not repeated by its interval (inherited from the revenue forecast's template projection). Production has no templates.
- Payroll and other one-off planned payments appear only if they exist as a recurring stream or a vendor bill.

## Migration 102

`tenant_forecast_settings.cash_floor NUMERIC(14,2) NULL` and `operating_account_ids JSON NULL`. The table has no create migration (built from the ORM), so the migration is a guarded add, a no-op without the table; existing rows get NULL for both ("not chosen"). Downgrade drops both columns; they hold only the two choices. Revision id kept to 26 characters (`alembic_version.version_num` is VARCHAR(32)). Tested on SQLite and on a throwaway Postgres 16.
