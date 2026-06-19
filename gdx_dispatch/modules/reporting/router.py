from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.reporting.models import SavedReport
from gdx_dispatch.modules.reporting.service import job_costing_report, revenue_report, tech_performance_report

router = APIRouter(prefix="/api", tags=["reporting"])


@router.get("/reporting/job-costing", response_model=None)
def get_job_costing(
    start_date: str = Query(...),
    end_date: str = Query(...),
    db: Session = Depends(get_db),
) -> Any:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    return job_costing_report(start, end, db)


@router.get("/reporting/tech-performance", response_model=None)
def get_tech_performance(
    start_date: str = Query(...),
    end_date: str = Query(...),
    db: Session = Depends(get_db),
) -> Any:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    return tech_performance_report(start, end, db)


@router.get("/reporting/revenue", response_model=None)
def get_revenue(
    start_date: str = Query(...),
    end_date: str = Query(...),
    db: Session = Depends(get_db),
) -> Any:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    return revenue_report(start, end, db)


@router.post("/reporting/saved", response_model=None)
def create_saved_report(
    payload: dict,
    db: Session = Depends(get_db),
) -> Any:
    report = SavedReport(
        name=payload["name"],
        report_type=payload["report_type"],
        config=payload.get("config"),
        created_by=payload.get("created_by"),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return {
        "id": str(report.id),
        "name": report.name,
        "report_type": report.report_type,
        "config": report.config,
        "created_by": report.created_by,
        "created_at": report.created_at.isoformat(),
    }


@router.get("/reporting/saved", response_model=None)
def list_saved_reports(db: Session = Depends(get_db)) -> Any:
    reports = db.query(SavedReport).order_by(SavedReport.created_at.desc()).all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "report_type": r.report_type,
            "config": r.config,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat(),
        }
        for r in reports
    ]
