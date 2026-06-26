from __future__ import annotations

from datetime import UTC, date, datetime, time, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.pii import EncryptedString, HashColumn

# All tenant DB models share TenantBase so all FK references resolve within one metadata
Base = TenantBase


def _now_utc() -> datetime:
    return datetime.now(UTC)


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    address: Mapped[str] = mapped_column(Text, nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    logo: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, default="America/New_York")
    enabled_modules: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notification_preferences: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    integrations: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    primary_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#0f172a")
    secondary_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#2563eb")
    # Per-tenant Google Maps JS API key. Plaintext is correct here — the
    # key is exposed to every browser that loads /maps anyway, so the real
    # control is HTTP-referrer restriction set in Google Cloud Console.
    google_maps_api_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Phone.com per-tenant integration. voip_id is the Phone.com account ID
    # (not a secret — appears in every API URL). default_extension_id chooses
    # which extension sends outbound SMS by default; default_caller_id is the
    # E.164 number shown to recipients.
    phone_com_voip_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phone_com_default_extension_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    phone_com_default_caller_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 2026-04-29 / UX audit F-55 — Outbound DID strategy.
    # Resolution priority when sending an SMS / placing an outbound call:
    #   conversation_sticky → tech_override → tenant_default
    # The strategy column toggles WHICH steps are active; default is
    # "tenant_default" (existing behavior). Other values:
    #   "tech_override"        → tech's preferred DID, fallback tenant default
    #   "conversation_sticky"  → reuse the DID last used in this thread,
    #                            fallback tenant default
    #   "priority_chain"       → all three in order
    phone_com_outbound_strategy: Mapped[str] = mapped_column(
        String(40), nullable=False, default="tenant_default", server_default="tenant_default"
    )
    phone_com_account_features: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Sprint tech_mobile S1-Z2 — per-tenant overrides for tech-mobile feature
    # settings. Keys + defaults live in gdx_dispatch/core/feature_defaults.py
    # (TECH_MOBILE_SETTINGS); this column only stores values that have been
    # explicitly overridden for the tenant.
    tenant_mobile_settings: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # Stamped at end of every successful run_phone_com_sync. UI shows
    # "Last synced: 5m ago". NULL until first sync. Sprint phone-com Wave B / S17.
    phone_com_last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Sprint dispatch-capacity (2026-05-20) — tenant default shop hours used
    # by the dispatch board to compute per-tech daily capacity. Per-user
    # overrides on users.shift_start/end/workdays; NULL there inherits these.
    # workdays is a Mon=1, Tue=2, Wed=4 ... Sun=64 bitmask; 31 = Mon-Fri.
    default_shift_start: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(8, 0), server_default=text("'08:00'")
    )
    default_shift_end: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(17, 0), server_default=text("'17:00'")
    )
    default_workdays: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=31, server_default=text("31")
    )
    # Sprint monthly-budget-history (2026-05-24) — accounting method for the
    # QBO ProfitAndLoss report that drives the budget actuals. Cash-basis
    # only counts paid items (good for owner-operators tracking actual cash
    # out); Accrual counts entered Bills/Purchases regardless of payment
    # status (matches QBO's default reports). One QBO API parameter, one
    # tenant choice.
    qb_accounting_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Accrual", server_default="Accrual"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Customer(Base):
    __tablename__ = "customers"
    # S122-9 slice 3 (2026-05-12): `address` is back on EncryptedString
    # after the user-facing readers/writers were refactored to ORM (the
    # raw-SQL bypass class that produced S122-1b is now lint-gated by
    # `gdx_dispatch/tools/raw_sql_on_encrypted_columns_scan.py`).
    #
    # `name`, `email`, `phone` stay plaintext for now: each is
    # substring-LIKE-searched in `list_customers` and friends, and the
    # `LOWER(col) LIKE :q` predicate matches nothing against ciphertext.
    # Bringing them under encryption needs the search-architecture decision
    # filed as D-S122-9-customer-search-encryption (sidecar tsvector vs
    # hash-only exact match vs drop substring search).
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=True)
    email: Mapped[str] = mapped_column(Text, nullable=True)
    email_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=True)
    phone: Mapped[str] = mapped_column(Text, nullable=True)
    phone_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=True)
    address: Mapped[str] = mapped_column(EncryptedString, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_type: Mapped[str] = mapped_column(String(50), nullable=True, default="Retail")
    # Sprint 1.0.5 — canonical pricing-engine input. NULL = engine treats as 'retail'.
    # Eventually replaces customer_type (kept until pave reload is hardened — D-PE-7).
    pricing_class: Mapped[str] = mapped_column(
        Enum("retail", "contractor", "wholesale", name="pricing_class"),
        nullable=True,
    )
    margin_override_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=True)
    # 2026-04-29 / UX audit F-36 — per-customer override for payment-terms
    # days. NULL means "use the tenant default for this pricing_class."
    payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=True)
    # Sprint 1.0.6 — denormalized cache of trailing-365-day paid invoice
    # volume. Refreshed on payment.received and on stale-read at
    # estimate-create. The pricing engine consumes this for rolling-volume
    # discount lookup; reading the live SUM per estimate doesn't scale.
    cached_rolling_volume_paid_12mo: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=True)
    cached_rolling_volume_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # S122-17: incremental-sync cursor for QuickBooks push. Pre-fix
    # ``sync_all_customers_task`` iterated EVERY customer on every full sync
    # — most pushes were no-ops because the row hadn't changed since the
    # last successful push. Same pattern as Invoice (S122-14): default True
    # so newly created customers always queue a push; before_update listener
    # auto-flips back to True when any non-internal column changes;
    # push_customer clears the flag after a successful upsert.
    qb_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    qb_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    email_opt_out: Mapped[bool] = mapped_column(Boolean, nullable=True)
    sms_opt_out: Mapped[bool] = mapped_column(Boolean, nullable=True)
    # Sprint 1.x-S2 — append-only narrative the AI write tier may UPDATE
    # under a column-grained GRANT (gdx_ai_write). Distinct from `notes`,
    # which humans edit; new AI tooling appends summaries here so the two
    # sources stay separable.
    notes_appended: Mapped[str | None] = mapped_column(Text, nullable=True)

    @validates("name", "email", "phone")
    def _set_hashes(self, key: str, value: str | None) -> str | None:
        # Phone numbers must hash by their E.164 form so that calls from
        # Phone.com (+1XXXXXXXXXX) match Customer rows stored as
        # "(XXX) XXX-XXXX" or any other formatting. The customer_resolver
        # always normalizes before hashing — both sides must agree.
        if key == "phone" and value:
            from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164
            normalized = normalize_e164(value) or value
            setattr(self, "phone_hash", HashColumn.hash_for_search(normalized))
        else:
            setattr(self, f"{key}_hash", HashColumn.hash_for_search(value) if value else None)
        return value


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=True)
    # 2026-04-29: tenant-friendly display number ("JOB-2026-001"). Format
    # template + counter live in TenantSettings. NULL on legacy rows until
    # backfilled — frontend falls back to the UUID prefix when missing.
    job_number: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    lifecycle_stage: Mapped[str] = mapped_column(Enum("lead", "service_call", "estimate", "scheduled", "in_progress", "completed", "cancelled", name="job_lifecycle_stage"), nullable=False, default="service_call", server_default="service_call")
    dispatch_status: Mapped[str] = mapped_column(Enum("unassigned", "assigned", "en_route", "on_site", "done", name="job_dispatch_status"), nullable=False, default="unassigned")
    billing_status: Mapped[str] = mapped_column(Enum("unbilled", "invoiced", "partial_paid", "paid", "overdue", "void", name="job_billing_status"), nullable=False, default="unbilled", server_default="unbilled")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_to: Mapped[str] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=True)
    is_return_visit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    parent_job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=True, default="Service")
    status: Mapped[str] = mapped_column(String(50), nullable=True)
    priority: Mapped[str] = mapped_column(String(50), nullable=True, default="Normal")
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # 2026-04-29 / UX audit F-8 — stamped by POST /api/jobs/{id}/start.
    # Distinct from arrived_at (which is the Dispatch app's "tech is on
    # site" signal); started_at = "tech tapped Start Job".
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    holding_area_id: Mapped[str] = mapped_column(String(36), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    signature_data: Mapped[str] = mapped_column(Text, nullable=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_by: Mapped[str] = mapped_column(String(200), nullable=True)
    # Sprint dispatch-capacity (2026-05-20) — the scheduler's expected
    # duration in decimal hours. Distinct from estimate-derived duration
    # (compute_man_hour_duration_minutes). The dispatch board prefers this
    # when set; falls back to estimate-derived; surfaces "?h" if both NULL.
    # Tracked against JobCloseout.hours_worked for the per-tech efficiency
    # report (foundation for bonus structure).
    scheduled_duration_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    # Sprint customer-multi-location (2026-05-21) — optional FK to the
    # customer_locations row for this job's service address. NULL means
    # "use the customer's primary location" (the JobDetailView fallback
    # path predates this column). Type is varchar(36) to match the
    # existing customer_locations.id storage (String(36), not Postgres UUID).
    location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customer_locations.id"), nullable=True, index=True
    )


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    tech_id: Mapped[str] = mapped_column(String(80), nullable=False)
    clock_in: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clock_out: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    entry_type: Mapped[str] = mapped_column(String(50), nullable=False, default="manual", server_default="manual")
    hourly_rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc, server_default=func.now())
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entry_type_old: Mapped[str] = mapped_column(String(50), nullable=True)
    gps_lat: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    gps_lng: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    signature_data: Mapped[str] = mapped_column(Text, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(200), nullable=True)
    tech_name: Mapped[str] = mapped_column(String(200), nullable=True)
    technician_id: Mapped[str] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=True)


