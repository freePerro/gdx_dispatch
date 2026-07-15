"""GL ledger ORM models — tenant-plane (per-tenant DB).

Registered onto ``TenantBase.metadata`` via import in
``gdx_dispatch/models/__init__.py`` so ``create_orm_tables()`` builds the
``gl_*`` tables *before* Alembic runs (#41 ordering). The integrity triggers
(balance / immutability / sealing) are DDL and ship in migration
``012_gl_core`` — they are Postgres-only and are NOT created by ``create_all``.

Design: docs/design/gl-phase1-core-ledger.md §3. Money is signed integer cents
(``BigInteger``), never float — floats are lint-banned on ledger paths (S4).

Account *roles* (``GlAccount.role``) are the stable keys the posting engine
binds to; the display code/name are operator-editable via the Accounting
Settings page (S4.5) without breaking posting. Roles are seeded in S2.
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

# TenantBase comes from its owner, core.audit — NOT via
# gdx_dispatch.models.tenant_models. Importing through the models package
# creates a cycle (this module ← models/__init__ ← tenant_models submodule
# import triggers the package init): whenever ledger models were imported
# before the package, the package init's re-import hit a partially
# initialized module and its `except ImportError` silently dropped every
# Gl* name from gdx_dispatch.models. Found by S2 audit round 1.
from gdx_dispatch.core.audit import TenantBase, utcnow

# Account roles the engine posts to. Stable identifiers — the operator may
# rename/renumber the account that *owns* a role (S4.5) but never reassign the
# role. Seeded in S2; listed here so both the seed and the engine share one
# source of truth. Non-system accounts (operator-added) carry role=None.
ROLE_AR = "AR"
ROLE_UNDEPOSITED = "UNDEPOSITED"
ROLE_OPERATING_BANK = "OPERATING_BANK"
ROLE_SALES_TAX_PAYABLE = "SALES_TAX_PAYABLE"
ROLE_CUSTOMER_CREDITS = "CUSTOMER_CREDITS"
ROLE_SALES_FALLBACK = "SALES_FALLBACK"
ROLE_DISCOUNTS = "DISCOUNTS"
ROLE_REFUNDS = "REFUNDS"
ROLE_OPENING_EQUITY = "OPENING_EQUITY"
ROLE_ROUNDING = "ROUNDING"
ROLE_WAGES = "WAGES"
ROLE_PAYROLL_TAX = "PAYROLL_TAX"
# Fallback for expense categories with no (or a dangling) account mapping —
# the expense-side twin of SALES_FALLBACK (spec §4: unknown → 6900 + memo flag).
ROLE_EXPENSE_FALLBACK = "EXPENSE_FALLBACK"

# Every role the engine may resolve. The S2 seed guarantees exactly one active
# system account per role; tests assert the two lists never drift.
ALL_ROLES = (
    ROLE_AR,
    ROLE_UNDEPOSITED,
    ROLE_OPERATING_BANK,
    ROLE_SALES_TAX_PAYABLE,
    ROLE_CUSTOMER_CREDITS,
    ROLE_SALES_FALLBACK,
    ROLE_DISCOUNTS,
    ROLE_REFUNDS,
    ROLE_OPENING_EQUITY,
    ROLE_ROUNDING,
    ROLE_WAGES,
    ROLE_PAYROLL_TAX,
    ROLE_EXPENSE_FALLBACK,
)

# Account classification — drives the balance sheet vs P&L split and the
# natural debit/credit sign in reports.
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")

# Journal-entry lifecycle. Entries are born ``posted`` and only ever move to
# ``reversed`` (via a reversing entry); the immutability trigger permits that
# one transition and nothing else. No ``draft`` in Phase 1.
ENTRY_STATUS_POSTED = "posted"
ENTRY_STATUS_REVERSED = "reversed"


class GlAccount(TenantBase):
    """A chart-of-accounts line. System accounts (``is_system``) own a role the
    engine posts to; they may be renamed/renumbered but never deleted or
    role-reassigned. Deactivate (``active=False``), never delete, once posted to.
    """

    __tablename__ = "gl_accounts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(
        Enum(*ACCOUNT_TYPES, name="gl_account_type"), nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gl_accounts.id"), nullable=True
    )
    # Stable engine binding. NULL for operator-added accounts. "Exactly one
    # ACTIVE system account per role" is DB-enforced by the partial unique
    # index below (a deactivated old account may still share the role).
    role: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        Index("ix_gl_accounts_code", "code"),
        # Makes ambiguous role ownership unrepresentable, and arbitrates the
        # concurrent-first-seed race: two racing seed_coa() transactions both
        # insert system rows, so the loser dies here and its whole seed
        # (non-system rows included) rolls back with it. Fresh DBs get this
        # via create_all; existing DBs via migration 020.
        Index(
            "uq_gl_accounts_active_system_role",
            "company_id",
            "role",
            unique=True,
            postgresql_where=text("is_system AND active"),
            sqlite_where=text("is_system AND active"),
        ),
    )


class GlJournalEntry(TenantBase):
    """One balanced journal entry (the header). Born ``posted``; corrected only
    by a reversing entry. Bitemporal: ``effective_at`` (economic date) +
    ``posted_at`` (when we recorded it). ``created_txid`` seals the entry so its
    lines can only be inserted in the same transaction (see the sealing trigger).
    """

    __tablename__ = "gl_journal_entries"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # Human-facing monotonic entry number. Postgres SEQUENCE (created by
    # create_all); on SQLite (tests) callers supply the value explicitly.
    entry_no: Mapped[int] = mapped_column(
        BigInteger, Sequence("gl_journal_entry_no_seq"), unique=True, nullable=False
    )
    effective_at: Mapped[date] = mapped_column(Date, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    status: Mapped[str] = mapped_column(
        Enum(ENTRY_STATUS_POSTED, ENTRY_STATUS_REVERSED, name="gl_entry_status"),
        nullable=False,
        default=ENTRY_STATUS_POSTED,
    )
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Content-derived liveness key (engine, S3). Unique; multiple NULLs allowed
    # (manual JEs). Reversal entries use their own ``reversal:{id}`` key form.
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    reverses_entry_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gl_journal_entries.id"), nullable=True
    )
    reversed_by_entry_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gl_journal_entries.id"), nullable=True
    )
    # txid_current() at insert — the sealing anchor. Set by the engine (S3).
    created_txid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    lines: Mapped[list[GlJournalLine]] = relationship(
        back_populates="entry",
        # No delete-orphan cascade: journal lines are append-only and the
        # immutability trigger rejects DELETE anyway. Cascade would only ever
        # fire on entry delete, which is itself blocked.
        cascade="save-update, merge",
        foreign_keys="GlJournalLine.entry_id",
    )

    __table_args__ = (
        Index("ix_gl_journal_entries_source", "source_type", "source_id"),
        Index("ix_gl_journal_entries_effective_at", "effective_at"),
    )


class GlJournalLine(TenantBase):
    """One debit (+) or credit (−) leg. Signed integer cents; never zero. The
    balance trigger enforces per-entry ``SUM(amount_cents)=0`` at commit.
    ``job_id`` / ``customer_id`` are reporting dimensions.
    """

    __tablename__ = "gl_journal_lines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gl_journal_entries.id"), nullable=False, index=True
    )
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gl_accounts.id"), nullable=False, index=True
    )
    # Debit positive, credit negative (Square 3-0). CHECK (<> 0) below.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reporting dimensions — soft references (no FK) to jobs/customers. Kept
    # FK-free so the append-only ledger doesn't couple to the lifecycle of
    # mutable operational rows (a hard-deleted job must never block or cascade
    # into posted history); a dangling dimension only means a report can't
    # resolve the name. Indexed for drill-down.
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    customer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    entry: Mapped[GlJournalEntry] = relationship(
        back_populates="lines", foreign_keys=[entry_id]
    )

    __table_args__ = (
        CheckConstraint("amount_cents <> 0", name="ck_gl_line_amount_nonzero"),
    )


class GlSettings(TenantBase):
    """The accounting config store — a per-company singleton (S2, spec §4 /
    plan guiding rule 3). Every CPA-dependent choice lives here as *data* the
    Accounting Settings page (S4.5) edits, never as a code constant. The
    posting engine (S3) reads role→account resolution and the maps below from
    this row; ``ledger_posting_enabled`` is the master switch S4/S9 gate on.

    Map values are account *roles* (see ``ALL_ROLES``), except
    ``expense_category_account_map`` whose values are ``gl_accounts.id``
    strings — categories map to ordinary (non-system) accounts the operator
    may renumber, so the stable id is the key, with ``EXPENSE_FALLBACK`` as
    the dangling-reference escape hatch.

    Guardrail (enforced in the S4.5 layer, recorded here): ``inventory_treatment``,
    ``cutover_month``, and ``payment_method_role_map`` become read-only once
    ``ledger_posting_enabled`` is on or any journal entry has posted.

    The JSON maps are nullable on purpose: NULL means "defaults not yet
    materialized" and ``ensure_gl_settings()`` (service.py) fills them — one
    source of truth for defaults, not two.
    """

    __tablename__ = "gl_settings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Master switch — nothing posts while False (S4 chokepoint reads this).
    ledger_posting_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)

    # Books are accrual; cash reports are derived (spec §6). tax_basis is what
    # the CPA files on — drives the §1.446-1(a)(4) reconciliation workpaper.
    reporting_basis: Mapped[str] = mapped_column(String(10), nullable=False, default="accrual")
    tax_basis: Mapped[str] = mapped_column(String(10), nullable=False, default="cash")

    # Phase 1.5 gate (spec §7): parts expensed or capitalized. [CPA]
    inventory_treatment: Mapped[str] = mapped_column(String(20), nullable=False, default="expense")

    # First day of the month the ledger becomes the money truth (P8 opening
    # balances post as of this date). NULL until cutover is planned.
    cutover_month: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Entity/filing basis. [CPA] — free-form-ish on purpose; validated set in S4.5.
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Opening-bank-balance attestation (Phase 2 tie-out anchor).
    opening_bank_attested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opening_bank_attested_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # payment method (normalized lowercase) → role: UNDEPOSITED vs OPERATING_BANK.
    payment_method_role_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # credit/refund reason → role: DISCOUNTS (4900) vs REFUNDS (4910). [CPA]
    credit_reason_role_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # expense category → gl_accounts.id (string). Unknown/dangling → EXPENSE_FALLBACK.
    expense_category_account_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # invoice-line category (free-form, estimate-derived) → gl_accounts.id.
    # Empty by default: unmapped/NULL categories credit 4000 SALES_FALLBACK,
    # memo-flagged (spec §5.1). Added in S5; migration 021 backfills the
    # column when gl_settings pre-dates it.
    revenue_category_account_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Per-key CPA-review stamps: {setting_key: {"reviewed_at": iso8601, "by": user_id}}.
    cpa_review: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # Singleton per company — ensure_gl_settings() get-or-creates.
        UniqueConstraint("company_id", name="uq_gl_settings_company"),
    )


class GlPeriodLock(TenantBase):
    """Append-only period-lock history. A posting with ``effective_at`` on or
    before the most-recent ``lock_date`` is hard-blocked unless the caller holds
    ``accounting.close`` (every override audit-logged). Late facts post to the
    first open day with a memo naming the true date.
    """

    __tablename__ = "gl_period_locks"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    lock_date: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
