"""GDXA-1808: POST /api/next-actions validates its body at the boundary.

``CreateActionRequest`` had no bound on any field. ``action_url`` went straight
into ``NextAction.action_url`` (``String(500)``) and from there into the
dashboard's ``router.push``: a 5000-character value failed the commit on
Postgres and came back as a bare 500, and ``https://…`` or ``//host`` was
stored as a click target. The validator checks the SHAPE of the path only:
an unknown in-app path such as ``/no-such-page`` is still accepted. Neither
create, complete nor snooze wrote an audit row.

Every request below goes through the real route, a real SQLite session and
the real audit chain; nothing is mocked except the login principal and, in one
test, the audit writer it proves the insert depends on.
"""
from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from annotated_types import MaxLen
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Imported before create_all so their models are on TenantBase.metadata.
from gdx_dispatch.core import ai_quote as ai_quote_mod
from gdx_dispatch.core import next_action as next_action_mod
from gdx_dispatch.core import next_action_routes as routes_mod
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.next_action import NextAction

TENANT = "11111111-1111-1111-1111-111111111808"
OFFICE = {"user_id": "00000000-0000-0000-0000-000000001809", "tenant_id": TENANT, "role": "admin"}


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
            db.close()

    app.dependency_overrides[get_db] = _like_get_db
    app.dependency_overrides[get_current_user] = lambda: user

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def _rows(SessionLocal) -> list[NextAction]:
    with SessionLocal() as db:
        return list(db.execute(select(NextAction)).scalars())


def _audits(SessionLocal) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.execute(select(AuditLog).where(AuditLog.entity_type == "next_action")).scalars())


_BASE = {"action_type": "follow_up", "title": "Call about the spring"}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        "javascript:alert(1)",
        "//evil.example/x",
        "/\\evil.example/x",
        "jobs/1",
        "",
        "/jobs/1 ",
        "/jobs/\n1",
        "/" + "a" * 500,
    ],
)
def test_an_action_url_that_is_not_an_in_app_path_is_a_422_and_writes_nothing(SessionLocal, url):
    r = _client(SessionLocal, routes_mod.router, OFFICE).post("/api/next-actions", json={**_BASE, "action_url": url})
    assert r.status_code == 422, r.text
    assert any(e["loc"][-1] == "action_url" for e in r.json()["detail"]), r.text
    assert _rows(SessionLocal) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_type", "x" * 51),
        ("title", "x" * 201),
        ("priority", "x" * 11),
        ("reference_id", "x" * 101),
        ("user_id", "x" * 101),
        ("action_type", ""),
        ("title", ""),
    ],
)
def test_a_field_its_column_cannot_hold_is_a_422_not_a_500(SessionLocal, field, value):
    r = _client(SessionLocal, routes_mod.router, OFFICE).post("/api/next-actions", json={**_BASE, field: value})
    assert r.status_code == 422, r.text
    assert any(e["loc"][-1] == field for e in r.json()["detail"]), r.text
    assert _rows(SessionLocal) == []


@pytest.mark.parametrize(
    "field", ["action_type", "title", "description", "priority", "action_url", "reference_id", "user_id"],
)
def test_a_nul_byte_is_a_422(SessionLocal, field):
    """Postgres refuses NUL at flush (a bare 500); SQLite stores it, so the
    model half is the part that can fail here."""
    value = "/a\x00b" if field == "action_url" else "a\x00b"
    with pytest.raises(ValidationError):
        routes_mod.CreateActionRequest(**{**_BASE, field: value})
    r = _client(SessionLocal, routes_mod.router, OFFICE).post("/api/next-actions", json={**_BASE, field: value})
    assert r.status_code == 422, r.text
    assert _rows(SessionLocal) == []


def test_complete_and_snooze_are_audited_with_the_actor(SessionLocal):
    client = _client(SessionLocal, routes_mod.router, OFFICE)
    first = client.post("/api/next-actions", json={**_BASE, "title": "one"}).json()["id"]
    second = client.post("/api/next-actions", json={**_BASE, "title": "two"}).json()["id"]
    assert client.post(f"/api/next-actions/{first}/complete").status_code == 200
    snoozed = client.post(f"/api/next-actions/{second}/snooze", json={"until": "2099-01-01T00:00:00+00:00"})
    assert snoozed.status_code == 200, snoozed.text
    by_action = {a.action: a for a in _audits(SessionLocal)}
    assert set(by_action) == {"next_action.create", "next_action.complete", "next_action.snooze"}
    assert by_action["next_action.complete"].entity_id == first
    assert by_action["next_action.complete"].details == {"from": "pending", "to": "completed"}
    assert by_action["next_action.snooze"].entity_id == second
    assert by_action["next_action.snooze"].details["to"] == "snoozed"
    for audit in by_action.values():
        assert audit.user_id == OFFICE["user_id"]


