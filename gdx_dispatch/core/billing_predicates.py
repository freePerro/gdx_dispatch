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


def job_finalized_invoice_exists():
    """SQLAlchemy EXISTS clause: the correlated Job has an invoice that
    FINALIZES its billing — live, non-void, non-deposit, and PAST DRAFT.

    Closeout autodraft (2026-08-07): closing out a job now machine-creates a
    priced draft, so "a draft exists" no longer means "someone is done
    billing this" — it means "review this draft." A NULL status reads as
    draft here (indeterminate = not advanced), the cash-flow-safe direction:
    the job keeps nagging until a human moves the invoice past draft.
    """
    return exists().where(
        Invoice.job_id == Job.id,
        Invoice.deleted_at.is_(None),
        Invoice.status.is_not(None),
        Invoice.status.notin_(["void", "draft"]),
        or_(Invoice.billing_type.is_(None), Invoice.billing_type != "deposit"),
    )


def job_billing_resolved():
    """SQLAlchemy clause: billing is SETTLED for the correlated Job — either
    an invoice moved PAST DRAFT (sent/paid/overdue), or the office marked the
    job not billable (055_job_not_billable, 2026-08-04).

    This is the exit condition for every UNBILLED-NAG surface (Ready-for-
    Billing queue, /api/invoices/summary count, billing-followup task,
    recommendations). Use ``~job_billing_resolved()`` there instead of
    ``~job_billed_exists()``.

    Autodraft change (2026-08-07): this used to lean on ``job_billed_exists``,
    whose ``total > 0 OR status != 'draft'`` arm counts a PRICED DRAFT as
    billing the job. With closeout minting a priced draft automatically, that
    would have emptied Ready-for-Billing at the exact moment the draft needs
    review — so the settle condition is now ``job_finalized_invoice_exists``:
    drafts (any total, machine- or office-made) keep the job in the queue,
    where the UI offers "review the draft" instead of "create an invoice."

    Keep ``job_billed_exists()`` for the surfaces that ask the narrower
    question "does a billing-real invoice exist" (closeout already-billed
    checks, invoice-create 409 guard, the mobile Bill button) — there a
    priced draft SHOULD block minting a second invoice. The two predicates
    now deliberately diverge on priced drafts: billed-exists says "don't
    create another," billing-resolved says "but billing isn't finished."
    """
    return or_(job_finalized_invoice_exists(), Job.not_billable_at.is_not(None))


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


def first_billing_real_invoice(db, job_id):
    """The double-billing guard's query — ONE spelled copy (M38 extracted it;
    the desktop and mobile create paths each hand-spelled it before, which is
    how drift starts).

    Returns the newest billing-real invoice on the job, else None. Conditions
    match the audited guard behavior both paths shipped with: raw ``!=`` on
    status/billing_type — NOT the NULL-coalescing twins above. On a row where
    either column were NULL the EXISTS/Python predicates read UNBILLED while
    this guard would also not match (NULL != 'void' is NULL → row excluded →
    guard treats it as unbilled too, via a different route). Both columns are
    ``nullable=False`` with defaults, so the divergence is unreachable today;
    it is documented here so the next reader doesn't have to re-derive it.
    """
    from sqlalchemy import select

    return db.execute(
        select(Invoice).where(
            Invoice.job_id == job_id,
            Invoice.deleted_at.is_(None),
            Invoice.status != "void",
            Invoice.billing_type != "deposit",
            or_(Invoice.total > 0, Invoice.status != "draft"),
        ).order_by(Invoice.created_at.desc()).limit(1)
    ).scalar_one_or_none()
