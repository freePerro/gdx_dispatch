from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.modules.timeclock.models import TimeClock
from gdx_dispatch.modules.timeclock.service import clock_out, daily_labor_report
from gdx_dispatch.routers.auth import get_current_user

# POST /clock-in and GET /status left this module 2026-09-06 (#569): routers/timeclock.py
# registers them first, so FastAPI never dispatched here. The two routes below are
# unique to this module.
router = APIRouter(prefix="/api/timeclock", tags=["timeclock"], dependencies=[Depends(require_module("timeclock"))])


@router.post("/clock-out/{timeclock_id}", response_model=None)
def post_clock_out(timeclock_id: UUID, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> TimeClock:
    _ = user
    try:
        return clock_out(timeclock_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from None


@router.get("/report", response_model=None)
def get_report(date: date, _: None = Depends(require_role("owner", "admin", "dispatcher", "manager", "accounting")), __: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return daily_labor_report(date, "America/New_York", db)
