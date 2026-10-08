"""Quote request models.

A `QuoteRequest` is a customer asking, from the portal, for a quote on new
garage doors: one job name, any number of doors. It is the structured input a
later AI estimate builder reads, so it is the ONLY copy of the doors — the
Lead it opens names the job and points here, and does not restate them —
two copies of the doors would drift the moment staff edited one.

`doors` is a JSON list validated on the way in (`service.DoorIn`), not a child
table: the builder reads a request whole and the door questions will change.
`doors_version` says which shape a row holds.

Tenant plane — db-per-tenant, no `company_id` columns (isolation is the
connection). Follows `modules/door_listings/models.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

#: The `doors` shape this code writes. Bump it when a door question changes.
DOORS_VERSION = 1


class QuoteRequest(TenantBase):
    __tablename__ = "quote_requests"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # Plain UUID columns, not ForeignKeys — the door_listings rule: these point
    # at tables other modules own.
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    #: The portal `CustomerUser` who submitted it.
    submitted_by_user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: The Lead this request opened in the office pipeline.
    lead_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)

    job_name: Mapped[str] = mapped_column(String(200), nullable=False)
    site_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    doors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    doors_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DOORS_VERSION, server_default="1"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the customer last changed the request from the portal.
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the customer withdrew it. A withdrawn request stays visible to both
    #: sides; it is not deleted.
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    photos: Mapped[list[QuoteRequestPhoto]] = relationship(
        "QuoteRequestPhoto",
        back_populates="quote_request",
        order_by="QuoteRequestPhoto.sort_order",
    )


class QuoteRequestPhoto(TenantBase):
    __tablename__ = "quote_request_photos"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    quote_request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("quote_requests.id"), nullable=False, index=True
    )
    #: Position of the door in `QuoteRequest.doors` this photo shows.
    door_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Basename only; looked up by photo id, never a path from the request.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(80), nullable=False, default="image/jpeg", server_default="image/jpeg"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    #: Set when the customer removes the door this photo shows.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    quote_request: Mapped[QuoteRequest] = relationship("QuoteRequest", back_populates="photos")
