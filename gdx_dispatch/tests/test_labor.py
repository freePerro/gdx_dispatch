from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import labor as labor_router


def _mock_request() -> SimpleNamespace:
    """Minimal Request stand-in for direct router calls.

    Router reads request.state.tenant with a {"id": "tenant-test"}
    fallback, so an empty state is sufficient to exercise the fallback.
    """
    return SimpleNamespace(state=SimpleNamespace())


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    db = SessionLocal()
    # Ensure tables exist even if metadata got corrupted by importlib.reload in other tests
    db.execute(text("CREATE TABLE IF NOT EXISTS technicians (id TEXT PRIMARY KEY, company_id TEXT, full_name TEXT, hourly_rate REAL, active BOOLEAN DEFAULT 1)"))
    db.execute(text("CREATE TABLE IF NOT EXISTS job_parts (id TEXT PRIMARY KEY, job_id TEXT, part_id TEXT, part_name TEXT, qty_used INTEGER, unit_cost_at_time REAL, created_at TIMESTAMP)"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_job(db_session) -> str:
    row = labor_router.Job(
        customer_id=None,
        title="Labor Test Job",
        description="test",
        lifecycle_stage="estimate",
        dispatch_status="unassigned",
        billing_status="unbilled",
        is_return_visit=False,
        created_at=datetime.now(UTC),
        company_id="tenant-test",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return str(row.id)


def _create_entry(db_session, job_id: str, **overrides) -> dict:
    payload = {
        "tech_id": "tech-1",
        "clock_in": datetime(2026, 3, 20, 8, 0, tzinfo=UTC),
        "clock_out": datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        "entry_type": "manual",
    }
    payload.update(overrides)
    return labor_router.create_job_time_entry(
        _mock_request(),
        UUID(job_id),
        labor_router.TimeEntryCreate(**payload),
        {},
        db_session,
    )


def test_create_and_list_job_time_entries(db_session):
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)

    assert UUID(created["id"])
    assert created["job_id"] == job_id
    assert created["tech_id"] == "tech-1"
    assert created["duration_minutes"] == 120
    assert created["entry_type"] == "manual"

    listed = labor_router.list_job_time_entries(UUID(job_id), {}, db_session)
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]


def test_create_time_entry_404_for_missing_job(db_session):
    with pytest.raises(HTTPException) as exc:
        labor_router.create_job_time_entry(
            _mock_request(),
            uuid4(),
            labor_router.TimeEntryCreate(
                tech_id="tech-1",
                clock_in=datetime(2026, 3, 20, 8, 0, tzinfo=UTC),
                clock_out=datetime(2026, 3, 20, 9, 0, tzinfo=UTC),
                entry_type="manual",
            ),
            {},
            db_session,
        )
    assert exc.value.status_code == 404


def test_create_time_entry_rejects_invalid_range(db_session):
    job_id = _seed_job(db_session)
    with pytest.raises(HTTPException) as exc:
        labor_router.create_job_time_entry(
            _mock_request(),
            UUID(job_id),
            labor_router.TimeEntryCreate(
                tech_id="tech-1",
                clock_in=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
                clock_out=datetime(2026, 3, 20, 9, 0, tzinfo=UTC),
                entry_type="manual",
            ),
            {},
            db_session,
        )
    assert exc.value.status_code == 422


def test_patch_time_entry_updates_duration_and_type(db_session):
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)

    updated = labor_router.update_time_entry(
        UUID(created["id"]),
        labor_router.TimeEntryPatch(
            clock_out=datetime(2026, 3, 20, 11, 30, tzinfo=UTC),
            entry_type="adjusted",
        ),
        {},
        db_session,
    )
    assert updated["entry_type"] == "adjusted"
    assert updated["duration_minutes"] == 210


def test_patch_time_entry_404_when_missing(db_session):
    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            uuid4(),
            labor_router.TimeEntryPatch(entry_type="adjusted"),
            {},
            db_session,
        )
    assert exc.value.status_code == 404


def test_delete_time_entry_soft_delete(db_session):
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)

    deleted = labor_router.delete_time_entry(UUID(created["id"]), {}, db_session)
    assert deleted == {"deleted": True}

    listed = labor_router.list_job_time_entries(UUID(job_id), {}, db_session)
    assert listed == []

    row = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    assert row is not None
    assert row.deleted_at is not None


