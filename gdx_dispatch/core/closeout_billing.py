"""Closeout → draft invoice: the ONE line-builder, and the autodraft hook.

Two callers, one pricing implementation (the A1 rule — never fork a second
implementation of an ownership/pricing gate):

* ``routers/mobile_invoicing.py`` — the tech taps "Create invoice" on the
  truck; lines are built into the invoice that endpoint minted.
* ``routers/jobs.py::closeout_job`` — the autodraft (Doug 2026-08-07):
  closing out a job with no accepted estimate creates a DRAFT invoice
  right away, so Ready-for-Billing reviews an existing priced draft
  instead of starting from a blank form.

Pricing itself lives in ``core/billing_lanes`` (service/install lanes) and
is unchanged here. UNPRICED closeout parts are deliberately never lined:
they stay on the office checklist, and the §11 verification gate holds the
invoice until a human prices them — nothing is silently $0-lined onto a
customer PDF.

The autodraft is REBUILDABLE while untouched: a re-closeout wipes and
re-lines the same draft (same invoice number — "the invoice for this job"
stays one thing to look at), and Not-billable voids it. "Untouched" is
strict — machine origin AND draft status AND never verified/sent/locked/
paid. The moment a human advances the draft, the machine keeps its hands
off and the §12 discrepancy flow takes over.
"""
from __future__ import annotations

import logging
import secrets
import uuid as _uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobCloseout, JobPartNeeded
from gdx_dispatch.modules.proposals.models import Estimate

log = logging.getLogger(__name__)

AUTODRAFT_ORIGIN = "closeout_autodraft"


def _money(v: Decimal | float | str) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def next_invoice_number(db: Session) -> str:
    """INV-000123-style sequence off the latest row (moved here from
    mobile_invoicing so the autodraft doesn't import from a router)."""
    row = db.execute(
        _text("SELECT invoice_number FROM invoices ORDER BY created_at DESC LIMIT 1")
    ).first()
    if row and row[0] and row[0].startswith("INV-"):
        try:
            n = int(row[0].split("-", 1)[1]) + 1
            return f"INV-{n:06d}"
        except (ValueError, AttributeError):
            pass
    return f"INV-{datetime.now(UTC):%y%m}{secrets.token_hex(2).upper()}"


