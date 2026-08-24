import datetime as _datetime
import json as _json
import logging
import secrets
import uuid as _uuid
from types import SimpleNamespace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.invoice_delivery import require_deliverable
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.models.tenant_models import (
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    JobPartNeeded,
    JobPhoto,
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
from gdx_dispatch.tasks.stale_intent_sweep import enqueue_stale_intent_sweep

log = logging.getLogger(__name__)

# How long two identical reference-less payments on one invoice are treated as
# the same payment.
#
# What this DOES cover: a double-tap, and a fast manual retry — the cases where
# a human fires the same payment twice in quick succession.
#
# What it does NOT cover, stated plainly so nobody reads it as solved: the
# offline queue's replay-after-lost-response. That queue has no drain timer
# (it fires on `online`, `visibilitychange`, and mount) and the client sets no
# fetch timeout, so a lost response typically rejects long after this window
# has closed. Closing that hole properly means persisting the
# `Idempotency-Key` the queue already sends on every replay — a new column and
# a partial unique index, deliberately left to its own change.
_CASHLIKE_DEDUPE_SECONDS = 120

# Marks the one 409 from record_payment that means "the money IS recorded".
# Every other 409 here means nothing was written.
DUPLICATE_PAYMENT_CODE = "duplicate_payment"

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


def _labor_price_was_overridden(line: object, item_id: object, db: Session | None) -> bool:
    """Did a human move this labor line's price off the matrix quote?

    Compare against the MATRIX ROW'S OWN PRICE, not `margin_pct_override`.
    That was the first implementation and it was wrong in a way no test caught:
    for labor lines the override column is never populated — EstimateView sends
    `cost: null` for anything with a `labor_price_item_id`, so the branch that
    would set it cannot fire, and the estimate router's explicit-price path
    keeps the id without setting one. Result: pick $650 from the matrix, type
    $900, accept, convert — and the invoice asserted the matrix quoted $900.

    Unresolvable row (archived, or no db handle) => treat as NOT overridden and
    keep the line as matrix. Guessing "a human repriced this" from missing data
    would invent provenance, which is the failure mode this whole column
    exists to prevent.
    """
    if item_id is None or db is None:
        return False
    try:
        from gdx_dispatch.models.labor_pricing import LaborPriceItem

        row = db.get(LaborPriceItem, item_id)
    except Exception:  # pragma: no cover - defensive
        log.exception("labor_provenance_matrix_lookup_failed")
        return False
    if row is None:
        return False
    quoted = Decimal(str(getattr(row, "flat_price", 0) or 0))
    actual = Decimal(str(getattr(line, "unit_price", 0) or 0))
    return abs(actual - quoted) > Decimal("0.005")


def _labor_provenance_for(line: object, db: Session | None = None) -> dict[str, object]:
    """Coherent labor provenance for a line COPIED from an estimate.

    The copy constructs InvoiceLine directly and so never runs
    InvoiceLineCreateIn's validator. Two shapes the validator rejects were
    reachable here, and both misrepresent how a line was priced:

    * `estimated_man_hours` with no `labor_source` — an hours figure nobody can
      attribute, which is the unanswerable provenance migration 071 exists to
      close.
    * `labor_source="matrix"` inferred from id-presence alone. An estimate line
      can carry a matrix id AND a human override of the price; calling that
      "matrix-quoted" credits the matrix for a number a person chose.

    A price the operator overrode is recorded as "manual" — it is still their
    number — and the matrix row it started from STAYS on the line as context.
    ("manual" with an id reads: started from row X, then repriced by a human.)
    """
    item_id = getattr(line, "labor_price_item_id", None)
    hours = getattr(line, "estimated_man_hours", None)
    overridden = _labor_price_was_overridden(line, item_id, db)

    if item_id is not None and not overridden:
        return {
            "labor_price_item_id": item_id,
            "estimated_man_hours": hours,
            "labor_source": "matrix",
        }
    if item_id is not None or hours is not None:
        # Priced by a human. Not a matrix quote — but the row it came from is
        # kept, because "manual, derived from row X" is the true statement and
        # throwing the id away would lose the very linkage this column exists
        # for.
        return {
            "labor_price_item_id": item_id,
            "estimated_man_hours": hours,
            "labor_source": "manual",
        }
    return {
        "labor_price_item_id": None,
        "estimated_man_hours": None,
        "labor_source": None,
    }


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
        # Whether this line's price already covers the install. False for
        # every pre-2026-08-19 row (server_default), so older invoices read
        # exactly as they always did.
        "includes_labor": bool(getattr(line, "includes_labor", False) or False),
        "cost_snapshot": _to_float(line.cost_snapshot) if getattr(line, "cost_snapshot", None) is not None else None,
        "margin_pct_snapshot": _to_float(line.margin_pct_snapshot) if getattr(line, "margin_pct_snapshot", None) is not None else None,
        "margin_pct_override": _to_float(line.margin_pct_override) if getattr(line, "margin_pct_override", None) is not None else None,
        # D-S122-line-removal-unbill: surface the part linkage for detail-view
        # badges + audit trail.
        "part_id": getattr(line, "part_id", None),
        # Labor provenance (migration 071) — which lane priced this line.
        # NULL on every pre-2026-08-20 row and on every non-labor line.
        "labor_price_item_id": (
            str(line.labor_price_item_id)
            if getattr(line, "labor_price_item_id", None) else None
        ),
        "estimated_man_hours": (
            _to_float(line.estimated_man_hours)
            if getattr(line, "estimated_man_hours", None) is not None else None
        ),
        "labor_source": getattr(line, "labor_source", None),
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


def _amount_overpaid(invoice: Invoice) -> float:
    """Money collected above what the invoice asks for (M11). 0 normally.

    Derived from the loaded payment/adjustment relationships rather than a
    query so the serializer stays cheap; falls back to 0 when they aren't
    loaded, which is the honest answer for a partial serialization.
    """
    try:
        payments = getattr(invoice, "payments", None) or []
        paid = sum(
            Decimal(str(p.amount or 0))
            for p in payments
            if getattr(p, "voided_at", None) is None
        )
    except Exception:  # detached / not loaded — don't break the payload
        return 0.0
    if not paid:
        return 0.0
    total = Decimal(str(invoice.total or 0))
    excess = paid - total
    return float(_money(excess)) if excess > Decimal("0.005") else 0.0


def _decode_photo_ids(invoice: Invoice) -> list[str]:
    raw = getattr(invoice, "attached_photo_ids", None)
    if not raw:
        return []
    try:
        return [str(i) for i in _json.loads(raw) if i]
    except (ValueError, TypeError):
        return []


def _serialize_invoice(
    invoice: Invoice,
    include_lines: bool = False,
    include_payments: bool = False,
    credit_total: object | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(invoice.id),
        "job_id": str(invoice.job_id) if invoice.job_id else None,
        # Deposit provenance (migration 036) — lets the detail view link back
        # to the source estimate.
        "estimate_id": str(invoice.estimate_id) if getattr(invoice, "estimate_id", None) else None,
        # Migration 072 — "the numbers started from this estimate", as opposed
        # to estimate_id's stronger "this invoice IS that estimate's bill".
        # Kept distinct because deposit netting and closeout reconciliation
        # both key on estimate_id and read it the strong way.
        "source_estimate_id": (
            str(invoice.source_estimate_id)
            if getattr(invoice, "source_estimate_id", None) else None
        ),
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
        # M11 (money audit 2026-08-04): balance_due clamps at 0, so money
        # collected ABOVE the total used to be invisible on every surface —
        # the invoice just read "paid". The GL has an overpayment gate but it
        # only runs when ledger posting is on (off in prod), so nothing
        # surfaced a double-collection. Always non-negative; 0 in the normal
        # case. This is also the detector for the duplicate-payment classes.
        "amount_overpaid": _amount_overpaid(invoice),
        "status": invoice.status,
        "effective_status": _effective_status(invoice),
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "notes": invoice.notes,
        # "Total-only" display — hides per-line prices on the invoice PDF.
        "hide_line_prices": bool(getattr(invoice, "hide_line_prices", False)),
        # Job photos picked for the PDF (migration 059) — decoded to a list
        # so the detail view can show the current selection.
        "attached_photo_ids": _decode_photo_ids(invoice),
        # Machine provenance (2026-08-08 audit): 'closeout_autodraft' was
        # stored but never serialized — the office reviewed machine-priced
        # invoices with no indication they were machine-priced.
        "origin": getattr(invoice, "origin", None),
        # PR6 — per-invoice dunning mute state for the detail-view toggle.
        "dunning_paused": bool(getattr(invoice, "dunning_paused", False)),
        "locked": bool(invoice.locked),
        "locked_at": _iso_dt(invoice.locked_at),
        "sent_at": _iso_dt(invoice.sent_at),
        # Migration 057 — HOW it was delivered ('email' | 'mail' | 'manual');
        # NULL on rows delivered before the column existed.
        "sent_via": getattr(invoice, "sent_via", None),
        # §11: office verification state — the billing screens badge it and
        # the mobile side keys "awaiting verification" off it.
        "verified_at": _iso_dt(invoice.verified_at),
        "verified_by_user_id": invoice.verified_by_user_id,
        "paid_at": _iso_dt(invoice.paid_at),
        "public_token": invoice.public_token,
        "created_at": _iso_dt(invoice.created_at),
        # Tier 10 — per-record QuickBooks push state. Written by push_invoice /
        # the before_update listener (S122-14) but serialized nowhere before, so
        # the office couldn't tell pushed / pending / never-pushed per invoice.
        # qb_synced_at is NULL until the first successful push; qb_dirty is True
        # whenever the row has un-pushed changes (default True for new rows).
        "qb_dirty": bool(getattr(invoice, "qb_dirty", True)),
        "qb_synced_at": _iso_dt(getattr(invoice, "qb_synced_at", None)),
    }
    if include_lines:
        active_lines = [ln for ln in invoice.lines if getattr(ln, "deleted_at", None) is None]
        lines = sorted(active_lines, key=lambda ln: (ln.sort_order, ln.created_at, ln.id))
        payload["lines"] = [_serialize_line(line) for line in lines]
    if include_payments:
        payments = sorted(invoice.payments, key=lambda p: (p.payment_date, p.created_at, p.id))
        payload["payments"] = [_serialize_payment(payment) for payment in payments]
        # M35 / ledger item #1: this payload never carried a paid-to-date at
        # all, so MobileBillingView's "Paid" row — gated on
        # `detail.amount_paid != null` — simply never rendered. The old fix for
        # that would have been to surface `Invoice.amount_paid`, a cache
        # nothing writes; instead derive it from the payments already loaded
        # here (no extra query, and no N+1 because list callers pass
        # include_payments=False). Voided payments stay as history but stop
        # counting, matching _recalculate_invoice.
        payload["amount_paid"] = float(
            sum(
                Decimal(str(p.amount or 0))
                for p in invoice.payments
                if getattr(p, "voided_at", None) is None
            )
        )
        # Credits are the OTHER term in the balance: _recalculate_invoice
        # computes balance = total - payments - credits. Emitting paid without
        # credits would let a credited invoice read
        # "Total 1000 / Paid 300 / Balance 0" with $700 unaccounted for.
        if credit_total is not None:
            payload["credit_total"] = float(credit_total)
    return payload


def _validate_uuid(value: str, entity: str = "Invoice") -> None:
    try:
        _uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{entity} not found") from None


def _next_invoice_number(db: Session) -> str:
    # 2026-08-08 audit: this was count-based (`count(*) + 1`, no deleted_at
    # filter, no fallback) — it re-issued an already-taken number whenever
    # the row count and the number high-water mark diverged (deleted rows,
    # hex-format historical numbers). Delegate to the ONE generator.
    from gdx_dispatch.core.closeout_billing import next_invoice_number

    return next_invoice_number(db)




def _remaining_receivable(invoice, db: Session) -> float:
    """total − credit memos/applied − live payments: THE remaining-receivable
    arithmetic. Extracted (M32 review) because it existed three times in this
    file — the exact duplicated-money-arithmetic shape the M8 postmortem says
    drifts: two of the copies were already one kind-tuple edit away from
    disagreeing.
    """
    already = db.execute(
        select(func.sum(Payment.amount)).where(
            Payment.invoice_id == invoice.id, Payment.voided_at.is_(None)
        )
    ).scalar_one_or_none() or 0
    credited = db.execute(
        select(func.sum(InvoiceAdjustment.amount)).where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.kind.in_(("credit_memo", "credit_applied")),
        )
    ).scalar_one_or_none() or 0
    return round(_to_float(invoice.total) - _to_float(credited) - _to_float(already), 2)


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


