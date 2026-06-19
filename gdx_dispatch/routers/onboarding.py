"""
Onboarding router — first-run wizard for new tenants.

Tracks which setup steps the dealer has completed, seeds a starter catalog,
and offers demo-data generation/cleanup so new dealers see a populated
dashboard immediately after signup.

Pattern: ORM models from gdx_dispatch.models.tenant_models for all database operations.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import (
    CustomCatalog,
    CustomCatalogItem,
    Customer,
    Invoice,
    Job,
    OnboardingState,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["onboarding"],
    dependencies=[Depends(require_module("jobs"))],
)


ONBOARDING_STEPS = ["profile", "catalog", "technicians", "notifications", "demo", "done"]

STEP_LABELS = {
    "profile": "Company Profile",
    "catalog": "Service Catalog",
    "technicians": "Add Technicians",
    "notifications": "Notification Settings",
    "demo": "Demo Data",
    "done": "Complete",
}

STARTER_CATALOG_ITEMS: list[dict[str, Any]] = [
    {"name": "Spring replacement", "description": "Torsion/extension spring replacement", "price": 285.00, "category": "Repair"},
    {"name": "Cable repair", "description": "Lift cable replacement (pair)", "price": 195.00, "category": "Repair"},
    {"name": "Opener install", "description": "New garage door opener installation", "price": 495.00, "category": "Install"},
    {"name": "Door tune-up", "description": "Full safety & operation tune-up", "price": 129.00, "category": "Maintenance"},
    {"name": "Track alignment", "description": "Track straightening and realignment", "price": 175.00, "category": "Repair"},
]


# ---------- Pydantic schemas ----------


class StepIn(BaseModel):
    step: str = Field(min_length=1, max_length=50)

    @field_validator("step")
    @classmethod
    def _must_be_valid(cls, v: str) -> str:
        if v not in ONBOARDING_STEPS:
            raise ValueError(f"step must be one of {ONBOARDING_STEPS}")
        return v


class ChecklistPatchIn(BaseModel):
    step: str = Field(min_length=1, max_length=50)
    completed: bool

    @field_validator("step")
    @classmethod
    def _must_be_valid(cls, v: str) -> str:
        if v not in ONBOARDING_STEPS:
            raise ValueError(f"step must be one of {ONBOARDING_STEPS}")
        return v


# ---------- Helpers ----------


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _load_completed(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (ValueError, TypeError):
        log.exception("onboarding_completed_steps_parse_failed raw=%r", raw)
    return []


def _dump_completed(items: list[str]) -> str:
    return json.dumps(items)


def _serialize(s: OnboardingState) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "company_id": s.company_id,
        "current_step": s.current_step,
        "completed_steps": _load_completed(s.completed_steps),
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        "catalog_seeded": bool(s.catalog_seeded),
        "demo_data_loaded": bool(s.demo_data_loaded),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _get_or_create(db: Session, tenant_id: str) -> OnboardingState:
    row = db.execute(
        select(OnboardingState).where(OnboardingState.company_id == tenant_id)
    ).scalar_one_or_none()
    if row:
        return row
    row = OnboardingState(
        company_id=tenant_id,
        current_step="profile",
        completed_steps="[]",
        catalog_seeded=False,
        demo_data_loaded=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type="onboarding",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("onboarding_audit_failed action=%s", action)
        db.rollback()


# ---------- Endpoints ----------


@router.get("/api/onboarding/state", response_model=None)
def get_state(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)
    return _serialize(row)


@router.post("/api/onboarding/step", response_model=None)
def advance_step(
    payload: StepIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)
    prior = row.current_step
    completed = _load_completed(row.completed_steps)
    # mark the prior step as completed when advancing forward
    if prior and prior != payload.step and prior not in completed:
        completed.append(prior)
    row.current_step = payload.step
    row.completed_steps = _dump_completed(completed)
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="onboarding_step_advanced",
        entity_id=str(row.id),
        details={"from": prior, "to": payload.step},
        request=request,
    )
    return _serialize(row)


@router.post("/api/onboarding/seed-catalog", response_model=None)
def seed_catalog(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)
    if row.catalog_seeded:
        return {"seeded": True, "items_created": 0, "already_seeded": True}

    items_created = 0
    try:
        catalog = CustomCatalog(
            name="Starter Catalog",
            source_system="onboarding",
        )
        db.add(catalog)
        db.flush()

        for item in STARTER_CATALOG_ITEMS:
            db.add(CustomCatalogItem(
                catalog_id=catalog.id,
                name=item["name"],
                description=item["description"],
                price=item["price"],
                cost=0,
                category=item["category"],
                active=True,
            ))
            items_created += 1
        db.commit()
    except (OperationalError, ProgrammingError):
        log.exception("onboarding_seed_catalog_schema_missing tenant=%s", tenant_id)
        db.rollback()
        return {"seeded": False, "items_created": 0, "error": "catalog_tables_unavailable"}
    except Exception:
        log.exception("onboarding_seed_catalog_failed tenant=%s", tenant_id)
        db.rollback()
        return {"seeded": False, "items_created": 0, "error": "seed_failed"}

    row.catalog_seeded = True
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="onboarding_catalog_seeded",
        entity_id=str(row.id),
        details={"items_created": items_created},
        request=request,
    )
    return {"seeded": True, "items_created": items_created}


@router.post("/api/onboarding/demo-data", response_model=None)
def load_demo_data(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)

    customers_count = 0
    jobs_count = 0
    invoices_count = 0

    demo_customers = [
        {"name": "DEMO - Acme Residential", "email": "demo1@example.com", "phone": "555-0101"},
        {"name": "DEMO - Smith Family", "email": "demo2@example.com", "phone": "555-0102"},
        {"name": "DEMO - Johnson Estates", "email": "demo3@example.com", "phone": "555-0103"},
    ]

    try:
        customer_ids: list[UUID] = []
        for c in demo_customers:
            cust = Customer(
                name=c["name"],
                email=c["email"],
                phone=c["phone"],
                company_id=tenant_id,
            )
            db.add(cust)
            db.flush()
            customer_ids.append(cust.id)
            customers_count += 1

        for cid in customer_ids:
            job = Job(
                customer_id=cid,
                title="DEMO - Garage door tune-up",
                status="scheduled",
                company_id=tenant_id,
            )
            db.add(job)
            jobs_count += 1

        db.flush()

        inv = Invoice(
            customer_id=customer_ids[0],
            job_id=None,
            invoice_number=f"DEMO-{uuid4().hex[:8].upper()}",
            total=285.00,
            status="sent",
            company_id=tenant_id,
        )
        db.add(inv)
        invoices_count += 1
        db.commit()
    except (OperationalError, ProgrammingError):
        log.exception("onboarding_demo_data_schema_missing tenant=%s", tenant_id)
        db.rollback()
        return {"loaded": False, "customers": 0, "jobs": 0, "invoices": 0, "error": "demo_tables_unavailable"}
    except Exception:
        log.exception("onboarding_demo_data_failed tenant=%s", tenant_id)
        db.rollback()
        return {"loaded": False, "customers": 0, "jobs": 0, "invoices": 0, "error": "load_failed"}

    row.demo_data_loaded = True
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="onboarding_demo_loaded",
        entity_id=str(row.id),
        details={"customers": customers_count, "jobs": jobs_count, "invoices": invoices_count},
        request=request,
    )
    return {"loaded": True, "customers": customers_count, "jobs": jobs_count, "invoices": invoices_count}


@router.post("/api/onboarding/clear-demo", response_model=None)
def clear_demo_data(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)

    prefix = "DEMO - %"

    try:
        # Find demo customer IDs first
        demo_customer_ids = db.execute(
            select(Customer.id).where(
                Customer.company_id == tenant_id,
                Customer.name.like(prefix),
            )
        ).scalars().all()

        invoices_count = 0
        jobs_count = 0
        customers_count = 0

        if demo_customer_ids:
            # Delete demo invoices tied to demo customers
            inv_rows = db.execute(
                select(Invoice).where(Invoice.customer_id.in_(demo_customer_ids))
            ).scalars().all()
            invoices_count = len(inv_rows)
            for inv in inv_rows:
                db.delete(inv)

            # Delete demo jobs tied to demo customers
            job_rows = db.execute(
                select(Job).where(Job.customer_id.in_(demo_customer_ids))
            ).scalars().all()
            jobs_count = len(job_rows)
            for job in job_rows:
                db.delete(job)

            # Delete demo customers
            cust_rows = db.execute(
                select(Customer).where(
                    Customer.company_id == tenant_id,
                    Customer.name.like(prefix),
                )
            ).scalars().all()
            customers_count = len(cust_rows)
            for cust in cust_rows:
                db.delete(cust)

        db.commit()
    except (OperationalError, ProgrammingError):
        log.exception("onboarding_clear_demo_schema_missing tenant=%s", tenant_id)
        db.rollback()
        return {"cleared": False, "customers": 0, "jobs": 0, "invoices": 0, "error": "demo_tables_unavailable"}
    except Exception:
        log.exception("onboarding_clear_demo_failed tenant=%s", tenant_id)
        db.rollback()
        return {"cleared": False, "customers": 0, "jobs": 0, "invoices": 0, "error": "clear_failed"}

    row.demo_data_loaded = False
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="onboarding_demo_cleared",
        entity_id=str(row.id),
        details={"customers": customers_count, "jobs": jobs_count, "invoices": invoices_count},
        request=request,
    )
    return {"cleared": True, "customers": customers_count, "jobs": jobs_count, "invoices": invoices_count}


@router.post("/api/onboarding/complete", response_model=None)
def complete_onboarding(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)
    completed = _load_completed(row.completed_steps)
    if "done" not in completed:
        completed.append("done")
    row.current_step = "done"
    row.completed_steps = _dump_completed(completed)
    row.completed_at = utcnow()
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="onboarding_completed",
        entity_id=str(row.id),
        details={},
        request=request,
    )
    return _serialize(row)


@router.get("/api/onboarding/checklist", response_model=None)
def get_checklist(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)
    completed = set(_load_completed(row.completed_steps))
    return [
        {
            "step": step,
            "label": STEP_LABELS.get(step, step.title()),
            "completed": step in completed,
        }
        for step in ONBOARDING_STEPS
    ]


@router.patch("/api/onboarding/checklist", response_model=None)
def patch_checklist(
    payload: ChecklistPatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    row = _get_or_create(db, tenant_id)
    completed = _load_completed(row.completed_steps)
    changed = False
    if payload.completed:
        if payload.step not in completed:
            completed.append(payload.step)
            changed = True
    else:
        if payload.step in completed:
            completed = [s for s in completed if s != payload.step]
            changed = True
    if changed:
        row.completed_steps = _dump_completed(completed)
        row.updated_at = utcnow()
        db.commit()
        db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="checklist_updated",
        entity_id=str(row.id),
        details={"step": payload.step, "completed": payload.completed},
        request=request,
    )
    completed_set = set(_load_completed(row.completed_steps))
    return [
        {
            "step": step,
            "label": STEP_LABELS.get(step, step.title()),
            "completed": step in completed_set,
        }
        for step in ONBOARDING_STEPS
    ]
