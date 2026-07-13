from datetime import datetime
from uuid import UUID, uuid4

from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class Estimate(TenantBase):
    __tablename__ = "estimates"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=True)
    estimate_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=True)
    jobsite_address: Mapped[str] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    # Per-estimate tax rate / discount overrides. Both NULL → estimate uses
    # tenant-wide default tax rate (Settings → Tax). tax_rate is stored as a
    # decimal (0.0825 = 8.25%); discount is a flat dollar amount.
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=True)
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=True)
    proposal_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "Total-only" display override (migration 019). NULLABLE tri-state:
    # NULL = inherit tenant default (tenant_settings.estimates_hide_line_prices);
    # True = hide per-line prices on the customer PDF/email (show only the bottom
    # line Total); False = force show. Mirrors the tax_rate/discount override
    # pattern above (NULL = tenant default). Purely presentational — never changes
    # the computed total.
    hide_line_prices: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        Enum("draft", "sent", "accepted", "declined", "rejected", "expired", name="estimate_status"),
        nullable=False,
        default="draft",
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    declined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    declined_reason: Mapped[str] = mapped_column(Text, nullable=True)
    accepted_tier_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("proposal_tiers.id"), nullable=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Sprint 2 S2-A4 — customer-on-truck quote-acceptance signature.
    # Captured on the tech's phone when the customer signs to accept.
    # Independent of Job.signature_data (job completion sig) and
    # DocumentSignature (tokenized off-device sig flow).
    signature_data: Mapped[str] = mapped_column(Text, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(200), nullable=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)  # tenant isolation: CLAUDE.md Build Rule 5
    reminder_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    public_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    lines: Mapped[list["EstimateLine"]] = relationship(
        back_populates="estimate",
        cascade="all, delete-orphan",
    )


class ProposalTier(TenantBase):
    __tablename__ = "proposal_tiers"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    estimate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("estimates.id"), nullable=False)
    tier_name: Mapped[str] = mapped_column(Enum("good", "better", "best", name="proposal_tier_name"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    total_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    includes_parts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    warranty_months: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stripe_payment_link: Mapped[str] = mapped_column(String(500), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EstimateLine(TenantBase):
    __tablename__ = "estimate_lines"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    estimate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("estimates.id"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Sprint 1.0.5 — pricing engine snapshot. Frozen at line-create so admin
    # tier edits never silently re-price old estimates. cost_snapshot is the
    # cost-at-time-of-create; margin_pct_snapshot is what the engine resolved
    # to for this line; margin_pct_override is operator-edited per-line beats
    # everything; pricing_source records which input won (tier|customer_override
    # |wholesale_tier|line_override).
    cost_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=True)
    margin_pct_snapshot: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=True)
    margin_pct_override: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=True)
    pricing_source: Mapped[str] = mapped_column(String(32), nullable=True)
    # S97 slice 4 — labor matrix link + man-hours snapshot.
    # FK to tenant-plane labor_price_items (the row picked from the matrix).
    # Nullable: lines can still be free-form non-labor rows. ON DELETE SET NULL
    # so archiving a matrix row never breaks historical estimates — the snapshot
    # values on this line stay intact.
    labor_price_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("labor_price_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    estimated_man_hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=True)
    # PLUGIN INTEGRATION POINT (ADR-013) — DO NOT REMOVE even if it looks unused
    # in core. A plugin (e.g. the operator-installed CHI pricing plugin) writes the
    # full captured source spec here when it adds a line: door specs, install
    # detail, receiving/load + weight, and source ids. It is deliberately generic
    # (any line may carry source metadata) and persists in CORE so it survives the
    # estimate→Job conversion (via estimate.job_id) and is readable downstream by
    # techs / receiving / order tracking — independent of whether the plugin that
    # wrote it is still installed.
    line_metadata: Mapped[dict] = mapped_column(JSON, nullable=True)
    # -- columns from production schema not yet in ORM --
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)

    estimate: Mapped[Estimate] = relationship(back_populates="lines")