def _validated_attached_photo_ids(
    db: Session, *, job_id: Any, raw_ids: Any
) -> list[str]:
    """The photo ids that may print on this invoice's PDF, or a 422.

    ONE implementation, shared by create and PATCH. Photos print on a document
    the customer receives, so every id must be a live photo on THIS invoice's
    job — a stray id would render someone else's premises onto a bill. The
    create path was added later (2026-08-12); a second, hand-rolled copy of
    this check there is exactly how the two drift until one of them is wrong.
    """
    ids = [str(i) for i in (raw_ids or []) if i]
    if not ids:
        return []
    if job_id is None:
        raise HTTPException(
            status_code=422,
            detail="invoice has no job — job photos can only be attached to job-linked invoices",
        )
    # Bind UUID objects, not strings — the Uuid column refuses str binds on
    # the SQLite test path (same trap as closeout/mobile).
    try:
        id_uuids = [_uuid.UUID(i) for i in ids]
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid photo id") from None
    valid = {
        str(row)
        for row in db.execute(
            select(JobPhoto.id).where(
                JobPhoto.id.in_(id_uuids),
                JobPhoto.job_id == job_id,
                JobPhoto.deleted_at.is_(None),
            )
        ).scalars()
    }
    bad = [i for i in ids if i not in valid]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"photo ids not on this invoice's job: {', '.join(bad[:5])}",
        )

    # Putting a photo on the customer's bill IS deciding the customer may see
    # it, so attaching marks it shared (migration 063's customer_visible).
    # Without this the office would tick a photo, watch it not print, and have
    # to find a second switch — two decisions for one intent.
    #
    # Two consequences, both deliberate, both noted because neither is obvious:
    #
    # 1. This publishes with the caller's INVOICE permission, not the job-photo
    #    one. The photos route requires assert_job_access (dispatch manager or
    #    the assigned tech); anyone who may edit this invoice can share a photo
    #    through it. That is the intent — accounting bills, and billing means
    #    choosing what the customer sees on the bill — but it does mean the
    #    photo-share decision has two doors with different locks.
    # 2. It is one-way ON PURPOSE. Detaching a photo (below, and on the PATCH
    #    path) does NOT un-share it: the office may well have shared it in the
    #    portal deliberately, and silently revoking that because an invoice line
    #    changed would be a surprise in the more dangerous direction. Taking a
    #    photo back is the explicit toggle on the job page.
    db.execute(
        update(JobPhoto)
        .where(JobPhoto.id.in_(id_uuids), JobPhoto.customer_visible.is_(False))
        .values(customer_visible=True)
    )
    return ids


def _recalculate_invoice(invoice: Invoice, db: Session) -> None:
    # M1 (money audit 2026-08-04): a totals-locked invoice keeps its header
    # figures. QuickBooks-imported rows have a correct imported total and a
    # lossy line set — the importer wrote QBO SubTotalLine/DiscountLine rows as
    # real lines, so Σlines is ~2x the truth (prod invoice #1111: lines
    # $2,741.50 vs total $1,471.84), and 282 imported rows have no lines at
    # all. Deriving total from lines here rewrote settled invoices and re-opened
    # them the moment the office recorded a backfill payment. Balance and status
    # still recompute — those depend on payments, not lines.
    locked = bool(getattr(invoice, "totals_locked", False))

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
    if rate is not None and not locked:
        taxable = sum(
            (Decimal(str(ln.line_total or 0))
             for ln in line_rows
             if bool(getattr(ln, "taxable", True))),
            Decimal("0"),
        )
        # A materialized discount line is taxable and negative (it reduces the
        # taxable base exactly as compute_estimate_totals does). Floor at 0 so a
        # discount larger than the taxable goods can never mint negative tax.
        if taxable < 0:
            taxable = Decimal("0")
        tax_amount = _money(taxable * Decimal(str(rate)))
        invoice.tax_amount = tax_amount
    else:
        tax_amount = _money(_to_float(invoice.tax_amount))

    if locked:
        # Header total is the truth; don't re-derive it from the lines.
        subtotal_amount = _money(_to_float(invoice.subtotal))
        total_amount = _money(_to_float(invoice.total))
    else:
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
    # This line's price already covers the installation (Doug 2026-08-19:
    # "sometimes the install price is in the part price"). Set at billing by
    # the office; drives the double-bill warning when a labor line is also
    # present. Default False = today's behaviour for every existing caller.
    includes_labor: bool = Field(default=False)
    # Labor provenance (migration 071). Optional on every existing caller.
    # `labor_source` is constrained because it is the field that distinguishes
    # a QUOTED flat price from ATTESTED hours, and free text would make that
    # distinction unreadable within a release.
    labor_price_item_id: UUID | None = None
    estimated_man_hours: float | None = Field(default=None, ge=0, le=999)
    labor_source: Literal["matrix", "attested", "manual"] | None = None

    @model_validator(mode="after")
    def _labor_provenance_is_coherent(self) -> "InvoiceLineCreateIn":
        """A matrix line must name the matrix row it came from.

        Without this, `labor_source="matrix"` with no id is a claim that
        cannot be checked — precisely the unanswerable provenance the column
        was added to prevent.
        """
        if self.labor_source == "matrix" and self.labor_price_item_id is None:
            raise ValueError(
                "labor_source='matrix' requires labor_price_item_id — a quoted "
                "flat price must name the matrix row that quoted it"
            )
        if self.labor_price_item_id is not None and self.labor_source is None:
            # A row id with no lane says a matrix row is involved but not how —
            # an unattributed linkage, which is the same unanswerable
            # provenance as hours with no lane.
            raise ValueError(
                "labor_price_item_id requires labor_source"
            )
        if self.labor_price_item_id is not None and self.labor_source == "attested":
            # Attested hours come from a tech's closeout. They have nothing to
            # do with a matrix row, so carrying one claims a quoted price priced
            # them — the lane confusion this field exists to prevent, and the
            # bug the invoice picker shipped with on 2026-08-20.
            #
            # 'manual' WITH an id is legal and meaningful: "started from matrix
            # row X, then a human repriced it". Forbidding that made the
            # honest state inexpressible and forced the estimate-copy path to
            # destroy the linkage migration 071 exists to preserve.
            raise ValueError(
                "labor_price_item_id is not valid with labor_source='attested'"
            )
        if self.estimated_man_hours is not None and self.labor_source is None:
            # An hours figure with no lane is an unattributable claim about how
            # long work took — the unanswerable provenance this column was
            # added to close.
            raise ValueError(
                "estimated_man_hours requires labor_source"
            )
        return self

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
    includes_labor: bool | None = None
    # A reprice DOWNGRADES matrix -> manual: the matrix quoted $650, so a
    # human-typed $900 is no longer matrix-quoted. Without this the row keeps
    # asserting a quote nobody made. Only the downgrade direction is offered —
    # `matrix` is not accepted here, because claiming a line became
    # matrix-quoted by editing it would be the same falsehood in reverse, and a
    # genuine matrix line is created through the picker, not a PATCH.
    labor_source: Literal["manual", "attested"] | None = None


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
    # PROVENANCE ONLY — deliberately separate from `estimate_id` above.
    #
    # `estimate_id` means "copy this estimate's lines and ignore mine". The
    # office create page can't use it: it prefills the editor from an accepted
    # estimate and then lets the operator EDIT those lines, so sending
    # estimate_id would throw their edits away and re-copy the original.
    #
    # The result was that /billing/new recorded no link at all — 5 of 340 prod
    # invoices have one, all from the mobile dialog — so the invoice detail
    # page's "linked estimate" chip was dead for every office-created invoice.
    # This field records where the numbers came from without touching them.
    source_estimate_id: UUID | None = None

    @model_validator(mode="after")
    def _source_estimate_is_not_the_copy_field(self) -> "InvoiceCreateIn":
        if self.source_estimate_id is not None and self.estimate_id is not None:
            raise ValueError(
                "send estimate_id (copy the estimate's lines) or "
                "source_estimate_id (provenance for lines you already have), "
                "not both"
            )
        if self.discount and self.estimate_id is not None:
            # The estimate-copy path mints the discount line from the
            # ESTIMATE's own discount field. Accepting one here too would bill
            # the customer two discounts for one negotiation.
            raise ValueError(
                "discount cannot be combined with estimate_id — the copied "
                "estimate carries its own discount"
            )
        if self.source_estimate_id is not None and self.job_id is None:
            # Estimates are job-scoped, so a counter sale cannot have come from
            # one. Same rule the copy path already enforces.
            raise ValueError(
                "source_estimate_id requires job_id — estimates are tied to a job"
            )
        return self
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
        # Job photos are job-scoped too — a counter-sale invoice has no job
        # whose photos could be attached.
        if self.attached_photo_ids and self.job_id is None:
            raise ValueError(
                "attached_photo_ids requires job_id — job photos are job-scoped."
            )
        return self
    # billing_type is enum-ish ("standard"/"recurring"/etc.), short bounded.
    billing_type: str = Field(default="standard", min_length=1, max_length=50)
    # §12: when the office bills a supplemental to reconcile a closeout that
    # changed after billing, this points at the original invoice it adjusts.
    # Provenance only — the office confirms the lines/amount; nothing auto-computes.
    adjusts_invoice_id: UUID | None = None
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
    # 2026-07-23 — double-billing guard override. Now that invoicing is no
    # longer UI-gated on job completion, a job that already has a
    # billing-real non-deposit invoice 409s unless the operator confirms
    # (the create page re-submits with force=true after a confirm dialog).
    force: bool = False
    # D2 (2026-08-19): a whole-invoice discount, mirroring the estimate's own
    # field rather than a discount LINE. The office could not enter one at all:
    # `unit_price` is `ge=0` and `quantity` is `gt=0`, so a negative line is
    # unrepresentable, and the only discount row the system mints comes from
    # the estimate-copy path — which /billing/new never triggers.
    #
    # Materialized server-side as the SAME `category="discount"` negative line
    # the estimate copy already mints, so both surfaces produce identical rows
    # and `_recalculate_invoice` needs no special case.
    discount: float | None = Field(default=None, ge=0, le=999999.99)

    # 2026-08-12 — job photos to print on this invoice's PDF, pickable at
    # CREATE time. The picker only existed on the invoice detail page, and
    # only once the invoice was already a draft, so the office building an
    # invoice on /billing/new had no way to attach the photos the tech took —
    # prod says the feature had never been used once. Same ids, same cap and
    # the same ownership validation as the PATCH: every id must be a live
    # photo on THIS invoice's job.
    attached_photo_ids: list[str] = Field(default_factory=list, max_length=20)


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
    # Job photos to print on this invoice's PDF (migration 059) — list of
    # job_photos.id strings, replaced wholesale on every PATCH. Empty list
    # clears the selection. Capped: a 20-photo invoice PDF is already huge.
    attached_photo_ids: list[str] | None = Field(default=None, max_length=20)


