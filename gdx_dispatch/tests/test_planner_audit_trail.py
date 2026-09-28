"""Every planner mutation leaves an audit row — who, what changed, when
(invariant #1). Before 2026-09-28 only task create did: checking a task off,
editing, deleting, linking a customer, plans, plan steps, threads and
messages all committed with no trail.

The client's get_db closes without committing, exactly like
core.database.get_db, so a row that is not in the handler's own commit is a
row these tests cannot see. The last test pins the other half of "in the same
commit": when the audit write fails, the change does not land either.
"""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import MessageThreadMember, PlannerTask
from gdx_dispatch.routers import planner as planner_mod

TENANT = "11111111-1111-1111-1111-111111111900"
ME = {"user_id": "00000000-0000-0000-0000-000000000901", "tenant_id": TENANT, "role": "admin"}
OTHER = "00000000-0000-0000-0000-000000000902"


@pytest.fixture
def SessionLocal():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _client(SessionLocal, user: dict = ME) -> TestClient:
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user

    app = FastAPI()
    app.include_router(planner_mod.router)

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


@pytest.fixture
def client(SessionLocal) -> TestClient:
    return _client(SessionLocal)


def _rows(SessionLocal, action: str) -> list[AuditLog]:
    with SessionLocal() as db:
        q = select(AuditLog).where(AuditLog.action == action)
        return list(db.execute(q.order_by(AuditLog.created_at, AuditLog.id)).scalars())


def _one(SessionLocal, action: str) -> AuditLog:
    rows = _rows(SessionLocal, action)
    assert len(rows) == 1, f"{action}: expected one audit row, got {len(rows)}"
    row = rows[0]
    assert row.user_id == ME["user_id"]
    assert row.tenant_id == TENANT
    return row


