"""GDXA-981 — technician writes need a user and users.write; nothing runs as "system".

``create_technician`` took its user from a dependency that returned ``{}`` when
no bearer token was sent, discarded it, and wrote the audit row as
``"system"``: an anonymous ``POST /api/technicians`` returned 201. The router's
only other gate was ``require_module("dispatch")``, a module switch.

These tests run the real router behind the real ``get_current_user`` and
``require_permission``: tokens are minted with ``_issue`` and the users they
name are real rows in an in-memory SQLite database built from the ORM. Only
the database and the module switch are substituted. The technician is a real,
active user, so its refusal comes from the permission gate and not from a
failed login, and the admin's success proves the gate is not refusing
everyone.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Technician, User
from gdx_dispatch.routers import technicians as technicians_mod
from gdx_dispatch.routers.auth.core import _issue

TENANT = "98198198-1981-4981-8981-981981981981"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("DENYLIST_BACKEND_MODE", "memory")
    engine = create_engine(
        "sqlite://", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    admin_id, tech_user_id = uuid4(), uuid4()
    existing_id = str(uuid4())
    now = datetime.now(UTC)
    with SessionLocal() as db:
        db.add(User(id=admin_id, username="Pat Admin", email="pat.admin@example.com",
                    role="admin", company_id=TENANT, active=True, password_hash="x"))
        db.add(User(id=tech_user_id, username="Terry Tech", email="terry.tech@example.com",
                    role="technician", company_id=TENANT, active=True, password_hash="x"))
        db.add(Technician(id=existing_id, company_id=TENANT, user_id=str(tech_user_id),
                          name="Terry Tech", skills="[]", hourly_rate=40.0, active=True,
                          created_at=now, updated_at=now))
        db.commit()

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(technicians_mod.router)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_module("dispatch")] = lambda: None

    def _hdr(sub, role):
        tok, _ = _issue(str(sub), TENANT, role, 300, "access")
        return {"Authorization": f"Bearer {tok}"}

    yield {
        "client": TestClient(app),
        "db": SessionLocal,
        "existing_id": existing_id,
        "admin_id": str(admin_id),
        "admin": _hdr(admin_id, "admin"),
        "tech": _hdr(tech_user_id, "technician"),
    }
    app.dependency_overrides.clear()
    engine.dispose()


def _writes(tech_id: str) -> list[tuple[str, str, dict]]:
    return [
        ("POST", "/api/technicians", {"user_id": "u1", "name": "Intruder"}),
        ("PATCH", f"/api/technicians/{tech_id}", {"hourly_rate": 999.0}),
        ("POST", f"/api/technicians/{tech_id}/skills", {"skill": "injected"}),
        ("DELETE", f"/api/technicians/{tech_id}", {}),
    ]


def _state(SessionLocal) -> tuple:
    """Everything a refused write could have changed."""
    with SessionLocal() as db:
        techs = sorted(
            (t.id, t.name, t.skills, float(t.hourly_rate or 0), t.deleted_at is None)
            for t in db.execute(select(Technician)).scalars()
        )
        audits = db.execute(select(AuditLog)).scalars().all()
        return techs, len(audits)


@pytest.mark.parametrize("index", range(4), ids=["create", "patch", "skill", "delete"])
def test_a_write_with_no_token_is_401_and_changes_nothing(env, index):
    method, path, body = _writes(env["existing_id"])[index]
    before = _state(env["db"])
    resp = env["client"].request(method, path, json=body)
    assert resp.status_code == 401, (method, path, resp.status_code, resp.text)
    assert _state(env["db"]) == before


@pytest.mark.parametrize("index", range(4), ids=["create", "patch", "skill", "delete"])
def test_a_write_by_a_technician_is_403_and_changes_nothing(env, index):
    method, path, body = _writes(env["existing_id"])[index]
    before = _state(env["db"])
    resp = env["client"].request(method, path, json=body, headers=env["tech"])
    assert resp.status_code == 403, (method, path, resp.status_code, resp.text)
    assert "users.write" in resp.text
    assert _state(env["db"]) == before


def test_an_admin_can_write_and_every_audit_row_names_the_admin(env):
    client, hdr = env["client"], env["admin"]
    # GDXA-1583: the new row links to a real login; the admin's own is one.
    created = client.post("/api/technicians", json={"user_id": env["admin_id"], "name": "New Tech"}, headers=hdr)
    assert created.status_code == 201, created.text
    tech_id = created.json()["id"]
    expected = [201, 200, 200, 200]
    for (method, path, body), code in zip(_writes(tech_id)[1:], expected[1:], strict=True):
        resp = client.request(method, path, json=body, headers=hdr)
        assert resp.status_code == code, (method, path, resp.status_code, resp.text)

    with env["db"]() as db:
        rows = db.execute(select(AuditLog.action, AuditLog.user_id)).all()
    actions = {a for a, _ in rows}
    assert {"technician_created", "technician_updated", "add_technician_skill",
            "delete_technician"} <= actions
    assert {str(u) for _, u in rows} == {env["admin_id"]}, rows


@pytest.mark.parametrize("path", ["/api/technicians", "/api/technicians/skills", "/api/technicians/{id}",
                                  "/api/technicians/{id}/skills"])
def test_a_read_needs_a_user_and_any_user_may_read(env, path):
    path = path.replace("{id}", env["existing_id"])
    assert env["client"].get(path).status_code == 401
    # Mobile and dispatch screens list technicians for the field tier.
    assert env["client"].get(path, headers=env["tech"]).status_code == 200


def test_the_duplicate_unavailability_routes_are_gone(env):
    """GDXA-1828: time off is one record, `TimeOffRequest`. The technicians
    router's own unavailability read and write are removed, not re-gated."""
    tid = env["existing_id"]
    client, hdr = env["client"], env["admin"]
    assert client.get(f"/api/technicians/{tid}/availability", headers=hdr).status_code in (404, 405)
    resp = client.post(f"/api/technicians/{tid}/unavailability", headers=hdr,
                       json={"start_at": "2026-10-20T08:00:00Z", "end_at": "2026-10-20T17:00:00Z"})
    assert resp.status_code in (404, 405)
