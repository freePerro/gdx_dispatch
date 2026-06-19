"""SS-32 model stub additions — SPIFFE workload registry + bundle cache.

TODO: these definitions live on a separate declarative
``SS32Base`` and are NOT mounted on the primary platform ``Base`` in
``gdx_dispatch/models/platform.py``. The SS-32 Alembic migration
(``TODO_ss32_spiffe_XXXX.py``) sits on placeholder
``down_revision = "TODO"``.

Tables:

* ``spiffe_workload_registration`` — one row per registered workload
  (SPIFFE-ID glob + capabilities + metadata). Overlays the
  JSON-declared defaults in :file:`gdx_dispatch/core/spiffe/workload_caps.json`.
* ``spiffe_trust_bundle_cache`` — persisted snapshot of the last-known
  SPIRE trust bundle per trust domain, so a restarted app serves stale
  bundles instantly rather than blocking on SPIRE at boot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    JSON,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS32Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SpiffeWorkloadRegistration(SS32Base):
    """A super-admin-registered SPIFFE workload.

    ``spiffe_id_glob`` is the pattern (see workload_capability_map glob
    semantics); ``capabilities`` is a JSON array of capability strings;
    ``tenant_scope`` is one of ``global`` / ``per-tenant``. Soft-delete
    via ``deleted_at``.
    """

    __tablename__ = "spiffe_workload_registration"
    __table_args__ = (
        Index("ix_spiffe_workload_glob", "spiffe_id_glob", unique=True),
        Index("ix_spiffe_workload_scope", "tenant_scope"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    spiffe_id_glob = Column(String(512), nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    tenant_scope = Column(String(32), nullable=False, default="per-tenant")
    spiffe_metadata = Column(JSON, nullable=True)
    registered_by_identity_id = Column(String(64), nullable=False)
    notes = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class SpiffeTrustBundleCache(SS32Base):
    """Persisted per-trust-domain bundle snapshot.

    One row per trust domain. ``bundle_json`` is the full upstream
    payload (x509_authorities + jwt_authorities). ``fetched_at`` /
    ``ttl_seconds`` drive the in-memory cache seed at app boot.
    """

    __tablename__ = "spiffe_trust_bundle_cache"
    __table_args__ = (
        Index("ix_spiffe_bundle_td", "trust_domain", unique=True),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trust_domain = Column(String(255), nullable=False)
    bundle_json = Column(JSON, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    ttl_seconds = Column(String(16), nullable=False, default="300")
    source_endpoint = Column(String(512), nullable=True)
    last_refresh_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