class Warranty(Base):
    __tablename__ = "warranties"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    terms: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("active", "voided", "claimed", "expired", name="warranty_status"),
        nullable=False,
        default="active",
    )
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_claim_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_claim_notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # 2026-05-04 — nullable now (Slice 2). QB-imported invoices don't correspond
    # to a GDX job workflow; pre-fix code attached them to a synthetic
    # "QuickBooks Import" job per customer, which misattributed every imported
    # invoice's revenue to one fake job. Customer_id is the required link;
    # job_id is optional. Callers that read job_id check for None first.
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    billing_type: Mapped[str] = mapped_column(Enum("standard", "deposit", "progress", "final", name="invoice_billing_type"), nullable=False, default="standard")
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # Decimal fraction (0.0738 == 7.38%). When set, tax_amount is computed
    # from this rate × the sum of taxable line_totals on every recalc; the
    # legacy flat-dollar tax_amount path is preserved for invoices created
    # before the rate column existed (tax_rate IS NULL → trust tax_amount).
    tax_rate: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    tax_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    balance_due: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        Enum("draft", "sent", "paid", "overdue", "void", name="invoice_status"),
        nullable=False,
        default="draft",
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    public_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(12, 2), nullable=True, default=0)
    # S122-14: qb_dirty drives the QB push filter. Pre-fix
    # ``sync_all_invoices_task`` filtered ``status != 'paid'`` and re-pushed
    # every non-paid invoice on every full sync (~80 % wasted at GDX scale
    # because most invoices haven't changed since the last push). Now the
    # task filters on this flag; ``push_invoice`` clears it after a
    # successful create, and a SQLAlchemy ``before_update`` listener
    # auto-sets it back to True when ANY non-(qb_dirty, qb_synced_at) field
    # changes. Default True so newly created invoices always queue a push.
    qb_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    qb_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # 2026-05-11 — tightened from nullable=True (which was set in the 2026-05-04
    # QB-import slice). Invoices without a customer are reporting/AR bombs;
    # all six Invoice() constructor callsites now guard upstream and the
    # corresponding tenant-DB migration is `migrate_invoices_customer_id_not_null.sql`.
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=True)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    invoice_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    # Per-line taxability — labor lines are typically marked False in MN
    # (services aren't sales-taxed; goods are). Default True so existing
    # callers stay correct on physical-good line items.
    taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # S122-b — mirror EstimateLine's category/cost/margin fields so the office
    # can edit invoices with the same shape as estimates (Doug 2026-05-11:
    # "you did not match the new estimate page"). Estimate-derived invoices
    # copy these from EstimateLine at create. Free-form invoices set them
    # from the operator's input on /billing/new.
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cost_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # margin_pct_snapshot: engine-resolved tier margin captured at copy-time
    # from an estimate. margin_pct_override: operator's per-line manual margin
    # set on /billing/new. Both are decimal fractions (0.40 = 40%).
    margin_pct_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    margin_pct_override: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    # D-S122-line-removal-unbill: surgical mapping back to the JobPartNeeded
    # row this line was created from. When this line is deleted, the part's
    # billed_invoice_id is released. Without this column, the bill linkage
    # could go stale (part still pointing at an invoice that no longer has
    # a line representing it). String for FK because JobPartNeeded.id is
    # String(36) — not a UUID column.
    part_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("job_parts_needed.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # Soft-delete so a removed line never re-appears on a re-load while
    # still leaving audit history intact.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    invoice_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    method: Mapped[str] = mapped_column(String(50), nullable=False, default="cash")
    payment_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    # External reference: check #, transaction ID, Zelle/Venmo memo. The
    # Record Payment dialog has had this field forever, but pre-fix the
    # column was missing — frontend sent the value, Pydantic silently
    # dropped it (no field in PaymentCreateIn), backend never persisted,
    # payment history rendered empty Reference cells. Apr 30 2026 walk-through.
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # -- columns from production schema not yet in ORM --
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    vendor: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)

    lines: Mapped[list[ExpenseLine]] = relationship(
        back_populates="expense",
        cascade="all, delete-orphan",
    )


class ExpenseLine(Base):
    __tablename__ = "expense_lines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    expense_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("expenses.id"), nullable=False)
    account: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    expense: Mapped[Expense] = relationship(back_populates="lines")


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rules: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AutomationSequence(Base):
    __tablename__ = "automation_sequences"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_event: Mapped[str] = mapped_column(
        Enum(
            "job_completed",
            "estimate_sent",
            "invoice_overdue",
            "customer_created",
            name="automation_trigger_event",
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class AutomationStep(Base):
    __tablename__ = "automation_steps"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sequence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("automation_sequences.id"), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    action_type: Mapped[str] = mapped_column(
        Enum(
            "send_email",
            "send_sms",
            "create_task",
            "update_status",
            "wait",
            name="automation_action_type",
        ),
        nullable=False,
    )
    delay_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AutomationEnrollment(Base):
    __tablename__ = "automation_enrollments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sequence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("automation_sequences.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("active", "paused", "completed", "stopped", name="automation_enrollment_status"),
        nullable=False,
        default="active",
    )
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    resumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_reason: Mapped[str] = mapped_column(String(120), nullable=True)


class DocumentFolder(Base):
    __tablename__ = "document_folders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_folders.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str] = mapped_column(String(150), nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    folder_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_folders.id"), nullable=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=True)
    estimate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("estimates.id"), nullable=True)
    tags: Mapped[str] = mapped_column(String(500), nullable=True)
    # Sprint vendor-statement-recon slice 1: sha256 of file bytes for dedup.
    # Indexed; nullable on legacy rows. New uploads compute + store; uploads
    # rejected if a non-deleted row in the tenant already carries the hash.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # created_at kept alongside uploaded_at — production rows carry both.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # size_bytes kept as legacy alias for file_size on pre-ORM-cutover rows.
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=True)
    # DEPRECATED — columns retained as nullable for one reason: pave
    # round-trip. Phase B3 (2026-04-24) removed them from app code; we'd
    # have removed them from ORM too, but production tenant DBs still
    # carry the columns and pg_dump emits them in COPY headers. Strict
    # pave then aborts on data reload because the freshly created
    # schema is missing these columns. Keeping them nullable preserves
    # the ORM-as-truth invariant (ORM matches live DB) while leaving
    # them de-facto unused.
    # Drop schedule: cleanup migration after Sprint 1.0 close-out
    # (2026-05-02+) — drop from every tenant DB in one pass.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=True)