def build_closeout_lines(
    db: Session,
    *,
    tenant_id: str,
    invoice: Invoice,
    closeout: JobCloseout,
    job_type: str | None,
    job_id: str,
) -> tuple[int, Decimal]:
    """Add invoice lines priced from the closeout. Returns (lines_added,
    lines_total). Extracted verbatim from the mobile truck path (plan §8).

    Lanes decide (core/billing_lanes): service → hourly labor line; install
    with a picked matrix row → flat price; everything else → labor stays
    UNPRICED and the §11 verification gate is the flag.

    Parts: the closeout's attested, still-unbilled checklist rows. PRICED
    rows become lines and are claimed with the same stamp-first rule as the
    office path (a row the stamp cannot claim was billed by a concurrent
    invoice — skip it, never double-bill). UNPRICED rows are deliberately
    left unstamped: they stay on the office checklist.
    """
    from gdx_dispatch.core.billing_lanes import (
        install_labor_line,
        lane_for_job,
        service_labor_line,
    )

    # The closeout's JobPartNeeded rows may be pending in this session
    # (closeout_job adds them in the same transaction); the SELECT below
    # must see them even with autoflush off.
    db.flush()

    sort = 1
    lines_added = 0
    lines_total = Decimal("0")
    lane = lane_for_job(job_type)
    if lane == "install" and getattr(closeout, "labor_matrix_item_id", None):
        # Install lane: flat price from the picked matrix row. If the row is
        # gone/inactive/$0, _install is None → labor stays unpriced (office
        # lane), never guessed.
        _install = install_labor_line(db, closeout.labor_matrix_item_id)
        if _install is not None:
            db.add(InvoiceLine(
                id=_uuid.uuid4(),
                invoice_id=invoice.id,
                description=_install.description,
                quantity=_install.quantity,
                unit_price=_install.unit_price,
                line_total=_install.line_total,
                sort_order=sort,
                company_id=str(tenant_id),
            ))
            lines_total += _install.line_total
            lines_added += 1
            sort += 1
    if lane == "service" and float(closeout.hours_worked or 0) > 0:
        labor = service_labor_line(
            db,
            hours_worked=float(closeout.hours_worked or 0),
            techs_on_site=int(getattr(closeout, "techs_on_site", 1) or 1),
        )
        db.add(InvoiceLine(
            id=_uuid.uuid4(),
            invoice_id=invoice.id,
            description=labor.description,
            quantity=labor.quantity,
            unit_price=labor.unit_price,
            line_total=labor.line_total,
            sort_order=sort,
            company_id=str(tenant_id),
        ))
        lines_total += labor.line_total
        lines_added += 1
        sort += 1

    candidate_rows = db.execute(
        select(JobPartNeeded).where(
            JobPartNeeded.job_id == str(job_id),
            JobPartNeeded.source == "closeout",
            JobPartNeeded.billed_invoice_id.is_(None),
            JobPartNeeded.unit_price.is_not(None),
            JobPartNeeded.unit_price > 0,
        )
    ).scalars().all()
    for part_row in candidate_rows:
        claimed = db.execute(
            update(JobPartNeeded)
            .where(
                JobPartNeeded.id == part_row.id,
                JobPartNeeded.billed_invoice_id.is_(None),
            )
            .values(billed_invoice_id=invoice.id)
        ).rowcount
        if not claimed:
            continue
        qty = int(part_row.quantity or 1)
        unit = _money(part_row.unit_price or 0)
        db.add(InvoiceLine(
            id=_uuid.uuid4(),
            invoice_id=invoice.id,
            description=(part_row.part_name or part_row.sku or "Part")[:500],
            quantity=qty,
            unit_price=unit,
            line_total=_money(float(unit) * qty),
            sort_order=sort,
            company_id=str(tenant_id),
        ))
        lines_total += _money(float(unit) * qty)
        lines_added += 1
        sort += 1
    return lines_added, lines_total


def is_untouched_autodraft(inv: Invoice) -> bool:
    """True only while the machine may still rebuild or void this invoice.

    Strict on purpose: any human advancement (verify, send, lock, payment,
    status past draft) permanently ends machine ownership."""
    return (
        (inv.origin or "") == AUTODRAFT_ORIGIN
        and (inv.status or "draft") == "draft"
        and inv.verified_at is None
        and inv.sent_at is None
        and not bool(inv.locked)
        and inv.paid_at is None
        and float(inv.amount_paid or 0) == 0
    )


