from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
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
            ),
            {},
            db_session,
        )
    assert exc.value.status_code == 422


def test_patch_time_entry_updates_duration(db_session):
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)

    updated = labor_router.update_time_entry(
        UUID(created["id"]),
        labor_router.TimeEntryPatch(clock_out=datetime(2026, 3, 20, 11, 30, tzinfo=UTC)),
        {},
        db_session,
    )
    assert updated["entry_type"] == "manual"
    assert updated["duration_minutes"] == 210


def test_entry_type_is_not_writable(db_session):
    """GDXA-1065 audit 3: a PATCH {"entry_type": "job", "clock_out": null}
    turned an office row into an open job timer that day close attests and
    billing claims; a POST could mint a "job" row the card then refuses to
    edit. Neither model takes entry_type any more, and POST stores "manual"."""
    with pytest.raises(ValidationError):
        labor_router.TimeEntryPatch(entry_type="job")
    with pytest.raises(ValidationError):
        labor_router.TimeEntryCreate(
            tech_id="tech-1",
            clock_in=datetime(2026, 3, 20, 8, 0, tzinfo=UTC),
            clock_out=datetime(2026, 3, 20, 9, 0, tzinfo=UTC),
            entry_type="job",
        )

    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    assert created["entry_type"] == "manual"
    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            UUID(created["id"]), labor_router.TimeEntryPatch(clock_out=None), {}, db_session,
        )
    assert exc.value.status_code == 422
    row = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    assert row.clock_out is not None and row.entry_type == "manual"


def test_patch_time_entry_404_when_missing(db_session):
    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            uuid4(),
            labor_router.TimeEntryPatch(tech_id="tech-2"),
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
    assert 'labor_router = _load_router("gdx_dispatch.routers", "labor")' in source
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


def _bill(db_session, entry_id: str) -> UUID:
    row = db_session.get(labor_router.TimeEntry, UUID(entry_id))
    row.billed_invoice_id = uuid4()
    row.day_closed_at = datetime(2026, 3, 20, 17, 0, tzinfo=UTC)
    db_session.commit()
    return row.billed_invoice_id


def test_billed_entry_refuses_patch_and_delete(db_session):
    """GDXA-1065: a day row an invoice claimed is that invoice's labor
    evidence; editing or deleting it would leave the invoice billing hours the
    record no longer shows."""
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    _bill(db_session, created["id"])

    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            UUID(created["id"]),
            labor_router.TimeEntryPatch(clock_out=datetime(2026, 3, 20, 12, 0, tzinfo=UTC)),
            {},
            db_session,
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        labor_router.delete_time_entry(UUID(created["id"]), {}, db_session)
    assert exc.value.status_code == 409

    row = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    assert row.duration_minutes == 120
    assert row.deleted_at is None



def test_day_closed_entry_refuses_patch_and_delete(db_session):
    """GDXA-1065: day close attests a row before any invoice claims it, and
    billing reads exactly the day-closed rows. Giving minutes to the 0-minute
    timer day close shut unvouched would bill labor already on the day's own
    row, so a day-closed row is refused with no invoice in sight."""
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    row = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    row.day_closed_at = datetime(2026, 3, 20, 17, 0, tzinfo=UTC)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            UUID(created["id"]),
            labor_router.TimeEntryPatch(clock_out=datetime(2026, 3, 20, 12, 0, tzinfo=UTC)),
            {},
            db_session,
        )
    assert exc.value.status_code == 409
    assert "day close" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        labor_router.delete_time_entry(UUID(created["id"]), {}, db_session)
    assert exc.value.status_code == 409

    db_session.expire_all()
    row = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    assert row.billed_invoice_id is None
    assert row.duration_minutes == 120
    assert row.deleted_at is None


def test_open_job_timer_refuses_patch_and_delete(db_session):
    """GDXA-1065: a tech's job timer that day close has not settled is day
    close's input (day_close.is_candidate). Giving it a clock_out here would
    make that tech's day close refuse them as having no open time."""
    from gdx_dispatch.services import day_close

    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    timer = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    timer.entry_type = "job"  # the shape mobile arrival opens: no clock_out
    timer.clock_out = None
    timer.duration_minutes = None
    db_session.commit()
    assert day_close.is_candidate(timer, "")

    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            timer.id,
            labor_router.TimeEntryPatch(clock_out=datetime(2026, 3, 20, 11, 0, tzinfo=UTC)),
            {},
            db_session,
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        labor_router.delete_time_entry(timer.id, {}, db_session)
    assert exc.value.status_code == 409

    db_session.expire_all()
    row = db_session.get(labor_router.TimeEntry, timer.id)
    assert row.clock_out is None
    assert row.deleted_at is None
    assert day_close.is_candidate(row, "")


