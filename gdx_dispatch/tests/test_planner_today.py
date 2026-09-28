"""Planner "Today" tab (the 2026-09-28 planner Today plan): tasks pinned to
today, suggestions after the day turns over, the per-day note with its
conflict check, and an audit row for every change.

HTTP tests go through a real TestClient whose get_db closes without
committing, exactly like core.database.get_db — so a change that does not
commit itself is a change these tests cannot see. Day-rollover cases call the
service with an explicit ``today``.
"""
from __future__ import annotations

import os
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import PlannerTask
from gdx_dispatch.routers import planner as planner_mod
from gdx_dispatch.services import planner_today as svc

TENANT = "11111111-1111-1111-1111-111111111800"
ME = {"user_id": "00000000-0000-0000-0000-000000000801", "tenant_id": TENANT, "role": "admin"}
OTHER = "00000000-0000-0000-0000-000000000802"
D = datetime(2026, 9, 28, tzinfo=timezone.utc)  # a business day, D@00:00 UTC


@pytest.fixture
def SessionLocal():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


@pytest.fixture
def client(SessionLocal) -> TestClient:
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
    app.dependency_overrides[get_current_user] = lambda: ME

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    return TestClient(app)


def _audit(SessionLocal, entity_type: str) -> list[AuditLog]:
    with SessionLocal() as db:
        q = select(AuditLog).where(AuditLog.entity_type == entity_type)
        return list(db.execute(q.order_by(AuditLog.created_at, AuditLog.id)).scalars())


def _task(SessionLocal, **kw) -> str:
    fields = {"id": kw.pop("id", None) or f"t-{kw['title']}", "company_id": TENANT,
              "status": "todo", "priority": "low", "created_by": ME["user_id"],
              "created_at": D - timedelta(days=30)}
    fields.update(kw)
    with SessionLocal() as db:
        db.add(PlannerTask(**fields))
        db.commit()
    return fields["id"]


# ── pinning ──────────────────────────────────────────────────────────────────


def test_pin_puts_the_task_on_today_and_leaves_one_audit_row(client, SessionLocal):
    tid = client.post("/api/planner/tasks", json={"title": "Call the spring supplier"}).json()["id"]
    r = client.put(f"/api/planner/tasks/{tid}/today", json={"on": True})
    assert r.status_code == 200, r.text
    assert r.json()["on_today"] is True

    today = client.get("/api/planner/today").json()
    assert [t["id"] for t in today["tasks"]] == [tid]
    assert today["date"] == svc.calendar_today_utc().date().isoformat()
    listed = client.get("/api/planner/tasks").json()["items"]
    assert listed[0]["on_today"] is True

    # pinning again changes nothing and records nothing
    assert client.put(f"/api/planner/tasks/{tid}/today", json={"on": True}).status_code == 200
    rows = [r for r in _audit(SessionLocal, "planner_task") if r.action.startswith("planner_today")]
    assert [r.action for r in rows] == ["planner_today_add"]
    assert rows[0].user_id == ME["user_id"]
    assert rows[0].tenant_id == TENANT
    assert rows[0].details["via"] == "user"


def test_unpin_takes_it_off_and_is_audited(client, SessionLocal):
    tid = client.post("/api/planner/tasks", json={"title": "Quote Smith", "today": True}).json()["id"]
    assert [t["id"] for t in client.get("/api/planner/today").json()["tasks"]] == [tid]
    assert client.put(f"/api/planner/tasks/{tid}/today", json={"on": False}).json()["on_today"] is False
    assert client.get("/api/planner/today").json()["tasks"] == []
    actions = [r.action for r in _audit(SessionLocal, "planner_task")]
    assert actions == ["create_task", "planner_today_remove"]
    create = _audit(SessionLocal, "planner_task")[0]
    assert create.details["today"] is True