def _live_autodraft(db: Session, job_id) -> Invoice | None:
    return db.execute(
        select(Invoice).where(
            Invoice.job_id == job_id,
            Invoice.deleted_at.is_(None),
            Invoice.origin == AUTODRAFT_ORIGIN,
            Invoice.status == "draft",
        )
        .order_by(Invoice.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def release_untouched_autodraft(db: Session, *, job: Job) -> Invoice | None:
    """Pre-restatement reset. MUST run BEFORE closeout_job's replace step:
    un-claiming the draft's part stamps turns those rows back into unbilled
    closeout rows, so the replace step deletes them (reversing stock) and the
    new attestation lands cleanly — otherwise the old claimed rows survive as
    "billed" and the re-attested lines get suppressed as billed dupes.

    Returns the emptied draft for ``autodraft_invoice_for_closeout`` to
    rebuild into (same invoice number), or None when there is nothing the
    machine may touch."""
    inv = _live_autodraft(db, job.id)
    if inv is None or not is_untouched_autodraft(inv):
        return None
    db.execute(
        update(JobPartNeeded)
        .where(JobPartNeeded.billed_invoice_id == inv.id)
        .values(billed_invoice_id=None)
    )
    db.execute(
        InvoiceLine.__table__.delete().where(InvoiceLine.invoice_id == inv.id)
    )
    inv.subtotal = _money(0)
    inv.tax_amount = _money(0)
    inv.total = _money(0)
    inv.balance_due = _money(0)
    return inv


def void_untouched_autodraft(db: Session, inv: Invoice) -> None:
    """Void an untouched autodraft and release its part claims (Not-billable
    path). Caller has already checked ``is_untouched_autodraft``."""
    db.execute(
        update(JobPartNeeded)
        .where(JobPartNeeded.billed_invoice_id == inv.id)
        .values(billed_invoice_id=None)
    )
    inv.status = "void"
    inv.balance_due = _money(0)


def autodraft_invoice_for_closeout(
    db: Session,
    *,
    tenant_id: str,
    job: Job,
    closeout: JobCloseout,
    reuse_invoice: Invoice | None = None,
) -> Invoice | None:
    """Create (or rebuild) the draft invoice for a just-submitted closeout.

    Runs inside closeout_job's transaction, AFTER the closeout snapshot and
    its parts rows are in the session. Eligibility — every skip returns
    None and the closeout proceeds unbilled (Ready-for-Billing still shows
    the job with the classic Create Invoice action):

    * office marked the job not billable → never bill it by machine
    * no customer → invoices.customer_id is NOT NULL
    * an accepted estimate exists → §15.1: the estimate outranks the lanes;
      the office/mobile estimate paths own that invoice
    * any live non-void invoice already exists (and we're not rebuilding) →
      one invoice story per job; deposits/partials mean a human is driving

    A NEW draft is only kept when at least one priced line landed — an empty
    $0 draft on every unpriceable closeout would just be queue noise. A
    REBUILT draft is kept even at zero lines: it already exists, and leaving
    it live keeps its number stable for the office to fill in.
    """
    if job.not_billable_at is not None:
        return None
    if job.customer_id is None:
        return None
    accepted = db.execute(
        select(Estimate.id).where(
            Estimate.job_id == job.id,
            Estimate.status == "accepted",
            Estimate.deleted_at.is_(None),
        ).limit(1)
    ).first()
    if accepted is not None:
        return None

    inv = reuse_invoice
    if inv is None:
        live = db.execute(
            select(Invoice.id).where(
                Invoice.job_id == job.id,
                Invoice.deleted_at.is_(None),
                or_(Invoice.status.is_(None), Invoice.status != "void"),
            ).limit(1)
        ).first()
        if live is not None:
            return None
        customer_id = job.customer_id
        if not isinstance(customer_id, _uuid.UUID):
            try:
                customer_id = _uuid.UUID(str(customer_id))
            except (ValueError, AttributeError):
                return None
        today = date.today()
        inv = Invoice(
            id=_uuid.uuid4(),
            job_id=job.id,
            invoice_number=next_invoice_number(db),
            billing_type="standard",
            sequence_number=1,
            subtotal=_money(0),
            tax_amount=_money(0),
            total=_money(0),
            balance_due=_money(0),
            status="draft",
            origin=AUTODRAFT_ORIGIN,
            invoice_date=today,
            due_date=today + timedelta(days=30),
            public_token=secrets.token_urlsafe(48)[:64],
            locked=False,
            customer_id=customer_id,
            company_id=str(tenant_id),
        )
        db.add(inv)
        db.flush()

    lines_added, lines_total = build_closeout_lines(
        db,
        tenant_id=str(tenant_id),
        invoice=inv,
        closeout=closeout,
        job_type=job.job_type,
        job_id=str(job.id),
    )
    if lines_added == 0 and reuse_invoice is None:
        db.delete(inv)
        return None
    if lines_total > 0:
        inv.subtotal = _money(lines_total)
        inv.total = _money(lines_total)
        inv.balance_due = _money(lines_total)
    return inv
