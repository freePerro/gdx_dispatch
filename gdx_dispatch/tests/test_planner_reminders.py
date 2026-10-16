"""Planner reminders, the backend half of the GDX Planner app.

A task's ``remind_at`` is set through the planner API, stamped with whoever
set it, and the per-minute task turns a due one into exactly one bell row for
that person, in the text timestamp format the phone feed compares against.

HTTP tests go through a TestClient whose get_db closes without committing,
like core.database.get_db, so a change that does not commit itself is
invisible here.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.scheduler import build_beat_schedule
from gdx_dispatch.models.tenant_models import Notification, PlannerTask
from gdx_dispatch.routers import planner as planner_mod
from gdx_dispatch.tasks import planner_reminders as rem

TENANT = "11111111-1111-1111-1111-111111111900"
ME = "00000000-0000-0000-0000-000000000901"
OTHER = "00000000-0000-0000-0000-000000000902"
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def SessionLocal():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


@pytest.fixture
def who():
    return {"user_id": ME, "tenant_id": TENANT, "role": "admin"}


@pytest.fixture
def client(SessionLocal, who) -> TestClient:
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user

    app = FastAPI()
    app.include_router(planner_mod.router)

    def _like_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _like_get_db
    app.dependency_overrides[get_current_user] = lambda: who

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    return TestClient(app)


def _task(SessionLocal, title="Call back", **kw) -> str:
    fields = {"id": f"t-{title}", "company_id": TENANT, "title": title, "status": "todo",
              "priority": "low", "created_by": ME, "created_at": NOW - timedelta(days=1)}
    fields.update(kw)
    with SessionLocal() as db:
        db.add(PlannerTask(**fields))
        db.commit()
    return fields["id"]


def _get(SessionLocal, task_id) -> PlannerTask:
    with SessionLocal() as db:
        return db.get(PlannerTask, task_id)


def _bells(SessionLocal) -> list[Notification]:
    with SessionLocal() as db:
        return list(db.execute(select(Notification)).scalars())


def _actions(SessionLocal) -> list[str]:
    with SessionLocal() as db:
        return [a.action for a in db.execute(select(AuditLog).order_by(AuditLog.id)).scalars()]


# ── API ─────────────────────────────────────────────────────────────────────

def test_create_with_a_reminder_stamps_the_caller_and_returns_it(client, SessionLocal):
    r = client.post("/api/planner/tasks", json={"title": "Order springs",
                                                "remind_at": "2026-10-14T20:30:00Z",
                                                "assigned_to": OTHER})
    assert r.status_code == 201, r.text
    task = _get(SessionLocal, r.json()["id"])
    assert task.remind_user_id == ME  # "remind me", not the assignee
    listed = client.get("/api/planner/tasks", params={"view": "all"}).json()["items"]
    row = next(t for t in listed if t["id"] == task.id)
    assert row["remind_at"] == "2026-10-14T20:30:00+00:00"
    assert row["reminder_sent_at"] is None


@pytest.mark.parametrize("bad", ["2026-10-14", "tomorrow", "2026-13-01T10:00:00+00:00",
                                 "2026-10-14T09:00"])  # no offset: local or UTC?
def test_a_reminder_that_is_not_an_instant_is_refused(client, SessionLocal, bad):
    r = client.post("/api/planner/tasks", json={"title": "x", "remind_at": bad})
    assert r.status_code == 422
    with SessionLocal() as db:
        assert db.execute(select(PlannerTask)).first() is None


def test_moving_a_reminder_rearms_it_and_audits_the_move(client, SessionLocal, who):
    tid = _task(SessionLocal, remind_at=NOW, remind_user_id=OTHER, reminder_sent_at=NOW)
    r = client.patch(f"/api/planner/tasks/{tid}", json={"remind_at": "2026-10-15T09:00:00-05:00"})
    assert r.status_code == 200, r.text
    task = _get(SessionLocal, tid)
    assert task.reminder_sent_at is None
    assert task.remind_user_id == ME  # whoever moved it is who it reminds now
    stored = task.remind_at if task.remind_at.tzinfo else task.remind_at.replace(tzinfo=timezone.utc)
    assert stored == datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)
    assert "update_task" in _actions(SessionLocal)


def test_resaving_the_same_reminder_neither_takes_it_over_nor_fires_it_again(client, SessionLocal):
    tid = _task(SessionLocal, remind_at=NOW - timedelta(minutes=5), remind_user_id=OTHER,
                reminder_sent_at=NOW)
    r = client.patch(f"/api/planner/tasks/{tid}",
                     json={"title": "renamed", "remind_at": (NOW - timedelta(minutes=5)).isoformat()})
    assert r.status_code == 200, r.text
    task = _get(SessionLocal, tid)
    assert task.remind_user_id == OTHER
    assert task.reminder_sent_at is not None
    with SessionLocal() as db:
        assert rem.send_due(db, now=NOW)["sent"] == 0


def test_clearing_a_reminder_clears_all_three(client, SessionLocal):
    tid = _task(SessionLocal, remind_at=NOW, remind_user_id=ME, reminder_sent_at=NOW)
    r = client.patch(f"/api/planner/tasks/{tid}", json={"remind_at": None})
    assert r.status_code == 200, r.text
    task = _get(SessionLocal, tid)
    assert (task.remind_at, task.remind_user_id, task.reminder_sent_at) == (None, None, None)


def test_a_patch_without_remind_at_leaves_the_reminder_alone(client, SessionLocal):
    tid = _task(SessionLocal, remind_at=NOW, remind_user_id=OTHER)
    assert client.patch(f"/api/planner/tasks/{tid}", json={"title": "renamed"}).status_code == 200
    task = _get(SessionLocal, tid)
    assert task.remind_user_id == OTHER and task.remind_at is not None


# ── the per-minute task ─────────────────────────────────────────────────────

def test_a_due_reminder_writes_one_bell_for_whoever_set_it(SessionLocal):
    tid = _task(SessionLocal, title="Call Smith", assigned_to=OTHER, remind_at=NOW - timedelta(minutes=1),
                remind_user_id=ME, due_date=datetime(2026, 10, 16, tzinfo=timezone.utc))
    with SessionLocal() as db:
        assert rem.send_due(db, now=NOW) == {"due": 1, "sent": 1, "failed": 0}
    [bell] = _bells(SessionLocal)
    assert bell.user_id == ME
    assert bell.tenant_id == TENANT
    assert bell.category == "planner_reminder"
    assert bell.title == "Reminder: Call Smith"
    assert bell.message == "Due 2026-10-16"
    # The feed compares created_at as text against an isoformat() cursor.
    assert "T" in bell.created_at and bell.created_at.endswith("+00:00")
    assert _get(SessionLocal, tid).reminder_sent_at is not None
    assert "planner_reminder_sent" in _actions(SessionLocal)


def test_a_reminder_is_sent_once_across_runs(SessionLocal):
    _task(SessionLocal, remind_at=NOW - timedelta(minutes=5), remind_user_id=ME)
    with SessionLocal() as db:
        rem.send_due(db, now=NOW)
    with SessionLocal() as db:
        assert rem.send_due(db, now=NOW + timedelta(minutes=1))["sent"] == 0
    assert len(_bells(SessionLocal)) == 1


def test_an_overlapping_run_that_read_the_same_list_sends_nothing(SessionLocal):
    tid = _task(SessionLocal, remind_at=NOW - timedelta(minutes=1), remind_user_id=ME)
    with SessionLocal() as db:
        assert rem._send_one(db, tid, NOW) is True
    with SessionLocal() as db:
        assert rem._send_one(db, tid, NOW) is False  # the claim's guard, not the select
    assert len(_bells(SessionLocal)) == 1


def test_future_and_done_tasks_are_not_reminded(SessionLocal):
    _task(SessionLocal, title="later", remind_at=NOW + timedelta(minutes=1), remind_user_id=ME)
    _task(SessionLocal, title="done", status="done", remind_at=NOW - timedelta(hours=1), remind_user_id=ME)
    with SessionLocal() as db:
        assert rem.send_due(db, now=NOW)["sent"] == 0
    assert _bells(SessionLocal) == []


def test_a_reminder_with_no_setter_goes_to_the_creator(SessionLocal):
    _task(SessionLocal, created_by=OTHER, remind_at=NOW - timedelta(minutes=1))
    with SessionLocal() as db:
        rem.send_due(db, now=NOW)
    assert _bells(SessionLocal)[0].user_id == OTHER


def test_a_moved_reminder_fires_again(client, SessionLocal):
    tid = _task(SessionLocal, remind_at=NOW - timedelta(minutes=1), remind_user_id=ME)
    with SessionLocal() as db:
        rem.send_due(db, now=NOW)
    later = (NOW + timedelta(hours=1)).isoformat()
    assert client.patch(f"/api/planner/tasks/{tid}", json={"remind_at": later}).status_code == 200
    with SessionLocal() as db:
        assert rem.send_due(db, now=NOW + timedelta(hours=1, minutes=1))["sent"] == 1
    assert len(_bells(SessionLocal)) == 2


def test_the_beat_runs_it_every_minute_on_the_high_queue():
    entry = build_beat_schedule()["send-due-planner-reminders-every-minute"]
    assert entry["task"] == rem.send_due_planner_reminders.name
    assert entry["options"] == {"queue": "priority:high"}
