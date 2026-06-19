from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.maintenance.models import CustomerPlanEnrollment, ServicePlan
from gdx_dispatch.modules.maintenance.service import cancel_plan, enroll_customer
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api", tags=["maintenance"], dependencies=[Depends(require_module("maintenance_plans"))])

class PlanIn(BaseModel): name: str; description: str | None = None; price_monthly: float | None = None; price_annual: float | None = None; visits_per_year: int = 2; includes_parts: bool = False; stripe_price_id_monthly: str | None = None; stripe_price_id_annual: str | None = None; is_active: bool = True  # noqa: E701,E702
class EnrollIn(BaseModel): plan_id: UUID; stripe_subscription_id: str | None = None  # noqa: E701,E702

@router.get("/maintenance/plans", response_model=None)
def list_plans(db: Session = Depends(get_db)) -> list[ServicePlan]:
    return list(db.execute(select(ServicePlan).where(ServicePlan.is_active.is_(True)).order_by(ServicePlan.created_at.desc())).scalars().all())

@router.post("/maintenance/plans", response_model=None)
def create_plan(payload: PlanIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> ServicePlan:
    if user.get("role") not in {"owner", "admin"}: raise HTTPException(status_code=403, detail="Insufficient role")  # noqa: E701,E702
    row = ServicePlan(**payload.model_dump()); db.add(row); db.commit(); db.refresh(row); return row  # noqa: E701,E702

@router.get("/customers/{customer_id}/plan", response_model=None)
def get_enrollment(customer_id: UUID, db: Session = Depends(get_db)) -> CustomerPlanEnrollment | None:
    return db.execute(select(CustomerPlanEnrollment).where(CustomerPlanEnrollment.customer_id == customer_id).order_by(CustomerPlanEnrollment.enrolled_at.desc())).scalar_one_or_none()

@router.post("/customers/{customer_id}/plan/enroll", response_model=None)
def enroll(customer_id: UUID, payload: EnrollIn, db: Session = Depends(get_db)) -> CustomerPlanEnrollment:
    try: return enroll_customer(customer_id, payload.plan_id, payload.stripe_subscription_id, db)  # noqa: E701,E702
    except ValueError as exc: raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from None  # noqa: E701,E702

@router.post("/customers/{customer_id}/plan/cancel", response_model=None)
def cancel(customer_id: UUID, db: Session = Depends(get_db)) -> CustomerPlanEnrollment:
    row = db.execute(select(CustomerPlanEnrollment).where(CustomerPlanEnrollment.customer_id == customer_id, CustomerPlanEnrollment.status != "canceled").order_by(CustomerPlanEnrollment.enrolled_at.desc())).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Active enrollment not found")  # noqa: E701,E702
    return cancel_plan(row.id, db)

@router.get("/maintenance/upcoming", response_model=None)
def upcoming(db: Session = Depends(get_db)) -> list[CustomerPlanEnrollment]:
    now = utcnow(); end = now + timedelta(days=7)  # noqa: E701,E702
    q = select(CustomerPlanEnrollment).where(CustomerPlanEnrollment.status == "active", CustomerPlanEnrollment.next_service_at.is_not(None), CustomerPlanEnrollment.next_service_at >= now, CustomerPlanEnrollment.next_service_at <= end).order_by(CustomerPlanEnrollment.next_service_at.asc())
    return list(db.execute(q).scalars().all())
