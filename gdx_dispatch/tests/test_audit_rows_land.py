"""#700: an audit row written after the final commit never landed.

``get_db()`` yields a fresh session and closes it WITHOUT committing, so a row
flushed after the handler's last commit is rolled back at the end of the
request. On prod that left 8 holding areas and 49 job assignments with no
audit trail, and nothing errored.

The harness has to reproduce that or it proves nothing: most router tests
hand every request one shared, never-closed session, where a flushed row
survives and a lost one can never be seen. Here each request gets its own
session that is closed without a commit — exactly ``core.database.get_db`` —
and every read happens in a separate session afterwards.

One surface per fix shape: a plain move before the commit (holding areas, job
assignments), a move inside a branch (holding-area update/delete), a
swallowing ``try/except`` removed (timeclock breaks, planner), a commit that
only ran on one branch (parts), and a service that commits for the handler,
where the row is now staged before the service is called (forecast settings,
QB schedule, integrations).

A row that lands must also say WHO — the rows this change makes land for
budgets, forecasting, QB and integrations used to name "api", "system" or the
tenant id, so those tests check ``user_id`` too.
"""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Every router driven below is imported at module load, before the fixture's
# create_all, so each model it touches is registered on TenantBase.metadata.
from gdx_dispatch.core import integrations as integrations_mod
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.modules.forecasting import router as forecasting_mod
from gdx_dispatch.modules.quickbooks import router as qb_router_mod
from gdx_dispatch.routers import budgets as budgets_mod
from gdx_dispatch.routers import holding_areas as holding_areas_mod
from gdx_dispatch.routers import job_assignments as job_assignments_mod
from gdx_dispatch.routers import parts_needed as parts_needed_mod
from gdx_dispatch.routers import planner as planner_mod
from gdx_dispatch.routers import timeclock as timeclock_mod

TENANT = "11111111-1111-1111-1111-111111111700"
# The shape prod's get_current_user returns (finalize_login_jwt): no `sub`, no
# `email` claim.
OFFICE = {"user_id": "00000000-0000-0000-0000-000000000701", "tenant_id": TENANT, "role": "admin"}
TECH = {"user_id": "00000000-0000-0000-0000-000000000702", "tenant_id": TENANT, "role": "technician"}


@pytest.fixture
def SessionLocal():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _client(SessionLocal, router, user: dict) -> TestClient:
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user

    app = FastAPI()
    app.include_router(router)

    def _like_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()  # no commit — exactly what core.database.get_db does

    app.dependency_overrides[get_db] = _like_get_db
    app.dependency_overrides[get_current_user] = lambda: user

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    return TestClient(app)


def _audit_rows(SessionLocal, *, entity_type: str, entity_id: str | None = None) -> list[AuditLog]:
    with SessionLocal() as db:
        q = select(AuditLog).where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            q = q.where(AuditLog.entity_id == entity_id)
        return list(db.execute(q.order_by(AuditLog.created_at, AuditLog.id)).scalars())


# ── holding areas: 8 on prod, 0 audit rows ────────────────────────────────────


def test_holding_area_create_update_delete_each_leave_a_row(SessionLocal):
    client = _client(SessionLocal, holding_areas_mod.router, OFFICE)
    created = client.post("/api/holding-areas", json={"name": "Back lot", "color": "#112233"})
    assert created.status_code == 201, created.text
    area_id = created.json()["id"]
    assert client.put(f"/api/holding-areas/{area_id}", json={"name": "Side lot"}).status_code == 200
    assert client.delete(f"/api/holding-areas/{area_id}").status_code == 200

    rows = _audit_rows(SessionLocal, entity_type="holding_area", entity_id=area_id)
    assert [r.action for r in rows] == ["create", "update", "delete"]
    assert {r.user_id for r in rows} == {OFFICE["user_id"]}
    assert rows[1].details == {"name": "Side lot"}


def test_holding_area_update_of_a_missing_area_records_nothing(SessionLocal):
    """Nothing changed, so there is nothing to attribute — the row that used to
    be flushed here for a nonexistent id never landed anyway."""
    client = _client(SessionLocal, holding_areas_mod.router, OFFICE)
    missing = "22222222-2222-2222-2222-222222222222"
    assert client.put(f"/api/holding-areas/{missing}", json={"name": "Nowhere"}).status_code == 200
    assert _audit_rows(SessionLocal, entity_type="holding_area") == []


