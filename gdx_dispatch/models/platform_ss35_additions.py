"""SS-35 model stub additions — PII flow / SAR / erasure.

INTEGRATION_TODO: these definitions live on a separate declarative Base
(``SS35Base``) and are NOT yet mounted on the primary platform Base in
``gdx_dispatch/models/platform.py``. The SS-35 Alembic migration
(``TODO_ss35_pii_tracking_XXXX.py``) sits on placeholder
``down_revision = "INTEGRATION_TODO"``.

Tables:
    - sar_request      — Subject Access Request filings + completed
                         export blob + single-use signed-URL token.
    - erasure_request  — Right-to-erasure filings + cooloff window +
                         operator approval + execution result.
    - pii_event_log    — append-only hash-chained log of SAR/erasure
                         events for compliance audit (companion to
                         SS-28 audit chain).

Tenant semantics
----------------

SARs and erasures are IDENTITY-SCOPED, not tenant-scoped — an identity
may belong to multiple tenants, and the SAR must return data across
all of them. The reporting role (SS-17 security-definer) is used when
running the walk; these control rows carry the target identity but no
tenant_id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    JSON,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS35Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SARRequest(SS35Base):
    """One row per filed Subject Access Request.

    ``export_json`` is the built SAR payload (a dict — stored as JSON).
    ``download_token`` + ``download_issued_at`` support single-use
    24-hour signed URLs; ``downloaded_at`` is set on first redemption
    and further attempts return 410.
    """

    __tablename__ = "sar_request"
    __table_args__ = (
        Index("ix_sar_target", "target_identity_id"),
        Index("ix_sar_status", "status"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    target_identity_id = Column(String(64), nullable=False)
    requested_by_identity_id = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="queued")
    export_json = Column(JSON, nullable=True)
    download_token = Column(String(256), nullable=True)
    download_issued_at = Column(String(64), nullable=True)
    downloaded_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ErasureRequest(SS35Base):
    """One row per filed right-to-erasure request.

    Lifecycle:
        queued → cooloff → approved → executed
                       └─> cancelled

    ``cooloff_until`` defaults to ``created_at + 30d``. ``executed_at``
    is only populated once an operator approves AND cooloff has elapsed.
    """

    __tablename__ = "erasure_request"
    __table_args__ = (
        Index("ix_erasure_target", "target_identity_id"),
        Index("ix_erasure_status", "status"),
        Index("ix_erasure_cooloff", "cooloff_until"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    target_identity_id = Column(String(64), nullable=False)
    requested_by_identity_id = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="queued")
    cooloff_until = Column(DateTime(timezone=True), nullable=False)
    approved_by_identity_id = Column(String(64), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    affected_field_count = Column(BigInteger, nullable=True)
    affected_summary = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class PIIEventLog(SS35Base):
    """Append-only hash-chained log of PII-governance events.

    Companion to the SS-28 audit chain — SAR + erasure events are
    written here AND emitted to ``event_outbox``. The hash chain makes
    tampering detectable even if the outbox is pruned.

    ``prev_hash`` points to the previous row's ``entry_hash``; the
    root row has ``prev_hash = ""``. ``entry_hash`` is SHA-256 over
    ``prev_hash|event_name|identity_id|payload_json|created_at``.
    """

    __tablename__ = "pii_event_log"
    __table_args__ = (
        Index("ix_pii_log_identity", "target_identity_id"),
        Index("ix_pii_log_event", "event_name"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_name = Column(String(128), nullable=False)
    target_identity_id = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=True)
    prev_hash = Column(String(64), nullable=False, default="")
    entry_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