class LoyaltyTier(Base):
    __tablename__ = "loyalty_tiers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    min_spend: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LoyaltyPoints(Base):
    __tablename__ = "loyalty_points"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LoyaltyReferral(Base):
    __tablename__ = "loyalty_referrals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(Text, nullable=True)
    referrer_id: Mapped[str] = mapped_column(Text, nullable=False)
    referee_name: Mapped[str] = mapped_column(Text, nullable=False)
    referee_phone: Mapped[str] = mapped_column(Text, nullable=False)
    referee_email: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    converted_at: Mapped[str] = mapped_column(Text, nullable=True)
    rewarded_at: Mapped[str] = mapped_column(Text, nullable=True)
    reward_given: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[str] = mapped_column(Text, nullable=True)
    # -- columns from production schema not yet in ORM --
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    converted_customer_id: Mapped[str] = mapped_column(String(36), nullable=True)


class CustomCatalog(Base):
    __tablename__ = "custom_catalogs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_system: Mapped[str] = mapped_column(String(60), nullable=False, default="manual")
    # Sprint typed-catalogs — discriminator. 'parts' default keeps existing
    # catalogs working unchanged; 'door' / 'opener' / 'spring' / 'track' /
    # 'remote' / 'labor' enable Class Table Inheritance lookups.
    product_class: Mapped[str] = mapped_column(String(40), nullable=False, default="parts", server_default="parts", index=True)
    # ADR-015 — no-code custom catalog types. When product_class='custom' this
    # holds the ordered field definitions ({name,label,type,section,required,
    # options}) the UI renders from; built-in classes leave it empty and use the
    # frontend registry / typed spec tables instead.
    field_schema: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    # ADR-015 Slice 2 — pluggable pricing. `pricing_strategy` is the strategy id
    # ('manual' default = keep entered price); `pricing_config` holds a
    # self-contained declarative spec ({kind, params}) when the strategy was
    # contributed by a Catalog Pack, so pricing runs in-core with no pack code.
    pricing_strategy: Mapped[str] = mapped_column(String(40), nullable=False, default="manual", server_default="manual")
    pricing_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    # #50 — whole-catalog enable/disable. Inactive catalogs stay on the Catalogs
    # page (with a badge) but their items leave the estimate/billing pickers.
    # Distinct from deleted_at (remove): active=False is "temporarily hide".
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    items: Mapped[list[CustomCatalogItem]] = relationship(
        back_populates="catalog",
        cascade="all, delete-orphan",
    )


class CustomCatalogItem(Base):
    __tablename__ = "custom_catalog_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    catalog_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("custom_catalogs.id"), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    category: Mapped[str] = mapped_column(String(120), nullable=True)
    # #55 — first-class vendor/supplier the item came from. Mirrors
    # InventoryItem.supplier; queryable (unlike stashing it in attributes).
    vendor: Mapped[str] = mapped_column(String(200), nullable=True)
    # Sprint 1.0.5 — pricing-engine label (doors/openers/parts/labor/other).
    # Independent of the free-form `category` field above.
    pricing_category: Mapped[str] = mapped_column(String(40), nullable=True, index=True)
    # Sprint typed-catalogs — Class Table Inheritance discriminator.
    # Mirrors parent CustomCatalog.product_class for fast filtered queries
    # without join. Kept in sync at write time by the catalog router.
    product_class: Mapped[str] = mapped_column(String(40), nullable=False, default="parts", server_default="parts", index=True)
    # ADR-015 — values for a custom catalog's user-defined fields, keyed by the
    # field_schema `name`. Empty for built-in classes (which use typed columns /
    # the door_spec table).
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    qb_item_id: Mapped[str] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    catalog: Mapped[CustomCatalog] = relationship(back_populates="items")
    door_spec: Mapped["DoorSpec | None"] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        uselist=False,
    )


class DoorSpec(Base):
    """Class Table Inheritance — install attributes for catalog items where
    product_class='door'. 1:1 with custom_catalog_items.

    Field shape mirrors ChiDoorCatalog so the install/estimate read-path
    can union CHI feed rows with tenant-custom door rows on the same shape.
    See ai-queue/plans/sprint_typed_product_catalogs.md for rationale.
    """
    __tablename__ = "door_specs"

    catalog_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("custom_catalog_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    manufacturer: Mapped[str] = mapped_column(String(255), nullable=True)
    model_number: Mapped[str] = mapped_column(String(100), nullable=True)
    door_type: Mapped[str] = mapped_column(String(100), nullable=True)
    sales_talking_point: Mapped[str] = mapped_column(Text, nullable=True)
    width: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=True)
    height: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=True)
    color: Mapped[str] = mapped_column(String(255), nullable=True)
    insulation_type: Mapped[str] = mapped_column(String(100), nullable=True)
    r_value: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    panel_style: Mapped[str] = mapped_column(String(255), nullable=True)
    section_construction: Mapped[str] = mapped_column(String(255), nullable=True)
    section_thickness_in: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    section_sides: Mapped[int] = mapped_column(Integer, nullable=True)
    section_material: Mapped[str] = mapped_column(String(255), nullable=True)
    window_option: Mapped[str] = mapped_column(String(10), nullable=True)
    window_rows: Mapped[int] = mapped_column(Integer, nullable=True)
    window_type: Mapped[str] = mapped_column(String(100), nullable=True)
    finish_type: Mapped[str] = mapped_column(String(100), nullable=True)
    high_lift: Mapped[str] = mapped_column(String(10), nullable=True)
    high_lift_in: Mapped[int] = mapped_column(Integer, nullable=True)
    web_source_url: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    item: Mapped[CustomCatalogItem] = relationship(back_populates="door_spec")


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    part_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    supplier: Mapped[str] = mapped_column(String(200), nullable=True)
    vendor_id: Mapped[str] = mapped_column(String(100), nullable=True)
    category: Mapped[str] = mapped_column(String(120), nullable=True)
    location: Mapped[str] = mapped_column(String(120), nullable=True)
    manufacturer_part_number: Mapped[str] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class StockAdjustment(Base):
    __tablename__ = "stock_adjustments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("inventory_items.id"), nullable=False, index=True)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(60), nullable=False, default="manual")
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class JobPhoto(Base):
    __tablename__ = "job_photos"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="during")
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=True)
    caption: Mapped[str] = mapped_column(String(500), nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(200), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    content_type: Mapped[str] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=True)


