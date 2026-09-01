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
        current_user={},
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
            current_user={},
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
        "/api/reports/labor-summary",
    }
    for route in labor_router.router.routes:
        if route.path not in auth_paths:
            continue
        deps = [getattr(dep, "call", None) for dep in route.dependant.dependencies]
        # Labor management now requires the dispatch/admin gate, which itself
        # authenticates via _current_user_dependency.
        assert labor_router._require_dispatch in deps


def test_entry_cost_falls_back_to_configured_cost_rate(db_session):
    """Plan §3: a labor row with no stored hourly_rate is costed at the
    tenant's configured loaded_labor_cost_per_hour ($65), not the $50 literal.
    A stored rate still wins (never re-resolve a written rate)."""
    from decimal import Decimal

    from gdx_dispatch.models.pricing_engine import PricingSettings
    from gdx_dispatch.models.tenant_models import TimeEntry

    PricingSettings.__table__.create(bind=db_session.get_bind(), checkfirst=True)
    db_session.add(PricingSettings(loaded_labor_cost_per_hour=Decimal("65")))
    db_session.commit()

    fallback = labor_router._cost_rate_fallback(db_session)
    assert fallback == 65.0

    no_rate = TimeEntry(
        company_id="tenant-test", job_id=None, tech_id="t", clock_in=None,
        duration_minutes=180, entry_type="work", hourly_rate=None,
    )
    # 3h × $65 = $195 (was $150 at the $50 literal).
    assert labor_router._entry_cost(no_rate, fallback) == 195.0

    stored = TimeEntry(
        company_id="tenant-test", job_id=None, tech_id="t", clock_in=None,
        duration_minutes=60, entry_type="work", hourly_rate=Decimal("42.5"),
    )
    assert labor_router._entry_cost(stored, fallback) == 42.5  # stored wins


def test_entry_to_dict_returns_tech_name_key(db_session):
    """Plan §3: the Tech column was blank because tech_name never existed in
    the serialized entry. It exists now (resolved name wins; stored tech_name
    is the fallback)."""
    from gdx_dispatch.models.tenant_models import TimeEntry

    e = TimeEntry(
        company_id="tenant-test", job_id=None, tech_id="t", clock_in=None,
        duration_minutes=60, entry_type="work", tech_name="Stored Name",
    )
    d = labor_router._entry_to_dict(e)
    assert "tech_name" in d
    assert d["tech_name"] == "Stored Name"
    assert labor_router._entry_to_dict(e, name="Resolved Name")["tech_name"] == "Resolved Name"


def test_entry_cost_stored_zero_stays_zero(db_session):
    """A deliberate $0 labor row (warranty / no-charge) must NOT be re-rated to
    the $65 fallback (audit round 2 — `x or fallback` treated 0.0 as falsy)."""
    from decimal import Decimal

    from gdx_dispatch.models.tenant_models import TimeEntry

    zero = TimeEntry(
        company_id="tenant-test", job_id=None, tech_id="t", clock_in=None,
        duration_minutes=120, entry_type="work", hourly_rate=Decimal("0"),
    )
    assert labor_router._entry_cost(zero, 65.0) == 0.0
    absent = TimeEntry(
        company_id="tenant-test", job_id=None, tech_id="t", clock_in=None,
        duration_minutes=120, entry_type="work", hourly_rate=None,
    )
    assert labor_router._entry_cost(absent, 65.0) == 130.0  # 2h × $65


def test_ui_compat_time_entries_requires_dispatch():
    """Plan §3 (audit round 2): the un-stubbed /api/labor/.../time-entries
    serves tenant-wide cost data and must carry the SAME dispatch gate as the
    real labor endpoint — a technician must get 403, not the rows."""
    from fastapi import HTTPException

    from gdx_dispatch.routers.ui_compat import list_labor_time_entries

    with pytest.raises(HTTPException) as ei:
        list_labor_time_entries(
            job_id="00000000-0000-4000-8000-000000000001",
            current_user={"role": "technician", "user_id": "u"},
            db=None,
        )
    assert ei.value.status_code == 403
    # An admin must NOT be blocked by the gate (it fails past the role check
    # into the query, which db=None makes raise a DIFFERENT error — proving the
    # gate itself let the admin through).
    with pytest.raises(Exception) as ei2:
        list_labor_time_entries(
            job_id="00000000-0000-4000-8000-000000000001",
            current_user={"role": "admin", "user_id": "u"},
            db=None,
        )
    assert not (isinstance(ei2.value, HTTPException) and ei2.value.status_code == 403)
