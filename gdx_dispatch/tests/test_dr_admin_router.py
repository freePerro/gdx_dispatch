"""SS-34 slice F tests — /api/admin/dr/drills router.

Post-Sprint-0.9-l: storage is DB-backed (dr_drill_run +
dr_verification_report). Fixtures set up an in-memory sqlite session
and inject it via ``request.state.db``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.dr.drill_orchestrator import DrillReport, reset_idempotency_cache
from gdx_dispatch.core.dr.restore_to_staging import ProductionTargetRefused
from gdx_dispatch.core.dr.verification_harness import (
    CheckResult,
    VerificationReport,
)
from gdx_dispatch.models.platform import DrDrillRun, DrVerificationReport
from gdx_dispatch.routers import dr_admin


@pytest.fixture
def db():
    """Fresh in-memory sqlite session with the DR tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only create the tables we need to avoid sqlite-incompatible DDL
    # from the full Base.metadata.
    DrDrillRun.__table__.create(engine)
    DrVerificationReport.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset():
    reset_idempotency_cache()
    yield
    reset_idempotency_cache()


def _app(db, *, role="super-admin", db_exec=None):
    app = FastAPI()

    @app.middleware("http")
    async def inject(request: Request, call_next):
        request.state.db = db
        if db_exec is not None:
            request.state.dr_db_exec = db_exec
        return await call_next(request)

    def _fake_principal():
        return SimpleNamespace(
            identity_id="admin-1",
            tenant_id="platform",
            principal_role=role,
            capabilities=(),
            is_super_admin=False,
        )

    app.dependency_overrides[get_current_principal] = _fake_principal
    app.include_router(dr_admin.router)
    return app


def _passing_report(scope="full", passed=True) -> DrillReport:
    vr = VerificationReport(run_started_at=datetime.now(timezone.utc))
    vr.checks.append(CheckResult(name="rowcount:identities", passed=passed, detail=""))
    vr.run_finished_at = datetime.now(timezone.utc)
    return DrillReport(
        drill_run_id=str(uuid4()),
        scheduled_for=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        scope=scope,
        passed=passed,
        failure_reason=None if passed else "verification: 1 failed checks",
        verification=vr,
    )


def _body():
    return {
        "scope": "full",
        "staging_db_url": "postgresql://u:p@staging/db",
        "source_db_url": "postgresql://u:p@src/db",
        "snapshot_target": "/tmp/s.pgc",
    }


def _fake_run(report_factory=_passing_report):
    def _inner(**kw):
        rep = report_factory()
        rep.drill_run_id = kw["drill_run_id"]
        return rep
    return _inner


def test_schedule_requires_super_admin(db):
    c = TestClient(_app(db, role="admin"))
    r = c.post("/api/admin/dr/drills", json=_body())
    assert r.status_code == 403


def test_schedule_happy_path_returns_report(db):
    c = TestClient(_app(db))
    with patch("gdx_dispatch.routers.dr_admin.run_drill", side_effect=_fake_run()):
        r = c.post("/api/admin/dr/drills", json=_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True
    assert body["scheduled_by_identity_id"] == "admin-1"


def test_schedule_bad_scope_400(db):
    c = TestClient(_app(db))
    body = _body()
    body["scope"] = "bogus"
    r = c.post("/api/admin/dr/drills", json=body)
    assert r.status_code == 400


def test_schedule_tenant_scope_requires_selector(db):
    c = TestClient(_app(db))
    body = _body()
    body["scope"] = "tenant"
    r = c.post("/api/admin/dr/drills", json=body)
    assert r.status_code == 400
    assert "scope_selector" in r.json()["detail"]


def test_schedule_verification_failure_returns_500(db):
    c = TestClient(_app(db))
    with patch(
        "gdx_dispatch.routers.dr_admin.run_drill",
        side_effect=_fake_run(lambda: _passing_report(passed=False)),
    ):
        r = c.post("/api/admin/dr/drills", json=_body())
    assert r.status_code == 500
    assert "verification failed" in r.json()["detail"]["message"]


def test_schedule_production_refused_returns_409(db):
    c = TestClient(_app(db))
    with patch(
        "gdx_dispatch.routers.dr_admin.run_drill",
        side_effect=ProductionTargetRefused("no."),
    ):
        r = c.post("/api/admin/dr/drills", json=_body())
    assert r.status_code == 409


def test_schedule_infra_failure_returns_500_and_persists_stub(db):
    c = TestClient(_app(db))
    with patch(
        "gdx_dispatch.routers.dr_admin.run_drill",
        side_effect=RuntimeError("pg_dump missing"),
    ):
        r = c.post("/api/admin/dr/drills", json=_body())
    assert r.status_code == 500
    # Stub should be in store so the list endpoint sees it.
    r = c.get("/api/admin/dr/drills")
    assert r.status_code == 200
    listing = r.json()
    assert listing["count"] == 1
    assert listing["drills"][0]["passed"] is False


def test_list_drills_empty(db):
    c = TestClient(_app(db))
    r = c.get("/api/admin/dr/drills")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "drills": []}


