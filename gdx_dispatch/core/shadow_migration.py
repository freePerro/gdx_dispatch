"""SS-29 slice B — ShadowWriter session wrapper for dual-write.

``ShadowWriter`` wraps a SQLAlchemy session. When a tenant is in
``shadow`` mode for a given table, every write to the canonical (v1)
row is replicated into the shadowed (v2) row using the mapping declared
in :mod:`gdx_dispatch.core.shadow_schema_map`. Drift is detected by computing a
canonical-JSON sha256 over the transformed old row and comparing it
against the same hash computed from the stored new row.

Design rules (per SS-29 plan "Rules" section):

* **Shadow writes MUST NEVER fail the canonical write.** Any exception
  raised by a shadow-write path is caught, logged loudly (``logger.error``
  with full context), and recorded in ``shadow_migration_drift`` — and
  then swallowed so the caller's canonical transaction survives.
* **Deterministic hashing.** Drift detection reuses the canonical-JSON
  pattern from :mod:`gdx_dispatch.core.audit_hash_chain`. Two rows that *should*
  be equal under the mapping produce identical sha256 digests byte-for-byte.
* **Mode state is external.** ShadowWriter reads the current mode for a
  (tenant, old_table) from ``shadow_migration_state`` (see
  :mod:`gdx_dispatch.models.platform_ss29_additions`) via a small fetcher that
  can be stubbed in tests. Mode transitions go through the admin router,
  not through this module.
* **Idempotent.** Re-shadowing a row that already exists in the new
  table with the same content is a no-op (zero drift rows).

TODO: at the main-chain merge, ShadowWriter will be wired
into the session-factory middleware so every tenant request auto-wraps
its session. Until then, callers invoke ``ShadowWriter(db).shadow_write(...)``
explicitly — used by slices C, D, E for tests and tooling.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from gdx_dispatch.core.audit_hash_chain import canonical_json
from gdx_dispatch.core.shadow_schema_map import ShadowMap, is_shadowed, shadow_for

logger = logging.getLogger(__name__)


# Mode constants — mirror the ``shadow_migration_state.mode`` enum values.
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_CUTOVER = "cutover"

_VALID_MODES = frozenset({MODE_OFF, MODE_SHADOW, MODE_CUTOVER})


# Module-level error counter. Fail-open paths increment this so the
# supervisor / metrics can observe silent-shadow-failure rate without
# grepping logs. Keyed by ``(op, error_type)``.
_SHADOW_ERROR_COUNTERS: dict[tuple[str, str], int] = {}


def _record_shadow_error(op: str, exc: BaseException) -> None:
    """Bump the named error counter for a fail-open shadow-write path.

    Rationale: the SS-29 contract says shadow writes MUST NEVER take
    down the canonical transaction (see module docstring "Rules" §1).
    That forces a broad catch at each fail-open seam. To keep the
    failure observable, we (a) log.error with exc_info, (b) record
    structured drift rows where relevant, and (c) increment this
    counter so supervisors can alert on a spike.
    """
    key = (op, type(exc).__name__)
    _SHADOW_ERROR_COUNTERS[key] = _SHADOW_ERROR_COUNTERS.get(key, 0) + 1


def shadow_error_counters() -> dict[tuple[str, str], int]:
    """Snapshot of fail-open shadow-write error counts (for supervisors)."""
    return dict(_SHADOW_ERROR_COUNTERS)


@dataclass(frozen=True)
class ShadowWriteResult:
    """Result of a single ``shadow_write`` call."""

    mode: str
    shadowed: bool
    drift: bool
    drift_reason: str | None = None
    old_hash: str | None = None
    new_hash: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def row_fingerprint(row: Mapping[str, Any]) -> str:
    """Canonical sha256 of a row dict.

    Reuses :func:`canonical_json` so the drift detector speaks the same
    dialect as the audit hash chain. None-valued keys are included so a
    silently-dropped column is detectable.
    """
    return hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()


class ShadowWriter:
    """Dual-writer: canonical row goes through untouched; shadow row is
    replicated into the v2 table and compared byte-for-byte post-write.

    Parameters
    ----------
    db:
        SQLAlchemy session (caller-owned, caller-committed).
    mode_lookup:
        Callable ``(tenant_id, old_table) -> mode_str``. Defaults to the
        DB-backed lookup in this module. Tests pass a stub.
    insert_new_row:
        Callable ``(new_table, row_dict) -> None`` that performs the
        actual INSERT into the v2 table. Defaults to a no-op so the
        class is importable before any v2 table exists. Production
        wiring will pass a SQL-exec callable.
    read_new_row:
        Callable ``(new_table, pk_col, pk_value) -> dict | None`` for
        drift-check reads. Default is a no-op returning None (in which
        case drift is reported as "new_row_missing" — tests pass a real
        reader).
    """

    def __init__(
        self,
        db: Any,
        *,
        mode_lookup: Callable[[str, str], str] | None = None,
        insert_new_row: Callable[[str, dict[str, Any]], None] | None = None,
        read_new_row: Callable[[str, str, Any], dict[str, Any] | None] | None = None,
    ) -> None:
        self.db = db
        self._mode_lookup = mode_lookup or _db_mode_lookup
        self._insert_new_row = insert_new_row or _noop_insert
        self._read_new_row = read_new_row or _noop_read

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current_mode(self, tenant_id: str, old_table: str) -> str:
        """Return the current mode for this (tenant, table) pair."""
        if not is_shadowed(old_table):
            return MODE_OFF
        mode = self._mode_lookup(self.db, tenant_id, old_table) if self._mode_lookup is _db_mode_lookup else self._mode_lookup(tenant_id, old_table)  # type: ignore[arg-type]
        if mode not in _VALID_MODES:
            logger.warning(
                "shadow_writer: unrecognized mode=%r for tenant=%s table=%s — treating as off",
                mode, tenant_id, old_table,
            )
            return MODE_OFF
        return mode

    def shadow_write(
        self,
        *,
        tenant_id: str,
        old_table: str,
        old_row: Mapping[str, Any],
    ) -> ShadowWriteResult:
        """Replicate ``old_row`` to the v2 table if the tenant is in shadow
        or cutover mode. Return a structured result; never re-raise.
        """
        if not tenant_id:
            raise ValueError("shadow_write: tenant_id is required")
        if not old_table:
            raise ValueError("shadow_write: old_table is required")

        # Fail-open on mode lookup: per SS-29 contract we MUST NOT raise
        # into the caller's canonical transaction. We catch broadly
        # because mode_lookup is pluggable (stubs in tests, SQLA in prod)
        # and can raise anything from OperationalError to AttributeError.
        # Loud structured log + counter keeps it observable.
        try:
            mode = self.current_mode(tenant_id, old_table)
        except Exception as exc:  # noqa: BLE001 — fail-open on mode lookup (see comment above)
            _record_shadow_error("mode_lookup", exc)
            logger.error(
                "shadow_writer.mode_lookup_failed",
                extra={
                    "op": "mode_lookup",
                    "tenant_id": tenant_id,
                    "old_table": old_table,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            return ShadowWriteResult(mode=MODE_OFF, shadowed=False, drift=False,
                                    drift_reason="mode_lookup_failed")

        if mode == MODE_OFF:
            return ShadowWriteResult(mode=MODE_OFF, shadowed=False, drift=False)

        try:
            sm = shadow_for(old_table)
        except KeyError:
            # Table is modeless-but-not-mapped; treat as off.
            return ShadowWriteResult(mode=mode, shadowed=False, drift=False)

        # Apply rename + transforms. Fail-open: user-supplied transform
        # callables in ShadowMap can raise anything (TypeError on shape
        # mismatch, KeyError on missing field, custom exceptions). We
        # broad-catch to honor the "never break canonical" contract;
        # every failure lands in shadow_migration_drift for audit.
        try:
            new_row = sm.transform_row(old_row)
        except Exception as exc:  # noqa: BLE001 — fail-open on transform (see comment)
            _record_shadow_error("transform", exc)
            self._record_drift_row(
                tenant_id=tenant_id, old_table=old_table, reason="transform_failed",
                old_hash=None, new_hash=None,
                details={"error": str(exc), "error_type": type(exc).__name__},
            )
            logger.error(
                "shadow_writer.transform_failed",
                extra={
                    "op": "transform",
                    "tenant_id": tenant_id,
                    "old_table": old_table,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            return ShadowWriteResult(
                mode=mode, shadowed=False, drift=True,
                drift_reason="transform_failed",
            )

        old_hash = row_fingerprint(new_row)  # shape of what SHOULD land in v2
        shadowed = False
        # Fail-open on INSERT into v2 table. Pluggable insert_new_row
        # may raise SQLA IntegrityError / OperationalError / anything
        # from test stubs. Contract: canonical transaction MUST survive.
        try:
            self._insert_new_row(sm.new_table, dict(new_row))
            shadowed = True
        except Exception as exc:  # noqa: BLE001 — fail-open on shadow insert (see comment)
            _record_shadow_error("insert", exc)
            self._record_drift_row(
                tenant_id=tenant_id, old_table=old_table,
                reason="insert_failed",
                old_hash=old_hash, new_hash=None,
                details={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "new_table": sm.new_table,
                },
            )
            logger.error(
                "shadow_writer.insert_failed",
                extra={
                    "op": "insert",
                    "tenant_id": tenant_id,
                    "old_table": old_table,
                    "new_table": sm.new_table,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            return ShadowWriteResult(
                mode=mode, shadowed=False, drift=True,
                drift_reason="insert_failed", old_hash=old_hash, new_hash=None,
            )

        # Drift check: read the row back and hash it; should match.
        pk_value = new_row.get(sm.column_renames.get(sm.primary_key, sm.primary_key))
        if pk_value is None:
            # No PK — cannot read back. Skip drift check (not a failure).
            return ShadowWriteResult(
                mode=mode, shadowed=True, drift=False, old_hash=old_hash,
            )

        # Fail-open on readback. If read_new_row raises we treat as
        # "row missing" → drift row recorded below. Broad catch because
        # reader is pluggable; structured log + counter keep it visible.
        new_pk_col = sm.column_renames.get(sm.primary_key, sm.primary_key)
        try:
            stored = self._read_new_row(sm.new_table, new_pk_col, pk_value)
        except Exception as exc:  # noqa: BLE001 — fail-open on readback (see comment)
            _record_shadow_error("readback", exc)
            logger.error(
                "shadow_writer.readback_failed",
                extra={
                    "op": "readback",
                    "tenant_id": tenant_id,
                    "new_table": sm.new_table,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            stored = None

        if stored is None:
            self._record_drift_row(
                tenant_id=tenant_id, old_table=old_table,
                reason="new_row_missing",
                old_hash=old_hash, new_hash=None,
                details={"pk": new_pk_col, "pk_value": str(pk_value)},
            )
            return ShadowWriteResult(
                mode=mode, shadowed=True, drift=True,
                drift_reason="new_row_missing", old_hash=old_hash,
            )

        new_hash = row_fingerprint(stored)
        if old_hash != new_hash:
            self._record_drift_row(
                tenant_id=tenant_id, old_table=old_table,
                reason="hash_mismatch",
                old_hash=old_hash, new_hash=new_hash,
                details={"pk": new_pk_col, "pk_value": str(pk_value)},
            )
            return ShadowWriteResult(
                mode=mode, shadowed=True, drift=True,
                drift_reason="hash_mismatch",
                old_hash=old_hash, new_hash=new_hash,
            )

        return ShadowWriteResult(
            mode=mode, shadowed=True, drift=False,
            old_hash=old_hash, new_hash=new_hash,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _record_drift_row(
        self,
        *,
        tenant_id: str,
        old_table: str,
        reason: str,
        old_hash: str | None,
        new_hash: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a ``shadow_migration_drift`` row.

        Catches DB exceptions itself so a failed drift-log write cannot
        take down the canonical transaction.
        """
        try:
            from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationDrift

            row = ShadowMigrationDrift(
                id=uuid4(),
                tenant_id=tenant_id,
                old_table=old_table,
                reason=reason,
                old_hash=old_hash,
                new_hash=new_hash,
                details=dict(details) if details is not None else None,
                created_at=_utcnow(),
            )
            self.db.add(row)
            self.db.flush()
        except Exception as exc:  # noqa: BLE001 — drift-log persist is last-resort; see comment
            # Drift-row persist MUST NOT re-raise: it's called from
            # inside other fail-open handlers. If it did raise, it would
            # subvert the whole "shadow never breaks canonical" contract.
            # Broad catch is load-bearing; loud log + counter preserve
            # observability.
            _record_shadow_error("drift_persist", exc)
            logger.error(
                "shadow_writer.drift_persist_failed",
                extra={
                    "op": "drift_persist",
                    "tenant_id": tenant_id,
                    "old_table": old_table,
                    "reason": reason,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )


# ----------------------------------------------------------------------
# Default pluggable fetchers (module-level so tests can monkeypatch)
# ----------------------------------------------------------------------


def _db_mode_lookup(db: Any, tenant_id: str, old_table: str) -> str:
    """Default mode lookup: read ``shadow_migration_state`` for this pair.

    Returns MODE_OFF when the state row is absent OR when the underlying
    table does not yet exist (pre-integration). Any other exception is
    logged loudly and still returns MODE_OFF to preserve the canonical
    transaction — but the failure is visible via the error counter.
    """
    from sqlalchemy.exc import OperationalError, ProgrammingError

    try:
        from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationState

        row = (
            db.query(ShadowMigrationState)
            .filter(
                ShadowMigrationState.tenant_id == tenant_id,
                ShadowMigrationState.old_table == old_table,
            )
            .first()
        )
        if row is None:
            return MODE_OFF
        return str(row.mode or MODE_OFF)
    except (OperationalError, ProgrammingError):
        # Table not present (common pre-integration) → off. This is the
        # expected shape before the SS-29 tables land — it is NOT an
        # error state, just unconfigured.
        return MODE_OFF
    except Exception as exc:  # noqa: BLE001 — fail-open per SS-29 contract
        # Anything else (ImportError on the model module, unexpected DB
        # driver error, etc.) is logged loudly and counted, then treated
        # as MODE_OFF so callers don't crash their canonical transaction.
        _record_shadow_error("db_mode_lookup", exc)
        logger.error(
            "shadow_writer.db_mode_lookup_failed",
            extra={
                "op": "db_mode_lookup",
                "tenant_id": tenant_id,
                "old_table": old_table,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return MODE_OFF


def _noop_insert(new_table: str, row: dict[str, Any]) -> None:  # pragma: no cover
    """Default no-op insert; replaced at integration-time."""
    logger.debug("shadow_writer: _noop_insert %s %s", new_table, row)


def _noop_read(new_table: str, pk_col: str, pk_value: Any) -> dict[str, Any] | None:
    """Default no-op readback; tests replace with a real reader."""
    return None
