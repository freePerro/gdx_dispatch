"""SS-34 slice D — drill_orchestrator.

Sequences:

    snapshot → restore → verify → emit events → write audit row.

A drill is **scheduled, not ad-hoc**. Every invocation carries a
``drill_run_id`` (caller-provided UUID) and a ``scheduled_for``
timestamp. Re-calling :func:`run_drill` with the same ``drill_run_id``
returns the prior :class:`DrillReport` — idempotency is keyed on the
in-memory ``_DRILL_CACHE`` for the process + the ``dr_drill_run`` row
if a persistence callback is supplied.

Production safety
-----------------

* ``staging_db_url`` MUST NOT equal ``os.getenv("DATABASE_URL")``.
  This is enforced here — the restore module has its own cheaper
  heuristic guard; this one is definitive.
* ``dry_run=True`` emits ``drill_scheduled`` + ``drill_started`` but
  short-circuits before running pg_dump/pg_restore; returns a report
  with ``dry_run=True``.
* Verification failures → emit ``drill_failed.v1`` and return a report
  with ``passed=False``. The orchestrator does NOT raise on
  verification failure — routers do (5xx). Snapshot / restore failures
  DO raise (they're infrastructure failures, not policy failures).
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from gdx_dispatch.core.dr.backup_snapshot import (
    SnapshotError,
    SnapshotManifest,
    create_snapshot,
)
from gdx_dispatch.core.dr.restore_to_staging import (
    ProductionTargetRefused,
    RestoreError,
    RestoreReport,
    restore_snapshot_to_staging,
)
from gdx_dispatch.core.dr.verification_harness import (
    VerificationConfig,
    VerificationReport,
    run_verification,
)

logger = logging.getLogger(__name__)

# Process-local cache for idempotency. A real production deployment
# would persist this in ``dr_drill_run``; the supervisor supplies a
# persistence callback at integration time.
_DRILL_CACHE: dict[str, "DrillReport"] = {}
_DRILL_CACHE_LOCK = threading.Lock()


@dataclasses.dataclass
class DrillReport:
    """End-to-end drill outcome.

    ``snapshot`` / ``restore`` / ``verification`` may be None if the
    drill failed early or is a dry run.
    """

    drill_run_id: str
    scheduled_for: datetime
    started_at: datetime
    finished_at: Optional[datetime] = None
    scope: str = "full"
    dry_run: bool = False
    passed: bool = False
    failure_reason: Optional[str] = None
    snapshot: Optional[SnapshotManifest] = None
    restore: Optional[RestoreReport] = None
    verification: Optional[VerificationReport] = None

    def to_dict(self) -> dict[str, Any]:
        def _iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None

        return {
            "drill_run_id": self.drill_run_id,
            "scheduled_for": _iso(self.scheduled_for),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "scope": self.scope,
            "dry_run": self.dry_run,
            "passed": self.passed,
            "failure_reason": self.failure_reason,
            "snapshot": (
                {
                    "id": self.snapshot.id,
                    "size_bytes": self.snapshot.size_bytes,
                    "sha256": self.snapshot.sha256,
                    "scope_description": self.snapshot.scope_description,
                    "created_at": self.snapshot.created_at.isoformat(),
                }
                if self.snapshot
                else None
            ),
            "restore": (
                {
                    "duration_s": self.restore.duration_s,
                    "integrity_verified": self.restore.integrity_verified,
                    "rows_by_table": self.restore.rows_by_table,
                    "errors": self.restore.errors,
                    "staging_db_url_redacted": self.restore.staging_db_url_redacted,
                }
                if self.restore
                else None
            ),
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
        }


def _refuse_if_production(staging_db_url: str) -> None:
    prod = os.getenv("DATABASE_URL") or ""
    if prod and prod == staging_db_url:
        raise ProductionTargetRefused(
            "staging_db_url == DATABASE_URL; refusing to restore to production"
        )


def _cache_get(drill_run_id: str) -> Optional["DrillReport"]:
    with _DRILL_CACHE_LOCK:
        return _DRILL_CACHE.get(drill_run_id)


def _cache_put(drill_run_id: str, report: "DrillReport") -> None:
    with _DRILL_CACHE_LOCK:
        _DRILL_CACHE[drill_run_id] = report


def reset_idempotency_cache() -> None:
    """Test hook — empties the process-local idempotency cache."""
    with _DRILL_CACHE_LOCK:
        _DRILL_CACHE.clear()


def run_drill(
    *,
    drill_run_id: str,
    scheduled_for: datetime,
    scope: str,
    staging_db_url: str,
    source_db_url: str,
    snapshot_target: str,
    scope_selector: Optional[str] = None,
    dry_run: bool = False,
    verification_config: Optional[VerificationConfig] = None,
    snapshot_fn: Callable[..., SnapshotManifest] = create_snapshot,
    restore_fn: Callable[..., RestoreReport] = restore_snapshot_to_staging,
    verify_fn: Callable[..., VerificationReport] = run_verification,
    emit_event: Optional[Callable[[str, dict], None]] = None,
    write_audit: Optional[Callable[[dict], None]] = None,
    db_exec=None,
    db_for_hashchain=None,
) -> DrillReport:
    """Run one drill.

    Idempotent on ``drill_run_id`` — a second call with the same id
    returns the cached report.

    :param drill_run_id: caller-supplied UUID (string). A cron CLI
        generates one per scheduled drill; the admin router generates
        one per POST.
    :param scheduled_for: when this drill was scheduled; informational.
    :param scope: ``full``/``tenant``/``schema``.
    :param staging_db_url: target DB; must NOT equal DATABASE_URL.
    :param source_db_url: DB to snapshot from.
    :param snapshot_target: local path for the pg_dump artifact.
    :param scope_selector: schema name for tenant/schema scopes.
    :param dry_run: if True, emit lifecycle events and return early
        without running pg_dump/pg_restore.
    :param verification_config: custom :class:`VerificationConfig`.
    :param snapshot_fn / restore_fn / verify_fn: dependency injection
        for tests.
    :param emit_event: optional ``(event_name, payload)`` callback.
    :param write_audit: optional ``(audit_row_dict)`` callback.
    """
    # Idempotency.
    cached = _cache_get(drill_run_id)
    if cached is not None:
        logger.info("drill_run_id=%s already ran; returning cached report", drill_run_id)
        return cached

    # Production guard (strict).
    _refuse_if_production(staging_db_url)

    started = datetime.now(timezone.utc)
    report = DrillReport(
        drill_run_id=drill_run_id,
        scheduled_for=scheduled_for,
        started_at=started,
        scope=scope,
        dry_run=dry_run,
    )

    _safe_emit(emit_event, "gdx_dispatch.dr.drill_scheduled.v1", {
        "drill_run_id": drill_run_id,
        "scheduled_for": scheduled_for.isoformat(),
        "scope": scope,
    })
    _safe_emit(emit_event, "gdx_dispatch.dr.drill_started.v1", {
        "drill_run_id": drill_run_id,
        "started_at": started.isoformat(),
        "scope": scope,
        "dry_run": dry_run,
    })

    if dry_run:
        report.finished_at = datetime.now(timezone.utc)
        report.passed = True
        _safe_emit(emit_event, "gdx_dispatch.dr.drill_completed.v1", {
            "drill_run_id": drill_run_id,
            "dry_run": True,
            "passed": True,
        })
        _safe_write_audit(write_audit, report)
        _cache_put(drill_run_id, report)
        return report

    # Snapshot.
    try:
        manifest = snapshot_fn(
            label=f"drill-{drill_run_id}",
            source_db_url=source_db_url,
            target_location=snapshot_target,
            scope=scope,
            scope_selector=scope_selector,
        )
    except SnapshotError as exc:
        report.failure_reason = f"snapshot: {exc}"
        report.finished_at = datetime.now(timezone.utc)
        _safe_emit(emit_event, "gdx_dispatch.dr.drill_failed.v1", {
            "drill_run_id": drill_run_id,
            "stage": "snapshot",
            "reason": str(exc),
        })
        _safe_write_audit(write_audit, report)
        _cache_put(drill_run_id, report)
        raise

    report.snapshot = manifest

    # Restore.
    try:
        restore_report = restore_fn(
            manifest=manifest,
            staging_db_url=staging_db_url,
            db_exec=db_exec,
            schema_filter=scope_selector if scope in ("tenant", "schema") else None,
        )
    except (RestoreError, ProductionTargetRefused) as exc:
        report.failure_reason = f"restore: {exc}"
        report.finished_at = datetime.now(timezone.utc)
        _safe_emit(emit_event, "gdx_dispatch.dr.drill_failed.v1", {
            "drill_run_id": drill_run_id,
            "stage": "restore",
            "reason": str(exc),
        })
        _safe_write_audit(write_audit, report)
        _cache_put(drill_run_id, report)
        raise

    report.restore = restore_report

    # Verify (never raises).
    verification = verify_fn(
        db_exec=db_exec or (lambda _sql: []),
        db_for_hashchain=db_for_hashchain,
        config=verification_config or VerificationConfig(),
    )
    report.verification = verification
    report.passed = verification.passed
    report.finished_at = datetime.now(timezone.utc)

    if verification.passed:
        _safe_emit(emit_event, "gdx_dispatch.dr.drill_completed.v1", {
            "drill_run_id": drill_run_id,
            "passed": True,
            "duration_s": restore_report.duration_s,
        })
    else:
        report.failure_reason = (
            f"verification: {len(verification.failed_checks)} failed checks"
        )
        _safe_emit(emit_event, "gdx_dispatch.dr.drill_failed.v1", {
            "drill_run_id": drill_run_id,
            "stage": "verification",
            "failed_count": len(verification.failed_checks),
            "failed_checks": [c.name for c in verification.failed_checks],
        })

    _safe_write_audit(write_audit, report)
    _cache_put(drill_run_id, report)
    return report


def _safe_emit(emit_event, name: str, payload: dict) -> None:
    if emit_event is None:
        return
    try:
        emit_event(name, payload)
    except Exception as exc:
        logger.warning("emit_event failed for %s: %s", name, exc)


def _safe_write_audit(write_audit, report: DrillReport) -> None:
    if write_audit is None:
        return
    try:
        write_audit(report.to_dict())
    except Exception as exc:
        logger.warning("write_audit failed for drill=%s: %s", report.drill_run_id, exc)