class JobNote(Base):
    __tablename__ = "job_notes"

    # NOTE: live DB has id/job_id as text, not uuid (Flask-era schema). ORM matches
    # reality to avoid PG `operator does not exist: text = uuid` on GET.
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(String(200), nullable=False)
    author_name: Mapped[str] = mapped_column(String(200), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="internal")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class JobDiagnosis(Base):
    """Sprint 5 / S5-B1 — structured per-service-type diagnosis.

    `service_type` selects a schema key (broken_spring, opener_replacement,
    panel_damage, off_track, ...); `data` is the filled checklist. The
    schema itself lives in code (gdx_dispatch/routers/job_diagnosis.py) so dispatch
    can search rows by service_type without a free-text scan.

    Tenant-plane: isolation is the connection. No tenant_id / company_id
    column on this table by design (three-plane invariant).
    """
    __tablename__ = "job_diagnoses"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=False, index=True)
    service_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class JobHazard(Base):
    """Sprint 5 / S5-B2 — safety hazards captured at the job site.

    A hazard saved with `applies_to_customer=True` becomes a sticky
    warning that surfaces on every future job for that customer
    (insurance/warranty disputes). Otherwise it stays scoped to the job.
    """
    __tablename__ = "job_hazards"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    photo_url: Mapped[str] = mapped_column(Text, nullable=True)
    applies_to_customer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class JobReceipt(Base):
    """Sprint 5 / S5-B3 — receipt photo for parts bought on the road.

    Lines up with QBO expense reconciliation downstream.
    """
    __tablename__ = "job_receipts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=False, index=True)
    vendor: Mapped[str] = mapped_column(String(200), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=True)
    photo_url: Mapped[str] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class VehicleInspection(Base):
    """Sprint 6 / S6-B1 — pre/post-trip vehicle inspection + fuel log.

    DOT requirement for some accounts. Tied to a fleet vehicle if known
    (vehicle_id), but `vehicle_label` lets a tech log against a truck
    that isn't yet in the fleet table.
    """
    __tablename__ = "vehicle_inspections"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    vehicle_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    vehicle_label: Mapped[str] = mapped_column(String(200), nullable=True)
    technician_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    inspection_type: Mapped[str] = mapped_column(String(30), nullable=False, default="pre_trip")
    odometer: Mapped[int] = mapped_column(Integer, nullable=True)
    fuel_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    fuel_gallons: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=True)
    photo_url: Mapped[str] = mapped_column(Text, nullable=True)
    issues_found: Mapped[str] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    inspection_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class TechLocation(Base):
    """Sprint 5 / S5-C — GPS breadcrumb (tech location at a moment in time).

    One row per breadcrumb sample. Privacy: only written while the tech has
    an open clock-in entry; clock-out stops sampling. Retention is enforced
    by a Celery beat task that drops rows older than the per-tenant
    `tech_mobile.gps_retention_days` setting (default 45, min 7, max 365).
    """
    __tablename__ = "tech_locations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    technician_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True)
    lat: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    lng: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    accuracy_m: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=True)
    speed_mps: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=True)
    heading_deg: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())


class JobChatMessage(Base):
    """Sprint tech_mobile Phase 4.1 — per-job chat thread (tech ↔ dispatch).

    Flat thread keyed by job_id. Quick-action messages carry kind=
    quick_action and the body is the chip label ("On my way", "Customer
    not home", etc.). Photo attachments deferred to v2; kind=photo
    placeholder reserved.

    Read receipts: tracked dispatch-side only (the tech doesn't get
    pressured by "seen" indicators per the industry pattern). Reads
    by dispatchers stamp read_by_user_id + read_at on the row.
    """

    __tablename__ = "job_chat_messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sender_user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sender_role: Mapped[str] = mapped_column(String(20), nullable=False, default="tech")
    sender_name: Mapped[str] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Quick-action canonical key — "on_my_way", "customer_not_home", etc.
    # NULL when kind=text. Used for analytics + future i18n / icon mapping.
    quick_action: Mapped[str] = mapped_column(String(40), nullable=True)
    read_by_user_id: Mapped[str] = mapped_column(String(200), nullable=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True,
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class StickyNote(Base):
    __tablename__ = "sticky_notes"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#fef3c7")
    pos_x: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pos_y: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=240)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#6366f1")
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_tag_name"),)


class TagAssignment(Base):
    __tablename__ = "tag_assignments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tag_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "tag_id", "entity_type", "entity_id", name="uq_tag_assignment"
        ),
    )


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    account_number: Mapped[str] = mapped_column(String(100), nullable=True)
    contact_name: Mapped[str] = mapped_column(String(200), nullable=True)
    phone: Mapped[str] = mapped_column(String(30), nullable=True)
    email: Mapped[str] = mapped_column(String(200), nullable=True)
    website: Mapped[str] = mapped_column(String(500), nullable=True)
    address: Mapped[str] = mapped_column(Text, nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=True)
    zip: Mapped[str] = mapped_column(String(20), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    payment_terms: Mapped[str] = mapped_column(String(60), nullable=True)
    tax_id: Mapped[str] = mapped_column(String(50), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    qb_vendor_id: Mapped[str] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


LIFECYCLE_TRANSITIONS = {"lead": {"estimate", "cancelled"}, "estimate": {"scheduled", "cancelled"}, "scheduled": {"in_progress", "cancelled"}, "in_progress": {"completed", "cancelled"}, "completed": set(), "cancelled": set()}
DISPATCH_TRANSITIONS = {"unassigned": {"assigned", "en_route", "on_site", "done"}, "assigned": {"en_route", "on_site", "done"}, "en_route": {"on_site", "done"}, "on_site": {"done"}, "done": set()}
BILLING_TRANSITIONS = {"unbilled": {"invoiced", "partial_paid", "paid", "overdue", "void"}, "invoiced": {"partial_paid", "paid", "overdue", "void"}, "partial_paid": {"paid", "overdue", "void"}, "paid": set(), "overdue": {"void"}, "void": set()}


def validate_job_transition(job: Job, field: str, new_value: str) -> None:
    mapping = {
        "lifecycle_stage": LIFECYCLE_TRANSITIONS,
        "dispatch_status": DISPATCH_TRANSITIONS,
        "billing_status": BILLING_TRANSITIONS,
    }
    if field not in mapping:
        raise ValueError(f"Unsupported transition field '{field}'.")
    current = getattr(job, field)
    if current == new_value:
        return
    allowed = mapping[field].get(current, set())
    if new_value not in allowed:
        raise ValueError(f"Invalid {field} transition: {current} -> {new_value}. Allowed: {sorted(allowed)}")


# ---------------------------------------------------------------------------
# Batch 2: collections, invoice_reminders, appointments, scheduling, leads,
#           tasks, messages, signatures, onboarding
# ---------------------------------------------------------------------------


class PaymentReminder(Base):
    __tablename__ = "payment_reminders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    invoice_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=True)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="friendly")
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_by: Mapped[str] = mapped_column(String(200), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    promised_payment_date: Mapped[date] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ReminderSettings(Base):
    __tablename__ = "reminder_settings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schedule_days: Mapped[str] = mapped_column(Text, nullable=False, default="[7,14,30]")
    subject_template: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="Payment reminder for invoice {invoice_number}",
    )
    body_template: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=(
            "Hi {customer_name},\n\nThis is a friendly reminder that invoice "
            "{invoice_number} for ${amount_due} is now {days_overdue} days "
            "overdue. Please remit payment at your earliest convenience.\n\n"
            "Due date: {due_date}\n\nThank you."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    tech_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    address: Mapped[str] = mapped_column(String(500), nullable=True)
    lat: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    lng: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled", server_default="scheduled")
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    en_route_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    customer_name: Mapped[str] = mapped_column(String(200), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=True)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    technician_id: Mapped[str] = mapped_column(String(36), nullable=True)


class TechUnavailability(Base):
    __tablename__ = "tech_unavailability"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tech_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class LandingLead(Base):
    __tablename__ = "landing_leads"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=True)
    email: Mapped[str] = mapped_column(String(254), nullable=True)
    phone: Mapped[str] = mapped_column(String(30), nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=True)
    referrer: Mapped[str] = mapped_column(String(500), nullable=True)
    utm_campaign: Mapped[str] = mapped_column(String(200), nullable=True)
    utm_source: Mapped[str] = mapped_column(String(200), nullable=True)
    utm_medium: Mapped[str] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", index=True
    )
    contacted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    landing_lead_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=True)
    phone: Mapped[str] = mapped_column(String(30), nullable=True)
    address: Mapped[str] = mapped_column(String(500), nullable=True)
    stage: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", index=True
    )
    estimated_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0
    )
    source: Mapped[str] = mapped_column(String(100), nullable=True)
    assigned_to: Mapped[str] = mapped_column(String(200), nullable=True, index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    converted_customer_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    converted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contact_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    contacted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=True)


class InternalTask(Base):
    __tablename__ = "internal_tasks"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assigned_to: Mapped[str] = mapped_column(String(200), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    related_job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    related_customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class TeamMessage(Base):
    __tablename__ = "team_messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sender_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    sender_name: Mapped[str] = mapped_column(String(200), nullable=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class TeamMessageRecipient(Base):
    __tablename__ = "team_message_recipients"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id", "message_id", "recipient_id", name="uq_message_recipient"
        ),
    )


