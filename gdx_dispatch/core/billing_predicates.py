"""Canonical "is this job billed" predicate — ONE definition, shared.

PR2-billing-capture (2026-07-07). `Job.billing_status` is a dead cache (only
ever written "unbilled" — see core/job_display_state.py's header), so every
consumer that filtered on it was wrong: the unbilled-work alert counted paid
jobs, and the two Ready-for-Billing queries disagreed with the display state
about voided invoices. Per the locked derive-don't-cache model (Doug
2026-05-17), readers derive from invoices instead.

The definition (design doc: docs/design/billing-capture-hardening-plan.md,
audit round 1):

    A job is BILLED iff it has an invoice with
        deleted_at IS NULL
        AND status != 'void'
        AND billing_type != 'deposit'
        AND (total > 0 OR status != 'draft')

Three deliberate exclusions:
- **Deposit invoices don't bill a job** (2026-07-23). A downpayment collected
  at estimate acceptance is money BEFORE the work; treating it as "billed"
  would silently remove every deposit-taking job from Ready-for-Billing and
  hide the mobile Bill button — the company collects 50% and loses the
  machinery for the other 50%. Final/standard/progress invoices still bill.
- **Void invoices don't bill a job.**
 The display state already treats void
  as dead money; the old RFB queries didn't — a job whose only invoice was
  voided silently vanished from Ready-for-Billing forever.
- **$0 DRAFTS don't bill a job.** `create_invoice_from_job` fabricates a
  single $0 draft line when a job has no estimate — treating that placeholder
  as "billed" would hide the job from every alert (silent false negative).
  A $0 invoice that was deliberately SENT does count (warranty work — the
  operator said "this is free", stop nagging).

Known edge (documented, accepted): the display state's `live_invoices` still
counts a $0 draft as "Invoiced" — the locked display model is untouched here.
A job with only the fabricated $0 draft shows "Invoiced" on the job card but
stays in Ready-for-Billing/alerts, which is the cash-flow-safe direction.
"""
from __future__ import annotations

from sqlalchemy import exists, or_

from gdx_dispatch.models.tenant_models import Invoice, Job


def job_billed_exists():
    """SQLAlchemy EXISTS clause: the correlated Job has a billing-real invoice.

    Use as a filter on a Job query:

        query.filter(job_billed_exists())          # billed jobs
        query.filter(~job_billed_exists())         # unbilled jobs
    """
    # NULL-safe (plan §7.3): SQL three-valued logic makes `status != 'void'`
    # and `billing_type != 'deposit'` evaluate to NULL (→ EXISTS fails →
    # job reads UNBILLED) whenever the column is NULL — even on a paid
    # invoice. The Python twin `invoice_bills_job` coalesces and returns the
    # opposite, so the two halves would silently disagree the first time a
    # NULL lands in either column. Prod has none today, but a QB import or a
    # future writer could, and the tests pin both against fixtures that always
    # set the field. `IS NULL OR …` restores the intended "a missing type is
    # not a deposit / not a void" reading.
    return exists().where(
        Invoice.job_id == Job.id,
        Invoice.deleted_at.is_(None),
        or_(Invoice.status.is_(None), Invoice.status != "void"),
        or_(Invoice.billing_type.is_(None), Invoice.billing_type != "deposit"),
        # The total>0 arm already rescues a paid invoice with a NULL status;
        # a NULL status at $0 stays unbilled (a fabricated placeholder must
        # not drop a job out of Ready-for-Billing — cash-flow-safe).
        or_(Invoice.total > 0, Invoice.status != "draft"),
    )


def job_billing_resolved():
    """SQLAlchemy clause: billing is SETTLED for the correlated Job — either a
    billing-real invoice exists, or the office marked it not billable
    (055_job_not_billable, 2026-08-04).

    This is the exit condition for every UNBILLED-NAG surface (Ready-for-
    Billing queue, /api/invoices/summary count, billing-followup task,
    recommendations). Use ``~job_billing_resolved()`` there instead of
    ``~job_billed_exists()``. The mobile Bill button gets the same outcome a
    different way: `billed` stays the narrow predicate (it must not lie) and
    the mark ships as its own `not_billable` key (routers/mobile.py).

    Keep ``job_billed_exists()`` for the surfaces that ask the narrower
    question "did an invoice actually go out" (closeout already-billed checks,
    invoice-create 409 guard) — a not-billable mark must not read as "an
    invoice exists" in those. A stale mark on a job that later got a real
    invoice is harmless here: either arm resolves.
    """
    return or_(job_billed_exists(), Job.not_billable_at.is_not(None))


def invoice_bills_job(
    status: str | None,
    total: float | None,
    deleted_at,
    billing_type: str | None = None,
) -> bool:
    """Python-side twin of job_billed_exists() for already-loaded rows.

    Keep the two in lockstep — a test pins them against the same fixtures.
    """
    if deleted_at is not None:
        return False
    s = (status or "").strip().lower()
    if s == "void":
        return False
    # Deposit invoices (2026-07-23): a downpayment collected at estimate
    # acceptance must NOT count as "the job is billed" — the work hasn't
    # happened yet, and the whole point of Ready-for-Billing / the mobile
    # Bill button is to produce the FINAL invoice later. Only standard/
    # progress/final invoices bill a job.
    if (billing_type or "").strip().lower() == "deposit":
        return False
    # NULL/empty status reads like draft here (indeterminate = not advanced
    # past draft), so a $0 NULL-status invoice is unbilled — lockstep with the
    # SQL, whose `status != 'draft'` goes NULL and fails EXISTS for that row.
    return float(total or 0) > 0 or s not in ("", "draft")
