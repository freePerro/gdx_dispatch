# Customer Statements — Plan

**Status:** **PLAN** — nothing built. Branch `feat/customer-statements`, no PR.
**Date:** 2026-09-15. Decisions 1–7 ruled by Doug 2026-09-14/15. Researched
against main @ `fbb83d3` (v1.120.0, alembic head `095`) and prod read-only
(`gdx-db-1`, 2026-09-14/15). §5 is the third version of the arithmetic: two
adversarial audits on 2026-09-15 each broke the one before it on real prod
data (§5, "Why not payment rows"); a third found QuickBooks-era payment rows
counted twice, which led to decision 7.
**Supersedes:** the 2026-09-03 "Customer Statements Groundwork" research, which
was never committed (a private page plus a scratchpad file that no longer
exists). Its 3-PR plan was cut to one PR the same day, when Doug asked whether
it was over-complicated; the decisions below settle its open questions.
`PHASE.md` still describes that superseded "3-PR plan, 7 decisions".
**Related:** `contact-opt-out-suppression-plan.md` (PLAN) — statements add a
new email kind its taxonomy must classify (§6).

> Repo hygiene: no customer, vendor or internal names in this doc or in any PR
> built from it.

---

## 1. Decisions (Doug, 2026-09-14 and 2026-09-15)