# ── job assignments: 49 on prod, no trail ─────────────────────────────────────


def test_job_assignment_add_lead_and_remove_each_leave_a_row(SessionLocal):
    client = _client(SessionLocal, job_assignments_mod.router, OFFICE)
    job_id = "33333333-3333-3333-3333-333333333333"
    added = client.post(f"/api/jobs/{job_id}/assignments", json={"tech_id": "tech-a"})
    assert added.status_code == 201, added.text
    assignment_id = added.json()["id"]
    assert client.put(f"/api/jobs/{job_id}/lead", json={"tech_id": "tech-a"}).status_code == 200
    assert client.delete(f"/api/jobs/{job_id}/assignments/{assignment_id}").status_code == 200

    trail = _audit_rows(SessionLocal, entity_type="job_assignment", entity_id=assignment_id)
    assert [r.action for r in trail] == ["assign", "unassign"]
    lead = _audit_rows(SessionLocal, entity_type="job", entity_id=job_id)
    assert [r.action for r in lead] == ["set_lead"]
    assert lead[0].details["lead_tech_id"] == "tech-a"
    assert {r.user_id for r in trail + lead} == {OFFICE["user_id"]}


# ── timeclock breaks: the audit used to sit in a swallowing try/except ────────


def test_break_start_and_end_each_leave_a_row(SessionLocal):
    client = _client(SessionLocal, timeclock_mod.router, TECH)
    started = client.post("/api/timeclock/break/start", json={"type": "lunch"})
    assert started.status_code == 201, started.text
    break_id = started.json()["id"]
    ended = client.post("/api/timeclock/break/end", json={"break_id": break_id})
    assert ended.status_code == 200, ended.text

    rows = _audit_rows(SessionLocal, entity_type="timeclock_break", entity_id=break_id)
    assert [r.action for r in rows] == ["timeclock_break_started", "timeclock_break_ended"]
    assert {r.user_id for r in rows} == {TECH["user_id"]}


