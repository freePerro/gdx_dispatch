"""Sprint Outlook Integration — tenant-plane SQLAlchemy models.

5 tables on TenantBase.metadata:

- outlook_accounts        per-user OAuth + mailbox metadata
- outlook_messages        message metadata + tagging (body lives on R2)
- outlook_attachments     attachment metadata (blob on R2)
- outlook_subscriptions   Microsoft Graph webhook subscription state
- email_settings        singleton per-tenant config (visibility/tagging rules)

Tenant plane: connection isolation. No tenant_id/company_id columns on any
of these — that's the three-plane invariant for tenant-plane models.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class OutlookAccount(TenantBase):
    """One row per (user, mailbox).

    user_id stores the tenant-plane User.id which is `String(36)` per GDX
    convention. No ForeignKey — matches the rest of tenant_models.py (e.g.
    Job.assigned_to, audit logs) which avoid FK-to-users to dodge PG's
    `text = uuid` operator error class.
    """
    __tablename__ = "outlook_accounts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="outlook")
    upn: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delta_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class OutlookMessage(TenantBase):
    """Email metadata + tagging + threading. Body persists to R2 keyed by body_r2_key."""
    __tablename__ = "outlook_messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("outlook_accounts.id"), nullable=False, index=True)
    graph_message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_address: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    to_addresses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cc_addresses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    bcc_addresses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False, default="inbound")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_r2_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linked_customer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=True, index=True)
    linked_job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True)
    tag_strategy: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tag_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    is_personal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(998), nullable=True)
    folder_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    folder_display_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "graph_message_id", name="uq_email_account_graph_id"),
    )


class OutlookAttachment(TenantBase):
    """Attachment metadata. Blob persists to R2 keyed by r2_key."""
    __tablename__ = "outlook_attachments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("outlook_messages.id"), nullable=False, index=True)
    graph_attachment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    r2_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class OutlookSubscription(TenantBase):
    """Microsoft Graph webhook subscription state. Renewed by cron (slice S16)."""
    __tablename__ = "outlook_subscriptions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("outlook_accounts.id"), nullable=False, unique=True)
    graph_subscription_id: Mapped[str] = mapped_column(String(255), nullable=False)
    notification_url: Mapped[str] = mapped_column(Text, nullable=False)
    client_state: Mapped[str] = mapped_column(String(128), nullable=False)
    expiration_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class OutlookFolder(TenantBase):
    """Cached mailbox folder list — Microsoft Graph mailFolder snapshot.

    One row per (account_id, graph_folder_id). Refreshed by sync; UI reads
    from this table for fast folder-rail rendering. Source of truth is
    Microsoft Graph; we never mutate folders here directly.
    """
    __tablename__ = "outlook_folders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("outlook_accounts.id"), nullable=False, index=True)
    graph_folder_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    parent_folder_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    well_known_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    child_folder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "graph_folder_id", name="uq_outlook_folder_account_graph_id"),
    )


class OutlookFolderSyncState(TenantBase):
    """Per-folder delta sync metadata. Folder delta is folder-scoped; each
    folder has its own delta_token. 410 Gone (token expired) → drop and
    full-resync that folder.

    Since 2026-07, ``delta_token`` stores the FULL @odata.deltaLink URL
    (replayed verbatim per the Graph contract, preserving the encoded
    $select). Legacy rows may still hold a bare token; the sync accepts
    both and upgrades to a URL after one cycle."""
    __tablename__ = "outlook_folder_sync_state"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("outlook_accounts.id"), nullable=False, index=True)
    folder_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    delta_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_resync_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "folder_id", name="uq_outlook_folder_sync_state_account_folder"),
    )


class OutlookFolderPrefs(TenantBase):
    """Per-user folder UI preferences (color/icon/pin/sort_order).

    Microsoft Graph has NO native folder color — these live entirely in
    GDX. Keyed by graph_folder_id (Graph IDs are immutable across rename
    + parent-move). Cascade-deleted by folder-deletion handler in sync.
    """
    __tablename__ = "outlook_folder_prefs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("outlook_accounts.id"), nullable=False, index=True)
    folder_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "folder_id", name="uq_outlook_folder_prefs_account_folder"),
    )


class OutlookSettings(TenantBase):
    """Per-tenant email integration configuration. Singleton: one row, id=1.

    Visibility / tagging / automation rules live in JSON columns for forward-
    compat — adding a new rule is a Settings-UI change, not a schema migration.
    """
    __tablename__ = "outlook_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    backfill_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    tag_strategy_order: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: ["auto_match", "job_thread", "ai"])
    tag_strategy_enabled: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {"auto_match": True, "job_thread": True, "ai": True})
    ai_tag_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, default=Decimal("0.85"))
    visibility_rules: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {
        "tagged_visibility_above_role": "tech_plus_one",
        "tech_recipient_visible_to_all_techs": True,
        "tech_outbound_no_tag_visibility": "only_sender",
        "tech_to_tech_internal_visibility": "only_participants",
        "above_tech_scope": "all_tagged",
        "untagged_visibility": "only_owner",
    })
    auto_email_triggers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # vendor-invoice-intake Phase 2: sender addresses/domains whose PDF
    # attachments are auto-ingested into the vendor-bills review queue during
    # the Outlook delta sync. EMPTY = feature off (nothing auto-ingests) —
    # Doug opts in per tenant by listing the supplier's From address or domain
    # (e.g. "billing@midwestwholesaledoors.com" or "midwestwholesaledoors.com").
    vendor_bill_sender_allowlist: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