def _new_task(client, **kw) -> str:
    r = client.post("/api/planner/tasks", json={"title": "Call back about the opener", **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── tasks ────────────────────────────────────────────────────────────────────


def test_checking_a_task_off_is_audited(client, SessionLocal):
    tid = _new_task(client)
    assert client.patch(f"/api/planner/tasks/{tid}", json={"status": "done"}).status_code == 200
    row = _one(SessionLocal, "update_task")
    assert row.entity_type == "planner_task" and row.entity_id == tid
    changed = row.details["changed"]
    assert changed["status"] == {"from": "todo", "to": "done"}
    assert changed["completed_at"]["from"] is None and changed["completed_at"]["to"]
    assert row.details["title"] == "Call back about the opener"


def test_re_saving_a_done_task_does_not_move_its_completion_time(client, SessionLocal):
    """The edit dialog re-sends status on every save (audit round 1)."""
    tid = _new_task(client)
    client.patch(f"/api/planner/tasks/{tid}", json={"status": "done"})
    with SessionLocal() as db:
        first = db.get(PlannerTask, tid).completed_at
    assert client.patch(f"/api/planner/tasks/{tid}", json={"status": "done", "title": "typo fixed"}).status_code == 200
    with SessionLocal() as db:
        assert db.get(PlannerTask, tid).completed_at == first
    second = _rows(SessionLocal, "update_task")[1]
    assert second.details["changed"] == {"title": {"from": "Call back about the opener", "to": "typo fixed"}}


def test_a_same_day_due_stamp_move_is_still_a_change(client, SessionLocal):
    tid = _new_task(client, due_date="2026-09-30")
    assert client.patch(f"/api/planner/tasks/{tid}", json={"due_date": "2026-09-30T20:00:00Z"}).status_code == 200
    changed = _one(SessionLocal, "update_task").details["changed"]
    assert changed == {"due_date": {"from": "2026-09-30", "to": "2026-09-30T20:00:00+00:00"}}


def test_an_edit_records_each_changed_field_from_and_to(client, SessionLocal):
    tid = _new_task(client, due_date="2026-09-30")
    r = client.patch(f"/api/planner/tasks/{tid}", json={
        "title": "Call back — opener reverses", "priority": "high",
        "due_date": None, "assigned_to": OTHER,
        "description": "",  # unchanged: sent with its current value
    })
    assert r.status_code == 200, r.text
    changed = _one(SessionLocal, "update_task").details["changed"]
    assert changed == {
        "title": {"from": "Call back about the opener", "to": "Call back — opener reverses"},
        "priority": {"from": "low", "to": "high"},
        "due_date": {"from": "2026-09-30", "to": None},
        "assigned_to": {"from": None, "to": OTHER},
    }


def test_a_patch_that_changes_nothing_records_nothing(client, SessionLocal):
    tid = _new_task(client)
    assert client.patch(f"/api/planner/tasks/{tid}", json={"status": "todo", "priority": "low"}).status_code == 200
    assert _rows(SessionLocal, "update_task") == []


def test_a_deleted_task_is_reconstructable_from_its_audit_row(client, SessionLocal):
    tid = _new_task(client, description="Wants two openers quoted", due_date="2026-10-01")
    assert client.delete(f"/api/planner/tasks/{tid}").status_code == 200
    with SessionLocal() as db:
        assert db.get(PlannerTask, tid) is None
    snapshot = _one(SessionLocal, "delete_task").details["task"]
    assert snapshot["id"] == tid
    assert snapshot["title"] == "Call back about the opener"
    assert snapshot["description"] == "Wants two openers quoted"
    assert snapshot["due_date"] == "2026-10-01"
    assert snapshot["created_by"] == ME["user_id"]


def test_a_deleted_task_keeps_a_due_time_that_is_not_midnight(client, SessionLocal):
    tid = _new_task(client, due_date="2026-09-30T20:00:00Z")
    client.delete(f"/api/planner/tasks/{tid}")
    assert _one(SessionLocal, "delete_task").details["task"]["due_date"] == "2026-09-30T20:00:00+00:00"


def test_deleting_a_task_that_is_not_there_records_nothing(client, SessionLocal):
    assert client.delete("/api/planner/tasks/nope").status_code == 200
    assert _rows(SessionLocal, "delete_task") == []


def test_linking_a_customer_is_audited_from_and_to(client, SessionLocal):
    tid = _new_task(client)
    cid = "33333333-3333-3333-3333-333333333333"
    r = client.post(f"/api/planner/tasks/{tid}/link-customer", json={"customer_id": cid})
    assert r.status_code == 200, r.text
    details = _one(SessionLocal, "link_customer").details
    assert details["customer_id"] == {"from": None, "to": cid}
    # A task with no captured call or phone never reaches the call backfill, so
    # nothing is listed; the path that stamps calls is not exercised here.
    assert details["calls_backfilled"] == []


# ── plans ────────────────────────────────────────────────────────────────────


def test_creating_a_plan_and_moving_a_step_are_audited(client, SessionLocal):
    r = client.post("/api/planner/plans", json={
        "title": "Spring install checklist",
        "steps": [{"title": "Order springs"}, {"title": "Book the lift", "assigned_to": OTHER}],
    })
    assert r.status_code == 201, r.text
    plan_id = r.json()["id"]
    created = _one(SessionLocal, "create_plan")
    assert created.entity_id == plan_id
    assert [s["title"] for s in created.details["steps"]] == ["Order springs", "Book the lift"]

    step_id = client.get(f"/api/planner/plans/{plan_id}").json()["steps"][0]["id"]
    assert client.patch(f"/api/planner/plans/steps/{step_id}?status=done").status_code == 200
    moved = _one(SessionLocal, "update_plan_step")
    assert moved.entity_id == step_id
    assert moved.details == {"plan_id": plan_id, "title": "Order springs",
                             "status": {"from": "todo", "to": "done"}}
    # the same status again is not a change
    assert client.patch(f"/api/planner/plans/steps/{step_id}?status=done").status_code == 200
    assert len(_rows(SessionLocal, "update_plan_step")) == 1


# ── messaging ────────────────────────────────────────────────────────────────


def test_a_thread_and_a_message_are_audited(client, SessionLocal):
    r = client.post("/api/planner/threads", json={"name": "Install crew", "type": "group", "members": [OTHER]})
    assert r.status_code == 201, r.text
    thread_id = r.json()["id"]
    thread = _one(SessionLocal, "create_thread")
    assert thread.details == {"type": "group", "name": "Install crew",
                              "members": sorted([ME["user_id"], OTHER])}

    m = client.post(f"/api/planner/threads/{thread_id}/messages", json={"body": "Springs are in"})
    assert m.status_code == 201, m.text
    sent = _one(SessionLocal, "send_message")
    assert sent.entity_id == m.json()["id"]
    assert sent.details == {"thread_id": thread_id, "length": len("Springs are in"),
                            "job_id": None, "customer_id": None}


def test_opening_a_thread_you_were_not_in_records_the_join_but_not_the_read(SessionLocal):
    owner = _client(SessionLocal)
    thread_id = owner.post("/api/planner/threads", json={"name": "Office"}).json()["id"]
    outsider = _client(SessionLocal, {**ME, "user_id": OTHER})

    assert outsider.get(f"/api/planner/threads/{thread_id}/messages").status_code == 200
    joins = _rows(SessionLocal, "join_thread")
    assert [(j.user_id, j.entity_id, j.details) for j in joins] == [
        (OTHER, thread_id, {"via": "opened_thread"})
    ]
    # a second open is only a read receipt — no new row
    assert outsider.get(f"/api/planner/threads/{thread_id}/messages").status_code == 200
    assert owner.get(f"/api/planner/threads/{thread_id}/messages").status_code == 200
    assert len(_rows(SessionLocal, "join_thread")) == 1
    with SessionLocal() as db:
        members = db.execute(
            select(MessageThreadMember.user_id).where(MessageThreadMember.thread_id == thread_id)
        ).scalars().all()
    assert OTHER in members


# ── the trail and the change commit together ────────────────────────────────


def test_when_the_audit_write_fails_the_check_off_does_not_land(client, SessionLocal, monkeypatch):
    tid = _new_task(client)

    def broken(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(planner_mod, "log_audit_event_sync", broken)
    with pytest.raises(RuntimeError, match="audit store down"):
        client.patch(f"/api/planner/tasks/{tid}", json={"status": "done"})
    with SessionLocal() as db:
        assert db.get(PlannerTask, tid).status == "todo"


def test_every_mutation_initialises_the_audit_table_before_staging(SessionLocal, monkeypatch):
    """ensure_audit_table COMMITS on its first run for an engine; called after a
    change is staged it would harden that change before its audit row exists
    (audit round 1: nothing tested this). The spy fails any call made with
    work already pending, and every route must make at least one call."""
    from gdx_dispatch.routers import planner as mod

    real = mod.ensure_audit_table
    calls: list[str] = []

    def spy(db):
        pending = [type(o).__name__ for o in list(db.new) + list(db.dirty) + list(db.deleted)]
        assert not pending, f"ensure_audit_table called with staged work: {pending}"
        calls.append("x")
        return real(db)

    from gdx_dispatch.services import planner_today as today_svc

    monkeypatch.setattr(mod, "ensure_audit_table", spy)
    monkeypatch.setattr(today_svc, "ensure_audit_table", spy)  # the Today routes stage in the service
    c = _client(SessionLocal)
    other = _client(SessionLocal, {**ME, "user_id": OTHER})

    def did(label, fn):
        before = len(calls)
        r = fn()
        assert r.status_code < 300, (label, r.status_code, r.text)
        assert len(calls) > before, f"{label}: never initialised the audit table"
        return r

    tid = did("create task", lambda: c.post("/api/planner/tasks", json={"title": "t"})).json()["id"]
    did("pin to Today", lambda: c.put(f"/api/planner/tasks/{tid}/today", json={"on": True}))
    note_day = c.get("/api/planner/today").json()["note"]["date"]
    did("save the note", lambda: c.put("/api/planner/today/note", json={"body": "n", "note_date": note_day}))
    did("check off", lambda: c.patch(f"/api/planner/tasks/{tid}", json={"status": "done"}))
    did("link customer", lambda: c.post(f"/api/planner/tasks/{tid}/link-customer",
                                        json={"customer_id": "33333333-3333-3333-3333-333333333333"}))
    did("delete", lambda: c.delete(f"/api/planner/tasks/{tid}"))
    plan_id = did("create plan", lambda: c.post("/api/planner/plans",
                                                json={"title": "p", "steps": [{"title": "s"}]})).json()["id"]
    step_id = c.get(f"/api/planner/plans/{plan_id}").json()["steps"][0]["id"]
    did("move step", lambda: c.patch(f"/api/planner/plans/steps/{step_id}?status=done"))
    thread_id = did("create thread", lambda: c.post("/api/planner/threads", json={"name": "n"})).json()["id"]
    did("send message", lambda: c.post(f"/api/planner/threads/{thread_id}/messages", json={"body": "b"}))
    did("join on open", lambda: other.get(f"/api/planner/threads/{thread_id}/messages"))


def test_every_planner_audit_row_carries_tenant_and_request(client, SessionLocal):
    """The activity feed filters on tenant_id, and ip/request_id tie a row to
    its request — create_task and the Today routes used to write neither."""
    tid = _new_task(client)
    client.patch(f"/api/planner/tasks/{tid}", json={"status": "done"})
    client.put(f"/api/planner/tasks/{tid}/today", json={"on": True})
    note = client.get("/api/planner/today").json()["note"]
    client.put("/api/planner/today/note", json={"body": "x", "note_date": note["date"]})
    with SessionLocal() as db:
        rows = db.execute(select(AuditLog)).scalars().all()
    assert {r.action for r in rows} >= {"create_task", "update_task", "planner_today_add",
                                        "planner_day_note_save"}
    for r in rows:
        assert r.tenant_id == TENANT, r.action
        assert r.ip_address, r.action  # TestClient's peer — None when the request was not passed