class DocumentSignature(Base):
    __tablename__ = "document_signatures"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    signature_data: Mapped[str] = mapped_column(Text, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(200), nullable=True)
    signed_by_email: Mapped[str] = mapped_column(String(254), nullable=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_ip: Mapped[str] = mapped_column(String(45), nullable=True)
    token: Mapped[str] = mapped_column(String(64), nullable=True, index=True, unique=True)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(200), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class OnboardingState(Base):
    __tablename__ = "onboarding_state"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, unique=True)
    current_step: Mapped[str] = mapped_column(String(50), nullable=False, default="profile")
    completed_steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    catalog_seeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    demo_data_loaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class UserTourProgress(Base):
    """Per-user in-app tour progress.

    Tenant-plane: isolation is the DB connection itself (no tenant_id column
    per CLAUDE.md three-plane invariant). `user_id` references this tenant's
    `users.id`. Version is bumped on the catalog side to force re-run after
    a tour rewrite. Status is `started`, `completed`, or `skipped`.

    Index name `ix_utp_user_id` is explicit (not the default `ix_<table>_<col>`)
    so this model matches the hand-DDL applied to existing tenants by
    `gdx_dispatch/tools/add_user_tour_progress_table.py` — keeps schema fingerprint
    identical across hand-applied and create_all-applied tenants.
    """
    __tablename__ = "user_tour_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "tour_id", name="uq_utp_user_tour"),
        Index("ix_utp_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    tour_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="started")
    last_step: Mapped[int] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Batch 3: surveys, payroll, maintenance, proposals, inbound_comms,
#           service_agreements, winback
# ---------------------------------------------------------------------------


class SurveyTemplate(Base):
    __tablename__ = "survey_templates"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="nps")
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    follow_up_question: Mapped[str] = mapped_column(String(500), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class SurveySend(Base):
    __tablename__ = "survey_sends"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    template_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    recipient_email: Mapped[str] = mapped_column(String(254), nullable=True)
    recipient_phone: Mapped[str] = mapped_column(String(30), nullable=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class SurveyResponse(Base):
    __tablename__ = "survey_responses"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    send_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    template_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=True)
    submitted_ip: Mapped[str] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TechCommissionRate(Base):
    __tablename__ = "tech_commission_rates"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tech_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rate_type: Mapped[str] = mapped_column(String(20), nullable=False, default="percent")
    rate_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    effective_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class MaintenancePlan(Base):
    __tablename__ = "maintenance_plans"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    visits_per_year: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    billing_type: Mapped[str] = mapped_column(String(20), nullable=False, default="annual")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanEnrollment(Base):
    __tablename__ = "plan_enrollments"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    next_service_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    visits_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    good_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    better_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    best_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    good_description: Mapped[str] = mapped_column(Text, nullable=True)
    better_description: Mapped[str] = mapped_column(Text, nullable=True)
    best_description: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    chosen_tier: Mapped[str] = mapped_column(String(10), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class InboundSMS(Base):
    __tablename__ = "inbound_sms"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    to_number: Mapped[str] = mapped_column(String(30), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="twilio")
    provider_message_id: Mapped[str] = mapped_column(String(100), nullable=True, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class InboundEmail(Base):
    __tablename__ = "inbound_emails"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    from_name: Mapped[str] = mapped_column(String(200), nullable=True)
    to_email: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=True)
    body_html: Mapped[str] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="m365")
    provider_message_id: Mapped[str] = mapped_column(String(200), nullable=True, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ServiceAgreementTemplate(Base):
    __tablename__ = "service_agreement_templates"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    default_duration_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    default_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    services_included: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceAgreement(Base):
    __tablename__ = "service_agreements"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    template_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    services_included: Mapped[str] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class WinbackCampaign(Base):
    __tablename__ = "winback_campaigns"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="sms")
    subject: Mapped[str] = mapped_column(String(200), nullable=True)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    inactivity_months: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class WinbackSend(Base):
    __tablename__ = "winback_sends"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    error: Mapped[str] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class FollowUp(Base):
    __tablename__ = "follow_ups"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assigned_to: Mapped[str] = mapped_column(String(200), nullable=True, index=True)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    note: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    channel: Mapped[str] = mapped_column(String(30), nullable=True)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    follow_up_type: Mapped[str] = mapped_column(String(50), nullable=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=True)


# ---------------------------------------------------------------------------
# Batch 4: job_costing, role_permissions
# ---------------------------------------------------------------------------


class MarkupRule(Base):
    __tablename__ = "markup_rules"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    markup_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=0)
    minimum_margin_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("company_id", "category", name="uq_markup_rule_category"),)


class TenantRole(Base):
    __tablename__ = "tenant_roles"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    permissions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_tenant_role_name"),)


class UserRoleAssignment(Base):
    __tablename__ = "user_role_assignments"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    role_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    assigned_by: Mapped[str] = mapped_column(String(200), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("company_id", "user_id", "role_id", name="uq_user_role_assignment"),
    )


# ---------------------------------------------------------------------------
# Phase 3: ORM models replacing _ensure_tables() DDL
# Each model matches the exact DDL from the router's CREATE TABLE statement.
# ---------------------------------------------------------------------------


class PlannerTask(Base):
    __tablename__ = "planner_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="todo")
    # 2026-04-13: default changed medium → low per Doug — "if you don't
    # think about it, it's low". High/urgent require active choice.
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    assigned_to: Mapped[str] = mapped_column(String(36), nullable=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=True)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    shared_with: Mapped[str] = mapped_column(Text, nullable=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class PlanStep(Base):
    __tablename__ = "plan_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    assigned_to: Mapped[str] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=True, default="todo")
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=True, default=0)


class MessageThread(Base):
    __tablename__ = "message_threads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="direct")
    name: Mapped[str] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class MessageThreadMember(Base):
    __tablename__ = "message_thread_members"
    thread_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    last_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    thread_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sender_id: Mapped[str] = mapped_column(String(36), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=True)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class SafetyChecklist(Base):
    __tablename__ = "safety_checklists"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    technician_id: Mapped[str] = mapped_column(String(36), nullable=False)
    items: Mapped[str] = mapped_column(Text, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    photo_url: Mapped[str] = mapped_column(Text, nullable=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class PdfTemplate(Base):
    __tablename__ = "pdf_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    template_type: Mapped[str] = mapped_column(String(50), nullable=False)
    brand_color: Mapped[str] = mapped_column(String(20), nullable=True, default="#0057a8")
    font_family: Mapped[str] = mapped_column(String(50), nullable=True, default="Helvetica")
    header_content: Mapped[str] = mapped_column(Text, nullable=True)
    footer_content: Mapped[str] = mapped_column(Text, nullable=True)
    blocks: Mapped[str] = mapped_column(Text, nullable=False)
    logo_url: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("company_id", "template_type", name="uq_pdf_template"),)


class BugReport(Base):
    __tablename__ = "bug_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=True, default="medium")
    page_url: Mapped[str] = mapped_column(Text, nullable=True)
    browser_info: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=True, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str] = mapped_column(String(36), nullable=True)
    resolution_notes: Mapped[str] = mapped_column(Text, nullable=True)


