from __future__ import annotations

import os

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.reconciliation import detect_schema_drift, run_billing_reconciliation


def _expected_tables() -> list[str]:
    raw = os.getenv("EXPECTED_TENANT_TABLES", "audit_log")
    return [t.strip() for t in raw.split(",") if t.strip()]


# No queue= kwarg: "low" isn't a consumed queue (2026-07-07 audit) and a
# decorator queue overrides task_routes, which sends reconciliation_tasks.*
# to priority:low.
@celery_app.task(acks_late=True)
def monthly_billing_reconciliation_task() -> dict:
    with SessionLocal() as db:
        return run_billing_reconciliation(db)


@celery_app.task(acks_late=True)
def weekly_schema_drift_task() -> dict:
    from gdx_dispatch.core.tenant import single_tenant
    t = single_tenant()
    expected = _expected_tables()
    with SessionLocal() as db:
        drifted = 1 if detect_schema_drift(t["id"], expected, db) else 0
    return {"checked": 1, "drifted": drifted}