class PaymentCreateIn(BaseModel):
    # M32: optional because `pay_remaining` is the other way to say it. The
    # validator below still demands exactly one of the two — a bare {} is a
    # 422, not a silent zero.
    amount: float | None = Field(default=None, gt=0)
    # M32 (money audit): "pay whatever is still owed", computed SERVER-side
    # inside the transaction. The bulk Mark-Paid path used to post the balance
    # from the row loaded into the browser, possibly minutes earlier — User A
    # records a $400 check, User B's stale tab bulk-pays $1,000, and $1,400
    # lands on a $1,000 invoice. This mode removes the client from the
    # arithmetic entirely.
    pay_remaining: bool = False
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

    @model_validator(mode="after")
    def _amount_xor_pay_remaining(self):
        # Exactly one way to say how much: a client that sends BOTH is
        # contradicting itself, and one that sends NEITHER is asking for $0.
        if self.pay_remaining and self.amount is not None:
            raise ValueError("send either amount or pay_remaining, not both")
        if not self.pay_remaining and self.amount is None:
            raise ValueError("amount is required unless pay_remaining is true")
        return self

    @field_validator("date")
    @classmethod
    def _no_future_dates(cls, v: _datetime.date) -> _datetime.date:
        # Backdating is the point (2026 QB-era corrections reach into 2025);
        # forward-dating is not — a post-dated check isn't received cash.
        # No slack needed: the client stamps the company-zone (America/
        # Chicago) day, which is BEHIND UTC — it can never exceed the UTC
        # day. Slack would only legalize genuine tomorrow-dating.
        if v > datetime.now(UTC).date():
            raise ValueError("payment date cannot be in the future")
        return v


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
    _amount = Invoice.total
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
    # 055: not-billable-marked jobs are resolved — excluded here so the count
    # keeps agreeing with the queue.
    from gdx_dispatch.core.billing_predicates import job_billing_resolved
    from gdx_dispatch.models.tenant_models import Job
    ready_for_billing = int(db.scalar(
        select(func.count(Job.id.distinct())).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            ~job_billing_resolved(),
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


@router.get(
    "/closeout-discrepancies",
    response_model=None,
    dependencies=[Depends(require_permission("invoices.read_all"))],
)
def closeout_billing_discrepancies(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Jobs billed from a closeout that was later revised (plan §12) — the
    invoice no longer matches the attested work, so the office reconciles.

    Company-gated: returns {enabled:false, items:[]} when the tenant hasn't
    turned on closeout_billing_reconciliation. Read-only — surfacing only; the
    supplemental-invoice / credit-memo action is a separate step.
    """
    from gdx_dispatch.core.closeout_reconciliation import find_closeout_billing_discrepancies

    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
    return find_closeout_billing_discrepancies(db, tenant_id)


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

    # Double-billing guard (2026-07-23): the old "Create Invoice only when
    # Complete" button was the de-facto guard; with mid-job invoicing open,
    # the guard is explicit here on the path the UI actually uses.
    # Conditions mirror core/billing_predicates.job_billed_exists (lockstep):
    # void invoices, $0 drafts, and DEPOSIT invoices don't count as billed.
    if (
        job is not None
        and payload.billing_type != "deposit"
        and not payload.force
    ):
        from gdx_dispatch.core.billing_predicates import first_billing_real_invoice

        _existing = first_billing_real_invoice(db, payload.job_id)
        if _existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Job is already billed on {_existing.invoice_number}. "
                    "Confirm to create another invoice."
                ),
            )

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
        # M23 (money audit 2026-08-04): this path checked existence, soft-delete
        # and job scope — but never STATUS. A job with accepted estimate A and a
        # later declined variant B would happily bill B, because `estimate_id`
        # means "copy this estimate's lines and ignore mine" and the copy asked
        # no questions. The other two conversion paths already refuse:
        # mobile_invoicing.py:447 and estimates.py's /deposit-invoice both 409.
        #
        # Honest scope: this field has no frontend caller today — see the
        # `source_estimate_id` contract note below, and InvoiceCreateView's
        # client-side accepted-filter. This closes the API surface, not a
        # live office hole. Worth closing anyway, because `estimate_id`
        # copies lines the operator never sees — the one shape where a
        # wrong estimate bills silently. The office prefill puts its lines
        # in an editor the operator reads before sending.
        #
        # Deliberately NOT applied to `source_estimate_id` below: that is
        # provenance for lines the caller already built, it copies nothing, and
        # a counter sale whose lines originated in a quote that was later
        # revised is legitimate. Gating it would refuse honest history.
        #
        # `status` is a DB enum — draft|sent|accepted|declined|rejected|expired
        # — so the case fold is belt-and-braces agreement with the deposit
        # path's identical check, not a reachable case.
        if (estimate.status or "").lower() != "accepted":
            raise HTTPException(
                status_code=409,
                detail=(
                    "estimate must be 'accepted' to invoice; current: "
                    f"{estimate.status or 'unset'}"
                ),
            )

    # `source_estimate_id` gets the SAME existence + soft-delete + job-scope
    # checks as the copy path above. It had none, so a counter sale could be
    # linked to an estimate that never existed — the exact shape the contract
    # calls incoherent for its sibling field. Provenance that cannot be
    # resolved is not provenance.
    if payload.source_estimate_id:
        src_estimate = db.execute(
            select(Estimate).where(
                Estimate.id == payload.source_estimate_id,
                Estimate.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not src_estimate:
            raise HTTPException(
                status_code=404, detail="source estimate not found"
            )
        if payload.job_id is not None and src_estimate.job_id != payload.job_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "source estimate belongs to a different job — the numbers "
                    "on this invoice did not come from it"
                ),
            )

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
    elif estimate is not None and getattr(estimate, "tax_rate", None) is not None:
        # M24 (money audit 2026-08-04): a per-estimate rate override IS the
        # quoted price. Falling through to the tenant default re-taxed an
        # estimate deliberately quoted at 0%, billing more than the customer
        # accepted.
        resolved_rate = Decimal(str(estimate.tax_rate))
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
    # Validated BEFORE the invoice row exists: a 422 here must not leave a
    # half-built invoice (and a burned invoice number) behind. Same helper the
    # PATCH uses — never a second copy of the rule.
    attached_photo_ids = _validated_attached_photo_ids(
        db, job_id=payload.job_id, raw_ids=payload.attached_photo_ids
    )

    invoice = Invoice(
        job_id=payload.job_id,
        # Job photos picked on the create screen (2026-08-12) — printed on the
        # PDF by pdf.py::_invoice_photos_for_pdf.
        attached_photo_ids=_json.dumps(attached_photo_ids) if attached_photo_ids else None,
        # Provenance thread for deposit netting + "deposit taken" surfaces:
        # which estimate this invoice was born from (2026-07-23).
        #
        # ONLY the copy path writes this. Deposit netting matches on
        # or_(job_id, estimate_id) and closeout reconciliation skips rows that
        # have it — both read it as "this invoice IS the estimate's bill", which
        # is only true when the server built the lines.
        estimate_id=payload.estimate_id,
        # SEPARATE column, deliberately. `estimate_id` means "the server copied
        # this estimate's lines", and deposit netting + closeout reconciliation
        # both read it that way. Writing merely-prefilled invoices into it
        # armed a dormant arm of the deposit matcher (or_(job_id, estimate_id))
        # and netted a DIFFERENT job's paid deposit into this invoice.
        source_estimate_id=payload.source_estimate_id,
        invoice_number=_next_invoice_number(db),
        billing_type=payload.billing_type,
        # §12 supplemental provenance — the original invoice this one adjusts.
        adjusts_invoice_id=payload.adjusts_invoice_id,
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
    # 2026-08-08 audit: two concurrent creates could compute the same number
    # and the second flush raised an uncaught IntegrityError → raw 500. The
    # shared helper (M17.4 consolidated all three creators onto it)
    # regenerates once; the unique constraint stays the referee.
    from gdx_dispatch.core.closeout_billing import flush_invoice_with_number_retry

    flush_invoice_with_number_retry(db, invoice)

    if estimate:
        # Accepted TIER (2026-08-14): the contract is the accepted tier's
        # content, not the estimate_lines rows. Office-built tiers keep base
        # scope lines there ($500 of scope under an $8,000 package), and the
        # MOBILE builder stores ALL THREE tiers' lines there untagged — the
        # old unconditional copy billed Good+Better+Best summed. Tier lines
        # (line-built tiers) copy like estimate lines; a flat tier becomes
        # one package line at the tier price. _recalculate_invoice derives
        # totals from these lines, so this copy IS the bill.
        _accepted_tier = None
        if getattr(estimate, "accepted_tier_id", None) is not None:
            from gdx_dispatch.modules.proposals.models import ProposalTier

            _accepted_tier = db.execute(
                select(ProposalTier).where(ProposalTier.id == estimate.accepted_tier_id)
            ).scalar_one_or_none()
        if _accepted_tier is not None:
            from gdx_dispatch.modules.proposals.service import tier_contract_lines

            lines = tier_contract_lines(db, _accepted_tier)
            if not lines:
                _label = {"good": "Good", "better": "Better", "best": "Best"}.get(
                    _accepted_tier.tier_name, _accepted_tier.tier_name
                )
                _desc = f"{_label} package"
                if _accepted_tier.description:
                    _desc = f"{_desc} — {_accepted_tier.description}"
                lines = [SimpleNamespace(
                    description=_desc[:500],
                    quantity=1,
                    unit_price=_accepted_tier.total_price,
                    line_total=_accepted_tier.total_price,
                    category=None,
                    cost_snapshot=None,
                    margin_pct_snapshot=None,
                    margin_pct_override=None,
                    sort_order=1,
                )]
        else:
            lines = db.execute(
                select(EstimateLine)
                .where(EstimateLine.estimate_id == estimate.id)
                .order_by(EstimateLine.sort_order.asc(), EstimateLine.created_at.asc(), EstimateLine.id.asc())
            ).scalars().all()
        # M24 (money audit 2026-08-04): estimates exclude labor from tax when
        # the tenant's tax_labor flag is off, but the copy below never carried
        # `taxable`, and InvoiceLine defaults it to True — so a quote of
        # $2,000 materials + $1,000 labor priced tax on $2,000 and then billed
        # tax on $3,000. Resolve the flag once for the whole copy.
        # Reuse the estimate's own helpers, not a reimplementation — the whole
        # point is that the two sides agree, and a second copy of the
        # category convention is how they drift apart again.
        try:
            from gdx_dispatch.modules.proposals.totals import (
                _is_labor_line,
                _load_tax_labor_flag,
            )
            _tax_labor = bool(_load_tax_labor_flag(db))
        except Exception:
            log.exception("invoice_create_tax_labor_flag_failed")
            # Match _load_tax_labor_flag's OWN default (False = don't tax
            # labor). Defaulting to True here would re-introduce the overbill
            # this fix exists to remove.
            _tax_labor = False

            def _is_labor_line(ln):  # noqa: F811 — local fallback
                return (getattr(ln, "category", None) or "").strip().lower() == "labor"

        def _line_is_taxable(ln) -> bool:
            return True if _tax_labor else not _is_labor_line(ln)

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
                    taxable=_line_is_taxable(line),
                    category=getattr(line, "category", None),
                    cost_snapshot=getattr(line, "cost_snapshot", None),
                    margin_pct_snapshot=getattr(line, "margin_pct_snapshot", None),
                    margin_pct_override=getattr(line, "margin_pct_override", None),
                    # Migration 071 closes an asymmetry this copy has had since
                    # S97: estimate_lines carried the matrix link and man-hours,
                    # invoice_lines had nowhere to put them, so converting an
                    # accepted estimate silently dropped "which matrix row
                    # quoted this labor".
                    #
                    # This block writes InvoiceLine DIRECTLY, so it bypasses
                    # InvoiceLineCreateIn's validator. It must therefore enforce
                    # the same coherence by hand, or the copy lands rows the API
                    # would 422 — hours with no lane, or a "matrix" claim on a
                    # price a human overrode. `_labor_provenance_for` is that
                    # enforcement, kept next to the validator's own rules.
                    **_labor_provenance_for(line, db),
                    sort_order=line.sort_order,
                )
            )

        # M7 (money audit 2026-08-04): materialize the estimate's discount as a
        # real negative line. `Invoice` has no discount column, and every path
        # that hand-set a discounted total lost it on the first recalc — or, on
        # this path, never applied it at all: an accepted $4,500 estimate
        # created a $5,000 invoice. As a line it is simply part of
        # `total = Σlines + tax`, so no recalc can drop it.
        #
        # Taxable ON PURPOSE: compute_estimate_totals subtracts the discount
        # from the taxable base, so the discount line must reduce it too or the
        # invoice would tax the undiscounted goods. `_recalculate_invoice`
        # floors the taxable base at 0 for the discount > goods case.
        _discount = Decimal(str(getattr(estimate, "discount", None) or 0))
        if _discount > 0:
            db.add(
                InvoiceLine(
                    company_id=invoice.company_id,
                    invoice_id=invoice.id,
                    description="Discount",
                    quantity=1,
                    unit_price=_money(-_discount),
                    line_total=_money(-_discount),
                    taxable=True,
                    category="discount",
                    sort_order=(max((ln.sort_order or 0) for ln in lines) + 1) if lines else 1,
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
                    includes_labor=bool(getattr(line, "includes_labor", False)),
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
                    # Labor provenance (071) — which lane priced this line.
                    labor_price_item_id=line.labor_price_item_id,
                    estimated_man_hours=(
                        Decimal(str(line.estimated_man_hours))
                        if line.estimated_man_hours is not None else None
                    ),
                    labor_source=line.labor_source,
                    sort_order=idx,
                )
            )

    # D2 — materialize an operator-entered discount as the same negative line
    # the estimate-copy path mints above. `sort_order` follows the operator's
    # lines; it is NOT guaranteed last once change orders append their own (the
    # CO block reads max(sort_order) on an autoflush=False session with no
    # intervening flush, so it restarts at 1 — pre-existing, display-only, and
    # not touched here). Ordering does not affect any total.
    #
    # It reduces the TAXABLE base exactly like its sibling (taxable=True).
    # `_recalculate_invoice` floors that BASE at 0 — but not the total, which
    # is why the explicit cap below exists rather than trusting a flooring that
    # only half applies.
    if payload.discount and payload.discount > 0 and not payload.estimate_id:
        _op_discount = Decimal(str(payload.discount))
        # A discount larger than the goods is not a discount, it is a refund,
        # and this path must not mint one. `_recalculate_invoice` floors the
        # TAXABLE BASE at zero but NOT the total, so an over-discount wrote a
        # negative invoice: subtotal -500, total -500, posting negative revenue
        # through repost_invoice_issuance -- while `balance_due` floored to 0,
        # skipping the paid auto-flip so the row sat in AR forever. The client
        # showed $0.00 for the same input, so nothing on screen revealed it.
        _goods = sum(
            (Decimal(str(li.unit_price)) * int(li.quantity) for li in payload.line_items),
            Decimal("0"),
        )
        if _op_discount > _goods:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"discount ({_op_discount}) exceeds the invoice's line total "
                    f"({_goods}) — a discount cannot make the invoice negative. "
                    "Issue a credit memo instead."
                ),
            )
        db.add(
            InvoiceLine(
                company_id=invoice.company_id,
                invoice_id=invoice.id,
                description="Discount",
                quantity=1,
                unit_price=_money(-_op_discount),
                line_total=_money(-_op_discount),
                taxable=True,
                category="discount",
                sort_order=len(payload.line_items) + 1,
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

    # Deposit netting (2026-07-23): a non-deposit invoice for a job (or
    # estimate) that collected a deposit gets a negative "Less deposit paid"
    # line for the PAID portion, and any unpaid deposit remainder is
    # superseded via credit memo — otherwise the customer owes deposit +
    # full total and the GL double-counts revenue. An exception here fails
    # the whole create atomically (better no invoice than wrong money).
    deposit_result: dict[str, object] | None = None
    if payload.billing_type != "deposit" and (payload.job_id or payload.estimate_id):
        from gdx_dispatch.modules.deposits import apply_deposits_to_final

        db.flush()
        deposit_result = apply_deposits_to_final(db, invoice, actor=_actor_id(_))

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
        details={
            "invoice_number": invoice.invoice_number,
            "status": invoice.status,
            # Which estimate this came from, and HOW — "copied" means the
            # server built the lines, "prefilled" means the operator arrived
            # with them and may have edited them before saving. Those are
            # different claims about who chose the numbers, so the audit trail
            # records which one happened rather than just that a link exists.
            **(
                {"estimate_id": str(invoice.estimate_id), "estimate_link": "copied"}
                if invoice.estimate_id else {}
            ),
            **(
                {
                    "source_estimate_id": str(invoice.source_estimate_id),
                    "estimate_link": "prefilled",
                }
                if invoice.source_estimate_id else {}
            ),
        },
    )
    db.commit()
    resp = _serialize_invoice(invoice)
    if zero_price_warnings:
        resp["warnings"] = zero_price_warnings
    if deposit_result:
        resp["deposit_netting"] = deposit_result
    return resp


@router.get("/{invoice_id}", response_model=None)
def get_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db, include_relations=True)
    _credits = db.execute(
        select(func.sum(InvoiceAdjustment.amount)).where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.kind.in_(("credit_memo", "credit_applied")),
        )
    ).scalar_one_or_none() or 0
    payload = _serialize_invoice(
        invoice, include_lines=True, include_payments=True, credit_total=_credits
    )
    # Tier 10 — authoritative "in QuickBooks" flag from QBEntityMap (what every
    # push path writes and the QB dashboard counts). qb_synced_at alone is NOT
    # this signal: it's un-backfilled, so a legacy/imported/manual invoice reads
    # NULL yet is in QB. The detail view renders the sync chip off this flag.
    from gdx_dispatch.core.quickbooks import qb_entity_is_mapped

    payload["qb_in_quickbooks"] = qb_entity_is_mapped(db, "invoice", invoice.id)

    # Adjustments (credit memos / refunds / applied credits) — without these
    # the detail view can't explain why balance_due ≠ total − payments. The
    # deposit lifecycle leans on them: a partially-paid deposit superseded at
    # final-create carries a credit_memo for the remainder and reads "paid",
    # which is misleading unless the memo is shown.
    adjustments = (
        db.execute(
            select(InvoiceAdjustment)
            .where(InvoiceAdjustment.invoice_id == invoice.id)
            .order_by(InvoiceAdjustment.created_at)
        )
        .scalars()
        .all()
    )
    payload["adjustments"] = [
        {
            "id": str(adj.id),
            "kind": adj.kind,
            "amount": _to_float(adj.amount),
            "reason": adj.reason,
            "refund_method": adj.refund_method,
            "created_at": _iso_dt(adj.created_at),
        }
        for adj in adjustments
    ]
    # Same predicate as the record-payment 409 guard — the frontend banner
    # and that guard must tell the same story.
    payload["is_superseded_deposit"] = bool(
        (invoice.billing_type or "") == "deposit"
        and _to_float(invoice.balance_due) <= 0
        and any(
            adj.kind == "credit_memo" and "superseded" in (adj.reason or "").lower()
            for adj in adjustments
        )
    )

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
    current_user: dict = Depends(get_current_user),
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
    if "attached_photo_ids" in updates and updates["attached_photo_ids"] is not None:
        ids = _validated_attached_photo_ids(
            db, job_id=invoice.job_id, raw_ids=updates["attached_photo_ids"]
        )
        invoice.attached_photo_ids = _json.dumps(ids) if ids else None

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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
    current_user: dict = Depends(get_current_user),
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
    # M37 (money audit 2026-08-04): recording a payment on a draft is legal
    # (record_payment blocks only void), and the draft stays a draft while a
    # balance remains — so a part-paid draft was deletable. The Payment row
    # survived the soft-delete, but every AR surface joins through non-deleted
    # invoices, so the cash simply vanished from the books. void_invoice has
    # carried this exact guard all along; delete never got it.
    live_payments = db.execute(
        select(func.count())
        .select_from(Payment)
        .where(Payment.invoice_id == invoice.id, Payment.voided_at.is_(None))
    ).scalar_one()
    if live_payments:
        raise HTTPException(
            status_code=409,
            detail="invoice has recorded payments — void or remove them first",
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
            user_id=resolve_audit_actor(current_user),
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
    "Total: {{total}}{{balance_line}}{{due_line}}\n\n"
    "Thanks,\n{{company_name}}"
)
# Receipt flavor (2026-08-17): composing on a PAID invoice is a thank-you,
# not an ask — the paid/balance figures come from _invoice_settlement, the
# same math the PDF prints, so the numbers in the body and the attachment
# always agree.
_DEFAULT_RECEIPT_SUBJECT_TEMPLATE = "Payment received — Invoice {{invoice_number}} from {{company_name}}"
_DEFAULT_RECEIPT_BODY_TEMPLATE = (
    "Hi {{customer_name}},\n\n"
    "Thank you for your payment on {{job_title}}. Invoice {{invoice_number}} "
    "is paid — a copy is attached for your records.\n"
    "Total: {{total}}{{paid_line}}{{balance_line}}\n\n"
    "We appreciate your business!\n\n"
    "Thanks,\n{{company_name}}"
)


def _prepare_invoice_email(
    db: Session,
    invoice,
    *,
    contact_id: str | None = None,
    body_text_override: str | None = None,
    subject_override: str | None = None,
    to_email_override: str | None = None,
    mint_token: bool = True,
) -> dict[str, object]:
    """One render for composer, preview and send — the invoice twin of
    estimates._prepare_estimate_email. Returns {customer, recipient, subject,
    body_text, html, is_paid, pay_url}."""
    from gdx_dispatch.core.email_layout import email_branding, linkify, nl2br
    from gdx_dispatch.core.email_recipients import resolve_recipient
    from gdx_dispatch.core.email_sender import build_invoice_email_html
    from gdx_dispatch.core.payments import public_pay_url
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.routers.estimates import _render_template
    from gdx_dispatch.routers.pdf import _invoice_settlement

    customer = None
    if invoice.customer_id:
        customer = db.execute(
            select(Customer).where(Customer.id == invoice.customer_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()
    if to_email_override and (to_email_override or "").strip():
        from gdx_dispatch.core.email_recipients import override_recipient
        recipient = override_recipient(
            to_email_override, (customer.name if customer else "") or "",
        )
    else:
        recipient = resolve_recipient(db, customer, contact_id) if customer is not None else None

    job_title = ""
    if invoice.job_id:
        job_row = db.execute(
            select(Job).where(Job.id == invoice.job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if job_row:
            job_title = (job_row.title or "").strip()

    branding = email_branding(db)
    company_name = branding["company_name"]

    invoice_label = job_title or f"Invoice {invoice.invoice_number or ''}".strip()
    due_line = f"\nDue: {invoice.due_date.isoformat()}" if invoice.due_date else ""
    _total_v = _to_float(invoice.total)
    _balance_v = _to_float(invoice.balance_due)
    balance_line = f"\nBalance Due: ${_balance_v:,.2f}" if abs(_balance_v - _total_v) > 0.005 else ""
    _is_paid = invoice.status == "paid"
    _paid_v, _credits_v = _invoice_settlement(invoice, db)
    paid_line = f"\nPaid: ${_paid_v:,.2f}" if (_is_paid and _paid_v > 0) else ""
    greeting = (recipient.greeting_name if recipient and recipient.ok else "") or \
        ((customer.name if customer else "") or "there")
    ctx = {
        "customer_name": greeting,
        "job_title": invoice_label,
        # Same fallback as the body/PDF name — an empty serial otherwise
        # renders "Invoice # from Acme" and breaks bounce rung-1 matching.
        "invoice_number": invoice.invoice_number or str(invoice.id)[:8],
        "company_name": company_name,
        "total": f"${_total_v:,.2f}",
        "balance_due": f"${_balance_v:,.2f}",
        "balance_line": balance_line,
        "paid_line": paid_line,
        "due_line": due_line,
    }
    if _is_paid:
        subject_tpl, body_tpl = _DEFAULT_RECEIPT_SUBJECT_TEMPLATE, _DEFAULT_RECEIPT_BODY_TEMPLATE
    else:
        subject_tpl, body_tpl = _DEFAULT_INVOICE_SUBJECT_TEMPLATE, _DEFAULT_INVOICE_BODY_TEMPLATE
    subject = (subject_override or "").strip() or _render_template(subject_tpl, ctx).strip() or invoice_label
    if body_text_override is not None and body_text_override.strip():
        body_text = body_text_override
    else:
        body_text = _render_template(body_tpl, ctx)

    pay_url = None
    if _balance_v > 0:
        if not invoice.public_token and mint_token:
            invoice.public_token = secrets.token_urlsafe(48)[:64]
            db.commit()
            db.refresh(invoice)
        if invoice.public_token:
            pay_url = public_pay_url(invoice.public_token)
    link_line = f"Pay online: {pay_url}" if pay_url else ""
    if pay_url and pay_url not in body_text:
        body_text = f"{body_text.rstrip()}\n\n{link_line}\n"

    copy_for_html = body_text
    if link_line:
        stripped = copy_for_html.rstrip()
        if stripped.endswith(link_line):
            copy_for_html = stripped[: -len(link_line)].rstrip()
    intro_html = "<p style=\"margin:0 0 12px;\">" + linkify(
        nl2br(copy_for_html), branding.get("accent") or "#2563eb"
    ).replace("<br><br>", "</p><p style=\"margin:0 0 12px;\">") + "</p>"

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
    html = build_invoice_email_html(
        company_name=company_name,
        invoice_number=invoice.invoice_number or str(invoice.id)[:8],
        customer_name=greeting,
        line_items=lines_data,
        subtotal=_to_float(invoice.subtotal),
        tax_amount=_to_float(invoice.tax_amount),
        total=_total_v,
        balance_due=_balance_v,
        due_date=invoice.due_date.isoformat() if invoice.due_date else "",
        notes=invoice.notes or "",
        portal_url=pay_url or "",
        tax_rate=float(invoice.tax_rate) if invoice.tax_rate is not None else None,
        paid_to_date=_paid_v,
        credits_applied=_credits_v,
        branding=branding,
        intro_html=intro_html,
        is_receipt=_is_paid,
    )
    return {
        "customer": customer,
        "recipient": recipient,
        "subject": subject,
        "body_text": body_text,
        "html": html,
        "is_paid": _is_paid,
        "pay_url": pay_url,
        "branding": branding,
    }


class SendInvoiceIn(BaseModel):
    """Optional composer payload for /send — empty body keeps the one-click
    behavior (template copy, resolver's default recipient)."""

    body_text: str | None = None
    subject: str | None = Field(default=None, max_length=500)
    contact_id: str | None = Field(default=None, max_length=36)
    to_email: str | None = Field(default=None, max_length=254)


@router.post("/{invoice_id}/email-preview", response_model=None)
def invoice_email_preview(
    invoice_id: UUID,
    payload: SendInvoiceIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db, include_relations=True)
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void — it cannot be emailed")
    require_deliverable(invoice)
    p = payload or SendInvoiceIn()
    prep = _prepare_invoice_email(
        db, invoice,
        contact_id=p.contact_id,
        body_text_override=p.body_text,
        subject_override=p.subject,
        to_email_override=p.to_email,
        mint_token=False,
    )
    recipient = prep["recipient"]
    return {
        "subject": prep["subject"],
        "html": prep["html"],
        "to_email": recipient.email if recipient else "",
        "to_name": recipient.to_name if recipient else "",
    }


@router.get("/{invoice_id}/email-compose", response_model=None)
def invoice_email_compose(
    invoice_id: UUID,
    contact_id: str | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    # 2026-08-08 audit: this was the widest hole in the delivery surface —
    # no guard of ANY kind. It composed voided invoices (minting a pay
    # token + embedding a live pay URL into the body) and unverified
    # drafts alike. Guards must run before the token-minting side effects
    # below.
    _guard_inv = _get_invoice_or_404(invoice_id, db)
    if _guard_inv.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void — it cannot be emailed")
    require_deliverable(_guard_inv)
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
    from gdx_dispatch.routers.estimates import _estimate_recipient_options
    from gdx_dispatch.routers.pdf import _branding_payload, _invoice_payload, _template_config

    invoice = _get_invoice_or_404(invoice_id, db, include_relations=True)
    prep = _prepare_invoice_email(db, invoice, contact_id=contact_id)
    customer = prep["customer"]
    recipient = prep["recipient"]
    subject = prep["subject"]
    body_text = prep["body_text"]
    _is_paid = prep["is_paid"]

    pdf_bytes = generate_invoice_pdf(
        invoice_data=_invoice_payload(invoice, customer, db),
        tenant_branding=_branding_payload(db),
        template_config=_template_config(db, "invoice"),
    )
    pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")
    _pdf_suffix = "-paid" if _is_paid else ""
    pdf_name = f"invoice-{invoice.invoice_number or str(invoice.id)[:8]}{_pdf_suffix}.pdf"

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
        "to": [recipient.email] if (recipient and recipient.ok) else [],
        "recipients": _estimate_recipient_options(db, customer),
        "selected_contact_id": recipient.contact_id if recipient else None,
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


class MarkSentIn(BaseModel):
    # 'manual' default keeps old callers' audit rows meaning what they always
    # meant ("operator says it went out, channel unknown"); the composer paths
    # pass 'email' and the paper-invoice button passes 'mail'.
    channel: Literal["email", "mail", "manual"] = "manual"


@router.post("/{invoice_id}/mark-sent", response_model=None)
def mark_invoice_sent(
    invoice_id: UUID,
    payload: MarkSentIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Flip status to 'sent' without firing a server-side email.

    Used after the composer hands off to Outlook (or the mailto fallback) —
    the operator's mail client owns delivery, so the server just stamps
    sent_at + mints the public_token. Mirrors mark_estimate_sent. `channel`
    records HOW it went out — 'mail' is the paper-invoice path ("Mark as
    Mailed"), the office's only honest exit from the Unsent bucket.
    """
    channel = payload.channel if payload else "manual"
    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void — it cannot be sent")
    # §11 rail (2026-08-08): Mark-as-Mailed on an unverified draft both
    # skipped review AND fed the draft into the auto-dunning population
    # (dunning filters on status='sent').
    require_deliverable(invoice)
    # PAID stays terminal (2026-08-17): the composer now sends paid invoices
    # too ("Send Receipt"), and its post-send handoff lands here. Regressing
    # paid→sent would resurrect AR and fire a GL S5 posting — so a paid
    # invoice only gets the delivery-fact stamp below, never the transition.
    if invoice.status != "paid":
        transition_invoice_status(db, invoice, "sent", actor=_actor_id(_))  # GL S5: P1 posts here when the flag is on
    invoice.sent_at = datetime.now(UTC)
    invoice.sent_via = channel
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
        details={"status": invoice.status, "channel": channel},
    )
    db.commit()
    return _serialize_invoice(invoice)


@router.post("/{invoice_id}/pay-link", response_model=None)
def get_invoice_pay_link(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Mint (idempotently) and return the customer-facing online-payment
    link for an invoice, for the office to text/email out.

    `url` is null unless the link would actually work end-to-end: the
    public base URL is set AND Stripe keys are configured. The flags let
    the UI say precisely which piece is missing instead of handing the
    operator a dead link.
    """
    from gdx_dispatch.core.payments import public_pay_url, stripe_configured

    invoice = _get_invoice_or_404(invoice_id, db)
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void")
    if _to_float(invoice.balance_due) <= 0:
        raise HTTPException(status_code=409, detail="invoice has no balance due")
    # §11 rail (2026-08-08): no pay link for an unverified draft — the /pay
    # page refuses drafts too, so the link would be a dead end anyway.
    require_deliverable(invoice)
    if not invoice.public_token:
        invoice.public_token = secrets.token_urlsafe(48)[:64]
        db.commit()
        db.refresh(invoice)
    return {
        "stripe_configured": stripe_configured(),
        "url": public_pay_url(invoice.public_token),
    }


@router.post("/{invoice_id}/send", response_model=None)
def send_invoice(
    invoice_id: UUID,
    payload: SendInvoiceIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    # PR1-billing-capture (2026-07-07, GL audit §12): sending a VOIDED
    # invoice silently resurrected it to "sent" — a cancelled bill came
    # back to life and re-entered AR. Mirror mark-sent's finalized guard.
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void — un-void or recreate it before sending")
    # §11 rail (2026-08-08): an unverified DRAFT may not reach a customer —
    # the mobile path enforced this from day one; the desktop path did not.
    require_deliverable(invoice)
    if invoice.status != "paid":
        transition_invoice_status(db, invoice, "sent", actor=_actor_id(_))  # GL S5
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
    p = payload or SendInvoiceIn()
    try:
        from gdx_dispatch.core.transactional_email import recently_sent, send_transactional_email
        tid = str(invoice.company_id) if invoice.company_id else None
        _dup_kind = "receipt" if invoice.status == "paid" else "document"
        if tid and invoice.customer_id and recently_sent(db, "invoice", str(invoice.id), kind=_dup_kind):
            # Server-side double-send guard (kind-scoped: a receipt right
            # after the invoice email is legitimate; the same receipt twice
            # in one window is not).
            email_skip_reason = "duplicate_send_suppressed"
        elif tid and invoice.customer_id:
            # One prep for composer sends and one-click/bulk sends alike —
            # template copy (or the operator's edit) inside the branded
            # shell, settlement rows, CTA button, person-aware recipient.
            # Receipt flavor rides invoice.status == 'paid' inside the prep.
            prep = _prepare_invoice_email(
                db, invoice,
                contact_id=p.contact_id,
                body_text_override=p.body_text,
                subject_override=p.subject,
                to_email_override=p.to_email,
            )
            cust = prep["customer"]
            recipient = prep["recipient"]
            if cust is not None and recipient is not None and recipient.ok:
                attachments: list[dict[str, object]] | None = None
                try:
                    import base64 as _b64

                    from gdx_dispatch.core.pdf_generator import generate_invoice_pdf
                    from gdx_dispatch.core.transactional_email import MAX_INLINE_ATTACHMENT_BYTES
                    from gdx_dispatch.routers.pdf import _branding_payload, _invoice_payload, _template_config
                    pdf_bytes = generate_invoice_pdf(
                        invoice_data=_invoice_payload(invoice, cust, db),
                        tenant_branding=_branding_payload(db),
                        template_config=_template_config(db, "invoice"),
                    )
                    if len(pdf_bytes) > MAX_INLINE_ATTACHMENT_BYTES:
                        log.warning(
                            "invoice_send_pdf_too_large_to_attach invoice=%s bytes=%s",
                            invoice.id, len(pdf_bytes),
                        )
                    else:
                        _sfx = "-paid" if invoice.status == "paid" else ""
                        attachments = [{
                            "name": f"invoice-{invoice.invoice_number or str(invoice.id)[:8]}{_sfx}.pdf",
                            "content_type": "application/pdf",
                            "content_base64": _b64.b64encode(pdf_bytes).decode("ascii"),
                        }]
                except Exception:
                    log.exception("invoice_send_pdf_attach_failed")
                email_sent, email_provider, email_skip_reason = send_transactional_email(
                    tenant_db=db,
                    tenant_id=tid,
                    user_id=str(_actor_id(_)),
                    to_email=recipient.email,
                    to_name=recipient.to_name,
                    subject=prep["subject"],
                    html_body=prep["html"],
                    attachments=attachments,
                    kind="receipt" if invoice.status == "paid" else "document",
                    entity_type="invoice",
                    entity_id=str(invoice.id),
                    recipient_source=recipient.source,
                    recipient_contact_id=recipient.contact_id,
                )
                pdf_attached = email_sent and bool(attachments)
            elif recipient is not None and recipient.source == "invalid_override":
                email_skip_reason = "invalid_recipient_email"
            elif cust is not None:
                email_skip_reason = "customer_has_no_email"
            else:
                email_skip_reason = "customer_not_found"
        elif not invoice.customer_id:
            email_skip_reason = "invoice_has_no_customer"
    except Exception:
        log.exception("invoice_email_send_failed")
        email_skip_reason = "exception"

    # sent_at is a DELIVERY fact, not an attempt fact — it feeds the Billing
    # "Last Sent" column. Pre-2026-07-22 this stamped before the email went
    # out, so a failed bulk-send showed "Last Sent: today" on the very rows
    # the toast reported as not delivered. Status still flips above (the
    # response's email_sent keeps the UI honest); only the stamp is gated.
    # Receipt sends (status 'paid' with an existing sent_at) must NOT
    # overwrite the ORIGINAL invoice delivery date — the receipt attempt is
    # fully recorded in outbound_emails; sent_at keeps meaning "when the
    # invoice itself reached the customer".
    if email_sent:
        if not (invoice.status == "paid" and invoice.sent_at is not None):
            invoice.sent_at = datetime.now(UTC)
            invoice.sent_via = "email"
        db.commit()
        db.refresh(invoice)

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
    current_user: dict = Depends(get_current_user),
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
        # 2026-08-19: this handler dropped BOTH of these on the floor.
        # part_id is the linkage the create path has always stored, so a line
        # added here could never release its part on delete; includes_labor
        # was accepted by the contract and then ignored, which is a control
        # that silently no-ops.
        part_id=payload.part_id,
        includes_labor=bool(getattr(payload, "includes_labor", False)),
        # 2026-08-20, same class of bug caught by the same review: the labor
        # provenance fields were added to the contract above and dropped here,
        # so a line added from the invoice DETAIL screen's Add Labor button
        # looked like it worked and wrote an unprovenanced row. A control that
        # silently no-ops is the defect this handler has now had twice.
        labor_price_item_id=payload.labor_price_item_id,
        estimated_man_hours=(
            Decimal(str(payload.estimated_man_hours))
            if payload.estimated_man_hours is not None else None
        ),
        labor_source=payload.labor_source,
        sort_order=sort_order,
    )
    db.add(line)
    db.flush()

    # Claim the part this line bills, with the SAME two guards the create
    # path earned the hard way (invoices.py:1364 — "the operator's payload
    # STILL carried those lines, so the amounts double-billed while the stamp
    # no-opped"). Both matter:
    #
    #   job_id scope — a part belongs to a job, so a line on a counter sale or
    #   on a different job's invoice must never claim it.
    #
    #   409 when nothing was stamped — a part already billed elsewhere would
    #   otherwise be CHARGED here while its stamp stays on the other invoice.
    #   Billing the customer twice and silencing the unbilled-parts banner in
    #   the same request is the worst possible failure of this feature.
    #
    # Without the claim the part stays "unbilled" forever: the money is
    # charged and every unbilled surface keeps reporting it as missing — a
    # warning that doing the right thing cannot clear, which is how a
    # checklist becomes wallpaper.
    if payload.part_id:
        claimed = db.execute(
            update(JobPartNeeded)
            .where(
                JobPartNeeded.id == payload.part_id,
                JobPartNeeded.job_id == str(invoice.job_id or ""),
                JobPartNeeded.billed_invoice_id.is_(None),
            )
            .values(billed_invoice_id=invoice.id)
        ).rowcount
        if not claimed:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "part not billable — already billed on another invoice, "
                    f"or not on this invoice's job: {payload.part_id}"
                ),
            )

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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
    # Deposit netting line (2026-07-23): same protection as delete — editing
    # "Less deposit paid" desyncs the invoice from the money that actually
    # moved on the deposit invoice.
    if (line.category or "") == "Deposit" and _to_float(line.line_total) < 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "this is the deposit netting line — it mirrors the deposit "
                "actually paid and can't be edited. Void the invoice and "
                "re-create it if the netting is wrong."
            ),
        )

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
    if "labor_source" in updates:
        line.labor_source = updates["labor_source"]
        # Dropping to attested means this is no longer priced off a matrix row,
        # and the contract forbids an id on an attested line. Clear it rather
        # than leave a linkage that contradicts the lane.
        if updates["labor_source"] == "attested":
            line.labor_price_item_id = None
    if "margin_pct_override" in updates:
        line.margin_pct_override = (
            Decimal(str(updates["margin_pct_override"]))
            if updates["margin_pct_override"] is not None else None
        )
    if "includes_labor" in updates:
        line.includes_labor = bool(updates["includes_labor"])

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
    # Deposit netting line (2026-07-23): deleting "Less deposit paid" would
    # spring the total back up by the already-collected deposit and nothing
    # would ever re-net it — a silent double-charge. Void the invoice and
    # re-create if the netting is wrong.
    if (line.category or "") == "Deposit" and _to_float(line.line_total) < 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "this is the deposit netting line — deleting it would re-bill "
                "an already-collected deposit. Void the invoice and re-create it instead."
            ),
        )
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
    # Last, with a default: several tests call this positionally, and FastAPI
    # injects a Request by annotation regardless of position.
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    # PR1-billing-capture (audit catch): a payment against a VOIDED invoice
    # ran _recalculate_invoice, which flips status to "paid" once balance
    # hits zero — the void resurrected into "Paid This Month" through the
    # payment door. Same class as the /send guard.
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="invoice is void — un-void it before recording a payment")

    # Deposit invoices (2026-07-23, implementation-audit catch): a deposit
    # superseded at final-invoice time (credit-memo'd remainder) reads as
    # settled — a late-arriving check recorded HERE would double-charge the
    # customer, whose final invoice already excludes only the PAID portion.
    # Point the operator at the final invoice instead.
    if (invoice.billing_type or "") == "deposit" and _to_float(invoice.balance_due) <= 0:
        supersede_reason = db.execute(
            select(InvoiceAdjustment.reason).where(
                InvoiceAdjustment.invoice_id == invoice.id,
                InvoiceAdjustment.kind == "credit_memo",
            )
        ).scalars().first()
        if supersede_reason and "superseded" in supersede_reason.lower():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"this deposit was closed out ({supersede_reason.strip()}) — "
                    "record the payment on the final invoice instead"
                ),
            )

    # M32: derive "whatever is still owed" HERE, server-side, before anything
    # reads payload.amount — the stale-browser overpayment (minutes of drift)
    # is gone. HONESTY (review catch): this is a READ COMMITTED read, not a
    # spell — two same-instant writers could still both derive pre-commit
    # sums, so the invoice row is locked for the duration of this request's
    # transaction (the same with_for_update pattern /verify uses), shrinking
    # the race to serialized-or-blocked instead of pretending it away.
    if payload.pay_remaining:
        db.execute(
            select(Invoice.id).where(Invoice.id == invoice.id).with_for_update()
        )
        # QB-legacy guard (review catch): an imported invoice settled off-book
        # can carry balance_due=0 with NO Payment rows — the sum-derivation
        # would happily re-charge its full total. balance_due is a clamp, not
        # the truth, but ≤0 is an unambiguous "nothing is owed".
        if _to_float(invoice.balance_due) <= 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "nothing_remaining",
                        "message": "nothing remains to pay on this invoice"},
            )
        _remaining = _remaining_receivable(invoice, db)
        if _remaining <= 0.005:
            # Structured like DUPLICATE_PAYMENT_CODE: this 409 means "the
            # invoice is settled", not "your payment failed" — a queued caller
            # rendering them identically would tell an operator to re-record
            # money that is already there.
            raise HTTPException(
                status_code=409,
                detail={"code": "nothing_remaining",
                        "message": "nothing remains to pay on this invoice"},
            )
        payload.amount = _remaining

    # GL S6: overpayment gate, active only when ledger posting is on (flag
    # off keeps today's permissive behavior — zero behavior change until
    # cutover). Opt-in routes the excess to 2300 Customer Credits.
    if ledger_posting_enabled(db, invoice.company_id) and not payload.allow_overpayment:
        # Audit round 3: the gate must measure against the REMAINING
        # receivable — total minus credit memos/applied credits — or a
        # payment of the printed total after a credit memo silently drives
        # AR negative instead of minting a customer credit. M32 extracted the
        # arithmetic into _remaining_receivable so this and the pay_remaining
        # derivation cannot drift apart.
        if float(_money(payload.amount)) > _remaining_receivable(invoice, db) + 0.005:
            raise HTTPException(
                status_code=422,
                detail=(
                    "payment exceeds the invoice's remaining balance — set "
                    "allow_overpayment to credit the excess to the customer's account"
                ),
            )

    reference_value = (payload.reference or "").strip() or None

    # M2 (money audit 2026-08-04): this endpoint had NO idempotency at all, so
    # a double-click on Record Payment — or the mobile offline queue replaying
    # a request whose response was lost — recorded the same money twice. Proven
    # in test_zz_money_correctness_probe.py: two rows for one reference,
    # recorded sequentially. A reference is the operator's own claim that two
    # requests describe the SAME payment (check #, Stripe intent), so honor it.
    # Migration 056's partial unique index is the un-raceable backstop; this
    # check is what turns the race into a clean 409 instead of a 500.
    if reference_value:
        prior = db.execute(
            select(Payment).where(
                Payment.invoice_id == invoice.id,
                Payment.reference == reference_value,
                Payment.voided_at.is_(None),
            )
        ).scalars().first()
        if prior is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a payment with reference '{reference_value}' is already recorded "
                    f"on this invoice ({_to_float(prior.amount):.2f} on "
                    f"{prior.payment_date.isoformat()}) — void it first to replace it"
                ),
            )

    # Reference-less dedupe window (2026-08-13). The guard above only fires
    # when there IS a reference, and migration 056's index is likewise partial
    # on `reference IS NOT NULL` — so CASH, which carries no reference, had no
    # protection at all.
    #
    # That became load-bearing when the field surfaces moved to the offline
    # queue: on a network error after the server already committed (the normal
    # dead-signal driveway failure) the queue leaves the row PENDING and
    # replays it. Until M36 (2026-08-24) the Idempotency-Key middleware that
    # should catch such a replay never ran in production — it returned early
    # unless request.state.principal was set, and nothing outside tests set
    # it. PrincipalStampMiddleware now feeds it, so header-carrying replays
    # are caught upstream; this window REMAINS as the belt for requests that
    # arrive without the header (a manual double-tap is one).
    #
    # A short window keyed on (invoice, amount, method) is the honest fix: two
    # identical reference-less payments seconds apart are a replay or a
    # double-tap, never two real handfuls of cash. Legitimate repeats stay
    # possible — wait out the window, or give the payment a reference.
    if reference_value is None:
        window_start = datetime.now(UTC) - timedelta(seconds=_CASHLIKE_DEDUPE_SECONDS)
        recent = db.execute(
            select(Payment).where(
                Payment.invoice_id == invoice.id,
                Payment.reference.is_(None),
                Payment.voided_at.is_(None),
                Payment.amount == _money(payload.amount),
                func.lower(Payment.method) == payload.method.strip().lower(),
                Payment.created_at >= window_start,
            )
        ).scalars().first()
        if recent is not None:
            # Structured, not a bare string: this 409 means "the money IS on
            # the invoice", which is the opposite of every other 409 this
            # endpoint raises (void invoice, superseded deposit, locked
            # period — all of those mean "nothing was recorded"). A client
            # that renders them identically tells a tech holding a customer's
            # check that the payment FAILED, and the tech records it again
            # once the window closes — turning a duplicate the server
            # successfully blocked into one the operator types in by hand.
            # Callers branch on `code`; see DUPLICATE_PAYMENT_CODE users.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": DUPLICATE_PAYMENT_CODE,
                    "message": (
                        f"an identical {payload.method.strip().lower()} payment of "
                        f"{_to_float(recent.amount):.2f} was recorded moments ago — "
                        "this one was not added. Add a reference if it is a "
                        "second, separate payment."
                    ),
                    "payment_id": str(recent.id),
                },
            )

    payment = Payment(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        amount=_money(payload.amount),
        method=payload.method.strip().lower(),
        payment_date=payload.date,
        reference=reference_value,
    )
    db.add(payment)
    try:
        db.flush()
    except IntegrityError as exc:
        # Lost the race to a concurrent request for the same reference — the
        # unique index caught what the SELECT above could not see.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"a payment with reference '{reference_value}' was just recorded "
                "on this invoice"
            ),
        ) from exc

    # Snapshot BEFORE the recalc: only the payment that flips the invoice to
    # paid in THIS request may carry paid_at with it. Deriving from
    # MAX(payment_date) instead would misfire on invoices settled by credit
    # memos (paid with zero payments) or backdate to an old partial.
    was_unpaid = invoice.paid_at is None
    if payment.payment_date < datetime.now(UTC).date():
        # QB phase-out sequencing breadcrumb: backfilling corrections while
        # the QB money pull can still run risks a webhook pull re-stamping
        # this very date. Warn, don't block — the pause is the office's
        # rollout step, and the log is how a missed step surfaces in triage.
        try:
            from gdx_dispatch.core.settings_flags import qb_money_pull_paused
            # Deliberately NOT the request session: a failed SELECT on the
            # in-flight transaction would poison it (InFailedSqlTransaction
            # on the next recalc) even with the exception swallowed. The
            # reader's own short-lived session keeps the breadcrumb inert.
            if not qb_money_pull_paused(invoice.company_id):
                log.warning(
                    "backdated_payment_with_qb_pull_active invoice=%s date=%s — "
                    "flip the QB money-pull pause before backfilling",
                    invoice.id, payment.payment_date,
                )
        except Exception:
            log.exception("qb_pause_breadcrumb_check_failed")
    _recalculate_invoice(invoice, db)
    if was_unpaid and invoice.paid_at is not None and payload.date < datetime.now(UTC).date():
        # Backdated zeroing payment: paid_at follows the payment's day as a
        # date-only stamp (UTC midnight) — the same "day known, minute not"
        # convention QB sync writes and formatStampDate/isDateOnlyStamp
        # render. Same-day payments keep the precise now() the recalc set.
        invoice.paid_at = datetime.combine(payload.date, datetime.min.time(), tzinfo=UTC)
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

    # M12. Money arrived by another route, so a PaymentIntent the customer
    # still has open on the pay page could collect a second time — it froze its
    # amount when it was minted, and `confirm` runs with `require_balance=False`.
    # Queued, not called: the sweep makes several Stripe calls, and holding
    # money locks across a third-party outage is the silent-write window this
    # repo ranks highest. Only intents that would OVERCHARGE are cancelled.
    queued = enqueue_stale_intent_sweep(
        invoice, why="payment_recorded"
    )

    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="payment_recorded",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={
            "payment_id": str(payment.id),
            "amount": _to_float(payment.amount),
            # Whether the sweep was QUEUED — not whether it cancelled
            # anything. Claiming a cancellation the task has not run yet
            # would be the kind of comfortable lie this fix removes.
            "stale_intent_sweep_queued": queued,
            # M32: whether the server derived the amount (pay-remaining mode).
            "pay_remaining": bool(payload.pay_remaining),
        },
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
    # M15 / migration 076. Say WHOSE void this is. A dispute reinstatement
    # only ever un-voids a dispute's own reversal, so recording "office_void"
    # here is what stops a later `charge.dispute.funds_reinstated` from
    # silently undoing a reversal the office made on purpose.
    payment.voided_reason = "office_void"
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