class ClientError(Base):
    __tablename__ = "client_errors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    api_url: Mapped[str] = mapped_column(Text, nullable=True)
    method: Mapped[str] = mapped_column(String(10), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=True)
    page_url: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailSetting(Base):
    __tablename__ = "email_settings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=True, default="disabled")
    smtp_host: Mapped[str] = mapped_column(String(200), nullable=True)
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=True, default=587)
    username: Mapped[str] = mapped_column(String(200), nullable=True)
    password_enc: Mapped[str] = mapped_column(Text, nullable=True)
    from_email: Mapped[str] = mapped_column(String(254), nullable=True)
    from_name: Mapped[str] = mapped_column(String(100), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class HoldingArea(Base):
    __tablename__ = "holding_areas"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str] = mapped_column(String(20), nullable=True, default="#6b7280")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="app")
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(Text, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    created_by: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class JobAssignment(Base):
    """Phase 1.4 D1+D2+D5 — multi-tech job assignment + per-tech state stamps.

    One row per (job, tech). ``Job.assigned_to`` remains the "primary"
    tech denormalization for backwards-compat with single-tech reads
    (dashboard, /api/jobs list). When dispatch assigns multiple techs,
    every tech gets a row here and a separate Appointment.tech_id row;
    ``is_lead`` marks the accountable tech (used by D4's
    ``completion_lead_tech_only`` gate).

    Three-plane: tenant connection IS the isolation; no company_id /
    tenant_id column. Existing tenants pick the table up via the
    Phase 1.4 migration script (``migrate_job_assignments_phase14``).
    """

    __tablename__ = "job_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tech_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=True)
    is_lead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=timezone.utc)
    )
    assigned_by: Mapped[str] = mapped_column(String(36), nullable=True)
    en_route_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class PushSubscription(Base):
    """Phase 1.5 E3 — DB-backed Web Push subscription.

    Replaces the in-memory ``_subscriptions`` dict in
    ``gdx_dispatch/core/push_notifications.py`` (which lost subs on every worker
    restart and couldn't fan out across workers). One row per
    (user_id, endpoint); endpoint is unique per browser/device because
    the browser's push service issues a unique URL per subscription.
    Tenant scope = the connection; no tenant_id column.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=True)
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=timezone.utc)
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class JobPartNeeded(Base):
    __tablename__ = "job_parts_needed"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    part_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=True, default=1)
    supplier: Mapped[str] = mapped_column(String(200), nullable=True)
    urgency: Mapped[str] = mapped_column(String(20), nullable=True, default="normal")
    status: Mapped[str] = mapped_column(String(20), nullable=True, default="needed")
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    # Phase 1.3 — catalog-picked SKU (free-text fallback when blank), tech who
    # filed the request (per-tech attribution for multi-tech jobs), optional
    # photo URL the tech captured at request time, dispatch-set ETA the tech
    # sees on their card.
    sku: Mapped[str] = mapped_column(String(64), nullable=True)
    requested_by_user_id: Mapped[str] = mapped_column(String(36), nullable=True)
    photo_url: Mapped[str] = mapped_column(Text, nullable=True)
    eta_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # S122 — invoice this part was billed on. Set by the invoice-create handler
    # when the operator pulls a part from the parts-from-job checklist into the
    # new invoice. Once set, the GET parts-needed?unbilled=true filter hides the
    # part so it can't be billed twice. NULL = never billed.
    #
    # FK ON DELETE SET NULL: a hard-deleted invoice releases its parts (the
    # soft-delete handler in invoices.py:delete_invoice does the same). Indexed
    # so the `unbilled=true` filter doesn't sequential-scan the table once
    # billed parts accumulate.
    billed_invoice_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class JobCloseout(Base):
    """Phase 2 closeout sheet — captures parts used + hours + signature at
    the moment a tech (or dispatcher) marks a job complete.

    Doug 2026-05-10: Phase 1 closed the silent-disappearing-job leak by
    routing the dispatch Status="Complete" through `POST /api/jobs/{id}/complete`
    so the existing tenant gates fire. Phase 2 promotes completion from a
    status flip to a closeout transaction — the dialog collects the closeout
    snapshot and writes it here for audit + billing.

    Why a separate table from `jobs`:
    - One row per job; recreatable if the job is uncompleted (soft delete
      this row, leave job_parts in place).
    - JSONB `parts_used` is the closeout-time snapshot (denormalized for
      audit). Authoritative `job_parts` rows still exist for inventory math.
    - Lets us migrate the schema additively without touching `jobs`.

    See `ai-queue/plans/sprint_job_closeout_sheet.md`.
    """

    __tablename__ = "job_closeouts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id"),
        nullable=False,
        index=True,
    )
    # Snapshot of the parts the tech logged at closeout time. List of dicts:
    #   [{ part_id?: uuid, sku?: str, name: str, qty: int, unit_cost: float, line_total: float }, ...]
    # Authoritative inventory math comes from job_parts rows; this is the
    # closeout-as-submitted snapshot for audit/billing.
    parts_used: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    hours_worked: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    signature_data: Mapped[str] = mapped_column(Text, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(200), nullable=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    closed_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc, server_default=func.now(),
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceTrigger(Base):
    __tablename__ = "service_triggers"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agreement_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    next_due: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    auto_create_job: Mapped[bool] = mapped_column(Boolean, nullable=True, default=True)
    last_triggered: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class BookingRequest(Base):
    __tablename__ = "booking_requests_router"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_date: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_slot: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    decline_reason: Mapped[str] = mapped_column(Text, nullable=True)
    approved_job_id: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class BookingJob(Base):
    __tablename__ = "booking_jobs_router"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    booking_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class MarketingCampaign(Base):
    __tablename__ = "marketing_campaigns"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="email")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    subject: Mapped[str] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=True)
    audience: Mapped[str] = mapped_column(String(64), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class ChecklistTemplate(Base):
    __tablename__ = "checklist_templates"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    items_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Checklist(Base):
    __tablename__ = "checklists"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    template_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class ChecklistItem(Base):
    __tablename__ = "checklist_items"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    checklist_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_label: Mapped[str] = mapped_column(Text, nullable=False)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CommissionRule(Base):
    __tablename__ = "commission_rules"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    parts_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    labor_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    bonus_per_review: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class CommissionEntry(Base):
    __tablename__ = "commission_entries"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parts_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    labor_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    bonus_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class EquipmentAsset(Base):
    __tablename__ = "equipment_assets"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    equipment_type: Mapped[str] = mapped_column(Text, nullable=False)
    manufacturer: Mapped[str] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=True)
    serial_number: Mapped[str] = mapped_column(Text, nullable=True)
    warranty_expires_on: Mapped[str] = mapped_column(Text, nullable=True)
    install_date: Mapped[str] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[str] = mapped_column(Text, nullable=True)


class EquipmentAssetHistory(Base):
    __tablename__ = "equipment_asset_history"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    equipment_id: Mapped[str] = mapped_column(Text, nullable=False)
    service_type: Mapped[str] = mapped_column(Text, nullable=False)
    service_date: Mapped[str] = mapped_column(Text, nullable=False)
    technician_id: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=True)


class EstimateNurtureRule(Base):
    __tablename__ = "estimate_nurture_rules"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    delay_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48)
    message_template: Mapped[str] = mapped_column(Text, nullable=True)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=True, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class EstimateNurtureLog(Base):
    __tablename__ = "estimate_nurture_log"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    estimate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=True, default="email")


class FleetVehicle(Base):
    __tablename__ = "fleet_vehicles_router"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    make: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    vin: Mapped[str] = mapped_column(Text, nullable=True)
    license_plate: Mapped[str] = mapped_column(Text, nullable=True)
    odometer: Mapped[int] = mapped_column(Integer, nullable=False)
    last_service_odometer: Mapped[int] = mapped_column(Integer, nullable=True)
    service_interval_miles: Mapped[int] = mapped_column(Integer, nullable=False)
    next_service_due_on: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[str] = mapped_column(Text, nullable=True)


class FleetServiceLog(Base):
    __tablename__ = "fleet_vehicle_service_logs_router"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    vehicle_id: Mapped[str] = mapped_column(Text, nullable=False)
    service_type: Mapped[str] = mapped_column(Text, nullable=False)
    mileage_at_service: Mapped[int] = mapped_column(Integer, nullable=False)
    service_date: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=True)


class NotificationSettings(Base):
    __tablename__ = "notifications_settings"
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    email_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sms_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sender_name: Mapped[str] = mapped_column(Text, nullable=False, default="Dispatch Team")
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class NotificationSentHistory(Base):
    __tablename__ = "notification_sent_history"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_message: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[str] = mapped_column(Text, nullable=False)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    is_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class PORequest(Base):
    __tablename__ = "po_requests"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(36), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=True)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="requested")
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class PORequestLine(Base):
    __tablename__ = "po_request_lines"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    po_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)


class SupplierCatalogItem(Base):
    __tablename__ = "supplier_catalog"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    stock_level: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    category: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class SupplierOrder(Base):
    __tablename__ = "supplier_orders"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class SupplierOrderLine(Base):
    __tablename__ = "supplier_order_lines"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)


class TimeclockEntry(Base):
    __tablename__ = "timeclock_entries_router"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    technician_id: Mapped[str] = mapped_column(Text, nullable=False)
    clock_in_at: Mapped[str] = mapped_column(Text, nullable=False)
    clock_out_at: Mapped[str] = mapped_column(Text, nullable=True)
    minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[str] = mapped_column(Text, nullable=True)


class TimeclockBreak(Base):
    __tablename__ = "timeclock_breaks_router"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    time_entry_id: Mapped[str] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(Text, nullable=False, default="lunch")
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    ended_at: Mapped[str] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class VanInventoryItem(Base):
    __tablename__ = "van_inventory"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    truck_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class VanInventoryLog(Base):
    __tablename__ = "van_inventory_log"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    van_inventory_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=True)
    quantity_change: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerReview(Base):
    """Actual customer review (rating + text). Canonical from reviews.py DDL."""
    __tablename__ = "customer_reviews"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=True)
    job_id: Mapped[str] = mapped_column(Text, nullable=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=True)
    token: Mapped[str] = mapped_column(Text, nullable=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=True)
    review_text: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    sent_at: Mapped[str] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # -- columns from production schema not yet in ORM --
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    google_reviews_link: Mapped[str] = mapped_column(String(500), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=True)


class ReviewRequest(Base):
    """Review solicitation (message + link). Renamed from marketing.py's customer_reviews."""
    __tablename__ = "review_requests"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, nullable=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    message: Mapped[str] = mapped_column(Text, nullable=True)
    google_reviews_link: Mapped[str] = mapped_column(Text, nullable=True)
    scheduled_for: Mapped[str] = mapped_column(Text, nullable=True)
    sent_at: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=True)


