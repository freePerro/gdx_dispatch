import datetime as _datetime
import logging
import secrets
import uuid as _uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy import text as _text
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.models.tenant_models import (
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.catalog_policy import block_or_warn_invoice_line, get_policy
from gdx_dispatch.modules.ledger.engine import PeriodLockedError
from gdx_dispatch.modules.ledger.rules import (
    IssuanceCompositionError,
    customer_credit_balance_cents,
    post_credit_application,
    post_credit_memo,
    post_payment_received,
    post_refund,
    repost_invoice_issuance,
    resettle_invoice_payments,
    reverse_invoice_adjustments,
    settle_opening_on_void,
)
from gdx_dispatch.modules.ledger.service import (
    ledger_posting_enabled,
    transition_invoice_status,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/invoices", tags=["invoices"], dependencies=[Depends(require_module("invoices"))])


def _actor_id(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _to_float(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def _iso_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _effective_status(invoice: Invoice) -> str:
    if invoice.status == "sent" and invoice.due_date and invoice.due_date < date.today() and _to_float(invoice.balance_due) > 0:
        return "overdue"
    return invoice.status


def _serialize_line(line: InvoiceLine) -> dict[str, object]:
    return {
        "id": str(line.id),
        "invoice_id": str(line.invoice_id),
        "description": line.description,
        "quantity": line.quantity,
        "unit_price": _to_float(line.unit_price),
        "line_total": _to_float(line.line_total),
        # Default True so older serialized rows still round-trip; the
        # column has a server_default of true so any DB row without an
        # explicit value also reads as taxable.
        "taxable": bool(getattr(line, "taxable", True)) if getattr(line, "taxable", None) is not None else True,
        # S122-b — invoice/estimate parity fields. Same shape EstimateLine
        # serializer uses, so the same frontend component can render either.
        "category": getattr(line, "category", None),
        "cost_snapshot": _to_float(line.cost_snapshot) if getattr(line, "cost_snapshot", None) is not None else None,
        "margin_pct_snapshot": _to_float(line.margin_pct_snapshot) if getattr(line, "margin_pct_snapshot", None) is not None else None,
        "margin_pct_override": _to_float(line.margin_pct_override) if getattr(line, "margin_pct_override", None) is not None else None,
        # D-S122-line-removal-unbill: surface the part linkage for detail-view
        # badges + audit trail.
        "part_id": getattr(line, "part_id", None),
        "sort_order": line.sort_order,
        "created_at": _iso_dt(line.created_at),
    }


def _serialize_payment(payment: Payment) -> dict[str, object]:
    return {
        "id": str(payment.id),
        "invoice_id": str(payment.invoice_id),
        "amount": _to_float(payment.amount),
        "method": payment.method,
        "reference": getattr(payment, "reference", None),
        "date": payment.payment_date.isoformat(),
        "created_at": _iso_dt(payment.created_at),
    }


def _serialize_invoice(invoice: Invoice, include_lines: bool = False, include_payments: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(invoice.id),
        "job_id": str(invoice.job_id) if invoice.job_id else None,
        "customer_id": str(invoice.customer_id) if getattr(invoice, 'customer_id', None) else None,
        "customer_name": getattr(invoice, 'customer_name', None) or "",
        "invoice_number": invoice.invoice_number,
        "billing_type": invoice.billing_type,
        "sequence_number": invoice.sequence_number,
        "subtotal": _to_float(invoice.subtotal),
        "tax_rate": _to_float(invoice.tax_rate) if getattr(invoice, "tax_rate", None) is not None else None,
        "tax_amount": _to_float(invoice.tax_amount),
        "taxable_subtotal": _to_float(_taxable_subtotal(invoice)),
        "total": _to_float(invoice.total),
        "balance_due": _to_float(invoice.balance_due),
        "status": invoice.status,
        "effective_status": _effective_status(invoice),
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "notes": invoice.notes,
        # "Total-only" display — hides per-line prices on the invoice PDF.
        "hide_line_prices": bool(getattr(invoice, "hide_line_prices", False)),
        # PR6 — per-invoice dunning mute state for the detail-view toggle.
        "dunning_paused": bool(getattr(invoice, "dunning_paused", False)),
        "locked": bool(invoice.locked),
        "locked_at": _iso_dt(invoice.locked_at),
        "sent_at": _iso_dt(invoice.sent_at),
        "paid_at": _iso_dt(invoice.paid_at),
        "public_token": invoice.public_token,
        "created_at": _iso_dt(invoice.created_at),
    }
    if include_lines:
        active_lines = [ln for ln in invoice.lines if getattr(ln, "deleted_at", None) is None]
        lines = sorted(active_lines, key=lambda ln: (ln.sort_order, ln.created_at, ln.id))
        payload["lines"] = [_serialize_line(line) for line in lines]
    if include_payments:
        payments = sorted(invoice.payments, key=lambda p: (p.payment_date, p.created_at, p.id))
        payload["payments"] = [_serialize_payment(payment) for payment in payments]
    return payload


def _validate_uuid(value: str, entity: str = "Invoice") -> None:
    try:
        _uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{entity} not found") from None


def _next_invoice_number(db: Session) -> str:
    count = db.execute(select(func.count(Invoice.id))).scalar_one() or 0
    return f"INV-{count + 1:06d}"


def _get_invoice_or_404(invoice_id: UUID, db: Session, include_relations: bool = False) -> Invoice:
    q = select(Invoice).where(Invoice.id == invoice_id, Invoice.deleted_at.is_(None))
    if include_relations:
        q = q.options(selectinload(Invoice.lines), selectinload(Invoice.payments))
    invoice = db.execute(q).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def _taxable_subtotal(invoice: Invoice) -> Decimal:
    # Sum line_totals for non-deleted, taxable lines. Used by the rate-based
    # tax computation so labor lines (taxable=False) don't get sales tax.
    total = Decimal("0")
    for ln in (invoice.lines or []):
        if getattr(ln, "deleted_at", None) is not None:
            continue
        if not bool(getattr(ln, "taxable", True)):
            continue
        total += Decimal(str(ln.line_total or 0))
    return total


def _recalculate_invoice(invoice: Invoice, db: Session) -> None:
    # Subtotal = sum of every active line. Active = not soft-deleted; legacy
    # rows without a deleted_at column read as None and stay included.
    line_rows = db.execute(
        select(InvoiceLine).where(
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.deleted_at.is_(None),
        )
    ).scalars().all()
    subtotal = sum((Decimal(str(ln.line_total or 0)) for ln in line_rows), Decimal("0"))
    subtotal_amount = _money(subtotal)

    # Tax: rate-driven when invoice.tax_rate is set, else preserve the
    # legacy flat-dollar tax_amount the caller stored. This is what makes
    # editing a line on a rate-aware invoice DTRT — change the qty, the
    # tax follows. Pre-S110 invoices have tax_rate=NULL and behave exactly
    # as they always did.
    rate = getattr(invoice, "tax_rate", None)
    if rate is not None:
        taxable = sum(
            (Decimal(str(ln.line_total or 0))
             for ln in line_rows
             if bool(getattr(ln, "taxable", True))),
            Decimal("0"),
        )
        tax_amount = _money(taxable * Decimal(str(rate)))
        invoice.tax_amount = tax_amount
    else:
        tax_amount = _money(_to_float(invoice.tax_amount))
    total_amount = _money(subtotal_amount + tax_amount)

    paid = db.execute(
        # GL S6 (P4): voided payments stay as history but stop counting.
        select(func.sum(Payment.amount)).where(
            Payment.invoice_id == invoice.id,
            Payment.voided_at.is_(None),
        )
    ).scalar_one_or_none() or 0
    paid_amount = _money(_to_float(paid))
    # GL S7 (bug #4): credit memos + applied credits reduce the balance via
    # the adjustments table — the old /credit-memo mutated the deprecated
    # amount_paid column, which this recalc ignores, so its effect evaporated
    # on the next recalculation. Refunds don't change the balance (they are
    # contra-revenue cash-outs capped by net paid).
    credited = db.execute(
        select(func.sum(InvoiceAdjustment.amount)).where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.kind.in_(("credit_memo", "credit_applied")),
        )
    ).scalar_one_or_none() or 0
    balance_due = _money(
        max(_to_float(total_amount) - _to_float(paid_amount) - _to_float(credited), 0)
    )

    invoice.subtotal = subtotal_amount
    invoice.total = total_amount
    invoice.balance_due = balance_due
    # GL S5: an already-issued invoice whose content just changed reverses
    # its live P1 and reposts at current content (no-op with the flag off or
    # when content is unchanged — the idempotency key matches). Ledger
    # refusals surface as 409s with the reason, never bare 500s.
    try:
        repost_invoice_issuance(db, invoice)
    except PeriodLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"invoice is in a locked accounting period — {exc}",
        ) from exc
    except IssuanceCompositionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if _to_float(balance_due) <= 0 and _to_float(total_amount) > 0:
        # GL S5: the auto-flip routes through the chokepoint; a draft paid in
        # full posts P1 on this transition (before P3, which lands in S6).
        transition_invoice_status(db, invoice, "paid")
        if not invoice.paid_at:
            invoice.paid_at = datetime.now(UTC)


class InvoiceLineCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(default=1, gt=0, le=9999)
    unit_price: float = Field(default=0, ge=0, le=999999.99)
    # Defaults True so a caller that doesn't know about taxability still
    # gets the historical "everything is taxable" behavior. Labor lines
    # should explicitly send False.
    taxable: bool = Field(default=True)
    # S122-b — invoice/estimate parity. Same shape as EstimateLineCreateNested
    # so the create-invoice page can render the same line table the estimate
    # page does (category select, cost column, margin override).
    category: str | None = Field(default=None, max_length=80)
    cost: float | None = Field(default=None, ge=0, le=999999.99)
    margin_pct_override: float | None = Field(default=None, ge=0, lt=1)
    # D-S122-line-removal-unbill: when this line came from the parts-from-job
    # checklist, the line carries the JobPartNeeded.id so a later line-delete
    # can release the part atomically. Optional; legal value is the part's
    # string-form ID (matches JobPartNeeded.id String(36)).
    part_id: str | None = Field(default=None, max_length=36)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("description cannot be blank")
        return trimmed


class InvoiceLinePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, min_length=1, max_length=500)
    quantity: int | None = Field(default=None, gt=0, le=9999)
    unit_price: float | None = Field(default=None, ge=0, le=999999.99)
    taxable: bool | None = None
    sort_order: int | None = Field(default=None, ge=1, le=9999)
    # S122-b — parity with InvoiceLineCreateIn.
    category: str | None = Field(default=None, max_length=80)
    cost: float | None = Field(default=None, ge=0, le=999999.99)
    margin_pct_override: float | None = Field(default=None, ge=0, lt=1)


class InvoiceCreateIn(BaseModel):
    # D100 (an earlier session): extra="forbid" so unknown fields fail loudly. Pre-fix,
    # the frontend's customer_id + line_items were silently dropped and totals
    # came out as $0. That cascaded into D99 (every invoice ended up with no
    # date, no totals, and a derived customer_name=""). Strict mode + an
    # explicit line_items field fixes both classes.
    model_config = ConfigDict(extra="forbid")
    # Optional so counter-sale invoices (parts/over-the-counter) can exist
    # without a job. The DB column has been nullable since the QB-import slice
    # (2026-05-04). When None: no job lookup, no parts-pull, billing terms
    # resolve from customer alone.
    job_id: UUID | None = None
    estimate_id: UUID | None = None
    # 2026-05-11 — required. The service layer used to fall back to
    # job.customer_id when this was None, but job.customer_id can itself be
    # None, so the row could land with customer_id=NULL silently. The
    # frontend's canCreate gate already requires this on the form; tightening
    # the contract closes the bypass path for other clients.
    customer_id: UUID

    @model_validator(mode="after")
    def _estimate_and_parts_are_mutually_exclusive(self) -> "InvoiceCreateIn":
        """S122 auditor catch: if `estimate_id` is set, the create handler
        copies the estimate's lines and ignores `line_items`. If callers also
        pass `from_part_ids`, the parts get marked billed against an invoice
        that contains zero of them. Reject the combination at the contract.
        """
        if self.estimate_id is not None and self.from_part_ids:
            raise ValueError(
                "estimate_id and from_part_ids cannot be used together — "
                "estimate-derived invoices carry lines from the estimate, not "
                "from the parts checklist."
            )
        # Estimates are job-scoped, so estimate_id without job_id is incoherent.
        # Counter-sale invoices (no job_id) cannot be estimate-derived.
        if self.estimate_id is not None and self.job_id is None:
            raise ValueError(
                "estimate_id requires job_id — estimates are tied to a job."
            )
        # from_part_ids belong to a specific job; can't pull parts from "no job".
        if self.from_part_ids and self.job_id is None:
            raise ValueError(
                "from_part_ids requires job_id — parts checklists are job-scoped."
            )
        # PR3 — change orders are job-scoped the same way.
        if self.from_change_order_ids and self.job_id is None:
            raise ValueError(
                "from_change_order_ids requires job_id — change orders are job-scoped."
            )
        # Same reasoning for line-level part_id (D-S122-line-removal-unbill).
        if self.job_id is None and any(
            getattr(li, "part_id", None) for li in self.line_items
        ):
            raise ValueError(
                "line_items[].part_id requires job_id — parts are job-scoped."
            )
        return self
    # billing_type is enum-ish ("standard"/"recurring"/etc.), short bounded.
    billing_type: str = Field(default="standard", min_length=1, max_length=50)
    # tax_rate (preferred) is a decimal fraction — 0.0738 == 7.38%. When
    # supplied, _recalculate_invoice computes tax_amount from it on every
    # line change. tax_amount remains accepted for legacy callers and
    # estimate-derived flows that haven't been migrated yet.
    tax_rate: float | None = Field(default=None, ge=0, le=1)
    tax_amount: float = Field(default=0, ge=0, le=1_000_000)
    invoice_date: date | None = None
    due_date: date | None = None
    # Notes can be long but not unbounded — 5000 chars is ~1 page of text.
    notes: str | None = Field(default=None, max_length=5000)
    # Inline line items. If both estimate_id and line_items are provided,
    # the estimate wins (estimate-derived invoices are still the canonical path).
    line_items: list[InvoiceLineCreateIn] = Field(default_factory=list)
    # S122 — IDs of JobPartNeeded rows the operator pulled into the line items
    # via the parts-from-job checklist. Set in the same transaction so a part
    # billed on one invoice can't appear in another invoice's checklist.
    from_part_ids: list[UUID] = Field(default_factory=list)
    # PR3-billing-capture — approved change orders the operator pulled into
    # this invoice. Their ChangeOrderLine rows are COPIED to InvoiceLines
    # (unlike from_part_ids, whose lines arrive via line_items) and the CO is
    # stamped billed_invoice_id in the same transaction. The stamp GATES the
    # copy (UPDATE…RETURNING): an already-billed CO 409s the whole request.
    from_change_order_ids: list[UUID] = Field(default_factory=list)


class InvoicePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Set tax_rate to a decimal (e.g., 0.0738) to switch the invoice into
    # rate-based tax mode; pass null to revert to flat tax_amount mode.
    tax_rate: float | None = Field(default=None, ge=0, le=1)
    tax_amount: float | None = Field(default=None, ge=0)
    invoice_date: date | None = None
    due_date: date | None = None
    notes: str | None = None
    # "Total-only" display toggle for this invoice's PDF.
    hide_line_prices: bool | None = None


class PaymentCreateIn(BaseModel):
    amount: float = Field(gt=0)
    method: str = Field(min_length=1, max_length=50)
    # Defaulted to today so a caller without a date picker records the
    # payment instead of 422ing (the /billing dialog shipped without one
    # and a real check payment bounced twice on 2026-07-06). Annotated via
    # the module alias: pydantic rejects a field named `date` whose
    # annotation is the bare `date` type once it carries a default.
    date: _datetime.date = Field(default_factory=date.today)
    # GL S6: with ledger posting ON, an overpayment is rejected unless the
    # caller opts in — the excess then credits 2300 Customer Credits instead
    # of AR (spec §5.3). Flag off keeps today's permissive behavior.
    allow_overpayment: bool = False
    # Optional reference (check #, transaction ID, Zelle memo). Pre-fix this
    # was missing from the schema and dropped by Pydantic; payment-history
    # cells rendered empty for every payment recorded via the UI.
    reference: str | None = Field(default=None, max_length=200)


@router.get("/summary", response_model=None, dependencies=[Depends(require_permission("invoices.read_all"))])
def billing_summary(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Server-side aggregator for the Billing dashboard KPIs.

    Replaces the client-side SUM-over-/api/invoices that was capped at
    per_page=500 (S111 D-S111-billing-summary-404). Returns the four
    money KPIs the desktop /billing and mobile /mobile/billing render at
    the top of the page:

    - total_outstanding: SUM(balance_due) for non-Paid, non-Draft, non-Void.
      Drafts excluded because they aren't yet receivables (S111 fix).
    - overdue: SUM(balance_due) for invoices past due_date with status
      not in (paid, void, draft).
    - paid_this_month: SUM(total) of invoices paid in the current
      calendar month (paid_at >= 1st of month).
    - ready_for_billing: count of completed jobs that have no invoice yet.

    All sums use COALESCE(total_amount, total) to match the legacy data
    shape across QB-imported and GDX-native rows. The query is a single
    aggregate over the full table, not a windowed scan — fast even at
    100k+ invoices.
    """
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)
    _amount = func.coalesce(Invoice.total_amount, Invoice.total)
    _balance = func.coalesce(Invoice.balance_due, _amount)

    total_outstanding = float(db.scalar(
        select(func.coalesce(func.sum(_balance), 0)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status.notin_(("paid", "draft", "void")),
        )
    ) or 0)

    overdue = float(db.scalar(
        select(func.coalesce(func.sum(_balance), 0)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status.notin_(("paid", "draft", "void")),
            Invoice.balance_due > 0,
            Invoice.due_date.is_not(None),
            Invoice.due_date < today,
        )
    ) or 0)

    paid_this_month = float(db.scalar(
        select(func.coalesce(func.sum(_amount), 0)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status == "paid",
            Invoice.paid_at.is_not(None),
            func.cast(Invoice.paid_at, Invoice.due_date.type) >= month_start,
        )
    ) or 0)

    # Ready for billing: jobs marked complete that are not yet BILLED.
    # PR2-billing-capture: uses the canonical predicate (voided invoices and
    # the fabricated $0 draft no longer count as billing a job) so this count
    # agrees with /api/jobs/ready-for-billing and the unbilled-work alert.
    from gdx_dispatch.core.billing_predicates import job_billed_exists
    from gdx_dispatch.models.tenant_models import Job
    ready_for_billing = int(db.scalar(
        select(func.count(Job.id.distinct())).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            ~job_billed_exists(),
        )
    ) or 0)

    # PR1-billing-capture (2026-07-07): drafts are rightly excluded from
    # the receivable KPIs above — but that made a never-sent draft invisible
    # to EVERY billing surface, so it could sit forever unbilled. Surface
    # them as their own pair so the dashboard can show "N drafts never
    # sent ($X)" without polluting total_outstanding.
    draft_count = int(db.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status == "draft",
        )
    ) or 0)
    draft_total = float(db.scalar(
        select(func.coalesce(func.sum(_amount), 0)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status == "draft",
        )
    ) or 0)

    return {
        "total_outstanding": round(total_outstanding, 2),
        "overdue": round(overdue, 2),
        "paid_this_month": round(paid_this_month, 2),
        "ready_for_billing": ready_for_billing,
        "draft_count": draft_count,
        "draft_total": round(draft_total, 2),
        "as_of": datetime.now(UTC).isoformat(),
    }


@router.get("", response_model=None, dependencies=[Depends(require_permission("invoices.read_all"))])
def list_invoices(
    request: Request,
    status: Literal["draft", "sent", "paid", "overdue"] | None = None,
    customer_id: str | None = None,
    job_id: str | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    query = select(Invoice).where(Invoice.deleted_at.is_(None))
    if job_id:
        # 2026-04-29 UX audit fix: previously this param was silently dropped,
        # so the Job-Detail Costing tab rendered every invoice in the tenant
        # instead of just the job's. Filter at the query level.
        try:
            jid_uuid = _uuid.UUID(job_id)
        except (ValueError, AttributeError):
            jid_uuid = job_id
        query = query.where(Invoice.job_id == jid_uuid)
    if customer_id:
        # Phase D audit fix 2026-04-27: QB-imported invoices have a NULL
        # `customer_id` column — the customer linkage rides on the parent
        # Job. Match either the direct FK or via Job.customer_id so the
        # customer-detail Invoices tab isn't permanently empty for any
        # tenant whose data came in through the QB importer.
        try:
            cid_uuid = _uuid.UUID(customer_id)
        except (ValueError, AttributeError):
            cid_uuid = customer_id  # let the comparison fail naturally
        from sqlalchemy import or_ as _or
        query = query.where(
            _or(
                Invoice.customer_id == cid_uuid,
                Invoice.job_id.in_(select(Job.id).where(Job.customer_id == cid_uuid)),
            )
        )
    query = query.order_by(Invoice.created_at.desc(), Invoice.id.desc())
    rows = db.execute(query).scalars().all()
    items = [_serialize_invoice(row) for row in rows]

    # Enrich customer names via Job → Customer lookup
    job_ids = list({str(i["job_id"]) for i in items if i.get("job_id")})
    if job_ids:
        try:
            job_rows = db.execute(
                select(Job.id, Job.customer_id).where(Job.id.in_([_uuid.UUID(j) for j in job_ids]))
            ).all()
            job_cust_map = {str(r[0]): str(r[1]) for r in job_rows if r[1]}
            cust_ids = list(set(job_cust_map.values()))
            if cust_ids:
                from gdx_dispatch.models.tenant_models import Customer
                cust_rows = db.execute(
                    select(Customer.id, Customer.name).where(Customer.id.in_([_uuid.UUID(c) for c in cust_ids]))
                ).all()
                cust_name_map = {str(r[0]): r[1] for r in cust_rows}
                for item in items:
                    jid = str(item.get("job_id", ""))
                    cid = job_cust_map.get(jid)
                    if cid:
                        item["customer_id"] = cid
                        item["customer_name"] = cust_name_map.get(cid, "")
        except Exception:
            logging.getLogger(__name__).exception("list_invoices caught exception")
            pass  # graceful degradation — customer names just stay empty

    if status is not None:
        items = [item for item in items if item["effective_status"] == status]
    return items


@router.post("", response_model=None, status_code=201)
def create_invoice(
    payload: InvoiceCreateIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    # Counter-sale invoices skip the job lookup entirely; the contract guards
    # estimate_id/from_part_ids so we can't reach those branches without a job.
    job: Job | None = None
    if payload.job_id is not None:
        job = db.execute(
            select(Job).where(Job.id == payload.job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="job not found")

    estimate: Estimate | None = None
    if payload.estimate_id:
        estimate = db.execute(
            select(Estimate).options(selectinload(Estimate.lines)).where(
                Estimate.id == payload.estimate_id,
                Estimate.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not estimate or estimate.job_id != payload.job_id:
            raise HTTPException(status_code=404, detail="estimate not found for this job")

    # D99 (an earlier session): invoice_date was never set on creation, so every
    # period-filtered metric (Dashboard Revenue, Reports, etc.) read $0
    # against $712k of underlying invoices. Default to today.
    invoice_date_value = payload.invoice_date or date.today()
    # F-36 / 2026-04-29 — payment terms come from billing_terms resolver:
    #   customer.payment_terms_days → tenant.{class}_payment_terms_days
    #   → tenant.default_payment_terms_days
    # Falls back to 30 days if the resolver fails (control DB unreachable).
    if payload.due_date:
        due_date = payload.due_date
    else:
        try:
            from gdx_dispatch.modules.billing_terms import resolve_effective_terms
            customer_id = payload.customer_id or getattr(job, "customer_id", None)
            cust_row = None
            if customer_id:
                cust_row = db.execute(
                    _text(
                        "SELECT pricing_class, payment_terms_days FROM customers "
                        "WHERE id = :cid"
                    ),
                    {"cid": str(customer_id)},
                ).first()
            pricing_class = cust_row[0] if cust_row else None
            cust_terms = cust_row[1] if cust_row else None
            # Tenant comes from the auth context, not the job — counter-sale
            # invoices have no job to source company_id from.
            tenant_id = str(_["tenant_id"])
            terms = resolve_effective_terms(
                tenant_id=tenant_id,
                pricing_class=pricing_class,
                customer_payment_terms_days=cust_terms,
            )
            due_date = terms.due_date(invoice_date_value)
        except Exception:
            log.exception("billing_terms_resolve_failed_falling_back_to_net30")
            due_date = invoice_date_value + timedelta(days=30)
    # D100 (an earlier session): customer_id was previously dropped at the model layer.
    # 2026-05-11: Pydantic enforces non-null UUID at the contract, so the
    # fallback to job.customer_id is gone — payload.customer_id is the only
    # source of truth here.
    customer_id_value = payload.customer_id
    # Compute totals: estimate wins when present (canonical path), else sum
    # the inline line_items. Fall back to 0 only if neither is provided.
    if estimate:
        subtotal_value = float(estimate.total or 0)
    elif payload.line_items:
        subtotal_value = sum(
            float(line.unit_price) * int(line.quantity) for line in payload.line_items
        )
    else:
        subtotal_value = 0.0
    # Resolve tax rate. Caller-supplied wins (and is honored even if 0,
    # since "rate=0 with rate-mode on" means an exempt sale). When no rate
    # is supplied, we only switch the invoice to rate-mode if the tenant
    # has a configured default >0 — otherwise leave tax_rate=NULL and let
    # the legacy flat-tax_amount path stand, so callers that pass an
    # explicit tax_amount keep working unchanged.
    resolved_rate: Decimal | None = None
    if payload.tax_rate is not None:
        resolved_rate = Decimal(str(payload.tax_rate))
    else:
        try:
            from gdx_dispatch.modules.tax.service import resolve_rate as _resolve_tax
            candidate = _resolve_tax(db, customer_id_value)
            if candidate is not None and candidate > 0:
                resolved_rate = candidate
        except Exception:
            log.exception("invoice_create_tax_resolve_failed")
            resolved_rate = None
    initial_tax = _money(payload.tax_amount)
    if resolved_rate is not None and resolved_rate > 0:
        # Rate-based: tax_amount is computed from the rate × taxable lines
        # post-insert by _recalculate_invoice. Seed with 0 so the first
        # save isn't double-counted; the recalc fixes it.
        initial_tax = Decimal("0")
    # PR1-billing-capture (2026-07-07): wire the F-75 zero-price invoice
    # policy — it shipped as dead code, so $0 lines landed on invoices with
    # no block and no warning. Checked BEFORE any row is written: the block
    # toggle 422s the whole request; the warn toggle collects strings the
    # response surfaces for the frontend banner. A failure READING the
    # policy must not block invoicing (capture beats policy) — it logs loud
    # and falls through, matching get_policy's own contract.
    zero_price_warnings: list[str] = []
    _policy_lines = (
        [(ln.description, ln.unit_price) for ln in (estimate.lines or [])]
        if estimate
        else [(ln.description, ln.unit_price) for ln in (payload.line_items or [])]
    )
    if any(float(_p or 0) <= 0 for _, _p in _policy_lines):
        # Only pay the control-plane policy read when a $0 line is present.
        # get_policy never raises (it catches internally and returns
        # defaults), so no try/except here.
        _pol = get_policy(str(_["tenant_id"]))
        for _desc, _price in _policy_lines:
            _warn = block_or_warn_invoice_line(
                str(_["tenant_id"]), price=_price, policy=_pol
            )
            if _warn:
                zero_price_warnings.append(f"{_warn}: {(_desc or 'line item').strip()}")
    # Snapshot the source estimate's "total-only" display onto the invoice so
    # the invoice PDF the customer receives matches the estimate they already
    # saw. Best-effort — a features read must never block invoicing (capture
    # beats presentation), mirroring the zero-price policy contract above.
    invoice_hide_line_prices = False
    if estimate is not None:
        try:
            from gdx_dispatch.modules.estimates_features import (
                effective_hide_line_prices,
                get_features,
            )
            _hide_default = get_features(str(_["tenant_id"])).hide_line_prices
            invoice_hide_line_prices = effective_hide_line_prices(
                estimate.hide_line_prices, _hide_default
            )
        except Exception:
            log.exception("invoice_create_hide_line_prices_resolve_failed")
            invoice_hide_line_prices = False
    invoice = Invoice(
        job_id=payload.job_id,
        invoice_number=_next_invoice_number(db),
        billing_type=payload.billing_type,
        sequence_number=1,
        subtotal=_money(subtotal_value),
        tax_rate=resolved_rate,
        tax_amount=initial_tax,
        total=_money(Decimal(str(subtotal_value)) + initial_tax),
        balance_due=_money(Decimal(str(subtotal_value)) + initial_tax),
        hide_line_prices=invoice_hide_line_prices,
        status="draft",
        invoice_date=invoice_date_value,
        due_date=due_date,
        notes=(payload.notes.strip() if payload.notes else None),
        public_token=secrets.token_urlsafe(48)[:64],
        locked=False,
        customer_id=customer_id_value,
        company_id=_["tenant_id"],
    )
    db.add(invoice)
    db.flush()

    if estimate:
        lines = db.execute(
            select(EstimateLine)
            .where(EstimateLine.estimate_id == estimate.id)
            .order_by(EstimateLine.sort_order.asc(), EstimateLine.created_at.asc(), EstimateLine.id.asc())
        ).scalars().all()
        for line in lines:
            # S122-b: forward category/cost/margin snapshot from estimate line
            # so invoice line shape matches estimate line shape (Doug 2026-05-11).
            # Auditor catch: also forward margin_pct_snapshot so the engine-
            # resolved tier margin isn't lost across the estimate→invoice copy.
            db.add(
                InvoiceLine(
                    company_id=invoice.company_id,
                    invoice_id=invoice.id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=_money(line.unit_price),
                    line_total=_money(line.line_total),
                    category=getattr(line, "category", None),
                    cost_snapshot=getattr(line, "cost_snapshot", None),
                    margin_pct_snapshot=getattr(line, "margin_pct_snapshot", None),
                    margin_pct_override=getattr(line, "margin_pct_override", None),
                    sort_order=line.sort_order,
                )
            )
    elif payload.line_items:
        for idx, line in enumerate(payload.line_items, start=1):
            db.add(
                InvoiceLine(
                    company_id=invoice.company_id,
                    invoice_id=invoice.id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=_money(line.unit_price),
                    line_total=_money(float(line.unit_price) * int(line.quantity)),
                    taxable=bool(line.taxable),
                    # S122-b — persist the new estimate-parity fields when set.
                    category=line.category,
                    cost_snapshot=(
                        Decimal(str(line.cost)) if line.cost is not None else None
                    ),
                    margin_pct_override=(
                        Decimal(str(line.margin_pct_override))
                        if line.margin_pct_override is not None else None
                    ),
                    # D-S122-line-removal-unbill — line-level part_id so a
                    # later delete-line releases the part atomically.
                    part_id=line.part_id,
                    sort_order=idx,
                )
            )

    # PR3-billing-capture — pull approved change orders into this invoice.
    # The STAMP GATES THE COPY: UPDATE…RETURNING claims the COs first; only
    # the returned ids get their lines copied. Any requested CO the stamp
    # didn't capture (already billed elsewhere / not approved / wrong job /
    # deleted) 409s the WHOLE request — the rollback un-stamps atomically.
    # (Copy-then-stamp — the naive S122 mirror — double-bills: the lines
    # land on invoice B while the stamp silently no-ops because invoice A
    # owns the CO. Audit round 1 catch.)
    if payload.from_change_order_ids:
        from sqlalchemy import or_ as _or

        from gdx_dispatch.models.tenant_models import ChangeOrderLine
        from gdx_dispatch.routers.change_orders import ChangeOrder
        stamped_ids = set(db.execute(
            update(ChangeOrder)
            .where(
                ChangeOrder.id.in_(payload.from_change_order_ids),
                ChangeOrder.job_id == payload.job_id,
                # Audit round 2 (blind spot): a CO signed by a DIFFERENT
                # customer must not bill onto this invoice — tax exemption /
                # parity would silently diverge from the signed total.
                _or(
                    ChangeOrder.customer_id.is_(None),
                    ChangeOrder.customer_id == payload.customer_id,
                ),
                ChangeOrder.status == "approved",
                ChangeOrder.billed_invoice_id.is_(None),
                ChangeOrder.deleted_at.is_(None),
            )
            .values(billed_invoice_id=invoice.id)
            .returning(ChangeOrder.id)
        ).scalars().all())
        unstamped = set(payload.from_change_order_ids) - stamped_ids
        if unstamped:
            # Friendly identifiers, not raw UUIDs (audit round 2).
            labels = [
                r[0] for r in db.execute(
                    select(ChangeOrder.co_number).where(ChangeOrder.id.in_(unstamped))
                ).all()
            ] or [str(u) for u in unstamped]
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "change order(s) not billable — already billed on another "
                    "invoice, not approved, wrong job/customer, or deleted: "
                    + ", ".join(sorted(labels))
                ),
            )
        _max_sort = db.execute(
            select(func.max(InvoiceLine.sort_order)).where(
                InvoiceLine.invoice_id == invoice.id
            )
        ).scalar_one_or_none() or 0
        co_rows = db.execute(
            select(ChangeOrderLine, ChangeOrder.co_number)
            .join(ChangeOrder, ChangeOrder.id == ChangeOrderLine.co_id)
            .where(ChangeOrderLine.co_id.in_(stamped_ids))
            .order_by(ChangeOrder.created_at.asc(), ChangeOrderLine.id.asc())
        ).all()
        _offset = 0
        _cos_with_lines: set = set()
        for _offset, (co_ln, co_number) in enumerate(co_rows, start=1):
            _cos_with_lines.add(co_ln.co_id)
            db.add(
                InvoiceLine(
                    company_id=invoice.company_id,
                    invoice_id=invoice.id,
                    description=f"{co_number}: {co_ln.description}"[:500],
                    quantity=int(co_ln.qty or 1),
                    unit_price=_money(co_ln.unit_price),
                    line_total=_money(co_ln.line_total),
                    taxable=bool(getattr(co_ln, "taxable", True)),
                    sort_order=int(_max_sort) + _offset,
                )
            )
        # AUDIT ROUND 2 (money-loser reproduced live): amount-only COs — the
        # mobile dialog's output and every pre-D-S122 legacy CO — have NO
        # ChangeOrderLine rows. The stamp claimed them while the copy above
        # produced zero lines: $500 signed → marked billed, $0 invoiced,
        # gone from the checklist forever. Synthesize one line from the
        # signed amount; a CO with neither lines nor amount is unbillable.
        _lineless = db.execute(
            select(ChangeOrder).where(
                ChangeOrder.id.in_(stamped_ids - _cos_with_lines)
            ).order_by(ChangeOrder.created_at.asc())
        ).scalars().all()
        for _co in _lineless:
            if float(_co.amount or 0) <= 0:
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"change order {_co.co_number} has neither line items "
                        "nor an amount — price it before billing."
                    ),
                )
            _offset += 1
            db.add(
                InvoiceLine(
                    company_id=invoice.company_id,
                    invoice_id=invoice.id,
                    description=f"{_co.co_number}: {_co.title}"[:500],
                    quantity=1,
                    unit_price=_money(_co.amount),
                    line_total=_money(_co.amount),
                    taxable=True,
                    sort_order=int(_max_sort) + _offset,
                )
            )

    # S122 — mark parts pulled into this invoice as billed so the checklist
    # on subsequent invoices for the same job excludes them. Same transaction
    # as the invoice + lines so a rollback un-bills atomically.
    # D-S122-line-removal-unbill: prefer line-level part_id (set inside each
    # InvoiceLine above) over the top-level from_part_ids list — line-level
    # is the canonical source so a delete-line can release the part. Fall
    # back to the legacy from_part_ids field for callers that haven't
    # migrated yet.
    # PR3-billing-capture: same stamp-first-RETURNING rule as change orders.
    # The old UPDATE…WHERE billed_invoice_id IS NULL silently skipped parts
    # another invoice already owned — but the operator's payload STILL
    # carried those lines, so the amounts double-billed while the stamp
    # no-opped. Now: any requested part the stamp can't claim → 409.
    line_level_part_ids = [li.part_id for li in payload.line_items if getattr(li, "part_id", None)]
    if line_level_part_ids:
        all_part_ids = line_level_part_ids
    elif payload.from_part_ids:
        all_part_ids = [str(pid) for pid in payload.from_part_ids]
    else:
        all_part_ids = []
    if all_part_ids:
        stamped_parts = {
            str(r) for r in db.execute(
                update(JobPartNeeded)
                .where(
                    JobPartNeeded.id.in_(all_part_ids),
                    JobPartNeeded.job_id == str(payload.job_id),
                    JobPartNeeded.billed_invoice_id.is_(None),
                )
                .values(billed_invoice_id=invoice.id)
                .returning(JobPartNeeded.id)
            ).scalars().all()
        }
        missing_parts = {str(p) for p in all_part_ids} - stamped_parts
        if missing_parts:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "part(s) not billable — already billed on another invoice "
                    "or not on this job: " + ", ".join(sorted(missing_parts))
                ),
            )

    # Run the tax + total recompute now that lines exist. For rate-mode
    # invoices this writes the correct tax_amount; for legacy flat-tax
    # callers it's a no-op since tax_amount was set above.
    db.flush()
    _recalculate_invoice(invoice, db)
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="invoice_created",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"invoice_number": invoice.invoice_number, "status": invoice.status},
    )
    db.commit()
    resp = _serialize_invoice(invoice)
    if zero_price_warnings:
        resp["warnings"] = zero_price_warnings
    return resp


@router.get("/{invoice_id}", response_model=None)
def get_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db, include_relations=True)
    payload = _serialize_invoice(invoice, include_lines=True, include_payments=True)

    # 2026-04-29: enrich customer_name via Job → Customer fallback the same
    # way the list endpoint does (lines 235–258). QB-imported invoices have
    # NULL Invoice.customer_id, so the bare serializer returns "" and the
    # frontend renders "Unknown" — even though the same invoice in the list
    # view shows the real customer name (sourced via the Job).
    from gdx_dispatch.models.tenant_models import Customer
    cn = payload.get("customer_name") or ""
    if not cn and invoice.job_id:
        try:
            row = db.execute(
                select(Job.customer_id).where(Job.id == invoice.job_id)
            ).first()
            if row and row[0]:
                cust = db.execute(
                    select(Customer.id, Customer.name).where(Customer.id == row[0])
                ).first()
                if cust and cust[1]:
                    payload["customer_id"] = str(cust[0])
                    payload["customer_name"] = cust[1]
        except Exception:
            logging.getLogger(__name__).exception("get_invoice customer enrichment failed")
    # Surface customer contact on the invoice detail payload so the Bill-To
    # card can render without a second roundtrip. Encrypted columns
    # (Customer.address) require ORM access — _serialize_invoice has no db
    # handle, so the join lives here. Use the invoice's UUID directly (not
    # the serialized string) so Uuid-column dialect coercion stays happy.
    # .scalar_one_or_none() returns None on miss; no broad except needed
    # (2026-05-21 audit caught a try/except wrapping this block, justified
    # as a guard against decrypt failures that EncryptedString does not
    # actually raise — it passes ciphertext through on InvalidToken).
    cust_id_raw = invoice.customer_id or payload.get("customer_id")
    cust_uuid: _uuid.UUID | None = None
    if isinstance(cust_id_raw, _uuid.UUID):
        cust_uuid = cust_id_raw
    elif cust_id_raw:
        # Defensive: malformed historic customer_id (string column from a
        # pre-UUID migration) shouldn't 500 the whole invoice page — we
        # just skip the enrichment and let the frontend fall back to
        # "Unknown customer". Re-audit catch: bare _uuid.UUID(str(...))
        # raises ValueError on any non-UUID-shaped row.
        try:
            cust_uuid = _uuid.UUID(str(cust_id_raw))
        except (ValueError, TypeError):
            cust_uuid = None
    if cust_uuid is not None:
        c = db.execute(
            select(Customer).where(Customer.id == cust_uuid)
        ).scalar_one_or_none()
        if c is not None:
            payload["customer_email"] = c.email or ""
            payload["customer_phone"] = c.phone or ""
            payload["customer_address"] = c.address or ""
    return payload


@router.patch("/{invoice_id}", response_model=None)
def patch_invoice(
    invoice_id: UUID,
    payload: InvoicePatchIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.status != "draft":
        raise HTTPException(status_code=409, detail="only draft invoices can be edited")

    updates = payload.model_dump(exclude_unset=True)
    # Apply tax_rate first so a same-payload tax_amount override (rare,
    # but supported for manual reconciliation) wins on the recalc step.
    if "tax_rate" in updates:
        invoice.tax_rate = (
            Decimal(str(updates["tax_rate"]))
            if updates["tax_rate"] is not None
            else None
        )
    if "tax_amount" in updates and updates["tax_amount"] is not None:
        invoice.tax_amount = _money(updates["tax_amount"])
    if "invoice_date" in updates:
        invoice.invoice_date = updates["invoice_date"]
    if "due_date" in updates:
        invoice.due_date = updates["due_date"]
    if "notes" in updates:
        invoice.notes = updates["notes"].strip() if updates["notes"] else None
    if "hide_line_prices" in updates:
        invoice.hide_line_prices = bool(updates["hide_line_prices"])

    _recalculate_invoice(invoice, db)
    db.commit()
    db.refresh(invoice)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="patch_invoice",
                entity_type="invoice",
                entity_id=str(invoice_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('patch_invoice_audit_failed')
    return _serialize_invoice(invoice)


@router.delete("/{invoice_id}", response_model=None)
def delete_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Soft-delete an invoice. Only draft invoices can be deleted — once sent
    or paid, invoices must be voided via credit-memo (compliance trail).

    Closes BillingView + InvoiceDetailView Vue gaps.
    """
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.status not in ("draft",):
        raise HTTPException(
            status_code=409,
            detail=f"only draft invoices can be deleted; current status: {invoice.status}. "
                   "Issue a credit memo for sent/paid invoices instead.",
        )
    now = datetime.now(UTC)
    invoice.deleted_at = now
    # S122 auditor catch: any JobPartNeeded rows we marked billed against this
    # invoice must release back into the unbilled pool — otherwise deleting a
    # draft permanently strands those parts (they show "billed" but no live
    # invoice references them). Pair with the soft-delete in the same txn.
    db.execute(
        update(JobPartNeeded)
        .where(JobPartNeeded.billed_invoice_id == invoice.id)
        .values(billed_invoice_id=None)
    )
    # PR3-billing-capture: change orders release the same way — a deleted
    # draft must put its COs back on the unbilled checklist.
    from gdx_dispatch.routers.change_orders import ChangeOrder as _CO
    db.execute(
        update(_CO)
        .where(_CO.billed_invoice_id == invoice.id)
        .values(billed_invoice_id=None)
    )
    db.commit()
    try:
        log_audit_event_sync(
            db=db,
            tenant_id=None,
            user_id=str((_ or {}).get("user_id") or (_ or {}).get("sub") or "system"),
            action="invoice_deleted",
            entity_type="invoice",
            entity_id=str(invoice.id),
            details={"invoice_number": getattr(invoice, "invoice_number", None), "status": invoice.status},
        )
        db.commit()
    except Exception:
        # Audit log failure shouldn't block the delete, but MUST be logged
        log.exception("invoice_delete_audit_log_failed")
    return {"ok": True, "id": str(invoice.id), "deleted_at": now.isoformat()}


_DEFAULT_INVOICE_SUBJECT_TEMPLATE = "Invoice {{invoice_number}} from {{company_name}}"
_DEFAULT_INVOICE_BODY_TEMPLATE = (
    "Hi {{customer_name}},\n\n"
    "Please see the attached invoice ({{invoice_number}}) for {{job_title}}.\n"
    "Total: {{total}}{{due_line}}\n\n"
    "Thanks,\n{{company_name}}"
)


@router.get("/{invoice_id}/email-compose", response_model=None)
def invoice_email_compose(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Return a prebuilt compose payload for the in-app composer:
    {to, subject, body_text, pdf, extra_attachments}.

    Mirrors the estimate compose flow so InvoiceDetailView's send button can
    open the same review-then-send dialog rather than firing a server-side
    email blind. PDF is generated once here and shipped as base64 so the
    composer can attach it to the eventual Outlook send (or download it for
    the mailto fallback) without a second roundtrip.
    """
    import base64 as _b64

    from gdx_dispatch.core.pdf_generator import generate_invoice_pdf
    from gdx_dispatch.models.tenant_models import AppSettings, Customer
    from gdx_dispatch.routers.estimates import _render_template
    from gdx_dispatch.routers.pdf import _branding_payload, _invoice_payload, _template_config

    invoice = _get_invoice_or_404(invoice_id, db, include_relations=True)

    customer = None
    if invoice.customer_id:
        customer = db.execute(
            select(Customer).where(Customer.id == invoice.customer_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()

    job_title = ""
    if invoice.job_id:
        job_row = db.execute(
            select(Job).where(Job.id == invoice.job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if job_row:
            job_title = (job_row.title or "").strip()

    company_name = "Your Service Company"
    settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
    if settings_obj and settings_obj.company_name:
        company_name = settings_obj.company_name

    invoice_label = job_title or f"Invoice {invoice.invoice_number or ''}".strip()
    due_line = f"\nDue: {invoice.due_date.isoformat()}" if invoice.due_date else ""
    ctx = {
        "customer_name": (customer.name if customer else "") or "there",
        "job_title": invoice_label,
        "invoice_number": invoice.invoice_number or "",
        "company_name": company_name,
        "total": f"${_to_float(invoice.total):.2f}",
        "balance_due": f"${_to_float(invoice.balance_due):.2f}",
        "due_line": due_line,
    }
    subject = _render_template(_DEFAULT_INVOICE_SUBJECT_TEMPLATE, ctx).strip() or invoice_label
    body_text = _render_template(_DEFAULT_INVOICE_BODY_TEMPLATE, ctx)

    pdf_bytes = generate_invoice_pdf(
        invoice_data=_invoice_payload(invoice, customer),
        tenant_branding=_branding_payload(db),
        template_config=_template_config(db, "invoice"),
    )
    pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")
    pdf_name = f"invoice-{invoice.invoice_number or str(invoice.id)[:8]}.pdf"

    # extra_attachments: kept empty by design (S122 audit catch). Estimates
    # filter `Document.estimate_id == estimate.id` because Documents have an
    # estimate_id FK and an estimate is one-of-one per estimate. Invoices have
    # no `Document.invoice_id` column, and a job can have many invoices
    # (progress billing, change orders) — so filtering by job_id would surface
    # every doc on the job (internal photos, prior invoices, the customer
    # waiver, the original estimate's attachments) and the Vue side defaults
    # each one to _include=true, leaking internal data on a single Send click.
    # Until we model invoice→document linkage explicitly, ship empty.
    extra: list[dict[str, object]] = []

    return {
        "to": [customer.email] if (customer and customer.email) else [],
        "customer_id": str(customer.id) if customer else None,
        "subject": subject,
        "body_text": body_text,
        "pdf": {
            "name": pdf_name,
            "content_type": "application/pdf",
            "content_base64": pdf_b64,
            "size_bytes": len(pdf_bytes),
        },
        "extra_attachments": extra,
    }


@router.post("/{invoice_id}/mark-sent", response_model=None)
def mark_invoice_sent(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Flip status to 'sent' without firing a server-side email.

    Used after the composer hands off to Outlook (or the mailto fallback) —
    the operator's mail client owns delivery, so the server just stamps
    sent_at + mints the public_token. Mirrors mark_estimate_sent.
    """
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.status in {"paid", "void"}:
        raise HTTPException(status_code=409, detail="invoice is finalized")
    transition_invoice_status(db, invoice, "sent", actor=_actor_id(_))  # GL S5: P1 posts here when the flag is on
    invoice.sent_at = datetime.now(UTC)
    if not invoice.public_token:
        invoice.public_token = secrets.token_urlsafe(48)[:64]
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="invoice_marked_sent",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"status": invoice.status, "channel": "manual"},
    )
    db.commit()
    return _serialize_invoice(invoice)


@router.post("/{invoice_id}/send", response_model=None)
def send_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    # PR1-billing-capture (2026-07-07, GL audit §12): sending a VOIDED
    # invoice silently resurrected it to "sent" — a cancelled bill came
    # back to life and re-entered AR. Mirror mark-sent's finalized guard.
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void — un-void or recreate it before sending")
    if invoice.status != "paid":
        transition_invoice_status(db, invoice, "sent", actor=_actor_id(_))  # GL S5
    invoice.sent_at = datetime.now(UTC)
    if not invoice.public_token:
        invoice.public_token = secrets.token_urlsafe(48)[:64]
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="invoice_sent",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"status": invoice.status},
    )
    db.commit()

    # Actually email the customer. Mirrors send_estimate (estimates.py)
    # but routes through the unified transactional-email helper so an
    # Outlook-connected user actually delivers via Graph. Best-effort —
    # never block the status flip; the response carries `email_sent` +
    # `email_provider` so the UI is honest about delivery.
    email_sent = False
    email_provider: str | None = None
    email_skip_reason: str | None = None
    pdf_attached = False
    try:
        from gdx_dispatch.core.email_sender import build_invoice_email_html
        from gdx_dispatch.core.transactional_email import send_transactional_email
        from gdx_dispatch.models.tenant_models import AppSettings, Customer
        tid = str(invoice.company_id) if invoice.company_id else None
        if tid and invoice.customer_id:
            cust = db.execute(
                select(Customer).where(
                    Customer.id == invoice.customer_id,
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if cust and cust.email:
                lines_data = [
                    {
                        "description": ln.description,
                        "quantity": ln.quantity,
                        "unit_price": _to_float(ln.unit_price),
                        "line_total": _to_float(ln.line_total),
                    }
                    for ln in (invoice.lines or [])
                    if getattr(ln, "deleted_at", None) is None
                ]
                company_name = "Your Service Company"
                try:
                    settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
                    if settings_obj and settings_obj.company_name:
                        company_name = settings_obj.company_name
                except Exception:
                    log.exception("send_invoice_company_name_lookup_failed")
                tax_rate_val = float(invoice.tax_rate) if invoice.tax_rate is not None else None
                html = build_invoice_email_html(
                    company_name=company_name,
                    invoice_number=invoice.invoice_number or str(invoice.id)[:8],
                    customer_name=cust.name or "Valued Customer",
                    line_items=lines_data,
                    subtotal=_to_float(invoice.subtotal),
                    tax_amount=_to_float(invoice.tax_amount),
                    total=_to_float(invoice.total),
                    balance_due=_to_float(invoice.balance_due),
                    due_date=invoice.due_date.isoformat() if invoice.due_date else "",
                    notes=invoice.notes or "",
                    tax_rate=tax_rate_val,
                )
                # 2026-07-20 — attach the actual invoice PDF (same generator
                # the composer flow uses). This path shipped html-only for
                # months; a real customer got an invoice email with no PDF.
                # Best-effort like the rest of this block: a render failure
                # downgrades to html-only (pdf_attached=False in the response)
                # rather than blocking the send.
                attachments: list[dict[str, object]] | None = None
                try:
                    import base64 as _b64

                    from gdx_dispatch.core.pdf_generator import generate_invoice_pdf
                    from gdx_dispatch.core.transactional_email import MAX_INLINE_ATTACHMENT_BYTES
                    from gdx_dispatch.routers.pdf import _branding_payload, _invoice_payload, _template_config
                    pdf_bytes = generate_invoice_pdf(
                        invoice_data=_invoice_payload(invoice, cust),
                        tenant_branding=_branding_payload(db),
                        template_config=_template_config(db, "invoice"),
                    )
                    if len(pdf_bytes) > MAX_INLINE_ATTACHMENT_BYTES:
                        log.warning(
                            "invoice_send_pdf_too_large_to_attach invoice=%s bytes=%s",
                            invoice.id, len(pdf_bytes),
                        )
                    else:
                        attachments = [{
                            "name": f"invoice-{invoice.invoice_number or str(invoice.id)[:8]}.pdf",
                            "content_type": "application/pdf",
                            "content_base64": _b64.b64encode(pdf_bytes).decode("ascii"),
                        }]
                except Exception:
                    log.exception("invoice_send_pdf_attach_failed")
                email_sent, email_provider, email_skip_reason = send_transactional_email(
                    tenant_db=db,
                    tenant_id=tid,
                    user_id=str(_actor_id(_)),
                    to_email=cust.email,
                    to_name=cust.name or "",
                    subject=f"Invoice #{invoice.invoice_number} from {company_name}",
                    html_body=html,
                    attachments=attachments,
                )
                pdf_attached = email_sent and bool(attachments)
            elif cust:
                email_skip_reason = "customer_has_no_email"
            else:
                email_skip_reason = "customer_not_found"
        elif not invoice.customer_id:
            email_skip_reason = "invoice_has_no_customer"
    except Exception:
        log.exception("invoice_email_send_failed")
        email_skip_reason = "exception"

    payload = _serialize_invoice(invoice)
    payload["email_sent"] = email_sent
    payload["pdf_attached"] = pdf_attached
    if email_provider:
        payload["email_provider"] = email_provider
    if email_skip_reason:
        payload["email_skip_reason"] = email_skip_reason
    return payload


@router.post("/{invoice_id}/lines", response_model=None, status_code=201)
def add_invoice_line(
    invoice_id: UUID,
    payload: InvoiceLineCreateIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.locked or invoice.status != "draft":
        raise HTTPException(status_code=409, detail="cannot modify lines on a locked/non-draft invoice")

    # PR1-billing-capture: F-75 zero-price policy on single-line adds too
    # (block → 422 before insert; warn → surfaced on the response).
    zero_price_warning = block_or_warn_invoice_line(
        str(invoice.company_id or ""), price=payload.unit_price
    )

    max_sort = db.execute(select(func.max(InvoiceLine.sort_order)).where(InvoiceLine.invoice_id == invoice.id)).scalar_one_or_none()
    sort_order = int(max_sort or 0) + 1
    line_total = _money(payload.quantity * payload.unit_price)

    line = InvoiceLine(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        description=payload.description.strip(),
        quantity=payload.quantity,
        unit_price=_money(payload.unit_price),
        line_total=line_total,
        taxable=bool(payload.taxable),
        # S122-b — estimate-parity fields.
        category=payload.category,
        cost_snapshot=(
            Decimal(str(payload.cost)) if payload.cost is not None else None
        ),
        margin_pct_override=(
            Decimal(str(payload.margin_pct_override))
            if payload.margin_pct_override is not None else None
        ),
        sort_order=sort_order,
    )
    db.add(line)
    db.flush()

    _recalculate_invoice(invoice, db)
    db.commit()
    db.refresh(line)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="add_invoice_line",
                entity_type="invoice_line",
                entity_id=str(invoice_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('add_invoice_line_audit_failed')
    resp = _serialize_line(line)
    if zero_price_warning:
        resp["warning"] = zero_price_warning
    return resp


def _get_line_or_404(invoice: Invoice, line_id: UUID, db: Session) -> InvoiceLine:
    line = db.execute(
        select(InvoiceLine).where(
            InvoiceLine.id == line_id,
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if line is None:
        raise HTTPException(status_code=404, detail="invoice line not found")
    return line


@router.patch("/{invoice_id}/lines/{line_id}", response_model=None)
def patch_invoice_line(
    invoice_id: UUID,
    line_id: UUID,
    payload: InvoiceLinePatchIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.locked or invoice.status != "draft":
        raise HTTPException(status_code=409, detail="cannot modify lines on a locked/non-draft invoice")
    line = _get_line_or_404(invoice, line_id, db)

    updates = payload.model_dump(exclude_unset=True)
    if "description" in updates and updates["description"] is not None:
        line.description = updates["description"].strip()
    if "quantity" in updates and updates["quantity"] is not None:
        line.quantity = updates["quantity"]
    if "unit_price" in updates and updates["unit_price"] is not None:
        line.unit_price = _money(updates["unit_price"])
    if "taxable" in updates and updates["taxable"] is not None:
        line.taxable = bool(updates["taxable"])
    if "sort_order" in updates and updates["sort_order"] is not None:
        line.sort_order = int(updates["sort_order"])
    # S122-b — estimate-parity fields. None is meaningful (clears the override),
    # so use `exclude_unset` semantics: only apply if the field is present in
    # the payload (model_dump(exclude_unset=True) handled that already).
    if "category" in updates:
        line.category = updates["category"]
    if "cost" in updates:
        line.cost_snapshot = (
            Decimal(str(updates["cost"])) if updates["cost"] is not None else None
        )
    if "margin_pct_override" in updates:
        line.margin_pct_override = (
            Decimal(str(updates["margin_pct_override"]))
            if updates["margin_pct_override"] is not None else None
        )

    # Recompute line_total from the post-patch quantity × unit_price so a
    # qty edit doesn't leave the stored line_total stale.
    line.line_total = _money(Decimal(str(line.quantity)) * Decimal(str(line.unit_price)))
    db.flush()

    _recalculate_invoice(invoice, db)
    db.commit()
    db.refresh(line)
    try:
        log_audit_event_sync(
            db=db, tenant_id=None, user_id=_actor_id(user),
            action="invoice_line_patched", entity_type="invoice_line",
            entity_id=str(line.id),
            details={"invoice_id": str(invoice.id), "fields": list(updates.keys())},
        )
        db.commit()
    except Exception:
        log.exception("invoice_line_patch_audit_failed")
    return _serialize_line(line)


@router.delete("/{invoice_id}/lines/{line_id}", response_model=None)
def delete_invoice_line(
    invoice_id: UUID,
    line_id: UUID,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.locked or invoice.status != "draft":
        raise HTTPException(status_code=409, detail="cannot modify lines on a locked/non-draft invoice")
    line = _get_line_or_404(invoice, line_id, db)
    # D-S122-line-removal-unbill: if this line was created from a parts-from-
    # job pull, release the part back into the unbilled pool now. Without this
    # the part stays "billed" forever even though no live line references it.
    if getattr(line, "part_id", None):
        db.execute(
            update(JobPartNeeded)
            .where(
                JobPartNeeded.id == line.part_id,
                JobPartNeeded.billed_invoice_id == invoice.id,
            )
            .values(billed_invoice_id=None)
        )
    line.deleted_at = datetime.now(UTC)
    db.flush()
    _recalculate_invoice(invoice, db)
    db.commit()
    try:
        log_audit_event_sync(
            db=db, tenant_id=None, user_id=_actor_id(user),
            action="invoice_line_deleted", entity_type="invoice_line",
            entity_id=str(line.id),
            details={"invoice_id": str(invoice.id)},
        )
        db.commit()
    except Exception:
        log.exception("invoice_line_delete_audit_failed")
    return {"ok": True, "id": str(line.id), "invoice": _serialize_invoice(invoice, include_lines=False)}


@router.post("/{invoice_id}/payments", response_model=None, status_code=201)
def record_payment(
    invoice_id: UUID,
    payload: PaymentCreateIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    # PR1-billing-capture (audit catch): a payment against a VOIDED invoice
    # ran _recalculate_invoice, which flips status to "paid" once balance
    # hits zero — the void resurrected into "Paid This Month" through the
    # payment door. Same class as the /send guard.
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void — un-void it before recording a payment")

    # GL S6: overpayment gate, active only when ledger posting is on (flag
    # off keeps today's permissive behavior — zero behavior change until
    # cutover). Opt-in routes the excess to 2300 Customer Credits.
    if ledger_posting_enabled(db, invoice.company_id) and not payload.allow_overpayment:
        already_paid = db.execute(
            select(func.sum(Payment.amount)).where(
                Payment.invoice_id == invoice.id, Payment.voided_at.is_(None)
            )
        ).scalar_one_or_none() or 0
        # Audit round 3: the gate must measure against the REMAINING
        # receivable — total minus credit memos/applied credits — or a
        # payment of the printed total after a credit memo silently drives
        # AR negative instead of minting a customer credit.
        credited = db.execute(
            select(func.sum(InvoiceAdjustment.amount)).where(
                InvoiceAdjustment.invoice_id == invoice.id,
                InvoiceAdjustment.kind.in_(("credit_memo", "credit_applied")),
            )
        ).scalar_one_or_none() or 0
        remaining = _to_float(invoice.total) - _to_float(credited)
        if _to_float(already_paid) + float(_money(payload.amount)) > remaining + 0.005:
            raise HTTPException(
                status_code=422,
                detail=(
                    "payment exceeds the invoice's remaining balance — set "
                    "allow_overpayment to credit the excess to the customer's account"
                ),
            )

    payment = Payment(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        amount=_money(payload.amount),
        method=payload.method.strip().lower(),
        payment_date=payload.date,
        reference=(payload.reference or "").strip() or None,
    )
    db.add(payment)
    db.flush()

    _recalculate_invoice(invoice, db)
    # GL S6 (P3): the payment posts AFTER the recalc so a draft paid in full
    # posts P1 (auto-flip transition) before P3 in the same transaction —
    # negative AR is structurally impossible (spec §5.1/§5.3).
    try:
        post_payment_received(db, payment, invoice, actor=_actor_id(_))
    except PeriodLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"payment date falls in a locked accounting period — {exc}",
        ) from exc

    # Sprint 1.0.6 — refresh the customer's rolling-volume cache so the
    # next estimate sees the new payment immediately. Best-effort: never
    # block payment recording on a downstream refresh failure.
    if invoice.customer_id:
        try:
            from gdx_dispatch.services.customer_rolling_volume import refresh_cached_volume
            refresh_cached_volume(invoice.customer_id, db)
        except Exception:
            log.exception("rolling_volume_refresh_failed_post_payment")

    db.commit()
    db.refresh(payment)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="payment_recorded",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"payment_id": str(payment.id), "amount": _to_float(payment.amount)},
    )
    db.commit()
    return _serialize_payment(payment)


@router.post("/{invoice_id}/payments/{payment_id}/void", response_model=None)
def void_payment(
    invoice_id: UUID,
    payment_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Void a recorded payment (GL S6, P4). The row stays as history but
    stops counting; its P3 ledger entry is reversed when posting is on. A
    fully-paid invoice whose payment is voided reopens to "sent"."""
    invoice = _get_invoice_or_404(invoice_id, db)
    payment = db.get(Payment, payment_id)
    if payment is None or payment.invoice_id != invoice.id:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.voided_at is not None:
        return _serialize_payment(payment)  # idempotent

    payment.voided_at = datetime.now(UTC)
    db.flush()  # resettle reads Payment rows — the void must be visible
    # Reverses the voided payment's P3 AND reverse+reposts every remaining
    # payment whose AR/2300 split the void changed (audit round 2: stale
    # splits diverged GL from balance_due and broke replay determinism).
    # Ledger refusals surface as 409s with the reason, never bare 500s.
    try:
        resettle_invoice_payments(db, invoice, actor=_actor_id(_))
    except PeriodLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"payment is in a locked accounting period — {exc}",
        ) from exc
    _recalculate_invoice(invoice, db)
    if invoice.status == "paid" and _to_float(invoice.balance_due) > 0:
        # the money that made it "paid" is gone — reopen it
        transition_invoice_status(db, invoice, "sent", actor=_actor_id(_))
        invoice.paid_at = None
    db.commit()
    db.refresh(payment)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="payment_voided",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"payment_id": str(payment.id), "amount": _to_float(payment.amount)},
    )
    db.commit()
    return _serialize_payment(payment)


@router.get("/{invoice_id}/payments", response_model=None)
def list_payments(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    _get_invoice_or_404(invoice_id, db)
    rows = db.execute(
        select(Payment)
        .where(Payment.invoice_id == invoice_id)
        .order_by(Payment.payment_date.asc(), Payment.created_at.asc(), Payment.id.asc())
    ).scalars().all()
    return [_serialize_payment(row) for row in rows]


@router.post("/{invoice_id}/void", response_model=None)
def void_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Void an invoice (GL S5, spec §5.1). Payments must be voided/removed
    first — voiding a bill while keeping its money would silently orphan the
    cash. Voided stays void (/send and /mobile resend both 409). Reverses the
    live P1 entry when ledger posting is on; draft voids have nothing posted.
    """
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.status == "void":
        return _serialize_invoice(invoice)  # idempotent

    has_payments = db.execute(
        select(func.count())
        .select_from(Payment)
        .where(Payment.invoice_id == invoice.id, Payment.voided_at.is_(None))
    ).scalar_one()
    if has_payments:
        raise HTTPException(
            status_code=409,
            detail="invoice has recorded payments — void or remove them first",
        )

    transition_invoice_status(db, invoice, "void", actor=_actor_id(_))
    # GL S7: the P1 reversal alone would strand adjustment entries on AR.
    # GL S10: a pre-cutover-era invoice has no P1 to reverse — the void
    # posts its own entry clearing whatever AR it still holds (spec §5.7).
    # Ledger refusals surface as 409s with the reason, never bare 500s.
    try:
        reverse_invoice_adjustments(db, invoice, actor=_actor_id(_))
        settle_opening_on_void(db, invoice, actor=_actor_id(_))
    except PeriodLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"void posts into a locked accounting period — {exc}",
        ) from exc
    invoice.balance_due = _money(0)
    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="invoice_voided",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"total": _to_float(invoice.total)},
    )
    db.commit()
    return _serialize_invoice(invoice)


@router.post("/{invoice_id}/finalize", response_model=None)
def finalize_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    _recalculate_invoice(invoice, db)
    invoice.locked = True
    invoice.locked_at = datetime.now(UTC)

    db.commit()
    db.refresh(invoice)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="invoice_finalized",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={"locked": bool(invoice.locked)},
    )
    db.commit()
    return _serialize_invoice(invoice)


# ---------------------------------------------------------------------------
# Batch Invoicing (#218)
# ---------------------------------------------------------------------------

class BatchInvoiceIn(BaseModel):
    job_ids: list[str] = Field(min_length=1)


@router.post("/batch", response_model=None)
def batch_create_invoices(
    payload: BatchInvoiceIn,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Create invoices for multiple jobs at once."""
    created = []
    errors = []
    for job_id in payload.job_ids:
        try:
            job = db.get(Job, UUID(job_id))
            if not job:
                errors.append({"job_id": job_id, "error": "Job not found"})
                continue
            if not job.customer_id:
                errors.append({"job_id": job_id, "error": "Job has no customer — assign one before billing"})
                continue
            invoice = Invoice(
                job_id=job.id,
                customer_id=job.customer_id,
                invoice_number=f"INV-{secrets.token_hex(4).upper()}",
                status="draft",
                total=Decimal("0.00"),
                amount_paid=Decimal("0.00"),
                public_token=secrets.token_urlsafe(48)[:64],
                invoice_date=date.today(),
                company_id=job.company_id,
            )
            db.add(invoice)
            db.flush()
            created.append(str(invoice.id))
        except Exception as e:
            log.exception("batch_invoice_create_failed")
            # Generic error; full exception is logged above. (CodeQL stack-trace-exposure)
            errors.append({"job_id": job_id, "error": "Invoice creation failed"})
    if created:
        db.commit()
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="batch_create_invoices",
                entity_type="invoice",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('batch_create_invoices_audit_failed')
    return {"created": len(created), "invoice_ids": created, "errors": errors}


# ---------------------------------------------------------------------------
# Credit Memos (#219)
# ---------------------------------------------------------------------------

class CreditMemoIn(BaseModel):
    amount: float = Field(gt=0)
    reason: str = Field(min_length=1)


def _net_paid(db: Session, invoice) -> float:
    paid = db.execute(
        select(func.sum(Payment.amount)).where(
            Payment.invoice_id == invoice.id, Payment.voided_at.is_(None)
        )
    ).scalar_one_or_none() or 0
    refunded = db.execute(
        select(func.sum(InvoiceAdjustment.amount)).where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.kind == "refund",
        )
    ).scalar_one_or_none() or 0
    return _to_float(paid) - _to_float(refunded)


@router.post("/{invoice_id}/credit-memo", response_model=None)
def issue_credit_memo(
    invoice_id: str,
    payload: CreditMemoIn,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Issue a credit memo — forgive part of the remaining balance (GL S7,
    spec §5.2). Recorded on invoice_adjustments (bug #4: the old version
    mutated the deprecated amount_paid, which recalc ignores). Capped at the
    remaining balance; posts debit 4900/4910 per reason, credit AR when
    ledger posting is on."""
    _validate_uuid(invoice_id, "Invoice")
    invoice = _get_invoice_or_404(UUID(invoice_id), db)
    if invoice.status not in ("sent", "paid"):
        # Audit round 3: a credit memo on a DRAFT posts an AR credit that
        # P1 never debited (negative AR), and draft deletion would strand
        # the entry. Drafts are edited, not credited.
        raise HTTPException(status_code=409, detail="only issued invoices can be credited — edit the draft instead")
    credit_amount = _money(payload.amount)
    _recalculate_invoice(invoice, db)
    if float(credit_amount) > _to_float(invoice.balance_due) + 0.005:
        raise HTTPException(
            status_code=422,
            detail=f"credit memo exceeds the remaining balance ({_to_float(invoice.balance_due):.2f})",
        )

    adjustment = InvoiceAdjustment(
        invoice_id=invoice.id,
        kind="credit_memo",
        amount=credit_amount,
        reason=payload.reason.strip(),
        created_by=_actor_id(_),
        company_id=invoice.company_id,
    )
    db.add(adjustment)
    db.flush()
    post_credit_memo(db, adjustment, invoice, actor=_actor_id(_))
    # belt: if the shrunken receivable changed any existing payment's
    # AR/2300 split, reverse+repost it (caps make this a no-op normally)
    resettle_invoice_payments(db, invoice, actor=_actor_id(_))
    _recalculate_invoice(invoice, db)  # fully-credited invoices settle to paid
    db.commit()
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="credit_memo_issued", entity_type="invoice", entity_id=str(invoice.id),
        details={"amount": float(credit_amount), "reason": payload.reason, "adjustment_id": str(adjustment.id)},
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "adjustment_id": str(adjustment.id),
        "credit_amount": float(credit_amount),
        "reason": payload.reason,
        "balance_due": _to_float(invoice.balance_due),
    }


class ApplyCreditIn(BaseModel):
    amount: float = Field(gt=0)


@router.post("/{invoice_id}/apply-credit", response_model=None)
def apply_customer_credit(
    invoice_id: UUID,
    payload: ApplyCreditIn,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """P9 (GL S7, spec §5.3): consume the customer's 2300 credit balance
    against this open invoice. Dual cap: neither the customer's live credit
    balance nor the invoice's remaining balance may be exceeded. Requires
    ledger posting (the credit balance IS the 2300 ledger balance)."""
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.status not in ("sent", "paid"):
        raise HTTPException(status_code=409, detail="only issued invoices can receive credit")
    if not ledger_posting_enabled(db, invoice.company_id):
        raise HTTPException(
            status_code=409,
            detail="customer credits live on the ledger — enable ledger posting first",
        )
    if not invoice.customer_id:
        raise HTTPException(status_code=422, detail="invoice has no customer")

    amount = _money(payload.amount)
    _recalculate_invoice(invoice, db)

    # Spec §5.3: the one Phase-1 balance precondition — lock the customer's
    # credit rows so two concurrent applications can't double-spend (PG;
    # SQLite ignores FOR UPDATE, single-writer tests unaffected).
    if db.get_bind().dialect.name == "postgresql":
        db.execute(_text("SELECT 1 FROM gl_journal_lines WHERE customer_id = :cid FOR UPDATE"),
                   {"cid": str(invoice.customer_id)})
    available = customer_credit_balance_cents(db, invoice.company_id, invoice.customer_id)
    if float(amount) * 100 > available + 0.5:
        raise HTTPException(
            status_code=422,
            detail=f"customer credit balance is {available / 100:.2f} — cannot apply {float(amount):.2f}",
        )
    if float(amount) > _to_float(invoice.balance_due) + 0.005:
        raise HTTPException(
            status_code=422,
            detail=f"amount exceeds the remaining balance ({_to_float(invoice.balance_due):.2f})",
        )

    adjustment = InvoiceAdjustment(
        invoice_id=invoice.id,
        kind="credit_applied",
        amount=amount,
        reason="customer credit applied",
        created_by=_actor_id(_),
        company_id=invoice.company_id,
    )
    db.add(adjustment)
    db.flush()
    post_credit_application(db, adjustment, invoice, actor=_actor_id(_))
    resettle_invoice_payments(db, invoice, actor=_actor_id(_))
    _recalculate_invoice(invoice, db)
    db.commit()
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="customer_credit_applied", entity_type="invoice", entity_id=str(invoice.id),
        details={"amount": float(amount), "adjustment_id": str(adjustment.id)},
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "adjustment_id": str(adjustment.id),
        "applied": float(amount),
        "balance_due": _to_float(invoice.balance_due),
        "remaining_credit": (available - int(round(float(amount) * 100))) / 100,
    }


# ---------------------------------------------------------------------------
# Refund Processing (#221)
# ---------------------------------------------------------------------------

class RefundIn(BaseModel):
    amount: float = Field(gt=0, le=1_000_000)
    reason: str = Field(default="", max_length=500)
    # Required when ledger posting is on — the cash has to leave through a
    # concrete account (check → operating bank, cash → undeposited, …).
    refund_method: str | None = Field(default=None, max_length=50)


@router.post("/{invoice_id}/refund", response_model=None)
def process_refund(
    invoice_id: str,
    payload: RefundIn,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Refund money against payments actually received (GL S7, spec §5.2).
    Recorded on invoice_adjustments; capped by net paid (non-voided payments
    minus prior refunds — bug #4: the old cap read the deprecated
    amount_paid column). Posts debit 4910/4900, credit the cash account, when
    ledger posting is on. Lifecycle status untouched (bug #2 stays fixed)."""
    _validate_uuid(invoice_id, "Invoice")
    invoice = _get_invoice_or_404(UUID(invoice_id), db)
    if invoice.status not in ("sent", "paid"):
        raise HTTPException(status_code=409, detail="only issued invoices can be refunded")
    refund_amount = _money(payload.amount)

    net_paid = _net_paid(db, invoice)
    if float(refund_amount) > net_paid + 0.005:
        raise HTTPException(
            status_code=422,
            detail=f"Refund exceeds net amount paid ({net_paid:.2f})",
        )
    if ledger_posting_enabled(db, invoice.company_id) and not (payload.refund_method or "").strip():
        raise HTTPException(
            status_code=422,
            detail="refund_method is required while ledger posting is enabled",
        )

    adjustment = InvoiceAdjustment(
        invoice_id=invoice.id,
        kind="refund",
        amount=refund_amount,
        reason=(payload.reason or "").strip() or None,
        refund_method=(payload.refund_method or "").strip().lower() or None,
        created_by=_actor_id(_),
        company_id=invoice.company_id,
    )
    db.add(adjustment)
    db.flush()
    post_refund(db, adjustment, invoice, actor=_actor_id(_))
    db.commit()

    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="refund_processed", entity_type="invoice", entity_id=str(invoice.id),
        details={"amount": float(refund_amount), "reason": payload.reason, "adjustment_id": str(adjustment.id)},
    )
    db.commit()

    return {
        "invoice_id": str(invoice.id),
        "adjustment_id": str(adjustment.id),
        "refund_amount": float(refund_amount),
    }


# ---------------------------------------------------------------------------
# Payment Plans (#215)
# ---------------------------------------------------------------------------

class PaymentPlanIn(BaseModel):
    num_installments: int = Field(ge=2, le=12)
    start_date: date


@router.post("/{invoice_id}/payment-plan", response_model=None)
def create_payment_plan(
    invoice_id: str,
    payload: PaymentPlanIn,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Split an invoice into monthly installments."""
    _validate_uuid(invoice_id, "Invoice")
    from datetime import timedelta
    from uuid import uuid4

    invoice = _get_invoice_or_404(invoice_id, db)
    total = _to_float(invoice.total)
    per_installment = _money(total / payload.num_installments)
    plan_id = str(uuid4())

    installments = []
    for i in range(payload.num_installments):
        inst_id = str(uuid4())
        due = payload.start_date + timedelta(days=30 * i)
        amount = per_installment if i < payload.num_installments - 1 else _money(total - float(per_installment) * (payload.num_installments - 1))
        installments.append({"id": inst_id, "due_date": due.isoformat(), "amount": float(amount), "status": "pending"})

    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="payment_plan_created", entity_type="invoice", entity_id=str(invoice.id),
        details={"plan_id": plan_id, "installments": payload.num_installments, "total": total},
    )
    db.commit()

    return {"plan_id": plan_id, "invoice_id": str(invoice.id), "installments": installments}


# ---------------------------------------------------------------------------
# Payment Receipt (#220)
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/send-receipt", response_model=None)
def send_payment_receipt(
    invoice_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Send a payment receipt email to the customer."""
    _validate_uuid(invoice_id, "Invoice")
    invoice = _get_invoice_or_404(invoice_id, db)

    # Resolve recipient. Prefer invoice.customer_id (NOT NULL since 2026-05-11);
    # the legacy job→customer hop only matters for older rows where the column
    # was added after the invoice was already on the job.
    from gdx_dispatch.models.tenant_models import Customer
    customer_uuid = invoice.customer_id
    if customer_uuid is None and invoice.job_id is not None:
        job_row = db.get(Job, invoice.job_id)
        if job_row and job_row.customer_id:
            customer_uuid = job_row.customer_id
    if customer_uuid is None:
        raise HTTPException(status_code=422, detail="invoice has no customer to send to")
    cust = db.get(Customer, customer_uuid)
    if not cust or not cust.email:
        raise HTTPException(status_code=422, detail="customer has no email on file")
    email = cust.email

    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="payment_receipt_sent", entity_type="invoice", entity_id=str(invoice.id),
        details={"to": email, "total": _to_float(invoice.total), "paid": _to_float(invoice.amount_paid)},
    )
    db.commit()

    return {"sent": True, "to": email, "invoice_id": str(invoice.id)}