def test_closeout_work_row_refuses_patch_and_delete(db_session):
    """GDXA-1065 audit 4: a final closeout with no arrival timer writes its
    attested row as entry_type "work", neither day-closed nor billed. That day
    bills from the closeout record and a re-closeout rewrites the row, so an
    edit here would split job costing from the invoice. Only "manual" rows are
    editable; any other type is refused by default."""
    from gdx_dispatch.routers.jobs import CLOSEOUT_LABOR_NOTE

    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    row = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    row.entry_type = "work"
    row.notes = CLOSEOUT_LABOR_NOTE
    db_session.commit()
    assert row.day_closed_at is None and row.billed_invoice_id is None

    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            row.id,
            labor_router.TimeEntryPatch(clock_out=datetime(2026, 3, 20, 11, 0, tzinfo=UTC)),
            {},
            db_session,
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        labor_router.delete_time_entry(row.id, {}, db_session)
    assert exc.value.status_code == 409

    db_session.expire_all()
    kept = db_session.get(labor_router.TimeEntry, row.id)
    assert kept.duration_minutes == 120
    assert kept.deleted_at is None


def test_manual_row_with_user_id_refuses_patch_and_delete(db_session):
    """GDXA-1065 audit 5: entry_type DEFAULTS to "manual", so the type alone
    fails open for a writer that omits it. A row carrying a user_id is a
    tech's own time (payroll's summary groups by it), and this router never
    sets one, so it is refused whatever its type."""
    from uuid import uuid4

    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    assert created["user_id"] is None
    row = db_session.get(labor_router.TimeEntry, UUID(created["id"]))
    row.user_id = str(uuid4())  # String(36), the shape mobile arrival writes
    db_session.commit()
    assert row.entry_type == "manual"

    with pytest.raises(HTTPException) as exc:
        labor_router.update_time_entry(
            row.id,
            labor_router.TimeEntryPatch(clock_out=datetime(2026, 3, 20, 11, 0, tzinfo=UTC)),
            {},
            db_session,
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        labor_router.delete_time_entry(row.id, {}, db_session)
    assert exc.value.status_code == 409

    db_session.expire_all()
    kept = db_session.get(labor_router.TimeEntry, row.id)
    assert kept.duration_minutes == 120
    assert kept.deleted_at is None
    listed = labor_router.list_job_time_entries(UUID(job_id), {}, db_session)
    assert listed[0]["user_id"] is not None


def test_entry_to_dict_exposes_billed_invoice(db_session):
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    assert created["billed_invoice_id"] is None
    assert created["day_closed_at"] is None
    inv = _bill(db_session, created["id"])

    listed = labor_router.list_job_time_entries(UUID(job_id), {}, db_session)
    assert listed[0]["billed_invoice_id"] == str(inv)
    assert listed[0]["day_closed_at"] is not None


def test_time_entry_writes_audit_what_changed(db_session, monkeypatch):
    """Every write says what changed: create records the row, update a
    before/after pair, delete the row it removed (GDXA-1065)."""
    calls: list[dict] = []
    monkeypatch.setattr(
        labor_router, "log_audit_event_sync", lambda _db, **kw: calls.append(kw)
    )
    job_id = _seed_job(db_session)
    created = _create_entry(db_session, job_id)
    labor_router.update_time_entry(
        UUID(created["id"]),
        labor_router.TimeEntryPatch(clock_out=datetime(2026, 3, 20, 11, 0, tzinfo=UTC)),
        {},
        db_session,
    )
    labor_router.delete_time_entry(UUID(created["id"]), {}, db_session)

    create, update, delete = calls
    assert create["action"] == "create_job_time_entry"
    assert create["details"]["time_entry_id"] == created["id"]
    assert create["details"]["after"]["duration_minutes"] == 120
    assert update["action"] == "update_time_entry"
    assert update["details"]["before"]["duration_minutes"] == 120
    assert update["details"]["after"]["duration_minutes"] == 180
    assert delete["action"] == "delete_time_entry"
    assert delete["details"]["before"]["duration_minutes"] == 180
    assert delete["details"]["job_id"] == job_id