def test_an_unknown_key_is_refused_rather_than_dropped(SessionLocal):
    r = _client(SessionLocal, routes_mod.router, OFFICE).post("/api/next-actions", json={**_BASE, "url": "/jobs/1"})
    assert r.status_code == 422, r.text
    assert _rows(SessionLocal) == []


def test_a_nan_estimated_value_is_refused_before_anything_is_written(SessionLocal):
    """Python's json accepts a bare NaN. Unbounded, the row was stored and the
    response encoder then refused it. Now the validator refuses it first, but
    the status is still 500, not 422: FastAPI's default validation handler
    echoes the NaN input and cannot encode it either (reproduced on a bare
    FastAPI app; the app installs no handler of its own). Pinned here so
    the day an app-wide handler fixes that, this test is tightened to 422.

    SQLite turns NaN into NULL and the NOT NULL column refuses it anyway, so
    the HTTP half cannot fail for a missing bound; the model half can."""
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            routes_mod.CreateActionRequest(**_BASE, estimated_value=bad)
    r = _client(SessionLocal, routes_mod.router, OFFICE).post(
        "/api/next-actions",
        content='{"action_type": "follow_up", "title": "t", "estimated_value": NaN}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code in (422, 500), r.text
    assert _rows(SessionLocal) == []
    assert _audits(SessionLocal) == []


def test_every_bound_matches_the_column_it_lands_in():
    """The bounds are only right while they equal the columns; a widened or
    narrowed column with an unchanged schema reopens the 500."""
    for name in ("action_type", "title", "priority", "action_url", "reference_id", "user_id"):
        field = routes_mod.CreateActionRequest.model_fields[name]
        bound = next(m.max_length for m in field.metadata if isinstance(m, MaxLen))
        assert bound == NextAction.__table__.c[name].type.length, name
    job_type = ai_quote_mod.LearnFromJobRequest.model_fields["job_type"]
    bound = next(m.max_length for m in job_type.metadata if isinstance(m, MaxLen))
    assert bound == ai_quote_mod.QuoteTemplate.__table__.c["job_type"].type.length


@pytest.mark.parametrize("url", ["/jobs/1", "/jobs/1?tab=notes", "/" + "a" * 499, None])
def test_a_valid_action_is_stored_and_audited_together(SessionLocal, url):
    body = {**_BASE, "action_url": url, "priority": "high", "reference_id": "job-1"}
    r = _client(SessionLocal, routes_mod.router, OFFICE).post("/api/next-actions", json=body)
    assert r.status_code == 200, r.text
    (row,) = _rows(SessionLocal)
    assert row.action_url == url
    assert r.json()["id"] == str(row.id)
    (audit,) = _audits(SessionLocal)
    assert audit.action == "next_action.create"
    assert UUID(audit.entity_id) == row.id
    assert audit.user_id == OFFICE["user_id"]
    assert audit.details["action_url"] == url
    assert audit.row_hash


def test_an_action_whose_audit_fails_is_not_stored(SessionLocal, monkeypatch):
    """The audit row rides the same transaction: no trail, no action."""
    def _boom(*_a, **_k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(next_action_mod, "log_audit_event_sync", _boom)
    r = _client(SessionLocal, routes_mod.router, OFFICE).post("/api/next-actions", json={**_BASE, "action_url": "/jobs/1"})
    assert r.status_code == 500, r.text
    assert _rows(SessionLocal) == []


def test_learn_from_job_refuses_a_job_type_its_column_cannot_hold(SessionLocal):
    client = _client(SessionLocal, ai_quote_mod.router, OFFICE)
    body = {"parts_cost": 10, "labor_hours": 1, "final_price": 200}
    r = client.post("/api/ai/learn-from-job", json={**body, "job_type": "x" * 101})
    assert r.status_code == 422, r.text
    ok = client.post("/api/ai/learn-from-job", json={**body, "job_type": "x" * 100})
    assert ok.status_code == 200, ok.text
