from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.modules.timeclock.models import TimeClock
from gdx_dispatch.modules.timeclock.service import clock_in, clock_out, daily_labor_report
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/timeclock", tags=["timeclock"], dependencies=[Depends(require_module("timeclock"))])


class ClockInBody(BaseModel):
    job_id: UUID | None = None


@router.post("/clock-in", response_model=None)
def post_clock_in(payload: ClockInBody, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> TimeClock:
    try:
        return clock_in(user["user_id"], payload.job_id, db, company_id=user.get("tenant_id", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/clock-out/{timeclock_id}", response_model=None)
def post_clock_out(timeclock_id: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> TimeClock:
    _ = user
    try:
        return clock_out(timeclock_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from None


@router.get("/status", response_model=None)
def get_status(user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(select(TimeClock).where(TimeClock.technician_id == user["user_id"], TimeClock.clock_out_at.is_(None)).order_by(TimeClock.clock_in_at.desc())).scalar_one_or_none()
    return {"clocked_in": bool(row), "timeclock": row}


@router.get("/report", response_model=None)
def get_report(date: date, _: None = Depends(require_role("owner", "admin", "dispatcher", "manager", "accounting")), __: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return daily_labor_report(date, "America/New_York", db)
