"""
Change Orders router — mid-job scope/price changes.

Port of archive/dispatch_flask/blueprints/api_change_orders.py (13 endpoints).
Tracks scope changes to jobs with customer approval workflow.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from gdx_dispatch.core.audit import TenantBase, audit_best_effort, resolve_audit_actor, utcnow
from gdx_dispatch.core.database import contained_read, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.quantities import recorded_quantity
from gdx_dispatch.modules.proposals.audit_details import audit_field_changes
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["change_orders"],
    dependencies=[Depends(require_module("change_orders"))],
)


CO_STATUSES = ("draft", "pending_approval", "approved", "declined", "completed")


class ChangeOrder(TenantBase):
    __tablename__ = "change_orders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    co_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(120), nullable=True)  # customer_request, scope_added, etc.
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    approved_by: Mapped[str] = mapped_column(String(200), nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    signature_url: Mapped[str] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_signature_token: Mapped[str] = mapped_column(String(255), nullable=True)
    # PR3-billing-capture (S122 pattern, mirrors job_parts_needed): the
    # invoice this CO was billed on. Set by the invoice-create handler via
    # UPDATE…RETURNING — the stamp GATES the line copy, so a CO can never
    # bill twice. NULL = never billed. FK ON DELETE SET NULL releases COs
    # when an invoice is hard-deleted (soft-delete handler does the same).
    # Billing truth lives HERE — `status` stays approval-workflow truth
    # (billing a CO does not flip its status).
    billed_invoice_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # GDXA-795 (migration 124): the rate this CO quotes, stamped by
    # `_stamp_tax_rate` on create, re-link and approval through the invoice's
    # own helper. A signed CO shows it; an unsigned one shows the live rate
    # approval would stamp. NULL = written before 124 and not approved since;
    # a signed NULL row keeps the live resolve it always had.
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)


class ChangeOrderLineIn(BaseModel):
    """D-S122-change-orders-create-flow — line-item shape for the create
    payload. Mirrors the InvoiceLineCreateIn shape so the frontend can mount
    the same <LineItemEditor> component."""
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(default=1, gt=0, le=9999)
    unit_price: float = Field(default=0, ge=0, le=999999.99)
    # PR3-billing-capture: COs are handled like invoices (Doug 2026-07-07) —
    # default True mirrors InvoiceLineCreateIn; labor lines send False where
    # the state allows.
    taxable: bool = Field(default=True)


class ChangeOrderIn(BaseModel):
    job_id: str | None = Field(default=None, max_length=36)
    customer_id: str | None = Field(default=None, max_length=36)
    customer_name: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    reason: str | None = Field(default=None, max_length=1000)
    # $1M ceiling catches obvious nonsense; ge=0 rejects negative amounts.
    # When `line_items` is supplied, this is recomputed from the line subtotal.
    # None-default so PATCH can distinguish "not sent" (preserve) from
    # "set to 0" (legacy bare-amount caller). Auditor round-2 catch.
    amount: float | None = Field(default=None, ge=0, le=1_000_000)
    status: str = Field(default="draft", min_length=1, max_length=50)
    # D-S122-change-orders-create-flow — optional line-items list. When set,
    # ChangeOrderLine rows are created and `amount` is overwritten by the sum.
    line_items: list[ChangeOrderLineIn] = Field(default_factory=list)


def _serialize(
    co: ChangeOrder,
    lines: list[Any] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(co.id),
        "co_number": co.co_number,
        "job_id": str(co.job_id) if co.job_id else None,
        "customer_id": str(co.customer_id) if co.customer_id else None,
        "customer_name": co.customer_name,
        "title": co.title,
        "description": co.description,
        "reason": co.reason,
        "status": co.status,
        "amount": float(co.amount or 0),
        "billed_invoice_id": str(co.billed_invoice_id) if co.billed_invoice_id else None,
        "approved_by": co.approved_by,
        "approved_at": co.approved_at.isoformat() if co.approved_at else None,
        "signature_url": co.signature_url,
        "created_by": co.created_by,
        "created_at": co.created_at.isoformat() if co.created_at else None,
        "updated_at": co.updated_at.isoformat() if co.updated_at else None,
    }
    if lines is not None:
        out["line_items"] = [
            {
                "id": str(ln.id),
                "description": ln.description,
                "quantity": int(recorded_quantity(ln.qty)),
                "unit_price": float(ln.unit_price or 0),
                "line_total": float(ln.line_total or 0),
                "taxable": bool(getattr(ln, "taxable", True)),
            }
            for ln in lines
        ]
        # PR3-billing-capture (Doug 2026-07-07): "handled like an invoice —
        # tax shown so the customer sees it." Same resolver as invoices.
        # Tax failure degrades to rate 0 with a LOUD log, never a 500 — the
        # approval screen must render even if tax config is broken.
        subtotal = sum(float(ln.line_total or 0) for ln in lines)
        tax_rate = 0.0
        if co.tax_rate is not None and co.status in _SIGNED_STATUSES:
            # GDXA-795: a signed CO shows the rate stamped when it was approved.
            tax_rate = float(co.tax_rate)
        elif db is not None:
            # Unsigned: the rate approval would stamp now, so a draft never
            # shows a rate its job or customer has since moved off (an
            # installation is untaxed the moment the job becomes one). A signed
            # pre-124 row (NULL) keeps the resolve it always had, which never
            # read the estimate, so no signed number moves.
            #
            # GDXA-157: the resolve reads on this session and re-raises, so the
            # degradation to rate 0 is this frame's; `_current_rate` holds it
            # in a savepoint. Only `get_change_order` passes `db`, a read-only
            # GET, but this is the shared serializer for a money surface: the
            # next caller to hand it a session with staged work would
            # otherwise inherit an aborted Postgres transaction (25P02).
            try:
                tax_rate = float(_current_rate(db, co, with_quote=co.status not in _SIGNED_STATUSES))
            except Exception:
                log.exception("change_order_tax_resolve_failed co=%s", co.id)
                if co.tax_rate is not None:
                    tax_rate = float(co.tax_rate)
        taxable_subtotal = sum(
            float(ln.line_total or 0)
            for ln in lines
            if bool(getattr(ln, "taxable", True))
        )
        tax_amount = round(taxable_subtotal * tax_rate, 2)
        out["subtotal"] = round(subtotal, 2)
        out["tax_rate"] = tax_rate
        out["tax_amount"] = tax_amount
        out["total"] = round(subtotal + tax_amount, 2)
    return out


# What a change-order PATCH can move, for its audit row's old/new pairs.
_CO_AUDIT_FIELDS = (
    "title", "description", "reason", "customer_name", "job_id", "customer_id", "amount", "status", "tax_rate",
)

# A CO in one of these has been signed by the customer: its job and customer
# are fixed until it is reopened (GDXA-795, maintainer ruling 2026-10-15).
_SIGNED_STATUSES = ("approved", "completed")


def _audit_status_change(
    db: Session,
    actor: dict,
    co: ChangeOrder,
    old_status: str,
    action: str,
    extra: dict[str, Any] | None = None,
) -> None:
    audit_best_effort(
        db,
        actor=actor,
        action=action,
        entity_type="change_order",
        entity_id=str(co.id),
        details={
            "co_number": co.co_number,
            "status": {"old": old_status, "new": co.status},
            "amount": co.amount,
            **(extra or {}),
        },
    )


def _current_rate(db: Session, co: ChangeOrder, *, with_quote: bool = True) -> Decimal:
    """The rate this CO quotes now (GDXA-795), through `resolve_rate_for_job`,
    the helper invoice create and `/api/tax/resolve` answer with: install lane
    0, exempt customer 0, else the quote, else the tenant default. The quote is
    the job's accepted estimate the office invoice form prefills from — the
    newest created, as `GET /api/estimates?job_id=` lists them. `with_quote`
    False is the pre-124 resolve, which never read the estimate.

    Read in a savepoint, so a failed read raises without aborting the
    caller's transaction."""
    from gdx_dispatch.models.tenant_models import Job  # noqa: PLC0415
    from gdx_dispatch.modules.proposals.models import Estimate  # noqa: PLC0415
    from gdx_dispatch.modules.tax.service import resolve_rate_for_job  # noqa: PLC0415

    with contained_read(db):
        job_row = quoted = None
        if co.job_id:
            job_row = db.execute(
                select(Job.job_type, Job.customer_id).where(
                    Job.id == co.job_id, Job.deleted_at.is_(None)
                )
            ).one_or_none()
            if with_quote:
                quoted = db.execute(
                    select(Estimate.tax_rate)
                    .where(
                        Estimate.job_id == co.job_id,
                        Estimate.status == "accepted",
                        Estimate.deleted_at.is_(None),
                    )
                    .order_by(Estimate.created_at.desc(), Estimate.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
        # A tech's CO can be saved with no customer (the mobile dialog sends
        # `customer_id: null` before the customer loads); the invoice checks
        # exemption against the job's customer, so this does too.
        customer_id = co.customer_id or (job_row.customer_id if job_row else None)
        return resolve_rate_for_job(
            db,
            customer_id,
            job_row.job_type if job_row else None,
            quoted_rate=Decimal(str(quoted)) if quoted is not None else None,
        )


def _stamp_tax_rate(db: Session, co: ChangeOrder) -> None:
    """Stamp `_current_rate` on the CO: on create and a re-link (the record of
    what it quoted then) and on approval, which is the rate it is signed at.
    It is the rate the office invoice form seeds when the CO is billed at
    once; billing reads the form, not this stamp, so a default, lane or
    exemption change between approval and billing still moves the invoice
    (parity at creation is enough: the maintainer's ruling on GDXA-795).

    A failed read keeps the prior stamp with a loud log (the maintainer's
    ruling): NULL on create; the previous rate on a re-link or approval. Never
    a lost CO."""
    try:
        rate = _current_rate(db, co)
        # Assigned outside the read-only savepoint, which warns on staged writes.
        co.tax_rate = rate
    except Exception:
        log.exception("change_order_tax_stamp_failed co=%s, keeping %s", co.id, co.tax_rate)


def _rate_detail(co: ChangeOrder) -> str | None:
    return str(co.tax_rate) if co.tax_rate is not None else None


def _next_co_number(db: Session) -> str:
    count = db.execute(select(ChangeOrder)).scalars().all()
    return f"CO-{len(count) + 1:05d}"


@router.get("/api/change-orders", response_model=None)
def list_change_orders(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    job_id: str | None = None,
    status: str | None = None,
    unbilled: bool = False,
) -> list[dict[str, Any]]:
    stmt = select(ChangeOrder).where(ChangeOrder.deleted_at.is_(None))
    if job_id:
        with contextlib.suppress(ValueError):
            stmt = stmt.where(ChangeOrder.job_id == UUID(job_id))
    if status:
        stmt = stmt.where(ChangeOrder.status == status)
    if unbilled:
        # PR3-billing-capture: the invoice-create checklist — approved,
        # never billed. Mirrors GET parts-needed?unbilled=true (S122).
        stmt = stmt.where(
            ChangeOrder.status == "approved",
            ChangeOrder.billed_invoice_id.is_(None),
        )
    rows = db.execute(stmt.order_by(ChangeOrder.created_at.desc())).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/change-orders", response_model=None, status_code=201)
def create_change_order(
    payload: ChangeOrderIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.title.strip():
        raise HTTPException(status_code=422, detail="Title is required")
    # D-S122-change-orders-create-flow — when line_items are provided, sum
    # them and override the flat amount field. Operators can still send a
    # bare amount (legacy callers + edits-without-lines).
    computed_amount = (
        sum(float(li.unit_price) * int(li.quantity) for li in payload.line_items)
        if payload.line_items else float(payload.amount or 0)
    )
    co = ChangeOrder(
        co_number=_next_co_number(db),
        job_id=UUID(payload.job_id) if payload.job_id else None,
        customer_id=UUID(payload.customer_id) if payload.customer_id else None,
        customer_name=payload.customer_name,
        title=payload.title,
        description=payload.description,
        reason=payload.reason,
        status=payload.status if payload.status in CO_STATUSES else "draft",
        amount=Decimal(str(computed_amount)),
        # Who did it (#701): the login dict is {user_id, tenant_id, role} —
        # it never carries an email, so reading one stored NULL for every row.
        created_by=resolve_audit_actor(user),
    )
    db.add(co)
    db.flush()
    _stamp_tax_rate(db, co)
    # Insert line items in the same transaction. ChangeOrderLine is the
    # canonical store; the flat `amount` on ChangeOrder is the denormalized
    # subtotal for fast list rendering.
    if payload.line_items:
        from gdx_dispatch.models.tenant_models import ChangeOrderLine
        for li in payload.line_items:
            db.add(ChangeOrderLine(
                co_id=co.id,
                description=li.description,
                qty=li.quantity,
                unit_price=Decimal(str(li.unit_price)),
                line_total=Decimal(str(float(li.unit_price) * int(li.quantity))),
                taxable=bool(li.taxable),
            ))
    db.commit()
    db.refresh(co)
    audit_best_effort(
        db,
        actor=user,
        action="create_change_order",
        entity_type="change_order",
        entity_id=str(co.id),
        details={
            "co_number": co.co_number,
            "status": co.status,
            "amount": co.amount,
            "job_id": co.job_id,
            "customer_id": co.customer_id,
            "line_count": len(payload.line_items),
            "tax_rate": _rate_detail(co),
        },
    )
    return _serialize(co)


@router.get("/api/change-orders/{co_id}", response_model=None)
def get_change_order(co_id: UUID, _: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    # D-S122-change-orders-create-flow — return line_items on detail view.
    # PR3-billing-capture: pass db so the detail/approval view carries
    # subtotal/tax/total (COs are handled like invoices).
    from gdx_dispatch.models.tenant_models import ChangeOrderLine
    lines = db.execute(
        select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id)
    ).scalars().all()
    return _serialize(co, lines=lines, db=db)


@router.patch("/api/change-orders/{co_id}", response_model=None)
def update_change_order(
    co_id: UUID,
    payload: ChangeOrderIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    # PR3 audit round 2: a billed CO is frozen — editing lines/amount after
    # billing silently diverges the invoice from the signed CO, and the
    # delta can never bill (the stamp holds). Release it first by deleting
    # the draft invoice that owns it.
    if co.billed_invoice_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{co.co_number} is billed on an invoice — delete that draft invoice to release it before editing.",
        )
    before = {field: getattr(co, field) for field in _CO_AUDIT_FIELDS}
    old_status = co.status
    # Tier-6 (2026-07): the edit dialog sends job_id/customer_id but only
    # customer_name was applied — display and storage silently split (the CO
    # showed the new customer's NAME while still linked to the old records).
    relink: dict[str, UUID | None] = {}
    for field in ("job_id", "customer_id"):
        val = getattr(payload, field, None)
        if val is not None:
            if str(val).strip():
                try:
                    relink[field] = UUID(str(val))
                except ValueError:
                    raise HTTPException(status_code=422, detail=f"{field} must be a UUID") from None
            else:
                relink[field] = None
    moved = any(getattr(co, field) != val for field, val in relink.items())
    # GDXA-795: a signed CO's total was quoted at its job's and customer's
    # rate; moving it would re-quote a total the customer already signed.
    # Checked against the stored status, so reopening is a save of its own.
    if moved and old_status in _SIGNED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{co.co_number} is {old_status} — the customer signed it at this job's tax rate. "
                "Set it back to draft or pending approval and save before moving it to another job or customer."
            ),
        )
    for field in ("title", "description", "reason", "customer_name"):
        val = getattr(payload, field, None)
        if val is not None:
            setattr(co, field, val)
    for field, val in relink.items():
        setattr(co, field, val)
    # D-S122-change-orders-create-flow auditor catch: PATCH was silently
    # dropping line_items, so the frontend's edit-with-lines flow blanked
    # the line set + set amount=0. Now we replace ChangeOrderLine rows in
    # the same transaction: delete the old set, write the new one, and
    # recompute amount from the new subtotal.
    from gdx_dispatch.models.tenant_models import ChangeOrderLine
    replaced_lines = None
    if payload.line_items:
        # The rows are hard-deleted below (no deleted_at), so the audit row is
        # the only record of what the customer had approved before (GDXA-955).
        replaced_lines = [
            {"description": ln.description, "qty": ln.qty, "unit_price": ln.unit_price,
             "line_total": ln.line_total, "taxable": ln.taxable}
            for ln in db.execute(select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id)).scalars()
        ]
        # Replace strategy: delete existing rows, insert the new set. Safe
        # because ChangeOrderLine has no FK references TO it (consumer is
        # only ChangeOrder itself).
        db.execute(
            ChangeOrderLine.__table__.delete().where(ChangeOrderLine.co_id == co.id)
        )
        for li in payload.line_items:
            db.add(ChangeOrderLine(
                co_id=co.id,
                description=li.description,
                qty=li.quantity,
                unit_price=Decimal(str(li.unit_price)),
                line_total=Decimal(str(float(li.unit_price) * int(li.quantity))),
                taxable=bool(li.taxable),
            ))
        co.amount = Decimal(str(
            sum(float(li.unit_price) * int(li.quantity) for li in payload.line_items)
        ))
    elif payload.amount is not None:
        # M25: a bare-amount PATCH on a CO that HAS lines left the stale
        # rows standing — and billing follows the LINES, so $500 billed
        # where $700 was approved. Amount and lines must agree; the office
        # edits the lines (or clears them), never the denormalized subtotal.
        from gdx_dispatch.models.tenant_models import ChangeOrderLine as _COL
        _lines = db.execute(
            _COL.__table__.select().where(_COL.co_id == co.id)
        ).fetchall()
        _lines_sum = float(sum((r.line_total or 0) for r in _lines))
        # Audit round 2 (replacement): the flatWins flow M31/M33 MANDATED —
        # all-$0 descriptive lines with the money on the flat amount — sends
        # line_items: [] (indistinguishable from "not sent"; the field
        # defaults to []). Zero-sum lines carry no money, so a bare amount
        # cannot diverge from them: exempt. The 409 guards only the real
        # divergence — stored lines that CARRY money disagreeing with a
        # bare amount (billing follows the lines).
        if _lines and _lines_sum != 0 and abs(_lines_sum - float(payload.amount)) > 0.005:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{co.co_number} has {len(_lines)} line item(s) totalling "
                    f"{_lines_sum:.2f} — billing follows the lines, so edit or "
                    "clear the lines instead of the flat amount"
                ),
            )
        co.amount = Decimal(str(payload.amount))
    if payload.status in CO_STATUSES:
        co.status = payload.status
    # GDXA-795: a CO moved to another job or customer quotes that one's rate,
    # and one signed here (the edit dialog's status select reaches approved
    # or completed straight from a draft) signs at the rate in force now, the
    # same as POST /approve.
    if moved or (co.status in _SIGNED_STATUSES and old_status not in _SIGNED_STATUSES):
        db.flush()
        _stamp_tax_rate(db, co)
    db.commit()
    db.refresh(co)
    audit_best_effort(
        db,
        actor=current_user,
        action="update_change_order",
        entity_type="change_order",
        entity_id=str(co_id),
        details={
            "co_number": co.co_number,
            "changed": audit_field_changes(before, co),
            "replaced_lines": replaced_lines,
        },
    )
    return _serialize(co)