class CompanyModuleGrant(Base):
    """Module access grants per company. Canonical from signup.py (most complete)."""
    __tablename__ = "company_module_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    module_key: Mapped[str] = mapped_column(String(100), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("company_id", "module_key", name="uq_company_module_grant"),)


class JobDependency(Base):
    __tablename__ = "job_dependencies"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    depends_on_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class JobTemplate(Base):
    __tablename__ = "job_templates"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    default_priority: Mapped[str] = mapped_column(Text, nullable=False)
    checklist: Mapped[str] = mapped_column(Text, nullable=True)
    estimated_duration: Mapped[int] = mapped_column(Integer, nullable=False)
    default_parts: Mapped[str] = mapped_column(Text, nullable=True)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[str] = mapped_column(Text, nullable=True)


class MobileSyncAction(Base):
    __tablename__ = "mobile_sync_actions"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    company_id: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=True)
    action_type: Mapped[str] = mapped_column(Text, nullable=True)
    entity_id: Mapped[str] = mapped_column(Text, nullable=True)
    queued_at: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=True)


class PortalBookingRequest(Base):
    __tablename__ = "portal_booking_requests"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    requested_date: Mapped[str] = mapped_column(Text, nullable=False)
    service_type: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class PortalMessage(Base):
    __tablename__ = "portal_messages"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class RecurringJobSchedule(Base):
    __tablename__ = "recurring_job_schedules"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_template_id: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    next_run: Mapped[str] = mapped_column(Text, nullable=False)
    last_run: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[str] = mapped_column(Text, nullable=True)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    permissions: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class SupplierInvitation(Base):
    __tablename__ = "supplier_invitations"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    supplier_email: Mapped[str] = mapped_column(String(254), nullable=False)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    token: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class SupplierAccount(Base):
    __tablename__ = "supplier_accounts"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class SupplierTenantLink(Base):
    __tablename__ = "supplier_tenant_links"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    supplier_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    __table_args__ = (UniqueConstraint("supplier_id", "tenant_id", name="uq_supplier_tenant"),)


class User(Base):
    """Application user (login credentials + profile)."""
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(200), nullable=True)
    email: Mapped[str] = mapped_column(String(254), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=True)
    role: Mapped[str] = mapped_column(String(50), nullable=True, default="user")
    active: Mapped[bool] = mapped_column(Boolean, nullable=True, default=True)
    schedulable: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=True)
    route_start_address: Mapped[str] = mapped_column(String(500), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    # Contact
    address: Mapped[str] = mapped_column(Text, nullable=True)
    auth_email: Mapped[str] = mapped_column(String(254), nullable=True)
    google_email: Mapped[str] = mapped_column(String(254), nullable=True)
    google_id: Mapped[str] = mapped_column(String(200), nullable=True)
    # HR
    department: Mapped[str] = mapped_column(String(100), nullable=True)
    hire_date: Mapped[date] = mapped_column(Date, nullable=True)
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    commission_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    certifications: Mapped[str] = mapped_column(Text, nullable=True)
    hr_notes: Mapped[str] = mapped_column(Text, nullable=True)
    position: Mapped[str] = mapped_column(String(200), nullable=True)
    # Field
    field_skills: Mapped[str] = mapped_column(Text, nullable=True)
    field_territory: Mapped[str] = mapped_column(String(200), nullable=True)
    route_start_lat: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    route_start_lng: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    availability_status: Mapped[str] = mapped_column(String(30), nullable=True)
    # Security
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=True)
    mfa_secret: Mapped[str] = mapped_column(String(200), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Timeclock permissions
    tc_can_approve: Mapped[bool] = mapped_column(Boolean, nullable=True)
    tc_can_edit: Mapped[bool] = mapped_column(Boolean, nullable=True)
    tc_can_view_others: Mapped[bool] = mapped_column(Boolean, nullable=True)
    tc_permissions: Mapped[str] = mapped_column(Text, nullable=True)
    # Emergency contact
    emergency_contact_name: Mapped[str] = mapped_column(String(200), nullable=True)
    emergency_contact_phone: Mapped[str] = mapped_column(String(50), nullable=True)
    # Feature flags
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=True)
    # Sprint dispatch-capacity (2026-05-20) — per-user shift override.
    # All three NULL = inherit AppSettings.default_shift_*. Set any one to
    # override only that field (e.g. an early-start tech keeps the tenant's
    # 17:00 end but moves shift_start to 07:00). workdays is the same
    # bitmask shape as AppSettings.default_workdays (Mon=1..Sun=64).
    shift_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    shift_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    workdays: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class PayrollEntry(Base):
    """One row per pay period per tech — UX audit F-82 / 2026-04-29.

    Holds the *true* labor cost: gross_pay over hours_paid for a date
    range. Job-costing prefers this over Technician.hourly_rate (the
    *estimated* rate fallback) when an entry covers the work date.

    `source` records how the row landed — 'manual' (Doug typed it),
    'csv_import' (CSV upload), or future: 'gusto', 'qbo_payroll'.
    External-first per Doug 2026-04-29.
    """
    __tablename__ = "payroll_entries"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tech_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    hours_paid: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    gross_pay: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    external_ref: Mapped[str] = mapped_column(String(100), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Technician(Base):
    """Field technician profile linked to a user."""
    __tablename__ = "technicians"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=True)
    email: Mapped[str] = mapped_column(String(254), nullable=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=True)
    skills: Mapped[str] = mapped_column(Text, nullable=True)
    hourly_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=True, default=True)
    territory: Mapped[str] = mapped_column(String(200), nullable=True)
    availability_status: Mapped[str] = mapped_column(String(30), nullable=True)
    commission_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerLocation(Base):
    __tablename__ = "customer_locations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=True)
    address: Mapped[str] = mapped_column(Text, nullable=True)
    access_notes: Mapped[str] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- columns from production schema not yet in ORM --
    city: Mapped[str] = mapped_column(String(120), nullable=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lat: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    lng: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=True)
    zip: Mapped[str] = mapped_column(String(20), nullable=True)


class TaxJurisdiction(Base):
    __tablename__ = "tax_jurisdictions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class WarrantyClaim(Base):
    __tablename__ = "warranty_claims"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    warranty_id: Mapped[str] = mapped_column(String(36), nullable=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=True)
    customer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    serial_number: Mapped[str] = mapped_column(String(120), nullable=True)
    manufacturer: Mapped[str] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="filed")
    claim_notes: Mapped[str] = mapped_column(Text, nullable=True)
    filed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