@router.post(
    "/{invoice_id}/void",
    response_model=None,
    # Gated when the UI landed (2026-08-23). Until then this endpoint had no
    # caller at all, so "any authenticated user" was academic; putting a button
    # on the invoice screen makes it reachable by every role that can open the
    # screen — and `/billing/:id` carries no route permission of its own.
    # `invoices.write` matches what `/billing/new` already requires and is what
    # `accounting` holds; `technician`, `dispatcher` and `sales` do not, which
    # is the intended line. A void is terminal and there is no un-void.
    dependencies=[Depends(require_permission("invoices.write"))],
)
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
    # A void is dead money — `core/billing_predicates.py` excludes voided
    # invoices from "is this job billed" for exactly that reason. But the parts
    # and change orders it claimed stayed stamped with its id, so they read
    # "billed" while no LIVE invoice bills them: gone from
    # `parts-needed?unbilled=true`, absent from Ready-for-Billing, and
    # unbillable forever. Money that was going to be charged silently stops
    # being charged.
    #
    # This is the same reasoning an auditor already applied to the soft-delete
    # path (see `delete_invoice`), and `void_untouched_autodraft` in
    # core/closeout_billing.py releases on its own void too. This was the one
    # void that did not — three void paths, two releases.
    released_parts = db.execute(
        update(JobPartNeeded)
        .where(JobPartNeeded.billed_invoice_id == invoice.id)
        .values(billed_invoice_id=None)
    ).rowcount
    from gdx_dispatch.routers.change_orders import ChangeOrder as _CO

    released_cos = db.execute(
        update(_CO)
        .where(_CO.billed_invoice_id == invoice.id)
        .values(billed_invoice_id=None)
    ).rowcount
    # M39 audit round 2: a void kills what is owed — an ACTIVE payment plan
    # scheduling money on a void invoice would be a standing lie. Cancel it
    # in the same transaction, with its own trail.
    _plan = _live_plan(db, invoice.id)
    if _plan is not None:
        _plan.status = "cancelled"
        _plan.cancelled_at = datetime.now(UTC)
        _plan.cancelled_by = _actor_id(_)
        log_audit_event_sync(
            db=db, tenant_id=None, user_id=_actor_id(_),
            action="payment_plan_cancelled", entity_type="invoice",
            entity_id=str(invoice.id),
            details={"plan_id": str(_plan.id), "why": "invoice voided"},
        )
    # ONE commit, and the audit row is staged into it -- deliberately not the
    # commit-then-log-then-commit shape `delete_invoice` uses. Releasing a
    # claim puts billable work back on the checklist, so a release that lands
    # while its trail does not is a silent write, the defect class this repo
    # ranks highest. Staging both means the database decides them together:
    # either the void, the releases and the record of them are all durable, or
    # none of them are. (Adversarial review 2026-08-23 caught the two-commit
    # window; `delete_invoice` swallows the same failure into a log line.)
    # M12. A void is the ONE event after which the invoice can never owe
    # anything again, so every open intent on it is stale by definition. This
    # was the call site an adversarial review found missing: a voided invoice
    # whose customer still had the pay page open could be paid, and the webhook
    # would book that payment onto a void whose parts and change orders had
    # already gone back to the unbilled checklist.
    #
    # Enqueued AFTER the commit below, unlike its three siblings which each
    # already have a commit behind them at this point. `void_invoice`
    # deliberately uses a SINGLE transaction — an earlier review forced the
    # void, its part/change-order releases and its audit row to land or roll
    # back together — so enqueueing here would let a `priority:high` worker
    # cancel the customer's live PaymentIntent at Stripe *before* the void is
    # durable. If the audit write or the commit then failed, the invoice would
    # still not be void and the intent would already be irreversibly cancelled:
    # the money side committed and the record rolled back, which is the exact
    # inversion of "the money commits first".
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="invoice_voided",
        entity_type="invoice",
        entity_id=str(invoice.id),
        # The release counts belong in the trail: voiding an invoice puts work
        # back on the unbilled checklist, and "what changed" has to include
        # that, not just the invoice total.
        details={
            "total": _to_float(invoice.total),
            "released_parts": int(released_parts or 0),
            "released_change_orders": int(released_cos or 0),
        },
    )
    db.commit()
    db.refresh(invoice)
    # Now that the void is durable. No `queued` flag in the audit row above:
    # it cannot be known before the commit, and inventing one there is what the
    # first version of this call site did. The sweep writes its own
    # `stale_payment_intents_canceled` row, so the trail exists either way.
    enqueue_stale_intent_sweep(invoice, why="invoice_voided", settled=True)
    return _serialize_invoice(invoice)


