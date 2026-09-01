"""Readers of time_entries must exclude soft-deleted labor rows."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Job
from gdx_dispatch.routers import jobs, mobile_day_summary

TENANT = "tenant-test"
USER = "user-1"
TECH = "tech-1"


def _engine_and_sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


def _app(router, SessionLocal):
    app = FastAPI()

    @app.middleware("http")
    async def _tenant_shim(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": TENANT}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[jobs.get_db] = lambda: SessionLocal()
    app.dependency_overrides[jobs.get_current_user] = lambda: {
        "user_id": USER,
        "tenant_id": TENANT,
        "role": "admin",
    }
    app.dependency_overrides[mobile_day_summary.get_current_user] = app.dependency_overrides[
        jobs.get_current_user
    ]
    app.dependency_overrides[require_module("mobile")] = lambda: True
    return TestClient(app)


def _insert_time_entry(
    db,
    job_id,
    *,
    duration_minutes: int,
    clock_in: datetime,
    deleted: bool = False,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO time_entries (
                id, job_id, company_id, tech_id, user_id, clock_in, clock_out,
                duration_minutes, entry_type, created_at, updated_at, deleted_at
            ) VALUES (
                :id, :job_id, :company_id, :tech_id, :user_id, :clock_in, :clock_out,
                :duration_minutes, 'manual', :clock_in, :clock_in, :deleted_at
            )
            """
        ),
        {
            "id": str(uuid4()),
            "job_id": job_id.hex,
            "company_id": TENANT,
            "tech_id": TECH,
            "user_id": USER,
            "clock_in": clock_in,
            "clock_out": clock_in + timedelta(minutes=duration_minutes),
            "duration_minutes": duration_minutes,
            "deleted_at": clock_in + timedelta(days=1) if deleted else None,
        },
    )


def test_time_entry_readers_exclude_soft_deleted_rows():
    SessionLocal, engine = _engine_and_sessions()
    client = _app(mobile_day_summary.router, SessionLocal)
    db = SessionLocal()
    try:
        now = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
        target = Job(
            id=uuid4(),
            company_id=TENANT,
            title="Target repair",
            status="Scheduled",
            job_type="repair",
        )
        completed = Job(
            id=uuid4(),
            company_id=TENANT,
            title="Completed repair",
            status="Completed",
            job_type="repair",
        )
        db.add_all([target, completed])
        db.flush()
        _insert_time_entry(db, target.id, duration_minutes=60, clock_in=now)
        _insert_time_entry(db, target.id, duration_minutes=120, clock_in=now, deleted=True)
        _insert_time_entry(db, completed.id, duration_minutes=60, clock_in=now)
        _insert_time_entry(db, completed.id, duration_minutes=600, clock_in=now, deleted=True)
        db.commit()
        visible_hours = db.execute(
            text(
                "SELECT COALESCE(SUM((julianday(clock_out) - julianday(clock_in)) * 24.0), 0) "
                "FROM time_entries WHERE CAST(user_id AS TEXT) = :uid "
                "AND clock_in >= :start AND clock_in < :end AND clock_out IS NOT NULL "
                "AND deleted_at IS NULL"
            ),
            {"uid": USER, "start": now.replace(hour=0), "end": now.replace(hour=0) + timedelta(days=1)},
        ).scalar()
        assert round(float(visible_hours), 2) == 2.0

        summary = client.get(f"/api/mobile/day-summary?date={now.date().isoformat()}")
        assert summary.status_code == 200, summary.text
        assert summary.json()["labor_hours"] == 2.0

        client = _app(jobs.router, SessionLocal)
        duration = client.get(f"/api/jobs/{target.id.hex}/duration")
        assert duration.status_code == 200, duration.text
        assert duration.json()["actual_minutes"] == 60
        assert duration.json()["estimated_hours"] == 1.0

        costing = client.get(f"/api/jobs/{target.id.hex}/costing")
        assert costing.status_code == 200, costing.text
        assert costing.json()["labor_minutes"] == 60
    finally:
        db.close()
        engine.dispose()