def test_get_job_costing_breakdown(db_session):
    job_id = _seed_job(db_session)
    _create_entry(
        db_session,
        job_id,
        clock_in=datetime(2026, 3, 20, 8, 0, tzinfo=UTC),
        clock_out=datetime(2026, 3, 20, 9, 0, tzinfo=UTC),
    )
    _create_entry(
        db_session,
        job_id,
        tech_id="tech-2",
        clock_in=datetime(2026, 3, 20, 9, 30, tzinfo=UTC),
        clock_out=datetime(2026, 3, 20, 10, 30, tzinfo=UTC),
    )

    # Use UUID hex format for IDs — the ORM JobPart model uses Uuid(as_uuid=True)
    # which stores as CHAR(32) hex in SQLite.  str(uuid4()) has dashes (36 chars)
    # and won't match the ORM query.
    _job_uuid = UUID(job_id)
    db_session.execute(
        text(
            """
            INSERT INTO job_parts (id, job_id, part_id, qty_used, unit_cost_at_time, created_at)
            VALUES (:id, :job_id, :part_id, :qty_used, :unit_cost_at_time, :created_at)
            """
        ),
        {
            "id": uuid4().hex,
            "job_id": _job_uuid.hex,
            "part_id": uuid4().hex,
            "qty_used": 2,
            "unit_cost_at_time": 30,
            "created_at": datetime.now(UTC),
        },
    )
    db_session.commit()

    body = labor_router.get_job_labor_costing(UUID(job_id), {}, db_session)
    assert body["job_id"] == job_id
    assert body["labor_cost"] == 100.0
    assert body["materials_cost"] == 60.0
    assert body["overhead_cost"] == 12.8
    assert body["total_cost"] == 172.8


def test_get_job_costing_404_for_missing_job(db_session):
    with pytest.raises(HTTPException) as exc:
        labor_router.get_job_labor_costing(uuid4(), {}, db_session)
    assert exc.value.status_code == 404


def test_labor_summary_by_technician(db_session):
    job_id = _seed_job(db_session)
    _create_entry(
        db_session,
        job_id,
        tech_id="tech-a",
        clock_in=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        clock_out=datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
    )
    _create_entry(
        db_session,
        job_id,
        tech_id="tech-a",
        clock_in=datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
        clock_out=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
    )
    _create_entry(
        db_session,
        job_id,
        tech_id="tech-b",
        clock_in=datetime(2026, 3, 2, 8, 0, tzinfo=UTC),
        clock_out=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
    )

    data = labor_router.labor_summary(
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 3),
        _={},
        db=db_session,
    )
    assert data["total_hours"] == 3.5
    assert data["total_cost"] == 175.0
    by_tech = {row["tech_id"]: row for row in data["items"]}
    assert by_tech["tech-a"]["hours"] == 1.5
    assert by_tech["tech-a"]["cost"] == 75.0
    assert by_tech["tech-b"]["hours"] == 2.0
    assert by_tech["tech-b"]["cost"] == 100.0


def test_labor_summary_rejects_invalid_date_range(db_session):
    with pytest.raises(HTTPException) as exc:
        labor_router.labor_summary(
            start_date=date(2026, 3, 10),
            end_date=date(2026, 3, 1),
            _={},
            db=db_session,
        )
    assert exc.value.status_code == 422


def test_auth_dependency_rejects_missing_token():
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc:
        import asyncio

        asyncio.run(labor_router._current_user_dependency(request))
    assert exc.value.status_code == 401


def test_auth_dependency_accepts_bearer_token(monkeypatch):
    async def _fake_get_current_user(request, token: str) -> dict[str, str]:
        assert token == "abc123"
        return {"user_id": "u-1"}

    monkeypatch.setattr(labor_router, "get_current_user", _fake_get_current_user)
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer abc123")],
        }
    )

    import asyncio

    user = asyncio.run(labor_router._current_user_dependency(request))
    assert user["user_id"] == "u-1"


def test_labor_routes_registered_in_main_app():
    from pathlib import Path

    source = Path("gdx_dispatch/app.py").read_text()
    assert "from gdx_dispatch.routers import labor as labor_router" in source
    assert "app.include_router(labor_router.router if hasattr(labor_router, \"router\") else labor_router)" in source


def test_labor_routes_require_auth_dependency():
    auth_paths = {
        "/api/jobs/{job_id}/time-entries",
        "/api/time-entries/{entry_id}",
        "/api/jobs/{job_id}/costing",
        "/api/reports/labor-summary",
    }
    for route in labor_router.router.routes:
        if route.path not in auth_paths:
            continue
        deps = [getattr(dep, "call", None) for dep in route.dependant.dependencies]
        # Labor management now requires the dispatch/admin gate, which itself
        # authenticates via _current_user_dependency.
        assert labor_router._require_dispatch in deps
