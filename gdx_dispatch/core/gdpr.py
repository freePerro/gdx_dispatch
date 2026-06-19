from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job
from gdx_dispatch.modules.change_orders.models import ModuleChangeOrder as ChangeOrder
from gdx_dispatch.modules.equipment.models import CustomerEquipment, EquipmentServiceHistory

gdpr_router = APIRouter(tags=["gdpr_customers"])

_NOT_FOUND = "Customer not found"
_DELETED_SENTINEL = "[DELETED]"

TenantDB = Annotated[Session, Depends(get_db)]


def _dump(row: object) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}  # type: ignore[attr-defined]


def export_customer_data(customer_id: str, db: Session) -> dict:
    cid = UUID(customer_id)
    customer = db.execute(select(Customer).where(Customer.id == cid)).scalar_one_or_none()
    if not customer: raise HTTPException(status_code=404, detail=_NOT_FOUND)  # noqa: E701,E702
    jobs = list(db.execute(select(Job).where(Job.customer_id == cid)).scalars().all()); job_ids = [j.id for j in jobs]  # noqa: E701,E702
    invoices = list(db.execute(select(Invoice).where(Invoice.job_id.in_(job_ids))).scalars().all()) if job_ids else []
    change_orders = list(db.execute(select(ChangeOrder).where(ChangeOrder.job_id.in_(job_ids))).scalars().all()) if job_ids else []
    svc = select(EquipmentServiceHistory).join(CustomerEquipment, EquipmentServiceHistory.equipment_id == CustomerEquipment.id).where(or_(CustomerEquipment.customer_id == cid, EquipmentServiceHistory.job_id.in_(job_ids)))
    service_history = list(db.execute(svc).scalars().all()) if job_ids else list(db.execute(svc.where(CustomerEquipment.customer_id == cid)).scalars().all())
    asyncio.run(log_audit_event(db, "gdpr_data_export", "system", "customer", customer_id, {"customer_id": customer_id}))
    db.commit()
    return {"customer": _dump(customer), "jobs": [_dump(r) for r in jobs], "invoices": [_dump(r) for r in invoices], "change_orders": [_dump(r) for r in change_orders], "service_history": [_dump(r) for r in service_history]}


def delete_customer_data(customer_id: str, db: Session, hard: bool = False) -> None:
    customer = db.execute(select(Customer).where(Customer.id == UUID(customer_id))).scalar_one_or_none()
    if not customer: raise HTTPException(status_code=404, detail=_NOT_FOUND)  # noqa: E701,E702
    if hard:
        customer.name = _DELETED_SENTINEL; customer.email = None; customer.phone = None; customer.address = None  # noqa: E701,E702
        customer.name_hash = None; customer.email_hash = None; customer.phone_hash = None  # noqa: E701,E702
    else:
        customer.deleted_at = utcnow()
    asyncio.run(log_audit_event(db, "gdpr_data_deleted", "system", "customer", customer_id, {"hard": hard}))
    db.commit()


@gdpr_router.delete(
    "/api/customers/{customer_id}",
    response_model=None,
    responses={404: {"description": _NOT_FOUND}},
)
def hard_delete_customer(customer_id: str, db: TenantDB) -> dict:
    """GDPR/CCPA hard delete: anonymise all PII fields for the given customer."""
    customer = db.execute(select(Customer).where(Customer.id == UUID(customer_id))).scalar_one_or_none()
    if not customer or customer.name == _DELETED_SENTINEL:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    customer.name = _DELETED_SENTINEL
    customer.email = None
    customer.phone = None
    customer.phone_hash = None
    customer.email_hash = None
    customer.metadata_ = {**(customer.metadata_ or {}), "do_not_sell": True}
    asyncio.run(log_audit_event(db, "gdpr_hard_delete", "system", "customer", customer_id, {"hard": True}))
    db.commit()
    return {"status": "deleted"}


@gdpr_router.get(
    "/api/customers/{customer_id}/export",
    response_model=None,
    responses={404: {"description": _NOT_FOUND}},
)
def export_customer(customer_id: str, db: TenantDB) -> dict:
    """GDPR data export: return all data associated with a customer."""
    return export_customer_data(customer_id, db)