@router.post("/{invoice_id}/finalize", response_model=None)
def finalize_invoice(
    invoice_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    invoice = _get_invoice_or_404(invoice_id, db)
    # Voids are terminal — locking one stamps an audit row that means
    # nothing. Drafts stay finalizable ON PURPOSE: locking a draft against
    # further edits is an established workflow (add-line rejects locked
    # invoices — see test_add_line_rejects_locked_invoice).
    if invoice.status == "void":
        raise HTTPException(
            status_code=409,
            detail="void invoices cannot be finalized",
        )
    if invoice.locked:
        return _serialize_invoice(invoice)
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


# POST /api/invoices/batch was DELETED here (2026-08-08 audit): it minted
# lineless $0 invoice shells with a THIRD numbering scheme (INV-{hex8},
# unparseable by the canonical generator) and a public pay token on every
# shell — and had zero frontend callers.

# The banner on InvoiceDetailView asks for exactly this: parts on the job that
# are ordered/received/used, unbilled, and not already lined on THIS invoice.
# Kept in lockstep with it deliberately — two definitions of "missing from this
# invoice" would let the screen and the server disagree about whether the
# office may proceed, and the office would be right to distrust both.
_UNBILLED_PART_STATUSES = ("ordered", "received", "used")


def _unbilled_parts_for_invoice(db: Session, invoice: Invoice) -> list[dict[str, object]]:
    """Parts recorded against this invoice's job that nothing is billing.

    Empty for an invoice with no job (a counter sale has no parts checklist),
    and empty for anything past DRAFT.

    Draft-only mirrors the banner exactly — `fetchUnbilledJobParts` returns
    early on any non-draft invoice — and that lockstep is the point: two
    definitions of "missing from this invoice" would let the screen and the
    server disagree about whether the office may proceed.

    It also matters on this tenant's real data. `require_deliverable` gates
    drafts only, because prod carries thousands of pre-rail invoices with
    `verified_at` NULL and status sent/paid. Verifying one of those is a
    backfill of an approval that already happened in the world, not a review
    of work still to be billed — refusing it would fire a warning on invoices
    no banner has ever shown, for parts the office decided about months ago.

    `wont_bill` is excluded by the status filter — it is the office's dismiss
    verb for warranty / goodwill / already-flat-priced parts, and re-raising a
    part they explicitly declined is how a warning becomes wallpaper.

    A part already on THIS invoice is excluded via `InvoiceLine.part_id`:
    "unbilled" is job-wide, but this gate claims something narrower — not on
    this invoice. Reporting a part that IS already charged here would push the
    office to add a second line for it, and the new claim would silence the
    warning. A false alarm laundering itself into a double charge.
    """
    if invoice.job_id is None:
        return []
    if (invoice.status or "draft").lower() != "draft":
        return []
    lined = {
        str(pid)
        for (pid,) in db.execute(
            select(InvoiceLine.part_id).where(
                InvoiceLine.invoice_id == invoice.id,
                InvoiceLine.deleted_at.is_(None),
                InvoiceLine.part_id.is_not(None),
            )
        ).all()
    }
    rows = db.execute(
        select(JobPartNeeded).where(
            JobPartNeeded.job_id == str(invoice.job_id),
            JobPartNeeded.billed_invoice_id.is_(None),
            JobPartNeeded.status.in_(_UNBILLED_PART_STATUSES),
        )
    ).scalars().all()
    return [
        {
            "id": str(row.id),
            "part_name": row.part_name,
            "quantity": int(row.quantity or 1),
            "unit_price": _to_float(row.unit_price) if row.unit_price is not None else None,
        }
        for row in rows
        if str(row.id) not in lined
    ]


class VerifyInvoiceIn(BaseModel):
    """Optional body for verify. Absent body == not acknowledged, which is the
    safe default: an API caller who does not know about the gate gets the 409
    rather than silently sailing past it."""

    acknowledge_unbilled_parts: bool = False


@router.post(
    "/{invoice_id}/verify",
    response_model=None,
    # invoices.write, not read_all (2026-08-08 audit): verification APPROVES
    # money — the read-only viewer tier must not be able to sign off a draft
    # for delivery now that the delivery gate keys on the stamp.
    dependencies=[Depends(require_permission("invoices.write"))],
)
def verify_invoice(
    invoice_id: str,
    payload: VerifyInvoiceIn | None = None,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Office verification — plan §11 (Doug: "have the office be called to
    verify the invoice").

    Stamps verified_at/verified_by_user_id. The mobile send endpoint refuses
    while verified_at is NULL, so a tech can CREATE an invoice from the truck
    but nothing reaches a customer until a second pair of eyes approved it —
    on the hourly lane the closeout hours ARE the price, and they are typed
    from memory. Gated on invoices.read_all (any office tier can verify;
    verification is an approval, not a money mutation). Idempotent: verifying
    twice keeps the FIRST stamp — approval history belongs to whoever looked
    first, and re-stamping would quietly launder a later look as the review.
    """
    _validate_uuid(invoice_id, "Invoice")
    # Row-locked re-select (concurrency guard, audit round 2): two office users
    # verifying at once must not both "win". with_for_update serializes them —
    # the second blocks until the first commits, then sees verified_at set.
    # A no-op on SQLite, a real row lock on Postgres. Mutation is via ORM
    # attributes (NOT a raw Core UPDATE) so the money-write governance guard
    # and the ledger flush hook both see it.
    invoice = db.execute(
        select(Invoice).where(
            Invoice.id == UUID(invoice_id), Invoice.deleted_at.is_(None)
        ).with_for_update()
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.verified_at is not None:
        return {
            "invoice_id": str(invoice.id),
            "verified_at": invoice.verified_at.isoformat(),
            "verified_by_user_id": invoice.verified_by_user_id,
            "already_verified": True,
        }
    # Follow-up 2 of closeout-parts-autopricing: the durable unbilled-parts
    # gate belongs on the SERVER. The banner on InvoiceDetailView is
    # client-side, so it cannot help the accounting role (holds invoices.write,
    # does NOT hold inventory.read, so the banner's own fetch 403s and the
    # empty banner reads as an all-clear on a money screen) or any API caller.
    # The plan also named the mobile lane; that was rhetoric — no mobile
    # surface calls /verify, it only reads verified_at and 409s on send.
    # Verify is still the right place: it is the one approval an invoice must
    # pass before a customer can see it, and it already row-locks.
    #
    # Refuses rather than warns, and lists what is missing. An acknowledgement
    # is required to proceed because plenty of parts legitimately go unbilled
    # (warranty, goodwill, already covered by a flat price) — the office
    # decides, the server just makes sure they were asked.
    unbilled = _unbilled_parts_for_invoice(db, invoice)
    if unbilled and not (payload and payload.acknowledge_unbilled_parts):
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{len(unbilled)} recorded part"
                    f"{'' if len(unbilled) == 1 else 's'} from this job "
                    f"{'is' if len(unbilled) == 1 else 'are'} not on this "
                    "invoice. Add them, or re-send with "
                    "acknowledge_unbilled_parts to verify anyway."
                ),
                "unbilled_parts": unbilled,
                "acknowledge_field": "acknowledge_unbilled_parts",
            },
        )
    invoice.verified_at = datetime.now(UTC)
    invoice.verified_by_user_id = _actor_id(_)
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="invoice_verified", entity_type="invoice", entity_id=str(invoice.id),
        details={
            "invoice_number": invoice.invoice_number,
            "total": float(invoice.total or 0),
            "status": invoice.status,
            # Verifying PAST a warning is a different act from verifying with
            # nothing outstanding, and the record has to be able to tell them
            # apart — "who approved this and what did they know" is the whole
            # point of the stamp. Only present when there was something to
            # acknowledge.
            "acknowledged_unbilled_parts": (
                [p["part_name"] for p in unbilled] if unbilled else None
            ),
        },
    )
    db.commit()
    return {
        "invoice_id": str(invoice.id),
        "verified_at": invoice.verified_at.isoformat(),
        "verified_by_user_id": invoice.verified_by_user_id,
        "already_verified": False,
    }


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
    # "overdue" is accepted defensively: prod rows stay raw "sent" (overdue
    # is derived at read time), but demo seeds and imports do write it, and
    # the invoices most likely to need a credit memo are the overdue ones.
    if invoice.status not in ("sent", "paid", "overdue"):
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

    from gdx_dispatch.core.invoice_tax import credit_tax_component

    adjustment = InvoiceAdjustment(
        invoice_id=invoice.id,
        kind="credit_memo",
        amount=credit_amount,
        tax_component=credit_tax_component(invoice, credit_amount),
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
    # M12: a credit can settle the invoice just as a payment can, and the
    # audit's prescription says "a payment OR CREDIT". An intent left open on
    # a fully-credited invoice would collect money against nothing owed.
    # M12. Money arrived by another route, so a PaymentIntent the customer
    # still has open on the pay page could collect a second time — it froze its
    # amount when it was minted, and `confirm` runs with `require_balance=False`.
    # Queued, not called: the sweep makes several Stripe calls, and holding
    # money locks across a third-party outage is the silent-write window this
    # repo ranks highest. Only intents that would OVERCHARGE are cancelled.
    queued = enqueue_stale_intent_sweep(
        invoice, why="credit_memo_issued"
    )
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="credit_memo_issued", entity_type="invoice", entity_id=str(invoice.id),
        details={
            "amount": float(credit_amount), "reason": payload.reason,
            "adjustment_id": str(adjustment.id),
            "stale_intent_sweep_queued": queued,
        },
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
    if invoice.status not in ("sent", "paid", "overdue"):
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

    from gdx_dispatch.core.invoice_tax import credit_tax_component

    adjustment = InvoiceAdjustment(
        invoice_id=invoice.id,
        kind="credit_applied",
        amount=amount,
        tax_component=credit_tax_component(invoice, amount),
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
    # M12. Money arrived by another route, so a PaymentIntent the customer
    # still has open on the pay page could collect a second time — it froze its
    # amount when it was minted, and `confirm` runs with `require_balance=False`.
    # Queued, not called: the sweep makes several Stripe calls, and holding
    # money locks across a third-party outage is the silent-write window this
    # repo ranks highest. Only intents that would OVERCHARGE are cancelled.
    queued = enqueue_stale_intent_sweep(
        invoice, why="customer_credit_applied"
    )
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="customer_credit_applied", entity_type="invoice", entity_id=str(invoice.id),
        details={
            "amount": float(amount), "adjustment_id": str(adjustment.id),
            "stale_intent_sweep_queued": queued,
        },
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
    if invoice.status not in ("sent", "paid", "overdue"):
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

    from gdx_dispatch.core.invoice_tax import credit_tax_component

    adjustment = InvoiceAdjustment(
        invoice_id=invoice.id,
        kind="refund",
        amount=refund_amount,
        tax_component=credit_tax_component(invoice, refund_amount),
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


def _payment_plans_enabled(db: Session) -> bool:
    from gdx_dispatch.models.tenant_models import AppSettings

    row = db.query(AppSettings).first()
    return bool(row and getattr(row, "payment_plans_enabled", False))


def _plan_out(plan, installments, *, invoice=None, db=None) -> dict[str, object]:
    """Audit round 2: nothing ever WRITES installment statuses (payments
    arrive through the normal paths, nothing auto-charges), so the stored
    'pending' would read as a lie on a paid invoice. Derive the display
    status at read time from money that actually arrived: an installment is
    'covered' once cumulative paid reaches its slice, else 'overdue' past
    its due date, else 'pending'."""
    from datetime import date as _date

    paid = 0.0
    if invoice is not None and db is not None:
        from gdx_dispatch.core.invoice_paid import paid_to_date

        try:
            paid = float(paid_to_date(db, invoice.id))
        except Exception:
            paid = 0.0
    today = _date.today()
    out_installments = []
    cumulative = 0.0
    for i in installments:
        cumulative = round(cumulative + _to_float(i.amount), 2)
        if paid + 0.005 >= cumulative:
            derived = "covered"
        elif i.due_date < today:
            derived = "overdue"
        else:
            derived = "pending"
        out_installments.append({
            "id": str(i.id),
            "seq": i.seq,
            "due_date": i.due_date.isoformat(),
            "amount": _to_float(i.amount),
            "status": derived,
        })
    return {
        "plan_id": str(plan.id),
        "invoice_id": str(plan.invoice_id),
        "status": plan.status,
        "num_installments": plan.num_installments,
        "total_amount": _to_float(plan.total_amount),
        "start_date": plan.start_date.isoformat(),
        "installments": out_installments,
    }


def _live_plan(db: Session, invoice_id):
    from gdx_dispatch.models.tenant_models import PaymentPlan

    return (
        db.query(PaymentPlan)
        .filter(PaymentPlan.invoice_id == invoice_id, PaymentPlan.status == "active")
        .order_by(PaymentPlan.created_at.desc())
        .first()
    )


@router.post(
    "/{invoice_id}/payment-plan",
    response_model=None,
    dependencies=[Depends(require_permission("invoices.write"))],
)
def create_payment_plan(
    invoice_id: str,
    payload: PaymentPlanIn,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Split an invoice into monthly installments — persisted (M39).

    The pre-2026-08-24 version computed a schedule, persisted NOTHING, and
    returned a plan_id that did not exist. Doug's ruling: payment plans are
    an OPTION — off by default here, honestly refusing when off, functional
    when on. A plan is an agreed schedule; payments still arrive through the
    normal payment paths and nothing auto-charges.
    """
    _validate_uuid(invoice_id, "Invoice")
    from datetime import timedelta
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import PaymentPlan, PaymentPlanInstallment

    if not _payment_plans_enabled(db):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "payment_plans_disabled",
                "message": (
                    "Payment plans are not enabled for this company — "
                    "turn them on in Settings first."
                ),
            },
        )

    invoice_uuid = UUID(invoice_id)
    invoice = _get_invoice_or_404(invoice_uuid, db)
    # Audit round 2: a plan on a draft/void invoice schedules money that may
    # never be owed (mirrors the credit-memo issuance gate).
    if invoice.status not in ("sent", "paid", "overdue"):
        raise HTTPException(
            status_code=409,
            detail="only issued invoices can carry a payment plan",
        )
    # Audit round 2: schedule what is still OWED, not the printed total — a
    # deposit-paid or partially-paid invoice plans its remainder.
    total = _remaining_receivable(invoice, db)
    if total <= 0:
        raise HTTPException(status_code=422, detail="nothing remains to schedule on this invoice")
    if _live_plan(db, invoice.id) is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plan_exists",
                "message": "this invoice already has an active payment plan — cancel it first",
            },
        )

    per_installment = _money(total / payload.num_installments)
    last = _money(total - float(per_installment) * (payload.num_installments - 1))
    if float(last) <= 0:
        # Audit round 2 (reproduced): $0.10 over 12 → eleven 1¢ rows and a
        # PERSISTED −1¢ installment. Too many slices for the amount.
        raise HTTPException(
            status_code=422,
            detail="too many installments for this amount — each installment must be at least a cent",
        )
    plan = PaymentPlan(
        id=uuid4(),
        invoice_id=invoice.id,
        status="active",
        num_installments=payload.num_installments,
        total_amount=_money(total),
        start_date=payload.start_date,
        created_by=_actor_id(_),
    )
    db.add(plan)
    db.flush()

    installments = []
    for i in range(payload.num_installments):
        due = payload.start_date + timedelta(days=30 * i)
        # The last installment absorbs the rounding remainder so the schedule
        # sums to the invoice total exactly.
        amount = per_installment if i < payload.num_installments - 1 else last
        inst = PaymentPlanInstallment(
            id=uuid4(), plan_id=plan.id, seq=i + 1, due_date=due, amount=amount
        )
        db.add(inst)
        installments.append(inst)
    db.flush()

    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="payment_plan_created", entity_type="invoice", entity_id=str(invoice.id),
        details={"plan_id": str(plan.id), "installments": payload.num_installments, "total": total},
    )
    db.commit()
    return _plan_out(plan, installments, invoice=invoice, db=db)


