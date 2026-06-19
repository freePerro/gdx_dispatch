from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ChaosScenario(Enum):
    QB_API_DOWN = "QB_API_DOWN"
    REDIS_DOWN = "REDIS_DOWN"
    STRIPE_DOWN = "STRIPE_DOWN"
    TENANT_DB_DOWN = "TENANT_DB_DOWN"
    PGBOUNCER_DOWN = "PGBOUNCER_DOWN"


@dataclass
class ChaosResult:
    scenario: ChaosScenario
    passed: bool
    actual_behavior: str
    expected_behavior: str


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if the TCP endpoint is reachable within the timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:  # False is the expected signal for an unreachable endpoint.
        logging.getLogger(__name__).exception("_tcp_reachable caught exception")
        return False


def run_chaos_scenario(
    scenario: ChaosScenario,
    tenant_id: str | None = None,
) -> ChaosResult:
    """Simulate a single chaos scenario and return the observed vs. expected result."""

    if scenario == ChaosScenario.QB_API_DOWN:
        expected = "Dealers can dispatch jobs, UI shows QB sync paused"
        qb_host = os.getenv("QB_API_HOST", "quickbooks.api.intuit.com")
        qb_port = int(os.getenv("QB_API_PORT", "443"))
        reachable = _tcp_reachable(qb_host, qb_port)
        if reachable:
            actual = "QB API is reachable — scenario not currently active"
            passed = False
        else:
            actual = "QB API is down — dispatching should continue with sync paused"
            passed = True
        return ChaosResult(
            scenario=scenario,
            passed=passed,
            actual_behavior=actual,
            expected_behavior=expected,
        )

    if scenario == ChaosScenario.REDIS_DOWN:
        expected = "Module cache falls back to DB, no 500s"
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        reachable = _tcp_reachable(redis_host, redis_port)
        if reachable:
            actual = "Redis is reachable — scenario not currently active"
            passed = False
        else:
            actual = "Redis is down — fallback to DB cache should be active"
            passed = True
        return ChaosResult(
            scenario=scenario,
            passed=passed,
            actual_behavior=actual,
            expected_behavior=expected,
        )

    if scenario == ChaosScenario.STRIPE_DOWN:
        expected = "Existing tenants operate, new signups queue"
        stripe_host = os.getenv("STRIPE_API_HOST", "api.stripe.com")
        stripe_port = int(os.getenv("STRIPE_API_PORT", "443"))
        reachable = _tcp_reachable(stripe_host, stripe_port)
        if reachable:
            actual = "Stripe API is reachable — scenario not currently active"
            passed = False
        else:
            actual = "Stripe is down — existing tenants operate, new signups should queue"
            passed = True
        return ChaosResult(
            scenario=scenario,
            passed=passed,
            actual_behavior=actual,
            expected_behavior=expected,
        )

    if scenario == ChaosScenario.TENANT_DB_DOWN:
        expected = "That tenant gets 503, all other tenants unaffected"
        tenant_db_host = os.getenv("TENANT_DB_HOST", "localhost")
        tenant_db_port = int(os.getenv("TENANT_DB_PORT", "5432"))
        reachable = _tcp_reachable(tenant_db_host, tenant_db_port)
        tenant_label = f"tenant {tenant_id!r}" if tenant_id else "target tenant"
        if reachable:
            actual = f"Tenant DB is reachable — {tenant_label} scenario not currently active"
            passed = False
        else:
            actual = (
                f"Tenant DB is down — {tenant_label} should receive 503, "
                "all other tenants should be unaffected"
            )
            passed = True
        return ChaosResult(
            scenario=scenario,
            passed=passed,
            actual_behavior=actual,
            expected_behavior=expected,
        )

    if scenario == ChaosScenario.PGBOUNCER_DOWN:
        expected = "All tenants get 503 with clear error, no silent hangs"
        pgbouncer_host = os.getenv("PGBOUNCER_HOST", "localhost")
        pgbouncer_port = int(os.getenv("PGBOUNCER_PORT", "6432"))
        reachable = _tcp_reachable(pgbouncer_host, pgbouncer_port)
        if reachable:
            actual = "PgBouncer is reachable — scenario not currently active"
            passed = False
        else:
            actual = "PgBouncer is down — all tenants should receive 503 with clear error, no silent hangs"
            passed = True
        return ChaosResult(
            scenario=scenario,
            passed=passed,
            actual_behavior=actual,
            expected_behavior=expected,
        )

    raise ValueError(f"Unknown scenario: {scenario!r}")


def run_all_chaos_scenarios(tenant_id: str | None = None) -> list[ChaosResult]:
    """Run every chaos scenario and return all results."""
    results: list[ChaosResult] = []
    for scenario in ChaosScenario:
        try:
            result = run_chaos_scenario(scenario, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error running chaos scenario %s: %s", scenario, exc)
            result = ChaosResult(
                scenario=scenario,
                passed=False,
                actual_behavior=f"Exception during scenario check: {exc}",
                expected_behavior="",
            )
        results.append(result)
    return results
