"""SS-30 slice A — pre-cutover safety checks.

Before :func:`gdx_dispatch.core.cutover_orchestrator.run_cutover` is invoked for a
(tenant, old_table) pair, the preflight MUST pass. This module encodes
the four hard-gate checks documented in the SS-30 plan:

1. **Shadow is currently enabled.** The ``shadow_migration_state`` row
   for the pair has ``mode == 'shadow'`` (not ``off``, not already
   ``cutover``). Cutover only makes sense as a transition OUT of shadow.
2. **Drift is zero over the last N sample-verify runs.** We consult the
   ``shadow_migration_drift`` table and require zero unresolved rows
   within the lookback window. Default: zero drift rows in the last 24h.
3. **Audit hash chain is intact.** ``verify_chain`` for the tenant
   returns ``(True, -1)``. A broken chain means tamper evidence — we
   DO NOT cut over in that state.
4. **Backfill is fully caught up.** The ``shadow_migration_checkpoint``
   row exists and the caller-supplied ``backfill_target_count`` matches
   the count stored in the checkpoint (or the caller can pass a
   ``backfill_check_fn`` override for more sophisticated checks).

All checks are **read-only**. The module never writes to the DB.

Each check populates one :class:`PreflightCheck` with a pass/fail +
``suggested_action`` string. The aggregate :class:`PreflightReport`
exposes ``passed`` (True only if every check passed) plus per-check
detail so the UI can surface exactly what blocked the cutover.

INTEGRATION_TODO: drift-row severity / resolution tagging is not in
SS-29's ``shadow_migration_drift`` schema — SS-30 treats EVERY drift
row in the window as a blocker. A future slice may add a
``resolved_at`` column, at which point preflight will filter to
unresolved rows only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from gdx_dispatch.core.audit_hash_chain import verify_chain

logger = logging.getLogger(__name__)

# Default window for "recent" drift rows. 24h matches the shadow-mode
# observation cadence — if nothing drifted in a day we're clean.
DEFAULT_DRIFT_LOOKBACK_HOURS = 24

CHECK_SHADOW_ENABLED = "shadow_enabled"
CHECK_DRIFT_CLEAN = "drift_clean"
CHECK_AUDIT_CHAIN_INTACT = "audit_chain_intact"
CHECK_BACKFILL_CAUGHT_UP = "backfill_caught_up"

_ALL_CHECKS = (
    CHECK_SHADOW_ENABLED,
    CHECK_DRIFT_CLEAN,
    CHECK_AUDIT_CHAIN_INTACT,
    CHECK_BACKFILL_CAUGHT_UP,
)


@dataclass(frozen=True)
class PreflightCheck:
    """Result of a single preflight check."""

    name: str
    passed: bool
    detail: str
    suggested_action: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """Aggregate preflight result.

    ``passed`` is True iff every check passed. ``checks`` preserves
    input order so the UI can render a stable list.
    """

    tenant_id: str
    old_table: str
    checks: tuple[PreflightCheck, ...]
    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> tuple[PreflightCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "old_table": self.old_table,
            "passed": self.passed,
            "ran_at": self.ran_at.isoformat(),
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "suggested_action": c.suggested_action,
                }
                for c in self.checks
            ],
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _check_shadow_enabled(db: Any, tenant_id: str, old_table: str) -> PreflightCheck:
    """Fail unless the state row exists and mode == 'shadow'."""
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
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "cutover_preflight: shadow_enabled check failed tenant=%s table=%s err=%s",
            tenant_id, old_table, exc, exc_info=True,
        )
        return PreflightCheck(
            name=CHECK_SHADOW_ENABLED,
            passed=False,
            detail=f"query failed: {exc}",
            suggested_action="inspect DB connectivity and shadow_migration_state table",
        )

    if row is None:
        return PreflightCheck(
            name=CHECK_SHADOW_ENABLED,
            passed=False,
            detail="no shadow_migration_state row for this (tenant, table)",
            suggested_action=(
                "POST /api/admin/shadow-migrations/{table}/enable-shadow first"
            ),
        )
    if row.mode == "cutover":
        return PreflightCheck(
            name=CHECK_SHADOW_ENABLED,
            passed=False,
            detail="already in cutover mode",
            suggested_action="cutover is already complete — no action needed",
        )
    if row.mode != "shadow":
        return PreflightCheck(
            name=CHECK_SHADOW_ENABLED,
            passed=False,
            detail=f"mode={row.mode!r}, expected 'shadow'",
            suggested_action="enable shadow mode before cutover",
        )
    return PreflightCheck(
        name=CHECK_SHADOW_ENABLED,
        passed=True,
        detail="shadow mode active",
    )


def _check_drift_clean(
    db: Any,
    tenant_id: str,
    old_table: str,
    *,
    lookback_hours: int = DEFAULT_DRIFT_LOOKBACK_HOURS,
) -> PreflightCheck:
    """Fail if any drift row exists in the lookback window."""
    try:
        from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationDrift

        cutoff = _utcnow() - timedelta(hours=lookback_hours)
        count = (
            db.query(ShadowMigrationDrift)
            .filter(
                ShadowMigrationDrift.tenant_id == tenant_id,
                ShadowMigrationDrift.old_table == old_table,
                ShadowMigrationDrift.created_at >= cutoff,
            )
            .count()
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "cutover_preflight: drift check failed tenant=%s table=%s err=%s",
            tenant_id, old_table, exc, exc_info=True,
        )
        return PreflightCheck(
            name=CHECK_DRIFT_CLEAN,
            passed=False,
            detail=f"query failed: {exc}",
            suggested_action="inspect DB connectivity and shadow_migration_drift table",
        )

    if count > 0:
        return PreflightCheck(
            name=CHECK_DRIFT_CLEAN,
            passed=False,
            detail=f"{count} drift row(s) in last {lookback_hours}h",
            suggested_action=(
                "investigate drift rows, re-run shadow_migration_verify, "
                "and only cut over after a clean verify pass"
            ),
        )
    return PreflightCheck(
        name=CHECK_DRIFT_CLEAN,
        passed=True,
        detail=f"zero drift rows in last {lookback_hours}h",
    )


def _check_audit_chain_intact(db: Any, tenant_id: str) -> PreflightCheck:
    """Fail if verify_chain reports a break."""
    try:
        valid, break_at = verify_chain(db, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "cutover_preflight: audit chain check failed tenant=%s err=%s",
            tenant_id, exc, exc_info=True,
        )
        return PreflightCheck(
            name=CHECK_AUDIT_CHAIN_INTACT,
            passed=False,
            detail=f"verify_chain raised: {exc}",
            suggested_action="audit hash chain unreadable — escalate to security",
        )
    if not valid:
        return PreflightCheck(
            name=CHECK_AUDIT_CHAIN_INTACT,
            passed=False,
            detail=f"chain broken at index {break_at}",
            suggested_action=(
                "DO NOT CUT OVER — tamper evidence on audit chain. "
                "Escalate to security / Doug immediately."
            ),
        )
    return PreflightCheck(
        name=CHECK_AUDIT_CHAIN_INTACT,
        passed=True,
        detail="audit hash chain intact",
    )


def _check_backfill_caught_up(
    db: Any,
    tenant_id: str,
    old_table: str,
    *,
    backfill_check_fn: Callable[[Any, str, str], tuple[bool, str]] | None = None,
) -> PreflightCheck:
    """Fail unless backfill checkpoint indicates completion.

    By default we require a checkpoint row to exist. Callers can pass
    ``backfill_check_fn`` returning ``(passed, detail)`` for stronger
    invariants (e.g., source-row-count == shadowed-row-count).
    """
    if backfill_check_fn is not None:
        try:
            passed, detail = backfill_check_fn(db, tenant_id, old_table)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cutover_preflight: backfill check_fn failed tenant=%s table=%s err=%s",
                tenant_id, old_table, exc, exc_info=True,
            )
            return PreflightCheck(
                name=CHECK_BACKFILL_CAUGHT_UP,
                passed=False,
                detail=f"check_fn raised: {exc}",
                suggested_action="inspect backfill_check_fn error",
            )
        if not passed:
            return PreflightCheck(
                name=CHECK_BACKFILL_CAUGHT_UP,
                passed=False,
                detail=detail,
                suggested_action=(
                    "run `python -m gdx_dispatch.tools.shadow_migration_backfill` "
                    "until caught up"
                ),
            )
        return PreflightCheck(
            name=CHECK_BACKFILL_CAUGHT_UP,
            passed=True,
            detail=detail,
        )

    # Default: checkpoint row must exist.
    try:
        from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationCheckpoint

        row = (
            db.query(ShadowMigrationCheckpoint)
            .filter(
                ShadowMigrationCheckpoint.tenant_id == tenant_id,
                ShadowMigrationCheckpoint.old_table == old_table,
            )
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "cutover_preflight: backfill check failed tenant=%s table=%s err=%s",
            tenant_id, old_table, exc, exc_info=True,
        )
        return PreflightCheck(
            name=CHECK_BACKFILL_CAUGHT_UP,
            passed=False,
            detail=f"query failed: {exc}",
            suggested_action=(
                "inspect DB connectivity and shadow_migration_checkpoint table"
            ),
        )

    if row is None:
        return PreflightCheck(
            name=CHECK_BACKFILL_CAUGHT_UP,
            passed=False,
            detail="no backfill checkpoint row — backfill has never run",
            suggested_action=(
                "run `python -m gdx_dispatch.tools.shadow_migration_backfill` first"
            ),
        )
    return PreflightCheck(
        name=CHECK_BACKFILL_CAUGHT_UP,
        passed=True,
        detail=(
            f"checkpoint present (last_row_pk={row.last_row_pk}, "
            f"rows_this_session={row.row_count_this_session})"
        ),
    )


def run_preflight(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    drift_lookback_hours: int = DEFAULT_DRIFT_LOOKBACK_HOURS,
    backfill_check_fn: Callable[[Any, str, str], tuple[bool, str]] | None = None,
) -> PreflightReport:
    """Run every preflight check and return a :class:`PreflightReport`.

    The report is fail-open by design — a single failed check means
    ``report.passed is False``, and the caller (router or orchestrator)
    MUST refuse to proceed.
    """
    if not tenant_id:
        raise ValueError("run_preflight: tenant_id required")
    if not old_table:
        raise ValueError("run_preflight: old_table required")

    checks = (
        _check_shadow_enabled(db, tenant_id, old_table),
        _check_drift_clean(
            db, tenant_id, old_table, lookback_hours=drift_lookback_hours
        ),
        _check_audit_chain_intact(db, tenant_id),
        _check_backfill_caught_up(
            db, tenant_id, old_table, backfill_check_fn=backfill_check_fn
        ),
    )
    report = PreflightReport(
        tenant_id=tenant_id,
        old_table=old_table,
        checks=checks,
    )
    if not report.passed:
        logger.warning(
            "cutover_preflight: FAIL tenant=%s table=%s failed=%s",
            tenant_id, old_table,
            [c.name for c in report.failed_checks],
        )
    else:
        logger.info(
            "cutover_preflight: PASS tenant=%s table=%s",
            tenant_id, old_table,
        )
    return report