def test_a_failed_audit_write_takes_the_break_down_with_it(SessionLocal, monkeypatch):
    """The old code swallowed an audit failure after the break had committed —
    a break nobody could attribute. Now the pair is atomic: no row, no break."""
    from gdx_dispatch.models.tenant_models import TimeclockBreak

    async def _refuse(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(timeclock_mod, "log_audit_event", _refuse)
    client = _client(SessionLocal, timeclock_mod.router, TECH)
    with pytest.raises(RuntimeError):
        client.post("/api/timeclock/break/start", json={"type": "rest"})
    with SessionLocal() as db:
        assert db.execute(select(TimeclockBreak)).scalars().all() == []


# ── planner: same swallow removed ─────────────────────────────────────────────


def test_planner_task_create_leaves_a_row(SessionLocal):
    client = _client(SessionLocal, planner_mod.router, OFFICE)
    r = client.post("/api/planner/tasks", json={"title": "Call back about the opener"})
    assert r.status_code == 201, r.text
    rows = _audit_rows(SessionLocal, entity_type="planner_task", entity_id=r.json()["id"])
    assert [r.action for r in rows] == ["create_task"]
    assert rows[0].user_id == OFFICE["user_id"]


# ── parts: the only later commit ran for critical parts ──────────────────────


def test_an_ordinary_part_and_a_wont_bill_flip_each_leave_a_row(SessionLocal):
    client = _client(SessionLocal, parts_needed_mod.router, OFFICE)
    job_id = "44444444-4444-4444-4444-444444444444"
    added = client.post(f"/api/jobs/{job_id}/parts-needed", json={"part_name": "Torsion spring"})
    assert added.status_code == 201, added.text
    part_id = added.json()["id"]
    flipped = client.patch(f"/api/parts-needed/{part_id}/status", json={"status": "wont_bill"})
    assert flipped.status_code == 200, flipped.text

    rows = _audit_rows(SessionLocal, entity_type="part_needed", entity_id=part_id)
    assert [r.action for r in rows] == ["create", "update"]
    assert rows[1].details["status"] == {"from": "needed", "to": "wont_bill"}
    assert {r.user_id for r in rows} == {OFFICE["user_id"]}


# ── budgets: rows landed as "api" ────────────────────────────────────────────


def test_budget_lock_and_unlock_leave_rows_naming_the_user(SessionLocal):
    from gdx_dispatch.models.tenant_models import MonthlyBudget

    with SessionLocal() as db:
        db.add(MonthlyBudget(id="line-700", year=2026, month=9, qb_account_id="61"))
        db.commit()
    client = _client(SessionLocal, budgets_mod.router, OFFICE)
    assert client.post("/api/budgets/line-700/lock").status_code == 200
    assert client.post("/api/budgets/line-700/unlock").status_code == 200

    rows = _audit_rows(SessionLocal, entity_type="monthly_budget", entity_id="line-700")
    assert [r.action for r in rows] == ["monthly_budget.lock", "monthly_budget.unlock"]
    assert {r.user_id for r in rows} == {OFFICE["user_id"]}


# ── forecast settings: staged before the service that commits ────────────────


def test_forecast_settings_update_leaves_a_row_naming_the_user(SessionLocal):
    client = _client(SessionLocal, forecasting_mod.router, OFFICE)
    r = client.put("/api/forecast/settings", json={"collect_rate_0_30": 0.9})
    assert r.status_code == 200, r.text
    rows = _audit_rows(SessionLocal, entity_type="forecast_settings")
    assert [r.action for r in rows] == ["forecast_settings.update"]
    assert rows[0].details == {"collect_rate_0_30": 0.9}
    # Read `sub` alone before — prod's login has none, so this said "system".
    assert rows[0].user_id == OFFICE["user_id"]


def test_a_failed_audit_write_leaves_forecast_settings_unchanged(SessionLocal, monkeypatch):
    """Written after update_settings' commit, a failing audit left the change
    saved behind a 500 with no trail. Staged before it, both roll back."""
    from gdx_dispatch.modules.forecasting.models import ForecastSettings

    def _refuse(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(forecasting_mod, "log_audit_event_sync", _refuse)
    client = _client(SessionLocal, forecasting_mod.router, OFFICE)
    with pytest.raises(RuntimeError):
        client.put("/api/forecast/settings", json={"collect_rate_0_30": 0.42})
    with SessionLocal() as db:
        rates = [s.collect_rate_0_30 for s in db.execute(select(ForecastSettings)).scalars()]
    assert all(float(r) != 0.42 for r in rates if r is not None)


# ── QB schedule: actor_id= / metadata= were dropped by the writer ────────────


def test_qb_schedule_change_leaves_a_row_with_actor_and_details(SessionLocal):
    client = _client(SessionLocal, qb_router_mod.router, OFFICE)
    r = client.put("/api/qb/schedule", json={"frequency": "daily"})
    assert r.status_code == 200, r.text
    rows = _audit_rows(SessionLocal, entity_type="qb_sync_schedule")
    assert [r.action for r in rows] == ["qb.schedule.update"]
    assert rows[0].user_id == OFFICE["user_id"]
    assert rows[0].details["frequency"] == "daily"


def test_a_refused_qb_frequency_records_nothing(SessionLocal):
    client = _client(SessionLocal, qb_router_mod.router, OFFICE)
    assert client.put("/api/qb/schedule", json={"frequency": "hourlyish"}).status_code == 422
    assert _audit_rows(SessionLocal, entity_type="qb_sync_schedule") == []


# ── integrations: a credential store, now atomic with its row ────────────────


def test_connecting_an_integration_leaves_a_row_naming_user_and_tenant(SessionLocal):
    client = _client(SessionLocal, integrations_mod.router, OFFICE)
    r = client.post("/api/integrations/stripe/connect", json={"credentials": {"api_key": "sk_test_700"}})
    assert r.status_code == 201, r.text
    rows = _audit_rows(SessionLocal, entity_type="integration_config", entity_id=r.json()["id"])
    assert [r.action for r in rows] == ["integration.connected"]
    # The positional call put the tenant id in the actor slot and no tenant.
    assert rows[0].user_id == OFFICE["user_id"]
    assert rows[0].tenant_id == TENANT


def test_a_failed_audit_write_stores_no_credential(SessionLocal, monkeypatch):
    async def _refuse(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(integrations_mod, "log_audit_event", _refuse)
    client = _client(SessionLocal, integrations_mod.router, OFFICE)
    with pytest.raises(RuntimeError):
        client.post("/api/integrations/stripe/connect", json={"credentials": {"api_key": "sk_test_701"}})
    with SessionLocal() as db:
        assert db.execute(select(integrations_mod.IntegrationConfig)).scalars().all() == []