def test_list_drills_newest_first(db):
    c = TestClient(_app(db))
    reports = [_passing_report(), _passing_report()]
    report_ids = []

    def fake(**kw):
        report = reports.pop(0)
        # Override id to the caller-supplied value if present.
        report.drill_run_id = kw["drill_run_id"]
        # Space the scheduled_for timestamps so ORDER BY is stable.
        report.scheduled_for = kw["scheduled_for"]
        report_ids.append(kw["drill_run_id"])
        return report

    with patch("gdx_dispatch.routers.dr_admin.run_drill", side_effect=fake):
        # Post with explicit, ordered scheduled_for so the newest-first
        # contract is deterministic across the two inserts.
        b1 = _body()
        b1["scheduled_for"] = "2026-04-20T10:00:00+00:00"
        b2 = _body()
        b2["scheduled_for"] = "2026-04-20T11:00:00+00:00"
        c.post("/api/admin/dr/drills", json=b1)
        c.post("/api/admin/dr/drills", json=b2)
    r = c.get("/api/admin/dr/drills")
    assert r.status_code == 200
    listing = r.json()
    assert listing["count"] == 2
    # Newest first — last-scheduled id is first.
    assert listing["drills"][0]["drill_run_id"] == report_ids[-1]


def test_get_drill_404(db):
    c = TestClient(_app(db))
    r = c.get("/api/admin/dr/drills/does-not-exist")
    assert r.status_code == 404


def test_get_drill_returns_full_report(db):
    c = TestClient(_app(db))
    with patch(
        "gdx_dispatch.routers.dr_admin.run_drill",
        side_effect=_fake_run(),
    ):
        r = c.post("/api/admin/dr/drills", json=_body())
    drill_id = r.json()["drill_run_id"]
    r = c.get(f"/api/admin/dr/drills/{drill_id}")
    assert r.status_code == 200
    assert r.json()["drill_run_id"] == drill_id
    assert r.json()["verification"] is not None


def test_rerun_verification_requires_store_row(db):
    c = TestClient(_app(db, db_exec=lambda sql: []))
    r = c.post(
        "/api/admin/dr/drills/nope/rerun-verification",
        json={"staging_db_url": "postgresql://u@stg/db"},
    )
    assert r.status_code == 404


def test_rerun_verification_passes_with_populated_db_exec(db):
    # Minimal stub that satisfies every default check.
    def db_exec(sql: str):
        if "information_schema" in sql:
            return []
        if "pg_policies" in sql:
            return [(1,)]
        if "tenants WHERE slug" in sql:
            return [(1,)]
        if '"identities"' in sql:
            return [(1,)]
        if '"tenants"' in sql:
            return [(1,)]
        if '"customers"' in sql:
            return [(1,)]
        if '"jobs"' in sql:
            return [(1,)]
        if '"audit_logs"' in sql:
            return [(1,)]
        return []

    c = TestClient(_app(db, db_exec=db_exec))
    # Seed a drill first.
    with patch(
        "gdx_dispatch.routers.dr_admin.run_drill",
        side_effect=_fake_run(),
    ):
        drill_id = c.post("/api/admin/dr/drills", json=_body()).json()["drill_run_id"]

    r = c.post(
        f"/api/admin/dr/drills/{drill_id}/rerun-verification",
        json={"staging_db_url": "postgresql://u@stg/db"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["passed"] is True


def test_rerun_verification_fails_returns_500(db):
    # db_exec returns empty everywhere → rowcount checks fail (lo=1).
    c = TestClient(_app(db, db_exec=lambda sql: []))
    with patch(
        "gdx_dispatch.routers.dr_admin.run_drill",
        side_effect=_fake_run(),
    ):
        drill_id = c.post("/api/admin/dr/drills", json=_body()).json()["drill_run_id"]

    r = c.post(
        f"/api/admin/dr/drills/{drill_id}/rerun-verification",
        json={"staging_db_url": "postgresql://u@stg/db"},
    )
    assert r.status_code == 500
    # Store now reflects the failure.
    r = c.get(f"/api/admin/dr/drills/{drill_id}")
    assert r.json()["passed"] is False


def test_dr_drill_state_survives_mock_restart(db):
    """Integration test: schedule a drill, close + reopen the DB session,
    confirm the drill + verification report are still retrievable via
    the router's query path — proving state is in Postgres, not in
    process memory.
    """
    # ── Phase 1: schedule via app, using session ``db``.
    app1 = _app(db)
    c1 = TestClient(app1)
    with patch("gdx_dispatch.routers.dr_admin.run_drill", side_effect=_fake_run()):
        r = c1.post("/api/admin/dr/drills", json=_body())
    assert r.status_code == 200, r.text
    drill_id = r.json()["drill_run_id"]

    # Verify verification report was persisted as its own row.
    v_count_before = (
        db.query(DrVerificationReport)
        .filter(DrVerificationReport.drill_run_id == drill_id)
        .count()
    )
    assert v_count_before == 1

    # ── Phase 2: simulate a mock restart by closing the session and
    # opening a fresh one on the same engine. The ORM identity map is
    # dropped; only DB-persistent state can survive.
    engine = db.get_bind()
    db.close()

    from sqlalchemy.orm import sessionmaker
    fresh = sessionmaker(bind=engine)()

    # ── Phase 3: re-query via a NEW app instance bound to the new session.
    app2 = _app(fresh)
    c2 = TestClient(app2)

    # List: drill shows up.
    r = c2.get("/api/admin/dr/drills")
    assert r.status_code == 200
    listing = r.json()
    assert listing["count"] == 1
    assert listing["drills"][0]["drill_run_id"] == drill_id
    assert listing["drills"][0]["passed"] is True

    # Get: full report + verification retrievable.
    r = c2.get(f"/api/admin/dr/drills/{drill_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["drill_run_id"] == drill_id
    assert body["verification"] is not None
    # The verification payload should carry at least the one passing
    # rowcount check from _passing_report().
    assert body["passed"] is True

    # Verification-report row survived the session swap.
    v_count_after = (
        fresh.query(DrVerificationReport)
        .filter(DrVerificationReport.drill_run_id == drill_id)
        .count()
    )
    assert v_count_after == 1

    fresh.close()