@router.post("/api/change-orders/{co_id}/approve", response_model=None)
def approve_change_order(
    co_id: UUID,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    old_status = co.status
    rate_before = _rate_detail(co)
    co.status = "approved"
    co.approved_by = resolve_audit_actor(user)  # the approver's id (#701), not an email the login never carries
    co.approved_at = utcnow()
    # GDXA-795: the customer signs at the rate in force at approval, not the
    # one stamped when the draft was written (maintainer ruling 2026-10-15).
    # A CO already signed keeps its rate; a billed one is always approved.
    if old_status not in _SIGNED_STATUSES:
        db.flush()
        _stamp_tax_rate(db, co)
    db.commit()
    db.refresh(co)
    _audit_status_change(
        db, user, co, old_status, "approve_change_order",
        extra={"tax_rate": {"old": rate_before, "new": _rate_detail(co)}},
    )
    return _serialize(co)


@router.post("/api/change-orders/{co_id}/decline", response_model=None)
def decline_change_order(co_id: UUID, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    old_status = co.status
    co.status = "declined"
    db.commit()
    db.refresh(co)
    _audit_status_change(db, current_user, co, old_status, "decline_change_order")
    return _serialize(co)


@router.delete("/api/change-orders/{co_id}", response_model=None, status_code=204)
def delete_change_order(co_id: UUID, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    # PR3 audit round 2: same freeze as PATCH — deleting a billed CO would
    # orphan the invoice lines' provenance.
    if co.billed_invoice_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{co.co_number} is billed on an invoice — delete that draft invoice to release it first.",
        )
    co.deleted_at = utcnow()
    db.commit()
    audit_best_effort(
        db,
        actor=current_user,
        action="delete_change_order",
        entity_type="change_order",
        entity_id=str(co_id),
        details={"co_number": co.co_number, "status": co.status, "amount": co.amount},
    )
    return None
