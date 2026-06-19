"""Pricing engine — tenant-plane models.

Single source of truth for cost→sell margin math. Replaces:
    - gdx_dispatch/routers/pricing.py in-memory tier/customer-rate state
    - gdx_dispatch/routers/door_catalog.py:186-212 hardcoded margin tiers
    - gdx_dispatch/models/tenant_models.py:MarkupRule (flat percent only, deprecated)

Three-axis lookup: pricing_category (doors/openers/parts/labor/other) ×
pricing_class (retail/contractor/wholesale) × cost tier [min, max).

Per CLAUDE.md three-plane isolation: tenant-plane = db-per-tenant,
isolation is the connection itself. NO tenant_id / company_id columns.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

# Canonical category labels. Stored as String so admin can extend per tenant
# without a schema change; constants below are the seed defaults.
PRICING_CATEGORIES = ("doors", "openers", "parts", "labor", "other")

# Pricing class enum — also used on Customer.customer_type. Lowercase for
# storage; UI capitalizes for display.
PricingClassEnum = Enum(
    "retail",
    "contractor",
    "wholesale",
    name="pricing_class",
)


class PricingTierSet(TenantBase):
    """One tier table per (category, pricing_class). Nine sets at seed:
    {doors,openers,parts,labor,other} × {retail,contractor,wholesale} = 15.
    """

    __tablename__ = "pricing_tier_sets"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    pricing_category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    pricing_class: Mapped[str] = mapped_column(PricingClassEnum, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    tiers: Mapped[list["MarginTier"]] = relationship(
        back_populates="tier_set",
        cascade="all, delete-orphan",
        order_by="MarginTier.sort_order",
    )

    __table_args__ = (
        UniqueConstraint(
            "pricing_category", "pricing_class", name="uq_pricing_tier_set_cat_class"
        ),
    )


class MarginTier(TenantBase):
    """One cost-range row inside a PricingTierSet.

    Range semantics: [cost_min, cost_max) — lower inclusive, upper exclusive.
    cost_max NULL = open-ended top tier.
    """

    __tablename__ = "margin_tiers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tier_set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pricing_tier_sets.id"), nullable=False, index=True
    )
    cost_min: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    cost_max: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=True)
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    tier_set: Mapped[PricingTierSet] = relationship(back_populates="tiers")


class PricingSettings(TenantBase):
    """Singleton per tenant — top-level pricing toggles."""

    __tablename__ = "pricing_settings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    volume_discount_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # 2026-05-05 — loaded technician cost per hour (wage + burden). Used to
    # derive cost_snapshot on labor-matrix-sourced estimate lines so labor
    # appears in the profit margin calculator. 0 means "labor is pure profit"
    # (lines still appear in the panel; admin sees 100% margin and knows to
    # configure). NOT null — silent-null drop is the bug this sprint fixes.
    loaded_labor_cost_per_hour: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    # 2026-05-07 — target blended sell rate per labor man-hour. Drives the
    # implied-rate sanity warning on the labor matrix admin (rows whose
    # flat_price / assumed_man_hours drifts >10% from this get a UI warning,
    # not a reject). Per Doug rule: GDX targets $100/hr blended; tenants set
    # their own. Default 100 so the warning is informative, not silent.
    target_labor_blended_rate_per_hour: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("100"), server_default="100"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    volume_tiers: Mapped[list["CustomerVolumeDiscountTier"]] = relationship(
        back_populates="settings",
        cascade="all, delete-orphan",
        order_by="CustomerVolumeDiscountTier.sort_order",
    )


class CustomerVolumeDiscountTier(TenantBase):
    """Per-customer rolling-12mo paid-volume discount tier (Sprint 1.0.6).

    Range semantics: [volume_min_12mo, volume_max_12mo) on the customer's
    cached_rolling_volume_paid_12mo. Cliff (Salesforce "Range" mode) — once
    a customer crosses a threshold, the full discount_pct applies to the
    estimate's sell subtotal. All pricing classes participate (wholesale
    customers doing real volume earn the discount on top of their already-
    lower wholesale margin — Doug 2026-04-25).

    Replaces the per-estimate-subtotal VolumeDiscountTier shipped in 1.0.5.
    Real customers think in account spend over a year, not single-job size.
    """

    __tablename__ = "customer_volume_discount_tiers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    settings_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pricing_settings.id"), nullable=False, index=True
    )
    volume_min_12mo: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    volume_max_12mo: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=True)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    settings: Mapped[PricingSettings] = relationship(back_populates="volume_tiers")


class PricingClassSettings(TenantBase):
    """Per-pricing-class toggles (Sprint 1.0.6).

    One row per enum value (retail/contractor/wholesale). Lets admin enable
    the rolling-volume discount for some classes and not others — e.g. on
    for wholesale customers doing real annual volume, off for one-off
    retail/contractor work where the loyalty signal is less meaningful.

    This is the second-level gate. The discount applies only when BOTH:
      - PricingSettings.volume_discount_enabled == True (master), AND
      - PricingClassSettings.rolling_volume_discount_enabled == True for
        the customer's resolved pricing_class.
    """

    __tablename__ = "pricing_class_settings"

    pricing_class: Mapped[str] = mapped_column(
        PricingClassEnum, primary_key=True
    )
    rolling_volume_discount_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Stub seeds — written into a fresh tenant DB at signup OR after pave.
# Numbers are placeholders. Doug edits via PricingView.vue on day one;
# real-world tier values live in the per-tenant DB, not in code.
# ---------------------------------------------------------------------------

# (cost_min, cost_max_or_None, margin_pct as Decimal in 0.0–1.0)
# NOTE: margin_pct is a true MARGIN (profit/sell), not markup. So sell = cost
# / (1 - margin_pct). Margin must be strictly < 1.0; 1.0 = infinite sell.
# Stub values are intentionally round so admin sees they're placeholders.
_STUB_TIERS_BY_CLASS: dict[str, list[tuple[Decimal, Decimal | None, Decimal]]] = {
    "retail": [
        (Decimal("0"), Decimal("100"), Decimal("0.6000")),  # 60%  → sell = 2.5× cost
        (Decimal("100"), Decimal("500"), Decimal("0.5000")),  # 50%  → sell = 2× cost
        (Decimal("500"), Decimal("2000"), Decimal("0.3500")),  # 35%
        (Decimal("2000"), None, Decimal("0.2500")),  # 25%
    ],
    "contractor": [
        (Decimal("0"), Decimal("500"), Decimal("0.3500")),  # 35%
        (Decimal("500"), Decimal("2000"), Decimal("0.2500")),  # 25%
        (Decimal("2000"), None, Decimal("0.2000")),  # 20%
    ],
    "wholesale": [
        (Decimal("0"), Decimal("1000"), Decimal("0.2000")),  # 20%
        (Decimal("1000"), None, Decimal("0.1500")),  # 15%
    ],
}


def seed_default_pricing(session) -> None:
    """Idempotent seed of default tier sets + settings into a tenant DB.

    Safe to call repeatedly — checks before inserting. Called from the
    tenant signup flow and from pave_tenant_db.py post-create_all().
    """
    # Settings singleton
    existing_settings = session.query(PricingSettings).first()
    if existing_settings is None:
        session.add(PricingSettings(volume_discount_enabled=False))

    # Per-class toggles — one row per enum value, default enabled. Admin
    # decides which classes participate in the rolling-volume discount.
    for pricing_class in ("retail", "contractor", "wholesale"):
        existing = (
            session.query(PricingClassSettings)
            .filter_by(pricing_class=pricing_class)
            .first()
        )
        if existing is None:
            session.add(PricingClassSettings(
                pricing_class=pricing_class,
                rolling_volume_discount_enabled=True,
            ))

    # Tier sets — one per (category × class) if missing
    for category in PRICING_CATEGORIES:
        for pricing_class, tier_rows in _STUB_TIERS_BY_CLASS.items():
            existing = (
                session.query(PricingTierSet)
                .filter_by(pricing_category=category, pricing_class=pricing_class)
                .first()
            )
            if existing is not None:
                continue
            tier_set = PricingTierSet(
                pricing_category=category,
                pricing_class=pricing_class,
                active=True,
            )
            session.add(tier_set)
            session.flush()  # get tier_set.id
            for sort_idx, (cmin, cmax, margin) in enumerate(tier_rows):
                session.add(
                    MarginTier(
                        tier_set_id=tier_set.id,
                        cost_min=cmin,
                        cost_max=cmax,
                        margin_pct=margin,
                        sort_order=sort_idx,
                    )
                )
    session.commit()
