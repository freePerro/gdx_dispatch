# Recurring Service-Agreement Billing — Design Sketch

**Status:** **NOT SCHEDULED** — no recurring-service customers exist. Filed
2026-08-23 to discharge a promise made in `billing-capture-hardening-plan.md`
§3 (2026-07-07) and never kept: "filing the real feature (schedule columns +
UI + task) as its own designed piece of work when a recurring customer
exists." Nothing here is built. Do not start it without a real customer.

## Why this doc exists at all

The predecessor was not a missing feature — it was a **booby trap**. A
`tasks/recurring_billing.py` sat in the tree issuing a raw INSERT against
`service_agreements` columns that **have never existed in any migration**:
`amount`, `billing_interval_months`, `next_billing_date`, `active`. The real
model carries `name / price / status / start_date / end_date`. The task was
never in `celery_app.py` includes and never in beat, so it never ran; wiring
it up would have crashed on the first tick. It was deleted on 2026-07-07
rather than revived.

Without this doc the next reader finds "recurring billing" named in an old
plan, greps, finds nothing, and rebuilds from the broken original. That is
the exact failure the 2026-08-18 corpus audit catalogued.

## What already exists (do not rebuild)

| Piece | Where | State |
|---|---|---|
| `ServiceAgreement` model | `models/tenant_models.py:1888` | Live. `name/price/status/start_date/end_date/services_included/notes`. **No billing-cycle fields.** |
| `ServiceAgreementTemplate` | `models/tenant_models.py:1874` | Live. `default_duration_months`, `default_price`. |
| CRUD API | `routers/service_agreements.py` | Live, audited, soft-delete. Handles the Vue's legacy `price` vs `default_price` split. |
| Office UI | `views/ServiceAgreementsView.vue`, route `/service-agreements` | Live and reachable. Describes itself as managing recurring **services**, and makes no promise of automatic invoicing — so today's surface is honest. |
| Invoice creation | `routers/invoices.py` `create_invoice` | Live, but a **FastAPI handler** (auth dependency + `_["tenant_id"]`). Not callable from Celery. |
| Dead-task regression guard | `tests/test_billing_followup_pr5.py:335` | Live. Asserts the task-exists-but-never-scheduled shape cannot return. |

**Prod reality, checked 2026-08-23:** `service_agreements` = 0 rows,
`service_agreement_templates` = 0 rows. Zero users. This is why it is not
scheduled.

## What it would take

1. **Migration — the schedule the model never had.** `billing_interval_months`
   (INT), `next_billing_date` (DATE), and a real `active` predicate (or reuse
   `status`). Every migration runs on both SQLite and Postgres; escape literal
   `%` as `%%`.
2. **Extract `create_invoice_core`.** `modules/billing/invoice_factory.py`
   exposing a plain `create_invoice_core(db, *, tenant_id, customer_id, lines,
   job_id=None, estimate_id=None, ...)` used by **both** the router and the
   task, so a generated invoice inherits the billing-terms due-date resolver,
   sequence number, tax path, and balance fields rather than re-deriving them.
   Router and task must produce byte-identical invoices; that parity is a test,
   not a hope.
3. **The task.** Add to `celery_app.py` includes + beat. New invoices stay
   `draft`; PR 1's draft surfacing and PR 5's follow-up loop chase them.
4. **Backlog guard before first run.** `SELECT count(*) FROM service_agreements
   WHERE active AND next_billing_date <= now()` on prod. A backlog bulk-creates
   invoices on the first tick. If nonzero: stop, show the list, decide whether
   to roll `next_billing_date` forward before enabling.
5. **Audit + money discipline.** Generated invoices are money mutations:
   `log_audit_event()` on every create, a non-anonymous acting identity for the
   automated path, and soft-delete only. MN construction contracts carry **no
   customer sales tax** — the tax path must not invent any.
6. **A visible way in.** A schedule the office can see and edit on the
   agreement, and a way to tell that an invoice was machine-generated. A
   silent biller is worse than no biller.

## Traps carried forward

- **Do not resurrect the deleted task.** Its column names are fiction. Start
  from the migration.
- **`create_invoice` cannot be called from Celery.** Extracting the core is
  step zero, not a refactor to do later.
- **Draft ≠ billed.** `core/billing_predicates.py` holds the canonical
  predicate; mass-created drafts must not make jobs invisible to alerts.
