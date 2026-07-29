"""Door listing models.

A `DoorListing` is one door we are trying to sell — a used door pulled off a
tear-out, new stock sitting on the shelf, or a quick-ship door we can source at
a discount. Each carries its own photos, marketing copy, and price, and is
published to garagedoorxperts.com once the office approves it.

Deliberately NOT an extension of `InventoryItem`: that model is a stocked SKU
with a reorder point and a `StockAdjustment` audit trail. A used door is a
single physical unit sold once, and overloading the inventory model would drag
PO receiving and stock-delta locking into a page whose only job is to sell one
door.

Tenant plane — db-per-tenant, no `company_id` columns (isolation is the
connection). Follows `modules/vendor_statements/models.py`.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

# ── Vocabularies ─────────────────────────────────────────────────────────────
# Kept as plain tuples (not DB enums) so adding a type is a code change, not a
# migration — the house pattern (see LEAD_STAGES in routers/leads.py).

LISTING_TYPES = ("used", "in_stock", "quick_ship")
LISTING_CONDITIONS = ("new", "like_new", "good", "fair")
PRICE_DISPLAYS = ("fixed", "call_for_price")
LISTING_SOURCES = ("office", "tech", "customer")

#: Every state a listing can hold. Only `published` is publicly visible.
LISTING_STATUSES = (
    "draft",           # office is still writing it
    "pending_review",  # a tech or customer submitted it; office hasn't ruled
    "published",       # live on garagedoorxperts.com
    "rejected",        # office said no; `rejection_reason` says why
    "sold",            # kept for history, gone from the site
    "archived",        # withdrawn without selling
)

#: Statuses a submitter may still edit their own row in. Once published, the
#: listing belongs to the office.
SUBMITTER_EDITABLE_STATUSES = ("pending_review", "rejected")


class DoorListing(TenantBase):
    __tablename__ = "door_listings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    listing_type: Mapped[str] = mapped_column(String(20), nullable=False, default="used")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # UNIQUE, not just indexed: unique_slug() is a read-then-write with no
    # lock, so two concurrent creates can pick the same slug. The constraint
    # is what actually keeps the public per-door URL unambiguous.
    slug: Mapped[str] = mapped_column(String(220), nullable=False, index=True, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft", index=True
    )
    condition: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Doors are sold by opening size; inches keeps 16'2" honest without a
    # feet+inches pair to keep in sync.
    width_in: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    height_in: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)

    material: Mapped[str | None] = mapped_column(String(60), nullable=True)
    color: Mapped[str | None] = mapped_column(String(60), nullable=True)
    insulation_r: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    has_windows: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    brand: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    #: Office pin — floats a door to the top of its ownership group (§7.1).
    featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    #: "was" price, for showing a discount on a quick-ship door.
    compare_at_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    #: `call_for_price` when a number isn't one we'd want to defend publicly.
    price_display: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fixed", server_default="fixed"
    )

    # ── Provenance / moderation ──────────────────────────────────────────────
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="office", server_default="office", index=True
    )
    # Plain UUID columns rather than ForeignKeys: these point at tables owned by
    # other modules, and a hard FK would couple create_all ordering (and the
    # raw-SQL migration) to them for no integrity we actually enforce here.
    submitted_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_by_customer_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The tear-out this door came off, when there was one.
    source_job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    photos: Mapped[list[DoorListingPhoto]] = relationship(
        "DoorListingPhoto",
        back_populates="listing",
        cascade="all, delete-orphan",
        order_by="DoorListingPhoto.sort_order",
    )

    @property
    def is_gdx_owned(self) -> bool:
        """GDX stock ranks above consignment on the public page (§7.1)."""
        return self.source in ("office", "tech")


class DoorListingPhoto(TenantBase):
    __tablename__ = "door_listing_photos"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    listing_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("door_listings.id"), nullable=False, index=True
    )
    #: Basename only. The public route looks this up by photo UUID and never
    #: accepts a path component from the request — see routers/door_listings.py.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(80), nullable=False, default="image/jpeg", server_default="image/jpeg"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    listing: Mapped[DoorListing] = relationship("DoorListing", back_populates="photos")
