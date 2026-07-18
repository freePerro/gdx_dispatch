"""Bank Feeds ORM models — tenant-plane (per-tenant DB).

Registered onto ``TenantBase.metadata`` via import in
``gdx_dispatch/models/__init__.py`` so ``create_orm_tables()`` builds the
tables before Alembic runs (#41 ordering). No migration ships with this
module — every table is new.

Money is signed integer cents (``BigInteger``), never float, matching the
GL ledger convention. Table names deliberately avoid ``bank_accounts`` /
``bank_statement_lines`` — those are reserved by the GL Phase 2
reconciliation design as the future evidence tables this feed maps into.

Secrets (institution client secrets, OAuth tokens) are Fernet-encrypted
via ``core.pii._FERNET`` — see ``oauth._encrypt``/``_decrypt``.
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

# ── auth_state values (mirrors QBTokenStore semantics) ─────────────────
AUTH_HEALTHY = "healthy"
AUTH_REFRESH_FAILED = "refresh_failed"          # transient — retried next run
AUTH_NEEDS_RECONNECT = "needs_reconnect"        # refresh token rejected
AUTH_DISCONNECTED = "disconnected"              # soft disconnect (tokens nulled)

# ── schedule frequencies (mirrors QBSyncSchedule) ──────────────────────
FREQ_MANUAL = "manual"
FREQ_HOURLY = "hourly"
FREQ_EVERY_4H = "every_4h"
FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
VALID_FREQUENCIES = (FREQ_MANUAL, FREQ_HOURLY, FREQ_EVERY_4H, FREQ_DAILY, FREQ_WEEKLY)

DOCUMENT_TYPES = ("statement", "notice", "tax")


class BannoInstitution(TenantBase):
    """One row per Banno-powered bank. Each institution provisions its own
    external application (client_id/secret) in its Banno People back office,
    so credentials are inherently per-institution.

    ``fi_host`` is a bare hostname (``digital.example.com``) — validated at
    write time (router) with a strict regex + the SSRF guard. Never a URL.
    """

    __tablename__ = "banno_institutions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    fi_host: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_label: Mapped[str] = mapped_column(String(120), nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BannoConnection(TenantBase):
    """OAuth token store — one ACTIVE connection per institution (enforced in
    the callback: a different bank user (``sub``) is rejected until the
    existing connection is disconnected, preventing joint-account
    double-ingestion).

    Disconnect is SOFT: token columns are nulled and ``auth_state`` becomes
    ``disconnected`` — the row is never deleted, so a reconnect for the same
    ``(institution_id, sub)`` reuses it and every child account's sync
    cursor and dedupe key survives. Deleting the row would orphan/duplicate
    the whole account tree on reconnect.

    ``fi_host`` is a snapshot of the host the tokens were minted against;
    ``get_banno_client`` refuses to serve tokens when the institution row's
    host was edited afterwards (mirrors QBTokenStore.environment).
    """

    __tablename__ = "banno_connections"
    __table_args__ = (
        UniqueConstraint("institution_id", "banno_user_id", name="uq_banno_conn_inst_user"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    institution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("banno_institutions.id"), nullable=False, index=True
    )
    fi_host: Mapped[str] = mapped_column(String(255), nullable=False)
    banno_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Banno does not document the refresh-token lifetime; kept nullable.
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AUTH_HEALTHY, server_default=AUTH_HEALTHY
    )
    # Documents (statements) eligibility. NULL = never probed. False is NOT
    # sticky — re-probed on manual Sync Now / reconnect so a transient 403
    # or later eStatement enrollment can't permanently disable the archive.
    documents_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    documents_synced_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    connected_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BankFeedAccount(TenantBase):
    """A bank account visible through a connection. ``provider`` future-proofs
    the store for non-Banno adapters (Plaid/CSV) joining the same tables."""

    __tablename__ = "bank_feed_accounts"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_account_id", name="uq_bank_feed_acct_conn_ext"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    connection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("banno_connections.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="banno", server_default="banno")
    external_account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    account_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    account_subtype: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Last-4 display only — the full account number is never stored.
    account_number_masked: Mapped[str | None] = mapped_column(String(30), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    balance: Mapped[object | None] = mapped_column(Numeric(14, 2), nullable=True)
    available_balance: Mapped[object | None] = mapped_column(Numeric(14, 2), nullable=True)
    balance_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_inactive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Backfill progress. ``backfill_synced_through`` commits after each
    # completed 90-day window so a run that dies (worker restart, token
    # wall) RESUMES instead of redoing the whole history.
    initial_backfill_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backfill_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backfill_synced_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Incremental watermark (Banno ``lastUpdated`` timebase — THEIRS, never
    # ours; see service.sync_account_transactions).
    updated_since_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    full_resync_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BankFeedTransaction(TenantBase):
    """A synced bank transaction.

    Consumers (recurring-debit detection, GL reconciliation, reports) MUST
    filter ``amount_cents IS NOT NULL AND pending = FALSE AND deleted_at IS
    NULL`` — pendings can lack amounts/dates and can be replaced by a new
    posted id on some cores (tombstone + fresh insert).

    ``line_hash`` is the GL-Phase-2-compatible content fingerprint
    (sha256 of account|posted_date|amount|normalized description). Defined
    for POSTED rows only — NULL while pending (the hash inputs aren't
    stable until posting).
    """

    __tablename__ = "bank_feed_transactions"
    __table_args__ = (
        UniqueConstraint("account_id", "external_transaction_id", name="uq_bank_feed_txn_acct_ext"),
        Index("ix_bank_feed_txn_acct_posted", "account_id", "posted_date"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_feed_accounts.id"), nullable=False, index=True
    )
    external_transaction_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # Signed cents; negative = money out. NULL when the source amount was
    # missing/unparseable (logged at ingest).
    amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Derived from posted_at in the tenant timezone (AppSettings.timezone).
    posted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payee: Mapped[str | None] = mapped_column(String(300), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    filtered_memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Display-only; point-in-time (out-of-order updatedSince delivery makes
    # it unreliable for arithmetic). Never overwritten with NULL.
    running_balance_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    txn_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    txn_subtype: Mapped[str | None] = mapped_column(String(50), nullable=True)
    merchant_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    line_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    external_last_updated: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BankFeedDocument(TenantBase):
    """Archived bank document (statement / notice / tax form).

    Metadata-first ingestion: the listing sweep inserts rows with
    ``fetched_at = NULL``, then downloads each PDF. Rows with NULL
    ``fetched_at`` ARE the retry queue — ``documents_synced_through`` on the
    connection only advances once every row in the sweep has been fetched,
    so a partial download failure can never become a silent archive gap.

    File storage follows the ExpenseReceipt precedent
    (``modules/ledger/models.py``): file on disk under the uploads dir,
    ``storage_path``/``sha256``/``size_bytes``/``content_type`` on the row.
    """

    __tablename__ = "bank_feed_documents"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_document_id", name="uq_bank_feed_doc_conn_ext"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    connection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("banno_connections.id"), nullable=False, index=True
    )
    external_document_id: Mapped[str] = mapped_column(String(120), nullable=False)
    document_type: Mapped[str] = mapped_column(String(20), nullable=False, default="statement")
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(200), nullable=True)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # Banno links one document to N account ids (may be empty for
    # user-scoped docs like tax forms).
    account_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BankFeedSyncSchedule(TenantBase):
    """Singleton row per tenant DB — drives the Celery beat dispatcher for
    ALL institutions (one schedule, sequential per-bank fan-out).

    frequency=manual disables scheduled sync (Sync Now still works).
    ``backfill_days`` bounds the initial history pull; raising it after a
    backfill completes does nothing until ``full_resync_required`` is set.
    """

    __tablename__ = "bank_feed_sync_schedule"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    frequency: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FREQ_MANUAL, server_default=FREQ_MANUAL
    )
    backfill_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365, server_default="365")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_run_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
