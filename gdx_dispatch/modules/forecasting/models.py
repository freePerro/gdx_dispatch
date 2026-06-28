"""Forecasting ORM models. Tenant-plane (per-tenant DB).

Per three-plane isolation rule: no tenant_id filter columns; isolation is
the connection. Registered onto TenantBase.metadata via import in
gdx_dispatch/models/__init__.py.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.models.tenant_models import TenantBase

# Stream sources
STREAM_SOURCE_OBSERVED = "observed"
STREAM_SOURCE_MANUAL = "manual"
STREAM_SOURCE_QBO_TEMPLATE = "qbo_template"

# Stream lifecycle
STREAM_STATUS_SUGGESTED = "suggested"
STREAM_STATUS_ACTIVE = "active"
STREAM_STATUS_PAID_OFF = "paid_off"
STREAM_STATUS_CANCELLED = "cancelled"
STREAM_STATUS_EXPIRED = "expired"

# Forecast-snapshot lifecycle (Stage A measurement loop)
SNAPSHOT_STATUS_PENDING = "pending"
SNAPSHOT_STATUS_RECONCILED = "reconciled"

# AR aging buckets, in order. Mirrors service._ar_aging_bucket boundaries.
AR_BUCKETS = ("0_30", "31_60", "61_90", "90_plus")

# Cadence options — covers everything from weekly utilities to annual policies
CADENCE_WEEKLY = "weekly"
CADENCE_BIWEEKLY = "biweekly"
CADENCE_MONTHLY = "monthly"
CADENCE_QUARTERLY = "quarterly"
CADENCE_SEMIANNUAL = "semiannual"
CADENCE_ANNUAL = "annual"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Industry-default AR collection probabilities by aging bucket. Tenants
# can override any of these via the settings endpoint. Numbers come from
# the AR-aging benchmark research: healthy SMBs see 0-30 collect at
# ~95%, 31-60 at ~80%, 61-90 at ~60%, 90+ at ~30%.
DEFAULT_COLLECT_0_30 = 0.95
DEFAULT_COLLECT_31_60 = 0.80
DEFAULT_COLLECT_61_90 = 0.60
DEFAULT_COLLECT_90_PLUS = 0.30
DEFAULT_SCHEDULED_REALIZATION = 0.70
DEFAULT_WINDOW_DAYS = 30


class ForecastSettings(TenantBase):
    """Per-tenant forecasting configuration. Singleton row per tenant DB."""

    __tablename__ = "tenant_forecast_settings"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    default_window_days: Mapped[int] = mapped_column(Numeric(4, 0), nullable=False, default=DEFAULT_WINDOW_DAYS, server_default=str(DEFAULT_WINDOW_DAYS))
    collect_rate_0_30: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=DEFAULT_COLLECT_0_30)
    collect_rate_31_60: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=DEFAULT_COLLECT_31_60)
    collect_rate_61_90: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=DEFAULT_COLLECT_61_90)
    collect_rate_90_plus: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=DEFAULT_COLLECT_90_PLUS)
    scheduled_realization_rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=DEFAULT_SCHEDULED_REALIZATION)
    include_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class QBRecurringTransaction(TenantBase):
    """Mirror of QuickBooks Online RecurringTransaction entity.

    Fields kept compact; full payload preserved in `raw_json` for forward
    compatibility (QBO has nested RecurringInfo + ScheduleInfo with many
    optional fields we don't need to columnize today).
    """

    __tablename__ = "qb_recurring_transactions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    qb_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    txn_type: Mapped[str] = mapped_column(String(40), nullable=False)  # Invoice / Bill / JournalEntry / SalesReceipt
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_qb_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    next_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    interval_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Daily / Weekly / Monthly / Yearly
    num_interval: Mapped[int | None] = mapped_column(Numeric(4, 0), nullable=True)
    days_of_week: Mapped[str | None] = mapped_column(String(60), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class RecurringStream(TenantBase):
    """A recurring payment stream — either detected from bank activity or
    labeled manually by the user.

    Detected streams arrive as `source='observed', status='suggested'` from
    the nightly detector. Users confirm them (→ `active`) or dismiss them
    (→ soft-delete). Manual streams arrive as `source='manual', status='active'`.

    Term is dual-mode: a stream can specify `term_total_occurrences` (e.g. a
    36-payment loan) OR `term_end_date` (e.g. an insurance policy ending
    2027-09-01) OR neither (open-ended subscription like Phone.com). The
    forecasting service stops projecting once whichever applies is reached.

    Ending a stream (paid off / cancelled / expired) keeps all history
    intact; status flips and `ended_at`/`ended_reason` are populated.
    Forecast stops including it past `ended_at`.
    """

    __tablename__ = "recurring_streams"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=STREAM_SOURCE_MANUAL)

    # Matcher — normalized payee fragment used by detector + dedup against QBO
    # templates. amount_min/max define the tolerance window.
    payee_pattern: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    amount_min: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    amount_max: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Cadence — coarse enum + anchor day. cadence_anchor_day is 1..31 for
    # monthly+, 1..7 for weekly/biweekly (1=Mon). Used to project next date.
    cadence: Mapped[str] = mapped_column(String(20), nullable=False, default=CADENCE_MONTHLY)
    cadence_anchor_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Term — either occurrences OR end_date OR neither (open-ended). CHECK
    # constraint below enforces the XOR-or-neither shape so a stream can't
    # carry conflicting fields. Integer (not Numeric) so arithmetic stays Python int.
    term_total_occurrences: Mapped[int | None] = mapped_column(Integer, nullable=True)
    term_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STREAM_STATUS_ACTIVE, index=True)
    occurrences_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_expected_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    last_observed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ended_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    hits: Mapped[list["RecurringStreamHit"]] = relationship(
        "RecurringStreamHit", back_populates="stream", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_recurring_streams_status_next", "status", "next_expected_date"),
        Index("ix_recurring_streams_source_status", "source", "status"),
        # Dual-term shape: at most ONE of term_total_occurrences / term_end_date may be set.
        CheckConstraint(
            "(term_total_occurrences IS NULL) OR (term_end_date IS NULL)",
            name="ck_recurring_streams_term_xor",
        ),
        # Amount window must be a window, not inverted.
        CheckConstraint(
            "amount_min <= amount_max",
            name="ck_recurring_streams_amount_window",
        ),
        # Anchor day range: weekly 1..7, monthly+ 1..31. NULL is fine.
        CheckConstraint(
            "cadence_anchor_day IS NULL OR (cadence_anchor_day BETWEEN 1 AND 31)",
            name="ck_recurring_streams_anchor_day",
        ),
        # Non-negative observation counter.
        CheckConstraint(
            "occurrences_seen >= 0",
            name="ck_recurring_streams_occurrences_seen",
        ),
    )


class RecurringStreamHit(TenantBase):
    """Join row connecting an observed bank transaction to a RecurringStream.

    Detector writes rows with `confirmed=false`; user can flip to true (or
    unlink) from the UI. qb_txn_id is the string id from `qb_bank_transactions`
    (not a hard FK — we tolerate that source row being repaved/resynced).
    """

    __tablename__ = "recurring_stream_hits"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    stream_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("recurring_streams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qb_txn_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    stream: Mapped["RecurringStream"] = relationship("RecurringStream", back_populates="hits")

    __table_args__ = (
        Index("ix_recurring_stream_hits_stream_date", "stream_id", "txn_date"),
        # Detector re-runs must not double-count the same bank txn against a
        # stream; without this a cron retry inflates occurrences_seen and a
        # 36-payment loan falsely declares paid_off early.
        UniqueConstraint("stream_id", "qb_txn_id", name="uq_recurring_stream_hit_txn"),
    )


class ForecastSnapshot(TenantBase):
    """A point-in-time capture of the open-AR population, scored later against
    actual collections to measure *within-window collection realization*
    (Stage A — see docs/forecasting-accuracy-roadmap.md).

    What this measures (and what it deliberately does NOT):
      The headline AR projection multiplies open balance by a *lifetime*
      collection rate (95/80/60/30% — "this fraction will EVENTUALLY pay") and
      ignores the window. Comparing that lifetime number to cash that lands in
      a 30-day window is a horizon mismatch, not forecast error — an early
      design did exactly that and an adversarial audit rejected it. So this
      loop does not score the lifetime number. Instead, per aging bucket, it
      records the fraction of snapshotted AR that is actually collected WITHIN
      the window — the empirical within-window rate. That is the dimensionally
      coherent quantity, and it is exactly the input Stage B needs to replace
      the hard-coded lifetime defaults with window-calibrated rates.

    The invoice population at `as_of` is stored in child rows
    (ForecastSnapshotInvoice) rather than a JSON id blob, so a tenant with
    thousands of open invoices doesn't balloon a single column.
    """

    __tablename__ = "forecast_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    as_of: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    horizon_end: Mapped[date] = mapped_column(Date, nullable=False)  # as_of + window_days

    open_ar_face: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    # The lifetime per-bucket rates in effect at capture, {bucket: rate}. Kept
    # for the calibration comparison; NOT the thing being scored.
    assumed_rates: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=SNAPSHOT_STATUS_PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # Reconciliation — null until the window closes and the scorer runs.
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-bucket results, {bucket: {face, collected_in_window, observed_window_rate, assumed_lifetime_rate}}.
    bucket_results: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    invoices: Mapped[list["ForecastSnapshotInvoice"]] = relationship(
        "ForecastSnapshotInvoice", back_populates="snapshot", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_forecast_snapshots_status_horizon", "status", "horizon_end"),
        CheckConstraint("window_days > 0", name="ck_forecast_snapshots_window_pos"),
    )


class ForecastSnapshotInvoice(TenantBase):
    """One open invoice captured in a ForecastSnapshot, with the aging bucket
    and face value it had AT snapshot time.

    `invoice_id` is not a hard FK — like RecurringStreamHit.qb_txn_id, we
    tolerate the source invoice being repaved/deleted without dropping the
    historical measurement. `face_at_snapshot` is frozen so reconciliation
    measures collection against the balance that was actually open then, not
    whatever the invoice looks like later.
    """

    __tablename__ = "forecast_snapshot_invoices"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("forecast_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    bucket: Mapped[str] = mapped_column(String(10), nullable=False)
    face_at_snapshot: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    snapshot: Mapped["ForecastSnapshot"] = relationship("ForecastSnapshot", back_populates="invoices")

    __table_args__ = (
        Index("ix_forecast_snapshot_invoices_snap_bucket", "snapshot_id", "bucket"),
        # A given invoice appears at most once per snapshot.
        UniqueConstraint("snapshot_id", "invoice_id", name="uq_forecast_snapshot_invoice"),
    )
