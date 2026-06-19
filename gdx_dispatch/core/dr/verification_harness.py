"""SS-34 slice C — post-restore verification harness.

Runs a canonical set of READ-ONLY sanity checks against a restored
staging database:

* **Row-count ranges** per major table (``identities``, ``tenants``,
  ``customers``, ``jobs``, ``audit_logs``) — configurable via
  :class:`VerificationConfig.row_count_ranges`.
* **RLS policies present** — every tenant-scoped table listed in
  :attr:`VerificationConfig.rls_required_tables` must have at least
  one policy row in ``pg_policies``.
* **Critical data present** — at least one ``system_tenant`` row and
  the configured ``known_identity_id`` row must exist.
* **Audit hash-chain intact** — invokes
  :func:`gdx_dispatch.core.audit_hash_chain.verify_chain` for every tenant id
  supplied in :attr:`VerificationConfig.tenant_ids_to_verify`.

Design rules
------------

* **Verification failures do NOT raise.** A failing check contributes
  a :class:`CheckResult` with ``passed=False`` and a human-readable
  ``detail``. The orchestrator decides whether a non-empty
  ``failed_checks`` list means drill failure.
* **Side-effect-free.** Only SELECTs; no DDL, no DML, no COMMIT.
* **Bounded.** Each check has a hard row cap so a runaway SELECT can't
  blow up the drill.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Per-check result tuple. Detail is free-form; keep it short enough
# to surface in the admin UI without a scroll.
@dataclasses.dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclasses.dataclass
class VerificationConfig:
    """Tunable inputs for the harness.

    Defaults are intentionally conservative — callers override from
    the drill orchestrator with tenant-specific thresholds.
    """

    row_count_ranges: dict[str, tuple[int, int]] = dataclasses.field(
        default_factory=lambda: {
            "identities": (1, 10_000_000),
            "tenants": (1, 1_000_000),
            "customers": (0, 100_000_000),
            "jobs": (0, 100_000_000),
            "audit_logs": (0, 10_000_000_000),
        }
    )
    # Three-plane: DR verification runs against the CONTROL DB, so the
    # required set is the A2 control-plane + A3 commerce-plane targets.
    # Tenant-plane tables (customers/jobs) live in per-tenant DBs where
    # RLS is a no-op by the isolation model — removed from this list
    # 2026-04-24 (Phase A4).
    rls_required_tables: tuple[str, ...] = (
        # Phase A3 — commerce plane (multi-party rows)
        "cross_tenant_share",
        "cross_tenant_share_acceptance",
        # Phase A2 — control plane (tenant-scoped rows)
        "audit_retention_policy",
        "billing_overage_event",
        "billing_plan",
        "cutover_schedule",
        "deprecated_table_record",
        "installations",
        "mcp_execution_log",
        "mcp_tool_execution_audit",
        "memberships",
        "metering_checkpoint",
        "metering_usage",
        "resource_instance",
        "sandbox_envs",
        "shadow_migration_checkpoint",
        "shadow_migration_drift",
        "shadow_migration_state",
        "shared_resources",
        "ss21_admin_consent_grants",
        "ss21_webhook_subscriptions",
        "ss31_federation_provider",
        "sso_configs",
        "platform_consumer_audit",
        "tenant_module_grants",
    )
    known_identity_id: Optional[str] = None
    tenant_ids_to_verify: tuple[str, ...] = ()


@dataclasses.dataclass
class VerificationReport:
    """Produced by :func:`run_verification`.

    ``checks`` is the full ordered list; ``failed_checks`` is a view.
    """

    run_started_at: datetime
    run_finished_at: Optional[datetime] = None
    checks: list[CheckResult] = dataclasses.field(default_factory=list)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def passed(self) -> bool:
        return len(self.failed_checks) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_started_at": self.run_started_at.isoformat(),
            "run_finished_at": (
                self.run_finished_at.isoformat() if self.run_finished_at else None
            ),
            "passed": self.passed,
            "failed_count": len(self.failed_checks),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


# Type alias: db_exec is a callable taking a SQL string and returning
# an iterable of row tuples. The orchestrator wraps SQLAlchemy; tests
# pass a plain callable.
DbExec = Callable[[str], Iterable[tuple]]


def _check_row_count(
    db_exec: DbExec,
    table: str,
    lo: int,
    hi: int,
) -> CheckResult:
    name = f"rowcount:{table}"
    try:
        rs = list(db_exec(f'SELECT COUNT(*) FROM "{table}"'))
        n = int(rs[0][0]) if rs and rs[0] else 0
    except Exception as exc:
        return CheckResult(
            name=name,
            passed=False,
            detail=f"query failed: {exc}",
        )
    if lo <= n <= hi:
        return CheckResult(name=name, passed=True, detail=f"{n} rows (in [{lo},{hi}])")
    return CheckResult(
        name=name,
        passed=False,
        detail=f"{n} rows OUT OF RANGE [{lo},{hi}]",
    )


def _check_rls_policy(
    db_exec: DbExec,
    table: str,
) -> CheckResult:
    name = f"rls:{table}"
    try:
        rs = list(db_exec(
            "SELECT COUNT(*) FROM pg_policies "
            f"WHERE tablename = '{table.replace(chr(39), chr(39)*2)}'"
        ))
        n = int(rs[0][0]) if rs and rs[0] else 0
    except Exception as exc:
        return CheckResult(name=name, passed=False, detail=f"query failed: {exc}")
    if n >= 1:
        return CheckResult(name=name, passed=True, detail=f"{n} policies")
    return CheckResult(
        name=name, passed=False, detail="no RLS policies on table",
    )


def _check_system_tenant(db_exec: DbExec) -> CheckResult:
    name = "critical:system_tenant"
    try:
        rs = list(db_exec(
            "SELECT COUNT(*) FROM tenants WHERE slug = 'system' OR is_system = true"
        ))
        n = int(rs[0][0]) if rs and rs[0] else 0
    except Exception as exc:
        return CheckResult(name=name, passed=False, detail=f"query failed: {exc}")
    if n >= 1:
        return CheckResult(name=name, passed=True, detail="present")
    return CheckResult(name=name, passed=False, detail="no system tenant row")


def _check_known_identity(db_exec: DbExec, ident: str) -> CheckResult:
    name = f"critical:identity:{ident}"
    # Quote the identity; SS-17 identity ids are already opaque strings.
    safe = ident.replace("'", "''")
    try:
        rs = list(db_exec(
            f"SELECT COUNT(*) FROM identities WHERE id = '{safe}'"
        ))
        n = int(rs[0][0]) if rs and rs[0] else 0
    except Exception as exc:
        return CheckResult(name=name, passed=False, detail=f"query failed: {exc}")
    if n >= 1:
        return CheckResult(name=name, passed=True, detail="present")
    return CheckResult(name=name, passed=False, detail="known identity missing")


def _check_hash_chain(db: Any, tenant_id: str) -> CheckResult:
    """Invoke SS-28 verify_chain for a tenant."""
    name = f"hashchain:{tenant_id}"
    try:
        # Local import — the SS-28 stub is not mounted on the primary
        # platform Base, so importing at module-load time would be
        # too eager for callers that don't need it.
        from gdx_dispatch.core.audit_hash_chain import verify_chain

        ok, break_at = verify_chain(db, tenant_id)
    except Exception as exc:
        return CheckResult(name=name, passed=False, detail=f"verify failed: {exc}")
    if ok:
        return CheckResult(name=name, passed=True, detail="chain intact")
    return CheckResult(
        name=name, passed=False, detail=f"chain broken at index {break_at}",
    )


def run_verification(
    *,
    db_exec: DbExec,
    db_for_hashchain: Any = None,
    config: Optional[VerificationConfig] = None,
) -> VerificationReport:
    """Run every configured check and return the full report.

    :param db_exec: read-only SQL executor — ``(sql) -> iterable of rows``.
    :param db_for_hashchain: SQLAlchemy session for ``verify_chain``.
                             If None, hash-chain checks are skipped
                             (and appended as a single skipped-check
                             entry so the skip is visible).
    :param config: :class:`VerificationConfig`; default is conservative.
    """
    cfg = config or VerificationConfig()
    report = VerificationReport(run_started_at=datetime.now(timezone.utc))

    # Row-count ranges.
    for table, (lo, hi) in cfg.row_count_ranges.items():
        report.checks.append(_check_row_count(db_exec, table, lo, hi))

    # RLS policy presence.
    for table in cfg.rls_required_tables:
        report.checks.append(_check_rls_policy(db_exec, table))

    # Critical rows.
    report.checks.append(_check_system_tenant(db_exec))
    if cfg.known_identity_id:
        report.checks.append(_check_known_identity(db_exec, cfg.known_identity_id))

    # Hash-chain per tenant.
    if cfg.tenant_ids_to_verify:
        if db_for_hashchain is None:
            report.checks.append(CheckResult(
                name="hashchain:skipped",
                passed=False,
                detail="no db session supplied for verify_chain",
            ))
        else:
            for tid in cfg.tenant_ids_to_verify:
                report.checks.append(_check_hash_chain(db_for_hashchain, tid))

    report.run_finished_at = datetime.now(timezone.utc)
    return report