def test_someone_elses_task_cannot_be_pinned(client, SessionLocal):
    tid = _task(SessionLocal, title="theirs", created_by=OTHER)
    delegated = _task(SessionLocal, title="delegated", assigned_to=OTHER)
    for t in (tid, delegated):
        r = client.put(f"/api/planner/tasks/{t}/today", json={"on": True})
        assert r.status_code == 403, r.text
    assert client.put("/api/planner/tasks/nope/today", json={"on": True}).status_code == 404
    assert _audit(SessionLocal, "planner_task") == []


def test_a_task_assigned_to_me_by_someone_else_can_be_pinned(client, SessionLocal):
    tid = _task(SessionLocal, title="handed to me", created_by=OTHER, assigned_to=ME["user_id"])
    assert client.put(f"/api/planner/tasks/{tid}/today", json={"on": True}).status_code == 200


def test_http_callers_cannot_mark_a_task_as_the_ais(client):
    with pytest.raises(ValidationError):
        planner_mod.TaskIn(title="x", source="ai")
    r = client.post("/api/planner/tasks", json={"title": "x", "source": "ai"})
    assert r.status_code == 422


# ── the day turns over ───────────────────────────────────────────────────────


def test_tomorrow_the_pin_is_a_suggestion_and_after_a_week_it_is_gone(SessionLocal):
    tid = _task(SessionLocal, title="pinned Friday", today_date=D)
    done = _task(SessionLocal, title="finished Friday", today_date=D, status="done")
    with SessionLocal() as db:
        same_day = svc.today_view(db, ME["user_id"], today=D)
        assert [t["id"] for t in same_day["tasks"]] == [tid, done]  # done sorts last
        assert same_day["carried"] == []

        monday = svc.today_view(db, ME["user_id"], today=D + timedelta(days=3))
        assert monday["tasks"] == []
        assert [t["id"] for t in monday["carried"]] == [tid]  # the finished one is not offered

        week_on = svc.today_view(db, ME["user_id"], today=D + timedelta(days=svc.CARRY_DAYS))
        assert [t["id"] for t in week_on["carried"]] == [tid]
        later = svc.today_view(db, ME["user_id"], today=D + timedelta(days=svc.CARRY_DAYS + 1))
        assert later["carried"] == []


def test_due_suggestions_skip_pinned_carried_done_and_future(SessionLocal):
    overdue = _task(SessionLocal, title="overdue", due_date=D - timedelta(days=3))
    due_today = _task(SessionLocal, title="due today", due_date=D)
    _task(SessionLocal, title="tomorrow", due_date=D + timedelta(days=1))
    _task(SessionLocal, title="done", due_date=D, status="done")
    _task(SessionLocal, title="pinned", due_date=D, today_date=D)
    _task(SessionLocal, title="carried", due_date=D, today_date=D - timedelta(days=1))
    _task(SessionLocal, title="not mine", due_date=D, created_by=OTHER)
    with SessionLocal() as db:
        view = svc.today_view(db, ME["user_id"], today=D)
    assert [t["id"] for t in view["due"]] == [overdue, due_today]
    assert [t["title"] for t in view["carried"]] == ["carried"]
    assert [t["title"] for t in view["tasks"]] == ["pinned"]
    assert view["due_more"] == 0


def test_due_suggestions_are_capped_with_a_count_of_the_rest(SessionLocal):
    for i in range(svc.DUE_CAP + 4):
        _task(SessionLocal, title=f"due {i:02d}", due_date=D - timedelta(days=i))
    with SessionLocal() as db:
        view = svc.today_view(db, ME["user_id"], today=D)
    assert len(view["due"]) == svc.DUE_CAP
    assert view["due_more"] == 4
    assert view["due"][0]["title"] == f"due {svc.DUE_CAP + 3:02d}"  # oldest first


# ── the note ─────────────────────────────────────────────────────────────────