@router.get("/{invoice_id}/payment-plan", response_model=None)
def get_payment_plan(
    invoice_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """{enabled, plan} in ONE call. Audit round 2: the view used to read
    /api/settings for the toggle, which is admin-gated — so the section
    silently never rendered for exactly the non-admin office staff who'd
    use it. Any authenticated reader may learn the toggle + the plan."""
    _validate_uuid(invoice_id, "Invoice")
    from gdx_dispatch.models.tenant_models import PaymentPlanInstallment

    invoice = _get_invoice_or_404(UUID(invoice_id), db)
    if not _payment_plans_enabled(db):
        return {"enabled": False, "plan": None}
    plan = _live_plan(db, invoice.id)
    if plan is None:
        return {"enabled": True, "plan": None}
    installments = (
        db.query(PaymentPlanInstallment)
        .filter(PaymentPlanInstallment.plan_id == plan.id)
        .order_by(PaymentPlanInstallment.seq.asc())
        .all()
    )
    return {"enabled": True, "plan": _plan_out(plan, installments, invoice=invoice, db=db)}


@router.delete(
    "/{invoice_id}/payment-plan",
    response_model=None,
    dependencies=[Depends(require_permission("invoices.write"))],
)
def cancel_payment_plan(
    invoice_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Cancel the active plan (soft — the schedule stays reconstructable)."""
    _validate_uuid(invoice_id, "Invoice")
    invoice = _get_invoice_or_404(UUID(invoice_id), db)
    plan = _live_plan(db, invoice.id)
    if plan is None:
        raise HTTPException(status_code=404, detail="no active payment plan on this invoice")
    plan.status = "cancelled"
    plan.cancelled_at = datetime.now(UTC)
    plan.cancelled_by = _actor_id(_)
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="payment_plan_cancelled", entity_type="invoice", entity_id=str(invoice.id),
        details={"plan_id": str(plan.id)},
    )
    db.commit()
    return {"plan_id": str(plan.id), "status": "cancelled"}


# ---------------------------------------------------------------------------
# Payment Receipt (#220)
# ---------------------------------------------------------------------------

@router.post("/{invoice_id}/send-receipt", response_model=None)
def send_payment_receipt(
    invoice_id: str,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Email the customer their payment receipt — the paid invoice itself.

    2026-08-17: this endpoint (issue #220) used to write a payment_receipt_sent
    audit row and return {"sent": true} WITHOUT SENDING ANYTHING — a
    fake-success no-op (frontend-contract class C6). It now delegates to the
    shared /send path, which is receipt-flavored for paid invoices
    ("Payment received" subject, -paid PDF name, Paid-to-Date in the body),
    and reports delivery honestly.
    """
    _validate_uuid(invoice_id, "Invoice")
    # A real UUID from here on — the string id the route captures dies in the
    # Uuid bind processor on SQLite (str has no .hex), which is exactly how
    # the old stub stayed green: it never touched the row hard enough to care.
    invoice_uuid = UUID(invoice_id)
    invoice = _get_invoice_or_404(invoice_uuid, db)
    # §11 rail (2026-08-08): mirror the mobile receipt gate — a receipt on
    # an unverified draft means money moved on unreviewed numbers.
    require_deliverable(invoice)
    if invoice.status != "paid":
        raise HTTPException(
            status_code=409,
            detail="invoice is not paid — use /send to (re)send the invoice itself",
        )
    # Real payments only: a credit-memo'd invoice reaches status 'paid' with
    # zero money received, and "payment received" over a write-off is a lie.
    # (Partial-payment receipts stay the mobile endpoint's domain — it
    # receipts a specific payment row.)
    from gdx_dispatch.routers.pdf import _invoice_settlement
    paid_to_date, _credits = _invoice_settlement(invoice, db)
    if paid_to_date <= 0:
        raise HTTPException(
            status_code=422,
            detail="no payment recorded on this invoice — record the payment before sending a receipt",
        )

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
    if invoice.customer_id is None:
        # Legacy row resolved via the job hop — backfill so the shared send
        # path (which reads invoice.customer_id) can actually deliver.
        invoice.customer_id = customer_uuid
        db.commit()

    payload = send_invoice(invoice_id=invoice_uuid, _=_, db=db)
    email_sent = bool(payload.get("email_sent"))

    log_audit_event_sync(
        db=db, tenant_id=None, user_id=_actor_id(_),
        action="payment_receipt_sent", entity_type="invoice", entity_id=str(invoice.id),
        details={
            "to": email,
            "total": _to_float(invoice.total),
            "paid": paid_to_date,
            "email_sent": email_sent,
        },
    )
    db.commit()

    return {
        "sent": email_sent,
        "to": email,
        "invoice_id": str(invoice.id),
        "pdf_attached": bool(payload.get("pdf_attached")),
        "email_provider": payload.get("email_provider"),
        "email_skip_reason": payload.get("email_skip_reason"),
    }
