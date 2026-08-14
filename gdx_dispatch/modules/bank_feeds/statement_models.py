"""Bank statement evidence tables — the GL Phase 2 reserved layer.

These are the tables the bank_feeds module docstring deliberately left
unclaimed: ``bank_accounts`` / ``bank_statement_lines`` /
``bank_statement_imports`` are the append-only *evidence of the bank*,
fed by manual statement import today and by the Banno/Plaid feeds when
GL Phase 2 wires them in (docs/design/gl-phase2-reconciliation.md §2.1,
docs/design/bank-statement-import-plan.md).

Unlike the Banno tables, these DO ship with an Alembic migration (050) —
new columns on them later must not depend on ``create_orm_tables()``,
which never alters an existing table.

Money is signed integer cents (``BigInteger``), never float; negative =
money out, matching ``BankFeedTransaction`` and the GL convention.

Evidence discipline:
- ``bank_statement_lines`` rows are never UPDATEd. A bad import is voided
  by batch.
- A line can be attested by more than one import (statement periods can
  overlap — observed in the real corpus). ``bank_statement_line_sources``
  records every attesting import; voiding an import removes its
  attestations and only lines with no remaining attestation drop. Without
  this, voiding one of two overlapping imports would silently delete
  evidence the surviving import still vouches for.
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

# ── statement import formats ───────────────────────────────────────────
FORMAT_PDF_COMMUNITY_BANK = "pdf_community_bank"
FORMAT_CSV_GENERIC = "csv_generic"
FORMAT_OFX = "ofx"
FORMAT_QFX = "qfx"
VALID_FORMATS = (FORMAT_PDF_COMMUNITY_BANK, FORMAT_CSV_GENERIC, FORMAT_OFX, FORMAT_QFX)

ACCOUNT_KINDS = ("checking", "savings", "other")

# ── line sections ──────────────────────────────────────────────────────
SECTION_DEPOSIT = "deposit"
SECTION_DEBIT = "debit"
SECTION_CHECK = "check"
SECTION_SERVICE_CHARGE = "service_charge"
SECTION_INTEREST = "interest"
VALID_SECTIONS = (
    SECTION_DEPOSIT,
    SECTION_DEBIT,
    SECTION_CHECK,
    SECTION_SERVICE_CHARGE,
    SECTION_INTEREST,
)

TIE_OUT_PASSED = "passed"
TIE_OUT_FAILED = "failed"


class BankAccount(TenantBase):
    """GDX's own registry of real-world bank accounts (NOT qb_accounts, NOT
    ``bank_feed_accounts`` — this row is the anchor evidence hangs off, so it
    exists independent of any feed connection).

    Auto-created on first statement import from the parsed account header
    (title + last-4); renamable afterwards. The full account number is never
    stored anywhere.
    """

    __tablename__ = "bank_accounts"
    __table_args__ = (
        UniqueConstraint("institution", "last4", name="uq_bank_accounts_inst_last4"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="checking")
    institution: Mapped[str] = mapped_column(String(120), nullable=False)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    statement_import_format: Mapped[str] = mapped_column(
        String(30), nullable=False, default=FORMAT_PDF_COMMUNITY_BANK,
        server_default=FORMAT_PDF_COMMUNITY_BANK,
    )
    # Wired when GL Phase 2 lands; carries no behavior until then.
    gl_account_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gl_accounts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BankStatementImport(TenantBase):
    """One row per uploaded statement file — the batch record.

    ``tie_out_status`` is the parse-integrity verdict (balance equation,
    summary counts/totals, daily-balance recompute). A FAILED tie-out keeps
    this row (with the report, for diagnosis) but inserts NO lines — nothing
    partially parsed ever enters the evidence table.

    ``file_sha256`` is unique among NON-VOIDED imports (partial index):
    re-uploading the identical file is rejected while its import stands,
    but a *voided* import's file can be imported again — voiding exists
    precisely for "the parser got this file wrong", and after a parser fix
    the same bytes must be importable. Overlapping-but-different files are
    always legal; their shared lines dedupe via ``line_hash`` and get an
    attestation row instead.
    """

    __tablename__ = "bank_statement_imports"
    __table_args__ = (
        Index(
            "uq_bank_stmt_import_sha_active",
            "file_sha256",
            unique=True,
            postgresql_where=text("voided_at IS NULL"),
            sqlite_where=text("voided_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    bank_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False, index=True
    )
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    beginning_balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ending_balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Counts are NULLABLE and stored as parsed: the savings summary form
    # prints no count at all, and fabricating a 0 would erase the "bank
    # didn't state one" fact the tie-out relies on (audit: no invented data
    # in an evidence table).
    deposits_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deposits_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    debits_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    debits_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    service_charge_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    interest_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tie_out_status: Mapped[str] = mapped_column(String(16), nullable=False)
    tie_out_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lines_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_deduped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BankStatementLine(TenantBase):
    """Append-only external evidence — one bank transaction as the statement
    states it. Never UPDATEd; removal only via import-void when the last
    attestation drops.

    ``line_hash`` = sha256(account|txn_date|amount_cents|normalized
    description|occurrence_n) where ``occurrence_n`` is the 1-based count of
    identical (date, amount, description) lines within that key — stable
    under file reordering, so identical real-world transactions dedupe
    correctly across overlapping statements (GL Phase 2 §2.1 [AUDIT-R2]).

    ``import_id`` is the FIRST import that saw the line (display/provenance);
    the full attestation set lives in ``bank_statement_line_sources``.
    """

    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        UniqueConstraint("bank_account_id", "line_hash", name="uq_bank_stmt_line_hash"),
        Index("ix_bank_stmt_lines_acct_date", "bank_account_id", "txn_date"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    bank_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False, index=True
    )
    import_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_statement_imports.id"), nullable=False, index=True
    )
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Signed cents; negative = money out.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str] = mapped_column(String(20), nullable=False)
    check_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    occurrence_n: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    line_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# ── match lifecycle (Phase B / PR 3) ───────────────────────────────────
MATCH_SUGGESTED = "suggested"
MATCH_CONFIRMED = "confirmed"
MATCH_REJECTED = "rejected"
VALID_MATCH_STATUSES = (MATCH_SUGGESTED, MATCH_CONFIRMED, MATCH_REJECTED)

MATCH_RULES = ("R2", "R3", "R5", "manual")

# Classify-only matches: bank-only reality that no books record should exist
# for (or that is recorded elsewhere) — a confirmed classification removes
# the line from the exception reports without inventing a books record.
CLASSIFICATIONS = ("transfer", "bank_fee", "interest", "owner", "processor", "other")

EXTERNAL_SOURCES = ("payments", "expenses", "vendor_invoices")


class BankMatch(TenantBase):
    """A suggest-and-confirm match between statement evidence and books
    records — the GL Phase 2 §3.2 three-table shape, pointed at operational
    records until the GL goes live.

    Cardinality: one match ↔ N statement lines AND N externals (the
    dominant revenue event is a batched deposit slip = one line ↔ many
    per-invoice payments, which a single line_id·matched_id row physically
    cannot hold — the plan-level audit's foundational finding).

    Books-convergence Track 1 ended the metadata-only phase, narrowly:
    confirming a match whose ONLY external is a single vendor bill records a
    ``vendor_bill_payments`` row (in the same transaction; refused entirely
    beyond the bill's open balance — see
    ``statement_matching._apply_confirm_effects`` for the full refusal
    ladder), and unconfirm voids exactly that payment.
    Everything else stays metadata-only: matches never edit Payments,
    Expenses, or the bill rows themselves, and every confirm remains
    reversible (unconfirm → suggested). ``created_expense_id`` is the other
    mutation seam — set only by create-expense-from-bank-line so unconfirm
    can find (and soft-delete if unmodified, else detach) exactly the
    expense the confirm created. ``classification`` set (with no externals)
    = classify-only (R5).
    """

    __tablename__ = "bank_matches"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    bank_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False, index=True
    )
    rule: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=MATCH_SUGGESTED, index=True)
    classification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[object | None] = mapped_column(Numeric(3, 2), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # create-expense-from-bank-line provenance (migration 065 — this table is
    # migration-shipped, column adds must never rely on create_all).
    created_expense_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class BankMatchLine(TenantBase):
    """Statement side of a match. ``match_status`` is denormalized from the
    parent (synced in the service on every transition, single-writer) so
    exclusivity is expressible as a partial unique index: a line may sit in
    at most ONE non-rejected match."""

    __tablename__ = "bank_match_lines"
    __table_args__ = (
        Index(
            "uq_bank_match_line_active",
            "line_id",
            unique=True,
            postgresql_where=text("match_status <> 'rejected'"),
            sqlite_where=text("match_status <> 'rejected'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    match_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_matches.id"), nullable=False, index=True
    )
    line_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_statement_lines.id"), nullable=False, index=True
    )
    match_status: Mapped[str] = mapped_column(String(12), nullable=False, default=MATCH_SUGGESTED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BankMatchExternal(TenantBase):
    """Books side of a match: a row in payments / expenses / vendor_invoices
    (no FK — three possible tables; existence validated at write time).
    Same denormalized-status exclusivity as the line side: a books record
    is consumed by at most one non-rejected match."""

    __tablename__ = "bank_match_externals"
    __table_args__ = (
        Index(
            "uq_bank_match_external_active",
            "source_table",
            "source_id",
            unique=True,
            postgresql_where=text("match_status <> 'rejected'"),
            sqlite_where=text("match_status <> 'rejected'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    match_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_matches.id"), nullable=False, index=True
    )
    source_table: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    match_status: Mapped[str] = mapped_column(String(12), nullable=False, default=MATCH_SUGGESTED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BankStatementLineImage(TenantBase):
    """A check or deposit-ticket scan extracted from a statement's images
    page, paired to its evidence line via the page's caption (amount +
    full date + check number). ``line_id`` is NULL when pairing couldn't
    be established (caption/image count mismatch, or no matching line) —
    the scan still lands in the import's gallery rather than being lost.

    The scans show full account numbers and signatures: files live under
    the private uploads tree and are served ONLY through the path-guarded,
    permission-gated download endpoint.

    Deleted when their import is voided (unlike evidence lines, images
    belong to the import that extracted them; a co-attesting overlap
    import contributes its own rows).
    """

    __tablename__ = "bank_statement_line_images"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    import_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_statement_imports.id"), nullable=False, index=True
    )
    line_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_statement_lines.id"), nullable=True, index=True
    )
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    caption_check_no: Mapped[str | None] = mapped_column(String(20), nullable=True)
    caption_amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    caption_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BankStatementLineSource(TenantBase):
    """Attestation: import X vouches for line Y.

    Written for the first-seen import AND every later overlapping import
    whose parse produced the same ``line_hash``. Voiding an import deletes
    its attestations, then deletes only lines left with zero attestations —
    evidence co-vouched by a surviving import stays.
    """

    __tablename__ = "bank_statement_line_sources"
    __table_args__ = (
        UniqueConstraint("line_id", "import_id", name="uq_bank_stmt_line_source"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    line_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_statement_lines.id"), nullable=False, index=True
    )
    import_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bank_statement_imports.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
