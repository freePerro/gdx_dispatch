"""Vendor order models — supplier order confirmations.

An order confirmation is what the supplier sends when an order is PLACED, and
it is deliberately not a bill: the document itself says the pricing is a quote
valid 30 days with shipping and tax estimated. Every amount here is therefore
``estimated_*``, and nothing downstream may add one to a payable balance.

Its value is the front of the chain. Verified against real mail: **the supplier's
order number becomes the invoice number**, so one key threads

    order confirmation  →  vendor invoice  →  statement line

and an order with no invoice yet is committed spend that is otherwise invisible
until a bill turns up.

Tenant plane — db-per-tenant, no ``tenant_id`` columns. Created via
``create_orm_tables()`` like its ``vendor_statements`` / ``vendor_invoices``
siblings; new tables appear on boot, so no migration is needed for these two
(unlike an added COLUMN, which does need one — see migration 039).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class VendorOrder(TenantBase):
    """One supplier order confirmation."""

    __tablename__ = "vendor_orders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Our customer number with the supplier. Two codes are two accounts.
    vendor_code: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # The supplier's order number — and later, their invoice number. This is
    # the join key for the whole lifecycle, so it is indexed and deduped on.
    order_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    order_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    salesperson: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Where it ships — the jobsite, which is NOT the bill-to.
    ship_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    terms: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Free-text reference the office gives the supplier at order time. Today it
    # holds customer names and dates; it is the field a GDX job number or CHI
    # quote number would go in to make the job link automatic.
    customer_po: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Two more free fields the supplier offers, empty on every confirmation seen
    # so far. Captured so the link works the day the office starts using one.
    lot_no: Mapped[str | None] = mapped_column(String(120), nullable=True)
    called_in_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # ESTIMATED — see the module docstring. Never a payable.
    estimated_subtotal: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    estimated_shipping: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    estimated_tax: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    estimated_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"))

    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id"), nullable=True, index=True
    )

    parser_name: Mapped[str] = mapped_column(String(60), nullable=False)
    parser_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Set only by a human confirming a suggestion (modules/vendor_orders/
    # confirm.py). Matching suggests; it never writes here. No migration needed:
    # this table is created fresh by create_orm_tables() and has never deployed.
    matched_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True
    )
    job_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    job_confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="email", server_default="email")
    uploaded_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[VendorOrderLine]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="VendorOrderLine.line_no",
    )

    __table_args__ = (
        # The "what's on order" query: live orders for an account, newest first.
        Index("ix_vendor_orders_account", "vendor_name", "vendor_code", "order_date"),
    )


class VendorOrderLine(TenantBase):
    __tablename__ = "vendor_order_lines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vendor_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The supplier bills everything as "Garage Door Material and Labor Pursuant
    # to Contract"; the real content is in `notes` — "CHI 4283 12x10 Black Long
    # Panel, LP INs Tempered windows...". That is the door spec, and the only
    # place the model/size/colour appears on any supplier document.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    unit: Mapped[str | None] = mapped_column(String(12), nullable=True)
    unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("0.00"))
    line_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)

    order: Mapped[VendorOrder] = relationship(back_populates="lines")


class HubxDoorOrder(TenantBase):
    """One door the office ORDERED from the manufacturer through HubX.

    HubX (the manufacturer's dealer order portal) emails an "Order submission"
    every time the office submits a cart. Each item in it carries the quote
    number (``QCD…``) — the same number the captured door spec stores on the
    estimate line (``line_metadata["Number"]``). That shared number is what
    lets the order flip the job's door from "needed" to "ordered" without a
    human (modules/vendor_orders/hubx.py).

    One row per QCD, ever: this is both the record of what the email said and
    the idempotency key. A door the office later sets BACK to "needed" (a
    cancelled order) must not be flipped again by the next mail sync re-reading
    the same message — the row existing is what prevents that.

    ``outcome`` records what the ingest did with it, so "why didn't my door move?"
    is answerable from the records: applied · no_estimate · ambiguous ·
    job_closed · nothing_needed.

    Tenant plane, created by ``create_orm_tables()`` at container start like its
    siblings in this module — a new table needs no migration.
    """

    __tablename__ = "hubx_door_orders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    qcd: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)

    # What the email said about the item.
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_po_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    size: Mapped[str | None] = mapped_column(String(60), nullable=True)
    submitted_text: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # Where it came from — the mirrored Outlook message.
    graph_message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # What the ingest did with it.
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    matched_estimate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    matched_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True
    )
    parts_marked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moved_from_area: Mapped[str | None] = mapped_column(String(100), nullable=True)
    moved_to_area: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)


class HubxOrderEmail(TenantBase):
    """One HubX submission email, read once.

    Keyed on the RFC 822 ``Message-ID`` (``internet_message_id``), not the
    Graph id: Graph ids change when a message moves folders, and the same mail
    mirrored into two mailboxes has two Graph ids but one Message-ID. Every
    fetched message gets a row — including one whose doors were all recorded
    already, or that parsed to no items — so no message is ever fetched twice
    (audit 2026-09-29: without this, such messages were re-fetched on every
    sync and could fill the per-sync cap, starving new orders).

    ``item_count`` of 0 means the body held no QCD: a format change shows up
    here as a run of zero rows rather than as silence.
    """

    __tablename__ = "hubx_order_emails"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    message_key: Mapped[str] = mapped_column(String(998), nullable=False, unique=True)
    graph_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