def test_note_first_save_then_versioned_saves(client, SessionLocal):
    first = client.get("/api/planner/today").json()["note"]
    assert first["body"] == "" and first["updated_at"] is None

    r = client.put("/api/planner/today/note", json={
        "body": "Ask about the 16x7", "note_date": first["date"], "base_updated_at": None,
    })
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["body"] == "Ask about the 16x7" and saved["updated_via"] == "user"

    r2 = client.put("/api/planner/today/note", json={
        "body": "Ask about the 16x7\nOrder rollers", "note_date": saved["date"],
        "base_updated_at": saved["updated_at"],
    })
    assert r2.status_code == 200, r2.text
    assert client.get("/api/planner/today").json()["note"]["body"] == "Ask about the 16x7\nOrder rollers"

    rows = _audit(SessionLocal, "planner_day_note")
    assert [r.action for r in rows] == ["planner_day_note_save"] * 2
    assert rows[0].details == {"note_date": saved["date"], "via": "user", "before_len": 0,
                               "after": "Ask about the 16x7"}
    assert rows[1].details["before_len"] == len("Ask about the 16x7")
    assert {r.user_id for r in rows} == {ME["user_id"]}


def test_a_stale_save_is_refused_with_the_current_note(client, SessionLocal):
    day = client.get("/api/planner/today").json()["note"]["date"]
    v1 = client.put("/api/planner/today/note", json={"body": "mine", "note_date": day}).json()
    # someone else (the AI, another tab) saves on top of v1
    client.put("/api/planner/today/note", json={
        "body": "mine\n- from the AI", "note_date": day, "base_updated_at": v1["updated_at"],
    })
    stale = client.put("/api/planner/today/note", json={
        "body": "mine, edited", "note_date": day, "base_updated_at": v1["updated_at"],
    })
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["reason"] == "stale"
    assert detail["current"]["body"] == "mine\n- from the AI"
    # a writer that saw no note cannot clobber one that exists
    blind = client.put("/api/planner/today/note", json={"body": "x", "note_date": day})
    assert blind.status_code == 409
    assert client.get("/api/planner/today").json()["note"]["body"] == "mine\n- from the AI"
    assert len(_audit(SessionLocal, "planner_day_note")) == 2


