"""Sprint 1.x — phone_com tenant-plane data tables.

Six tables on the per-tenant DB:

- ``phone_com_calls``       — every inbound/outbound call
- ``phone_com_messages``    — every SMS send/receive
- ``phone_com_voicemails``  — voicemail with audio + transcript
- ``phone_com_extensions``  — cached extension catalog (refreshed nightly)
- ``phone_com_numbers``     — cached DID catalog
- ``phone_com_stats_daily`` — rolled-up daily metrics for the dashboard

No ``tenant_id`` columns — isolation is by connection (per-tenant DB).
FKs to ``customers``/``jobs``/``users`` use string refs (no Python
imports — keeps the module decoupled from ``tenant_models.py`` at
import time).
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

Base = TenantBase


class PhoneComCall(Base):
    __tablename__ = "phone_com_calls"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    phone_com_call_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # in / out
    from_number: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    to_number: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)  # completed / missed / voicemail / busy / ...
    # Wave F / S11: structured "what happened next" target. Phone.com's raw
    # final_action mixes shape + value (e.g. "dial_out +13202325143") which
    # leaked tech mobile numbers into the status string. We now strip the
    # number/extension into this column and keep status as a clean enum.
    final_action_target: Mapped[str | None] = mapped_column(String(80), nullable=True)
    extension_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class PhoneComMessage(Base):
    __tablename__ = "phone_com_messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    phone_com_message_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    # P2.9 — Phone.com's conversation_id. Lets us mark-read on the upstream
    # so the desk phone / mobile app see the same read state GDX does.
    # Null until we backfill or until the next webhook delivers it.
    phone_com_conversation_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    # Canonical "conversation key" — sorted E.164 pair so inbound and outbound
    # for the same other-party group together regardless of from/to direction.
    thread_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    from_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    delivery_failed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    media_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sent_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class PhoneComVoicemail(Base):
    __tablename__ = "phone_com_voicemails"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    phone_com_voicemail_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    call_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("phone_com_calls.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    extension_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # phone_com / ai / null
    heard_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heard_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PhoneComExtension(Base):
    __tablename__ = "phone_com_extensions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    phone_com_extension_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 2026-04-29 / UX audit F-55 — per-tech outbound DID override.
    # When tenant.phone_com_outbound_strategy is "tech_override" or
    # "priority_chain", the resolver checks this column first.
    preferred_outbound_did: Mapped[str | None] = mapped_column(String(40), nullable=True)


class PhoneComNumber(Base):
    __tablename__ = "phone_com_numbers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    phone_com_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_default_outbound: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 2026-04-29 / UX audit F-55 — marketing attribution. Tenants with
    # multiple DIDs assign each to a campaign ("Google Ads", "Facebook",
    # "Yard Sign Spring 2026") so inbound calls can be tallied by source.
    campaign_tag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PhoneComFax(Base):
    """P2.7 — inbound + outbound fax records.

    Faxes are still load-bearing for garage-door companies (permits,
    distributor invoices, COIs). Phone.com's fax API is a first-class
    resource — we mirror what they store and proxy the binary download.
    """
    __tablename__ = "phone_com_faxes"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    phone_com_fax_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # in / out
    from_number: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    to_number: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    pdf_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    heard_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heard_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
    )


class PhoneComContactPush(Base):
    """P2.8 — log of GDX customers pushed to Phone.com as contacts.

    One row per (customer_id, phone_e164) pair. Used by ``push_contacts``
    to know which customers/numbers we've already synced and skip them on
    the next run. Phone.com's contact id (``phone_com_contact_id``) is
    stored so we can ``PATCH`` the contact when the customer's name
    changes.
    """
    __tablename__ = "phone_com_contact_push"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phone_e164: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    phone_com_contact_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    last_pushed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_pushed: Mapped[str | None] = mapped_column(String(200), nullable=True)


class PhoneComStatsDaily(Base):
    __tablename__ = "phone_com_stats_daily"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    stat_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    calls_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calls_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calls_missed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sms_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sms_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    voicemails_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_call_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