# ── Utility / logging tables (previously DDL-only, now ORM-managed) ──────────


class SecurityEvent(Base):
    __tablename__ = "security_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str] = mapped_column(Text, nullable=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class AiQuoteLog(Base):
    __tablename__ = "ai_quote_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=True)
    input_notes: Mapped[str] = mapped_column(Text, nullable=True)
    generated_quote: Mapped[dict] = mapped_column(JSON, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=True)
    final_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=True)
    feedback_notes: Mapped[str] = mapped_column(Text, nullable=True)
    feedback_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AiUsageLog(Base):
    __tablename__ = "ai_usage_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=True)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=True)
    details: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class PerformanceSlowEvent(Base):
    __tablename__ = "performance_slow_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=True)
    path: Mapped[str] = mapped_column(Text, nullable=True)
    sql_text: Mapped[str] = mapped_column(Text, nullable=True)
    params_json: Mapped[str] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


class GdprDataAccessLog(Base):
    __tablename__ = "gdpr_data_access_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=True)
    access_type: Mapped[str] = mapped_column(Text, nullable=False)
    fields_accessed: Mapped[str] = mapped_column(Text, nullable=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=True)
    details: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


# ---------------------------------------------------------------------------
# Phase 2: ORM models for tables that existed as raw DDL but had no model
# Added 2026-04-12 an earlier session — needed for nuke-and-pave schema parity
# ---------------------------------------------------------------------------


class ChiDoorCatalog(Base):
    """CHI garage door product catalog — imported from CHI's data feed."""
    __tablename__ = "chi_door_catalog"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sku: Mapped[str] = mapped_column(String(255), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=True)
    brand: Mapped[str] = mapped_column(String(255), nullable=True)
    manufacturer: Mapped[str] = mapped_column(String(255), nullable=True)
    model_number: Mapped[str] = mapped_column(String(100), nullable=True)
    door_type: Mapped[str] = mapped_column(String(100), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    sales_talking_point: Mapped[str] = mapped_column(Text, nullable=True)
    width: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=True)
    height: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=True)
    color: Mapped[str] = mapped_column(String(255), nullable=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    insulation_type: Mapped[str] = mapped_column(String(100), nullable=True)
    r_value: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    panel_style: Mapped[str] = mapped_column(String(255), nullable=True)
    section_construction: Mapped[str] = mapped_column(String(255), nullable=True)
    section_thickness_in: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=True)
    section_sides: Mapped[int] = mapped_column(Integer, nullable=True)
    section_material: Mapped[str] = mapped_column(String(255), nullable=True)
    window_option: Mapped[str] = mapped_column(String(10), nullable=True)
    window_rows: Mapped[int] = mapped_column(Integer, nullable=True)
    window_type: Mapped[str] = mapped_column(String(100), nullable=True)
    finish_type: Mapped[str] = mapped_column(String(100), nullable=True)
    high_lift: Mapped[str] = mapped_column(String(10), nullable=True)
    high_lift_in: Mapped[int] = mapped_column(Integer, nullable=True)
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    web_source_url: Mapped[str] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    chi_order_number: Mapped[str] = mapped_column(String(100), nullable=True)
    sell_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    # Sprint 1.0.5 — engine label. Defaults 'doors' at signup/pave seeder.
    pricing_category: Mapped[str] = mapped_column(String(40), nullable=True, index=True, default="doors")


class ChiPartsCatalog(Base):
    """CHI parts/accessories catalog — imported from CHI's data feed."""
    __tablename__ = "chi_parts_catalog"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sku: Mapped[str] = mapped_column(String(255), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    part_type: Mapped[str] = mapped_column(String(100), nullable=True)
    brand: Mapped[str] = mapped_column(String(255), nullable=True)
    manufacturer: Mapped[str] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(255), nullable=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    rail_length_ft: Mapped[int] = mapped_column(Integer, nullable=True)
    mount_type: Mapped[str] = mapped_column(String(100), nullable=True)
    window_style: Mapped[str] = mapped_column(String(100), nullable=True)
    window_inserts: Mapped[str] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sell_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    # Sprint 1.0.5 — engine label. Defaults 'parts' at signup/pave seeder.
    pricing_category: Mapped[str] = mapped_column(String(40), nullable=True, index=True, default="parts")


class ChangeOrderLine(Base):
    """Line item on a change order."""
    __tablename__ = "change_order_lines"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    co_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)


class QBPnlMonthly(Base):
    """Cached QuickBooks Profit & Loss totals by month × account.

    Sprint monthly-budget (2026-05-24). Filled by
    gdx_dispatch.modules.quickbooks.pnl.pull_profit_and_loss — one QBO Report API
    call per year per tenant returns all 12 months × every P&L account.
    Cached so budget vs actual is fast and survives QBO rate limits.

    Refreshed on demand by the budget UI (current month auto-refreshes
    when viewed; older months can be re-pulled manually). Amounts stored
    as positive dollars for Expense / COGS rows and as Income for
    Income rows — callers filter by account_type when summing.
    """
    __tablename__ = "qb_pnl_monthly"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    qb_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    account_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("year", "month", "qb_account_id", name="uq_qb_pnl_monthly_year_month_account"),
        Index("ix_qb_pnl_monthly_year_month", "year", "month"),
    )


class MonthlyBudget(Base):
    """One expense-budget line per (year, month, qb_account).

    Sprint monthly-budget (2026-05-24). Source of truth lives in GDX; QB is
    read-only for budgets. ``qb_account_id`` is a soft reference to the
    qb_accounts.qb_account_id value — no FK because qb_accounts is inline DDL
    in sync.pull_accounts (not ORM-managed) and the account may be removed
    upstream while we still want the historical budget row.
    """
    __tablename__ = "monthly_budgets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    qb_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    # 'fixed' | 'variable' | 'percent_of_revenue'
    line_type: Mapped[str] = mapped_column(String(20), nullable=False, default="fixed")
    pct_of_revenue: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    # 'auto_seed' | 'user'
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("year", "month", "qb_account_id", name="uq_monthly_budget_year_month_account"),
        Index("ix_monthly_budgets_year_month", "year", "month"),
    )


class Company(Base):
    """Tenant's own company record — referenced by payments and Stripe."""
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    stripe_connect_account_id: Mapped[str] = mapped_column(String(100), nullable=True)
    stripe_customer_id: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)


# ─── S122-14 + S122-17: auto-mark Invoice/Customer qb_dirty on update ───────
#
# When ANY field on an Invoice or Customer changes, the row needs to push to
# QuickBooks. Pre-fix the Celery sync tasks iterated every non-paid invoice
# (status != 'paid') / every customer on every run; most pushes were no-ops
# because the row hadn't actually changed since the last successful push.
#
# These event listeners auto-flip ``qb_dirty=True`` whenever the ORM is
# about to UPDATE — UNLESS the only column changing is qb_dirty or
# qb_synced_at itself (so push_* can clear the flag without bouncing).
# Raw-SQL UPDATEs and bulk ``Session.execute(update(...))`` statements
# bypass the listener; that's an accepted limitation (every prod write
# path uses ORM attribute-set today).
_QB_DIRTY_INTERNAL_COLS = frozenset({"qb_dirty", "qb_synced_at"})


def _mark_qb_dirty_on_change(target) -> None:
    state = inspect(target)
    for attr in state.attrs:
        if attr.key in _QB_DIRTY_INTERNAL_COLS:
            continue
        if attr.history.has_changes():
            target.qb_dirty = True
            return


@event.listens_for(Invoice, "before_update")
def _mark_invoice_qb_dirty_on_change(_mapper, _connection, target: Invoice) -> None:
    _mark_qb_dirty_on_change(target)


@event.listens_for(Customer, "before_update")
def _mark_customer_qb_dirty_on_change(_mapper, _connection, target: Customer) -> None:
    _mark_qb_dirty_on_change(target)