1. **Live, not stored.** A statement is computed each time it is produced.
   No statements table, no `STMT-` numbers, no migration. What a send
   recorded is the audit row (who, when, to whom, the range, the figures and
   the invoice lines as sent) and the `outbound_emails` row (the email body
   and the attachment's name and size — not the PDF). Producing the same range
   later can give different numbers; that is what live means.
2. **Every statement covers a date range.** Presets: last 30, 60 and 90 days,
   year to date, completed calendar years (2026 onward — decision 7), plus a
   custom range.
   ("Fiscal year" meant calendar year.)
3. **Every statement shows a previous balance and the total unpaid balance.**
4. **Every invoice dated in the period is listed, paid or not** — not just
   balances.
5. **The dialog opens on "last 90 days".**
6. **A payment recorded against a void invoice is left off** the statement,
   along with the void invoice itself.
7. **Statements start at 2026-01-01 until the QuickBooks-era payment rows are
   repaired.** Year presets offer 2026 onward; a custom range cannot start
   earlier. Offered options were: repair first, build with a "may not match"
   note on imported payments, or build with years from 2026 — Doug chose the
   last. Lifting it is a one-constant change once the repair lands (§5).

### Defaults taken, not yet ruled (object to any of these)

- Draft and void invoices never appear.
- Pay links are per invoice. No "pay the whole balance" link (that needs a new
  Stripe intent path and an allocator — its own plan).
- Manual, one customer at a time. No batch run, no monthly schedule. The
  business sent zero statements through its previous FSM, which had bulk
  sending; one hand-built statement exists, for one builder account.
- No customer-portal tab (3 portal logins on prod).
- Fixed email subject and body. Editable copy would add a `tenant_settings`
  pair and a migration; defer until someone asks.
- Sent through the existing transactional send path, as the acting user. No
  change to designated-sender rules.
- Credit on account is shown beside the unpaid balance, not netted against it,
  because the Pay links charge invoice balances and a credit is applied by the
  office.
- An invoice whose records don't add up (§5, "Warning") produces a warning in
  the preview and in the `statement_sent` audit row — not on the customer's
  PDF — and does not block Send.
- **Every open invoice is listed with its Pay link, whatever its date** (§5,
  section 3), as the invoices that make up the total unpaid balance. Without
  it, decision 7 leaves 7 prod customers who owe only on pre-2026 invoices a
  statement with a balance and nothing to pay, and a range ending before
  today hides invoices dated after it.

## 2. What already exists (do not rebuild)

Verified on main @ `fbb83d3`.

| Need | Reuse | Where |
|---|---|---|
| PDF engine | WeasyPrint + shared Jinja env, `money` filter | `core/pdf_generator.py:13-18`, `_render_template` `:197` |
| A third document type without the template registry | Timesheet PDF imports `_JINJA_ENV`/`_TEMPLATES_DIR` and calls `write_pdf()` | `core/timesheet_export.py:187-194` |
| Branding (name, logo, address) | `_branding_payload(db)` — already imported by three other routers | `routers/pdf.py:60` |
| Send + one log row per attempt | `send_transactional_email(kind, entity_type, entity_id)` → `(sent, provider, skip_reason)`; the log row keeps `body_html` and `attachments_meta`, not attachment bytes | `core/transactional_email.py:305`, `models/tenant_models.py:3434,3475,3478` |
| Double-send guard | `recently_sent` | `core/transactional_email.py:248` |
| Recipient choice | picked contact → primary contact → account email | `core/email_recipients.py:54` |
| Per-invoice pay link | `public_pay_url(token)` — None when Stripe or the base URL is not configured, so a dead link is never rendered | `core/payments.py:160` |
| Shop calendar | `shop_today_from_settings`, `shop_day_of` (#728) | `core/pay_periods.py:321,359` |
| Settled amount per invoice | `total − balance_due`; `balance_due` is written by `_recalculate_invoice` (total − live payments − credit memos − applied credits, floored at 0) | `routers/invoices.py:517` |
| Customer credit on account | `customer_credit_balance_cents` — the ledger's 2300 balance; what apply-credit spends and what a refund draws first | `modules/ledger/rules.py:891`, `post_refund` `:766` |
| Per-invoice ledger sum, pattern to copy | `invoice_ar_balance_cents` — builds the invoice's payment and adjustment id lists in Python (avoids the SQLite dashless-UUID trap), sums every entry sourced from them across all statuses so reversals net | `modules/ledger/rules.py:1249` |
| Ledger switch | `ledger_posting_enabled` | `modules/ledger/service.py:213` |
| Aging buckets | the bucket loop inside `aging_report` (tenant-wide, inline) | `routers/collections.py:149-161` |
| Audit | `log_audit_event` | `core/audit.py:469` |
| Permissions | `invoices.read_all`, `invoices.send` — no new key, no grant migration | `core/permissions.py:54,56` |
| Email log screen | list and detail of outbound emails | `routers/outbound_emails.py:61,106` |
| Customer record header | Edit button — a Statement action sits beside it | `views/CustomerDetailView.vue:22` |

Does **not** exist: any per-customer balance, open-invoice rollup or aging
function; any customer-statement code, template or doc (searched backend,
frontend, templates, plugins, `docs/`); any read of `email_opt_out` before a
send.

## 3. Prod evidence (read-only)

- Open receivable (2026-09-14): 31 invoices, 27 customers, $40,019.93. 24
  customers owe on one invoice, 3 on two or three. 14 invoices are 90+ days
  past due ($8,266.64).
- 17 customers were billed 4+ times in the last 365 days — the accounts a
  statement is really for. Customer type cannot select them (a builder is
  typed "Retail"), and `payment_terms_days` is set on 0 customers.
- Invoices by invoice year (2026-09-15): 2024 = 102, 2025 = 176, 2026 = 80.
- **Payment rows are not a complete ledger** (2026-09-15):
  - 10 paid invoices, `balance_due` 0, whose payment rows fall short of their
    totals by $24,837.57 — mostly round amounts, their payments all
    `method='quickbooks'`. Every one carries a `paid_at` — which the importer
    set to the invoice date itself, so it says nothing about when the money
    arrived.
  - 3 paid invoices whose payment rows exceed their totals by $10,329.16, at
    least $7,052.79 of it three QuickBooks payments counted twice (§5,
    "Warning"). The ledger's customer-credit account (2300) holds no lines at
    all, so none of it was ever recognised as credit.
  - 22 live payments ($91,352.83, 11 customers) are dated before their own
    invoice — deposits and imports.
- The only statement the business has produced by hand listed every job for
  one builder account — paid ones too — with a job/site summary, completion
  date, total, payments and balance, and the total outstanding at the top.

## 4. Prior art (read 2026-09-14)

- QuickBooks Online — balance forward, open item, transaction; statements
  update when underlying transactions change (help article updated
  2026-06-01): <https://quickbooks.intuit.com/learn-support/en-us/help-article/customer-statements/create-send-customer-statements-quickbooks-online/L8bvb69Gg_US_en_US>,
  <https://plugin-qbo.intuit.com/qbo-ush/helpcontent/production/latest/en_US/QBO_Plus/Content/Topics/customer/help_customer_statements_types.htm>
- ServiceTitan — "as of date" and balance forward; bulk from AR Management;
  marks "Sent on [date]" (updated 2026-07-10):
  <https://help.servicetitan.com/docs/send-customer-statements>
- Jobber — from the client's billing history; lifetime / since last zero
  balance / pick a start; running balance; email or PDF; cannot pay from the
  statement; no bulk (updated 2026-08-11):
  <https://help.getjobber.com/en/articles/client-billing-history/>

Why build rather than adopt: the statement is a view of this system's own
invoices and payments; QuickBooks is being phased out and holds no current data.

## 5. The statement

Inputs: `customer_id`, `start`, `end` — shop-calendar dates, `start` no
earlier than `STATEMENT_EARLIEST = 2026-01-01` (decision 7; 422 otherwise),
`end` no later than the shop's today. Presets resolve on the server against
`shop_today_from_settings`, so the PDF and the dialog agree; a preset that
would start before the floor starts at the floor, and every preset's end is
capped at the shop's today. Year presets are **completed** calendar years from
2026, so none is offered until 2027 — the current year is "year to date".

**Invoice set:** `Invoice.customer_id = :id` (never through jobs — the portal's
job join hides jobless invoices), `deleted_at IS NULL`, status not `draft` or
`void`. An invoice's date is `invoice_date`, falling back to the shop day of
`created_at`; a date after the shop's today counts as today, because nothing
stops a future `invoice_date` on create or update (`routers/invoices.py:836,
2083`) and the balance-at-today identity needs every invoice dated (prod: 0
future-dated).

### What is true, and where it comes from

Two figures the system already acts on:

- **How much of an invoice is settled: `total − balance_due`.** It is what the
  Pay page charges against and what every AR report reads.
- **A customer's credit on account: the ledger's 2300 balance**
  (`customer_credit_balance_cents`). It is what apply-credit spends and what
  `post_refund` draws down first (#445).

Payment rows, credit memos and applied credits supply only **when** an
invoice's settlement happened.

### Dating each invoice's settlement

For each invoice, take its settlement records in date order — live payments by
`payment_date`, credit memos and applied credits by the shop day of
`created_at`; ties by `created_at`, then id — and count each one until the
invoice's settled amount is reached.

- **Records beyond the settled amount are not counted.** The excess is
  overpayment. If the business recognised it as credit, it is in 2300 and
  appears as credit on account; if not (the 3 prod invoices), it appears
  nowhere, because it is not owed to anyone in these books.
- **Records short of the settled amount** leave a remainder, shown as one entry
  "Paid — no payment detail on record", dated at the shop day of `paid_at`,
  else the latest non-zero record's date, else the invoice date (the 10 prod
  invoices).
  A `paid_at` at exactly 00:00:00 UTC is a date-only stamp — the QuickBooks
  importer writes the invoice date that way (`modules/quickbooks/sync.py`) —
  and is read as that UTC date, as the frontend's `isDateOnlyStamp` does;
  otherwise `paid_at` is read on the shop's calendar.
- **A record dated after the shop's today counts on today.** Card payments are
  written on the UTC day (§5, known conditions).

**Balance at a date d** = Σ totals of invoices dated ≤ d − Σ counted entries
dated ≤ d, across the whole invoice set. A deposit paid before its invoice is
dated shows as a credit until the invoice exists, which is true.

At d = today this is Σ `balance_due` exactly, because every invoice's counted
entries add up to `total − balance_due`. That is by construction, and it is
asserted per invoice in tests (§9) — it is not a reconciliation line on the
customer's PDF.

### Why not payment rows (the two versions this replaced)

- **v1** split the previous balance by invoice date. A payment dated before its
  invoice fell in neither half, and a customer who owes nothing was shown
  $22,088.32.
- **v2** dated every payment by its own date but still treated payment rows as
  the ledger. On prod that produced $24,837.57 of debt across 10 customers who
  owe nothing, and $10,329.16 of credit across 3 customers that the books do
  not hold. A "named on the statement" flag would still have printed those
  figures on customers' PDFs.

### Warning: an invoice whose records don't add up

Capping and the unrecorded remainder make every invoice's figures consistent;
they do not make them *right*. When an invoice's settlement records don't sum
to its settled amount, which record is wrong is unknowable from the rows —
the third audit found QuickBooks payments 2192, 2465 and 2486 recorded in full
on one invoice **and** again, split, on others ($7,052.79), where capping in
date order keeps the copy and trims the genuine later payment.

An invoice is **warned** on a statement when both hold:

- **Its records don't add up:** live payments + credit memos + applied credits
  differ from `total − balance_due` + the overpayment its own payments posted
  to 2300 (GL lines on 2300 from entries with `source_type = 'payment'` and
  `source_id` among **all** its payments, voided ones included, across all
  entry statuses so reversals net — the `invoice_ar_balance_cents` pattern)
  by more than $0.005. Without the
  2300 term, every correctly booked overpayment — `allow_overpayment` keeps
  the full payment row (`routers/invoices.py:3158`) and posts the excess to
  2300 — would be warned forever.
- **It can move a figure on this statement:** the invoice is dated on or
  after `start`, or any of its non-zero records is, or its unrecorded
  remainder is. Zero-amount rows do not count. The rule looks at *records*,
  not at what was counted: when an invoice's records don't add up, the dates
  of its counted entries are exactly what is in doubt — a duplicate dated
  before `start` can be counted while the genuine payment after `start` is
  trimmed (the fifth audit's case, warned under this rule and missed by the
  one it replaced).

The preview names each warned invoice — "payment records for this invoice
don't add up; figures that depend on it may be wrong" — **without a dollar
difference**, because the difference is not the size of the error: invoice
50530713 differs by $30.25, but carries two QuickBooks payments that together
equal another invoice's total, so up to $1,420.75 could be misplaced. The
office decides whether to send. The warned invoices are written into the
`statement_sent` audit row, so a send of flagged figures stays reconstructable
after a repair changes them.

**A second warning: a non-deposit draft invoice holding a live payment.**
Drafts never appear on a statement, but `record_payment` refuses only void
invoices (`routers/invoices.py:3097`), so a customer's payment can sit on a
draft and vanish from every statement. When the customer has such a draft,
the preview names it ("a payment is recorded on draft invoice N, which this
statement does not show") and the audit row records it. **Deposit drafts are
exempt:** deposit invoices are created as drafts (`modules/deposits/service.py`,
`create_deposit_invoice`), and the final invoice's netting sums payments on
every non-void deposit, drafts included, subtracting them as "Less deposit
paid" — so the money is already on the statement inside the final invoice's
total, and a warning there would be wrong on every deposit customer. Prod count not yet verified (§5,
known conditions).

**The warning cannot catch** a duplicate that sits on an invoice whose records
do add up while its twin is elsewhere (the 50187678 half of the case above).
Only the QuickBooks-era repair closes that (§11).

### What the prod run shows (2026-09-15, read-only extract)

v3 over every non-draft, non-void invoice and settlement record on prod:

- **Invoices whose records don't add up: 14** (3 over, 11 short; 2300 holds no
  lines, so none is excused as booked credit).
- **Warned on some 2026 statement: 3**, across 3 customers. (A first reach
  test that counted any record dated in 2026 named 6; three of those reached
  2026 only through $0.00 rows.)
- **The warned set is exactly** invoices 49908124, 50151705 and 50530713 on the
  2026-09-15 extract. The extract lived in a session scratchpad and will not
  survive; the build re-runs the check on a fresh copy of prod data, where
  every difference from these three must be explained by a named record
  change (an office backfill, a repair, a new payment) — never by adjusting
  the rule until it matches. 50530713 is warned only by the invoice-date clause: dated in 2026,
  it carries QuickBooks payments dated 2025, so its customer's 2026 statement
  shows a previous balance of −$1,420.75 that may not exist.
- **Allocation-order check:** for each of the 14, allocate its records
  latest-first instead of earliest-first and recompute every statement for its
  customer — every start day from 2026-01-01, every month-end and today as the
  end: 18,830 ranges. Figures moved for 2 invoices, both warned. **This is not
  a test of the reach rule** — reordering an invoice's own records cannot move
  a figure when they all fall before `start`, so it stays at 0 unwarned even
  with a clause deleted (sixth audit, run). It shows only that the warning
  covers every invoice whose allocation order matters.
- Two parts of the rule are proven by fixtures (§9) alone, because nothing in
  the prod data can detect their absence: the **2300 term** (prod's 2300 holds
  no lines) and the **remainder clause** of the reach rule (deleting it leaves
  both the warned set and the allocation-order check unchanged — seventh
  audit, run). Deleting the records clause is caught (383 unwarned moves);
  deleting the invoice-date clause is caught only by the named warned set.
- **Open invoices dated before 2026: 9**, $5,114.46, 9 customers; 7 of those
  customers have no 2026 invoice. All are listed under "Open invoices".
- Not evidence, because they cannot fail: balance at today equals Σ
  `balance_due` for 201 of 201 customers (true by construction), and every
  negative month-end balance traces to a payment dated before its invoice
  (guaranteed, since counted entries never exceed an invoice's total).
- The builder account v1 showed owing $22,088.32 from 2025-12-01 now reads
  previous $726.34, invoiced $38,277.68, received $39,004.02, ending $0.00 —
  a range decision 7 no longer offers.

### Sections, in order

1. **Header** — company branding, customer name and billing address, the range,
   the date produced.
2. **Total unpaid balance** — Σ `balance_due` over the invoice set, today, split
   by age on the shop's calendar, where days past due = shop today −
   `due_date`: **current** (0 or fewer days — due today counts here — or no
   `due_date`), **1–30**, **31–60**, **61–90**, **over 90** (91 or more).
   Every open invoice falls in exactly one bucket, so the buckets add up to
   the total. **Credit on account** appears beside it when the ledger
   is posting and 2300 is above zero.
3. **Open invoices** — every invoice in the set with `balance_due` above zero,
   whatever its date: date, number, job or site (the job's title; blank when
   there is no job, which is every QuickBooks-imported invoice), total,
   balance, days past due, Pay link. They add up to the total unpaid balance;
   this is where the customer pays.
4. **Previous balance** — balance at the day before `start`.
5. **Invoices in the period** — every invoice dated in `[start, end]`, paid or
   not: date, number, job or site, total, paid (`total − balance_due`),
   balance today. No Pay links here; the open ones are in section 3.
6. **Payments and credits in the period** — every counted entry dated in
   `[start, end]`, with the invoice it settled (even when that invoice falls
   outside the period). Refunds in the period are listed beneath as
   information: `post_refund` leaves receivables untouched, so a refund does
   not change what is owed, and a refunded overpayment leaves 2300.
7. **Ending balance** — previous balance + invoiced − payments and credits in
   the period, which is the balance at `end`. For a range ending today it is
   the total unpaid balance. For a past range the statement labels the two
   "Balance at end of period" and "Unpaid today".

### How the other money paths land

- **Applied credit** — the overpayment was capped on its own invoice, and the
  application counts on the invoice it settles. Counted once.
- **Refund of an overpayment** — 2300 drops, so credit on account drops. No
  entry moves.
- **Full Stripe refund or dispute** — `_reverse_recorded_payment` voids the
  payment with a reason and recalculates, so the invoice is owed again and the
  payment no longer counts in any period. That is live (decision 1).
- **Payment on a void invoice** — excluded with the invoice (decision 6). If
  its excess reached 2300 and was applied elsewhere, the application counts
  where it lands.

### Known data conditions (not fixed by this plan)

- **9 open invoices dated 2024–2025, $5,114.46, none with a payment row.**
  Some may be QuickBooks-era invoices paid off the books; the office backfill
  in `qb-import-paid-status-repair-plan.md` Phase 2 was still open at that
  doc's last status. The preview is the guard: every one is listed under
  "Open invoices", so the office sees it before anything is sent.
- **`payment_date` still carries two meanings** — field-received vs. banked
  (`payment-date-recording-plan.md`, contradiction unresolved). A check taken
  Dec 28 and banked Jan 3 can land in the next year's statement. This plan
  reads the column as it is and labels it "Payment date".
- **Card and API payments are dated on the UTC day.** `_mark_invoice_paid`
  (`core/payments.py:808`) writes `datetime.now(timezone.utc).date()`, and
  `PaymentCreateIn.date` defaults to `date.today` (`routers/invoices.py:914`)
  — the sibling #728 did not reach. The statement shows such a payment on
  today; the writer fix is its own PR, Doug's call (§11).
- **Draft invoices carrying live payments:** not verified on prod (the sixth
  audit and this author both lost access on 2026-09-15). An older local copy,
  which also holds E2E test rows, has 2 drafts with $3,400.98 of payments.
  Handled by the second warning either way.
- One void invoice on prod carries a $100 `balance_due`. It never reaches a
  statement: void invoices are excluded by status, not by balance.

## 6. Surface

**User:** office staff at a desk. Not techs, not mobile.

- **Way in:** a **Statement** button on the customer record header, beside
  Edit, shown only with `invoices.read_all`.
- **Dialog:** preset picker, opening on last 90 days (30 / 60 / 90 days,
  year to date, each completed calendar year from 2026 that holds an invoice
  for this customer, custom from 2026-01-01 — decision 7), then a rendered
  preview of the actual PDF with any warnings above it.
- **Actions:** Download PDF (`invoices.read_all`); Email (`invoices.send`) —
  recipient prefilled by `resolve_recipient`, editable.
- **Opt-out:** nothing reads `email_opt_out` before any send today — invoices
  included. Statements go through `send_transactional_email` with
  `kind="statement"`, which is where `contact-opt-out-suppression-plan.md`
  puts the check, so they inherit it when that plan is built. Its taxonomy
  must classify `statement`; the natural class is payment reminders. This plan
  adds no separate check.
- **After Email:** a result dialog. Sent → "Sent to X" and the send appears in
  the customer's email log. Not sent → the skip reason, never a green toast.
- **Customer:** gets an email with the PDF attached and the open invoices'
  Pay links in the body too, so paying from a phone does not depend on the
  mail app's PDF viewer supporting links (not yet tested either way). Phone
  test required.

## 7. Endpoints

| Method | Path | Gate | Writes |
|---|---|---|---|
| GET | `/api/customers/{id}/statement?preset=` or `?start=&end=` | `invoices.read_all` | nothing |
| GET | `/api/customers/{id}/statement/pdf?…` | `invoices.read_all` | nothing |
| POST | `/api/customers/{id}/statement/send` `{start, end, to_email}` | `invoices.send` | `outbound_emails` row (via the send path); audit `statement_sent` on entity `customer` |

The audit row's details hold the statement as sent: range, previous balance,
invoiced, payments and credits, ending balance, unpaid today, credit on
account, the aging buckets, recipient, provider or skip reason, each listed
invoice's number, total, balance and days past due, and both kinds of warning. `send_transactional_email` does not return the log row's id;
the row is found by `entity_type`, `entity_id`, `kind` and time.

One service function computes the statement; the JSON, the PDF and the email
all call it, so preview and send cannot drift. `openapi_routes.txt` is
regenerated in the same PR. No public, unauthenticated route.

## 8. Packaging

One PR: service, template, three endpoints, the customer-record dialog, tests,
route snapshot, and the help article. `help/articles/invoices.md:46`
("Statements and balances") is rewritten to describe the real feature — it
currently describes a widget and a resend list that do not exist.
`contact-opt-out-suppression-plan.md` gets a one-line amendment naming the new
`statement` kind.

Bundled nothing else. The aging buckets are defined in the service (§5,
section 2), not copied from `collections.py`, whose loop skips invoices with no
`due_date` and invoices not yet due, so its buckets would not add up to the
total. Unifying the five agings is its own work.

## 9. Tests and verification

- **Service (SQLite and Postgres, docker-app).** Fixtures take the shapes prod
  actually holds, not the causes the author expected:
  - a **QuickBooks payment recorded in full on one invoice and again, split,
    on another** → the first invoice is warned, with no dollar difference;
  - an **overpayment booked to 2300** through `allow_overpayment` → not
    warned; the same overpayment with no 2300 line → warned;
  - an anomalous invoice whose only in-range record is **$0.00** → not warned;
  - a **duplicate dated before `start` and the genuine payment after it** →
    warned; the same shape in a **range ending before today**, invoice dated
    after `end` → warned;
  - one fixture per reach clause, each warned by that clause alone: an
    anomalous invoice **dated on or after `start` with every record before
    it**; a **short invoice with every record before `start` and `paid_at` on
    or after it**; an anomalous invoice with a **non-zero record on or after
    `start`**;
  - a **draft invoice holding a live payment** → absent from the statement,
    present in the draft warning and the audit row; a **draft deposit**
    holding a payment and netted on a final invoice → not warned;
  - the **"year to date" and year presets** on a fixed shop today in 2026 → no
    year preset offered; in 2027 → "2026" ends 2026-12-31;
  - an **open invoice dated before `start`**, and one **dated after `end`** in
    a past range → both listed under "Open invoices" with Pay links;
  - **aging buckets** including an invoice with no `due_date`, one due today,
    one not yet due, and ones exactly 30 / 31 and 90 / 91 days past due → the
    buckets add up to the total unpaid balance;
  - an **invoice dated after today** → counts as today;
  - a send with warnings → the warned invoices are in the audit details;
  - a range starting **before 2026-01-01** → 422; a preset reaching before it
    → starts at the floor; an invoice dated 2025 with a 2026 payment → present
    in the previous balance and, if its records don't add up, warned;
  - a `paid_at` at 00:00 UTC → read as that date, not the day before;
  - a **paid invoice short of its payment rows** with `paid_at` set → no debt
    in any range; a "no payment detail" entry on `paid_at`;
  - a **paid invoice over its payment rows** with no 2300 → no credit on
    account, and the invoice warned;
  - a **payment dated before its invoice**, with `start` between them;
  - a **payment dated tomorrow** (UTC) → counts on today;
  - an **overpayment posted to 2300**, then **applied** to a second invoice →
    counted once; then that overpayment **refunded** → credit on account
    drops (ledger posting on);
  - a **full Stripe refund** (payment voided with a reason) → invoice owed
    again, payment absent from every range;
  - a **payment on a void invoice** → absent; draft and void invoices absent;
    jobless invoices present;
  - a **credit memo created at 11pm Central on Dec 31** → dated Dec 31;
  - presets against a fixed shop today, including Jan 1; range edges inclusive.
  - For every fixture: each invoice's counted entries add up to
    `total − balance_due`, and balance at today equals Σ `balance_due`.
- **PDF:** one real WeasyPrint render, not mocked; totals text present; no Pay
  link when Stripe is unconfigured.
- **Send:** real call with no mail transport → skip reason returned, one
  `outbound_emails` row, one audit row carrying the invoice lines; 403 without
  `invoices.send`.
- **Frontend:** vitest for preset date math; Playwright e2e for open dialog →
  preview → download.
- **Matrix:** `tools/run_tests_split.sh` (N=7), every FAIL and SKIP named, lint
  ratchet checked.
- **Whole-book check (throwaway container, copy of prod data):** run the
  service for every customer, every 2026 start day and every month-end plus
  today as the end. Compare the warned set with the three named in §5 and
  explain every difference by a record change; an unexplained difference is a
  failure. The allocation-order check must stay at 0 unwarned.
- **Browser walk (throwaway container):** the office role on desktop, light and
  dark; a customer with a QuickBooks-era short invoice, one with a deposit paid
  before its invoice, one with no email. The received email and PDF opened on
  the Pixel 8 AVD; Pay link tapped.
- **After deploy:** produce one real statement on prod (preview only) for a
  multi-invoice account and walk it.

## 10. Open questions

None. Decisions 5–7 were ruled 2026-09-15.

## 11. Not in this plan (found on the way, recorded in the close-out)

- The portal invoice list has no status filter and joins through jobs
  (`routers/portal.py`, `portal_invoices`).
- The collections aging always returns an empty customer name
  (`routers/collections.py`, `aging_report`).
- `_recalculate_invoice` writes `balance_due` on a void invoice.
- **QuickBooks payments counted twice** (2192, 2465, 2486; $7,052.79), the
  possible duplicate of QuickBooks payments 1831 and 1983 across invoices
  50530713 and 50187678 (unconfirmed without QuickBooks' allocation), and the
  wider QuickBooks-era payment-row repair that lifts decision 7
  (`qb-import-paid-status-repair-plan.md`). A money-data repair; its own plan.
- **Card and API payments dated on the UTC day** (`core/payments.py:808`,
  `routers/invoices.py:914`). A focused PR in #728's shape if Doug rules for
  it. The statement works either way.
