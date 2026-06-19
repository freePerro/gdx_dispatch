from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.modules.maintenance.models import CustomerPlanEnrollment, ServicePlan


def _days(visits_per_year: int) -> float:
    return 365 / max(visits_per_year, 1)


def enroll_customer(customer_id: UUID, plan_id: UUID, stripe_sub_id: str | None, db: Session) -> CustomerPlanEnrollment:
    plan = db.execute(select(ServicePlan).where(ServicePlan.id == plan_id)).scalar_one_or_none()
    if not plan:
        raise ValueError("Plan not found")
    row = CustomerPlanEnrollment(customer_id=customer_id, plan_id=plan_id, stripe_subscription_id=stripe_sub_id, next_service_at=utcnow() + timedelta(days=_days(plan.visits_per_year)))
    db.add(row); db.flush(); asyncio.run(log_audit_event(db, "plan_enrolled", "system", "customer_plan_enrollment", str(row.id), {"customer_id": str(customer_id), "plan_id": str(plan_id)})); db.commit(); db.refresh(row)  # noqa: E701,E702
    return row


def schedule_next_visit(enrollment_id: UUID, db: Session) -> CustomerPlanEnrollment:
    row = db.execute(select(CustomerPlanEnrollment).where(CustomerPlanEnrollment.id == enrollment_id)).scalar_one_or_none()
    if not row:
        raise ValueError("Enrollment not found")
    plan = db.execute(select(ServicePlan).where(ServicePlan.id == row.plan_id)).scalar_one_or_none()
    if not plan:
        raise ValueError("Plan not found")
    row.next_service_at = (row.next_service_at or utcnow()) + timedelta(days=_days(plan.visits_per_year))
    asyncio.run(log_audit_event(db, "next_visit_scheduled", "system", "customer_plan_enrollment", str(row.id), {"next_service_at": row.next_service_at.isoformat()})); db.commit(); db.refresh(row)  # noqa: E701,E702
    return row


def cancel_plan(enrollment_id: UUID, db: Session) -> CustomerPlanEnrollment:
    row = db.execute(select(CustomerPlanEnrollment).where(CustomerPlanEnrollment.id == enrollment_id)).scalar_one_or_none()
    if not row:
        raise ValueError("Enrollment not found")
    # Stripe cancellation is handled by the caller (for example, a Celery task).
    row.status, row.canceled_at = "canceled", utcnow()
    asyncio.run(log_audit_event(db, "plan_canceled", "system", "customer_plan_enrollment", str(row.id), {"stripe_subscription_id": row.stripe_subscription_id}))
    db.commit(); db.refresh(row); return row  # noqa: E701,E702
