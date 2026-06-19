from __future__ import annotations

from typing import Any

from gdx_dispatch.core.celery_app import celery_app


@celery_app.task(queue="priority:low")
def apply_late_fees(tenant_id: str) -> dict[str, Any]:
    overdue = _find_overdue_invoices(tenant_id)
    applied_count = 0
    for invoice in overdue:
        _apply_late_fee(invoice)
        applied_count += 1
    return {"tenant_id": tenant_id, "applied_count": applied_count}


@celery_app.task(queue="priority:low")
def apply_late_fees_for_all_tenants() -> dict[str, int]:
    tenants = _list_tenant_ids()
    for tenant_id in tenants:
        apply_late_fees.delay(tenant_id)
    return {"tenant_count": len(tenants)}


def _find_overdue_invoices(tenant_id: str) -> list[dict[str, Any]]:
    _ = tenant_id
    return []


def _apply_late_fee(invoice: dict[str, Any]) -> None:
    _ = invoice


def _list_tenant_ids() -> list[str]:
    return []