def test_a_page_left_open_past_midnight_cannot_write_into_the_new_day(client):
    r = client.put("/api/planner/today/note", json={"body": "yesterday's", "note_date": "2000-01-01"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "day_changed"


def test_an_unchanged_save_writes_nothing(client, SessionLocal):
    day = client.get("/api/planner/today").json()["note"]["date"]
    v1 = client.put("/api/planner/today/note", json={"body": "same", "note_date": day}).json()
    again = client.put("/api/planner/today/note", json={
        "body": "same", "note_date": day, "base_updated_at": v1["updated_at"],
    })
    assert again.status_code == 200
    assert again.json()["updated_at"] == v1["updated_at"]
    assert len(_audit(SessionLocal, "planner_day_note")) == 1


def test_notes_are_per_user_and_per_day(SessionLocal):
    with SessionLocal() as db:
        svc.save_note(db, tid=TENANT, uid=ME["user_id"], body="mine", note_date=None,
                      base_updated_at=None, today=D)
        svc.save_note(db, tid=TENANT, uid=OTHER, body="theirs", note_date=None,
                      base_updated_at=None, today=D)
        assert svc.today_view(db, ME["user_id"], today=D)["note"]["body"] == "mine"
        assert svc.today_view(db, OTHER, today=D)["note"]["body"] == "theirs"
        tomorrow = svc.today_view(db, ME["user_id"], today=D + timedelta(days=1))["note"]
        assert tomorrow == {"date": "2026-09-29", "body": "", "updated_at": None, "updated_via": None}


def test_an_over_long_note_is_rejected(client):
    r = client.put("/api/planner/today/note", json={"body": "x" * (svc.NOTE_MAX + 1)})
    assert r.status_code == 422


# ── the real race: a save commits between another's check and its write ─────
#
# The audit (2026-09-28) reproduced a silent lost write when the version check
# lived only in Python: both writers read version V, both passed, the second
# overwrote the first. These tests land the competing save INSIDE that window —
# ensure_audit_table runs after the check and before the write — so they can
# only pass if the write itself is conditional on the version it checked.

_PG_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


def _race_engine(kind: str, tmp_path):
    if kind == "sqlite":
        return create_engine(f"sqlite:///{tmp_path / 'race.db'}"), None
    if "postgresql" not in _PG_URL:
        pytest.skip("the Postgres arm of the note race needs TEST_DATABASE_URL/DATABASE_URL")
    schema = f"today_race_{os.getpid()}"
    admin = create_engine(_PG_URL)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    admin.dispose()
    eng = create_engine(_PG_URL, connect_args={"options": f"-csearch_path={schema}"})
    return eng, schema


@pytest.mark.parametrize("kind", ["sqlite", "postgres"])
def test_a_save_landing_mid_write_is_a_conflict_not_an_overwrite(kind, tmp_path, monkeypatch):
    eng, schema = _race_engine(kind, tmp_path)
    try:
        TenantBase.metadata.create_all(eng, checkfirst=True)
        SL = sessionmaker(bind=eng, autoflush=False, autocommit=False)
        with SL() as db:
            v1 = svc.save_note(db, tid=TENANT, uid=ME["user_id"], body="first", note_date=None,
                               base_updated_at=None, today=D)

        real_ensure = svc.ensure_audit_table
        fired = []

        def ai_saves_in_the_window(db):
            real_ensure(db)
            if not fired:
                fired.append(True)
                with SL() as other:  # the AI, on its own connection, on the same V
                    svc.save_note(other, tid=TENANT, uid=ME["user_id"], body="first\n- AI append",
                                  note_date=None, base_updated_at=v1["updated_at"], via="ai", today=D)

        monkeypatch.setattr(svc, "ensure_audit_table", ai_saves_in_the_window)
        with SL() as db, pytest.raises(svc.NoteConflict) as refused:
            svc.save_note(db, tid=TENANT, uid=ME["user_id"], body="first + typing", note_date=None,
                          base_updated_at=v1["updated_at"], today=D)
        assert fired, "the competing save never ran — the test proved nothing"
        assert refused.value.current["body"] == "first\n- AI append"
        with SL() as db:
            assert svc.today_view(db, ME["user_id"], today=D)["note"]["body"] == "first\n- AI append"
            saves = db.execute(
                select(AuditLog).where(AuditLog.action == "planner_day_note_save")
            ).scalars().all()
            assert [s.details["via"] for s in saves] == ["user", "ai"]  # the refused save left no row
    finally:
        eng.dispose()
        if schema:
            admin = create_engine(_PG_URL)
            with admin.begin() as c:
                c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            admin.dispose()


def test_a_first_save_racing_another_first_save_is_a_conflict(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'race1.db'}")
    try:
        TenantBase.metadata.create_all(eng, checkfirst=True)
        SL = sessionmaker(bind=eng, autoflush=False, autocommit=False)
        real_ensure = svc.ensure_audit_table
        fired = []

        def other_first_save(db):
            real_ensure(db)
            if not fired:
                fired.append(True)
                with SL() as other:
                    svc.save_note(other, tid=TENANT, uid=ME["user_id"], body="theirs", note_date=None,
                                  base_updated_at=None, today=D)

        monkeypatch.setattr(svc, "ensure_audit_table", other_first_save)
        with SL() as db, pytest.raises(svc.NoteConflict) as refused:
            svc.save_note(db, tid=TENANT, uid=ME["user_id"], body="mine", note_date=None,
                          base_updated_at=None, today=D)
        assert refused.value.current["body"] == "theirs"
    finally:
        eng.dispose()


# ── a pin never lands on someone else's Today (audit 2026-09-28, round 2) ────


def test_a_task_created_for_someone_else_cannot_be_created_on_today(client, SessionLocal):
    r = client.post("/api/planner/tasks", json={"title": "for Other", "assigned_to": OTHER, "today": True})
    assert r.status_code == 422, r.text
    with SessionLocal() as db:
        assert svc.today_view(db, OTHER)["tasks"] == []
        assert db.execute(select(PlannerTask)).scalars().all() == []
    # assigning to yourself explicitly is still yours
    mine = client.post("/api/planner/tasks", json={"title": "mine", "assigned_to": ME["user_id"], "today": True})
    assert mine.status_code == 201, mine.text


def test_reassigning_a_pinned_task_takes_it_off_today_and_says_so(client, SessionLocal):
    tid = client.post("/api/planner/tasks", json={"title": "pinned then handed off", "today": True}).json()["id"]
    r = client.patch(f"/api/planner/tasks/{tid}", json={"assigned_to": OTHER})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert svc.today_view(db, OTHER)["tasks"] == []
        assert svc.today_view(db, ME["user_id"])["tasks"] == []
    removals = [a for a in _audit(SessionLocal, "planner_task") if a.action == "planner_today_remove"]
    assert len(removals) == 1
    assert removals[0].details == {"title": "pinned then handed off", "via": "user", "reason": "reassigned",
                                   "from": ME["user_id"], "to": OTHER}


def test_an_edit_that_keeps_the_assignee_keeps_the_pin(client, SessionLocal):
    tid = client.post("/api/planner/tasks", json={"title": "rename me", "today": True}).json()["id"]
    assert client.patch(f"/api/planner/tasks/{tid}", json={"title": "renamed", "assigned_to": None}).status_code == 200
    with SessionLocal() as db:
        assert [t["title"] for t in svc.today_view(db, ME["user_id"])["tasks"]] == ["renamed"]
    assert not [a for a in _audit(SessionLocal, "planner_task") if a.action == "planner_today_remove"]


def test_assigning_a_pinned_task_to_yourself_keeps_the_pin(client, SessionLocal):
    """Unassigned-and-mine → assigned-to-me is the same owner (round-3 audit)."""
    tid = client.post("/api/planner/tasks", json={"title": "self-assign", "today": True}).json()["id"]
    assert client.patch(f"/api/planner/tasks/{tid}", json={"assigned_to": ME["user_id"]}).status_code == 200
    assert client.patch(f"/api/planner/tasks/{tid}", json={"assigned_to": None}).status_code == 200
    with SessionLocal() as db:
        assert [t["id"] for t in svc.today_view(db, ME["user_id"])["tasks"]] == [tid]
    assert not [a for a in _audit(SessionLocal, "planner_task") if a.action == "planner_today_remove"]


def test_unassigning_a_task_someone_else_created_hands_it_back_and_drops_the_pin(client, SessionLocal):
    tid = _task(SessionLocal, title="theirs, handed to me", created_by=OTHER, assigned_to=ME["user_id"], today_date=svc.calendar_today_utc())
    assert client.patch(f"/api/planner/tasks/{tid}", json={"assigned_to": None}).status_code == 200
    with SessionLocal() as db:
        assert svc.today_view(db, OTHER)["tasks"] == []
    removal = [a for a in _audit(SessionLocal, "planner_task") if a.action == "planner_today_remove"]
    assert [(r.details["from"], r.details["to"]) for r in removal] == [(ME["user_id"], OTHER)]


def test_a_blank_assignee_is_unassigned(client, SessionLocal):
    r = client.post("/api/planner/tasks", json={"title": "blank", "assigned_to": "", "today": True})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        assert [t["title"] for t in svc.today_view(db, ME["user_id"])["tasks"]] == ["blank"]
        assert db.execute(select(PlannerTask)).scalar_one().assigned_to is None
    tid = r.json()["id"]
    assert client.patch(f"/api/planner/tasks/{tid}", json={"assigned_to": ""}).status_code == 200
    with SessionLocal() as db:
        assert [t["title"] for t in svc.today_view(db, ME["user_id"])["tasks"]] == ["blank"]
