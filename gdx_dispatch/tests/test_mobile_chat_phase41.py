"""Sprint tech_mobile Phase 4.1 — Per-job dispatch chat tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (registers JobChatMessage)
from gdx_dispatch.routers import mobile_chat

_TECH = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}
_DISPATCHER = {"user_id": "user-2", "role": "dispatcher", "tenant_id": "tenant-a"}


def _as_json(r):
    return json.loads(r.body)


def _req(tenant_id="tenant-a"):
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    req.state.tenant_id = tenant_id
    return req


@pytest.fixture()
def session_factory(tmp_path):
    db_file = tmp_path / "mobile_chat_test.sqlite3"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def _seed(SessionLocal):
    db = SessionLocal()
    now = datetime.now(UTC)
    job_id = str(uuid4())
    customer_id = str(uuid4())
    try:
        db.execute(text("INSERT INTO customers (id, name, address, company_id) VALUES (:id, 'Acme', '1 Main', 'tenant-a')"), {"id": customer_id})
        db.execute(text("INSERT INTO technicians (id, company_id, user_id, active, created_at) VALUES ('tech-1','tenant-a','user-1',1,:n)"), {"n": now})
        db.execute(text("INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, assigned_to, scheduled_at, created_at) VALUES (:id,'tenant-a',:cid,'Repair','on_site','user-1',:n,:n)"), {"id": job_id, "cid": customer_id, "n": now})
        db.commit()
        return {"job_id": job_id, "customer_id": customer_id}
    finally:
        db.close()


def test_send_text_then_get(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        send = mobile_chat.send_job_chat(
            job_id=seed["job_id"],
            payload=mobile_chat.SendChatIn(kind="text", body="On the way, ETA 8 min"),
            request=_req(), current_user=_TECH, db=db,
        )
        assert send.status_code == 201, send.body
        msg = _as_json(send)
        assert msg["body"] == "On the way, ETA 8 min"
        assert msg["sender_role"] == "tech"

        get = mobile_chat.get_job_chat(
            job_id=seed["job_id"], request=_req(), current_user=_TECH, db=db,
        )
        body = _as_json(get)
        assert len(body["messages"]) == 1
        assert "on_my_way" in body["quick_actions"]
    finally:
        db.close()


def test_quick_action_send(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_chat.send_job_chat(
            job_id=seed["job_id"],
            payload=mobile_chat.SendChatIn(kind="quick_action", quick_action="on_my_way"),
            request=_req(), current_user=_TECH, db=db,
        )
        assert resp.status_code == 201
        msg = _as_json(resp)
        assert msg["kind"] == "quick_action"
        assert msg["quick_action"] == "on_my_way"
        assert msg["body"] == "On my way"
    finally:
        db.close()


def test_quick_action_unknown_400(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_chat.send_job_chat(
            job_id=seed["job_id"],
            payload=mobile_chat.SendChatIn(kind="quick_action", quick_action="bogus"),
            request=_req(), current_user=_TECH, db=db,
        )
        assert resp.status_code == 400
    finally:
        db.close()


def test_text_no_body_400(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_chat.send_job_chat(
            job_id=seed["job_id"],
            payload=mobile_chat.SendChatIn(kind="text", body=""),
            request=_req(), current_user=_TECH, db=db,
        )
        assert resp.status_code == 400
    finally:
        db.close()


def test_unauthorized_job_404(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        resp = mobile_chat.send_job_chat(
            job_id=seed["job_id"],
            payload=mobile_chat.SendChatIn(kind="text", body="hi"),
            request=_req(),
            current_user={"user_id": "other", "role": "technician"},
            db=db,
        )
        assert resp.status_code == 404
    finally:
        db.close()


def test_dispatcher_sees_all_jobs_and_marks_read(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        # Tech sends
        send = mobile_chat.send_job_chat(
            job_id=seed["job_id"],
            payload=mobile_chat.SendChatIn(kind="text", body="Need cable, urgent"),
            request=_req(), current_user=_TECH, db=db,
        )
        msg = _as_json(send)

        # Dispatcher reads + marks read
        get = mobile_chat.get_job_chat(
            job_id=seed["job_id"], request=_req(), current_user=_DISPATCHER, db=db,
        )
        assert get.status_code == 200
        assert len(_as_json(get)["messages"]) == 1

        read = mobile_chat.mark_chat_read(
            message_id=msg["id"], request=_req(), current_user=_DISPATCHER, db=db,
        )
        assert read.status_code == 200
        assert _as_json(read)["read_at"] is not None
    finally:
        db.close()


def test_tech_cannot_mark_read_403(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        send = mobile_chat.send_job_chat(
            job_id=seed["job_id"],
            payload=mobile_chat.SendChatIn(kind="text", body="hi"),
            request=_req(), current_user=_TECH, db=db,
        )
        msg = _as_json(send)
        resp = mobile_chat.mark_chat_read(
            message_id=msg["id"], request=_req(), current_user=_TECH, db=db,
        )
        assert resp.status_code == 403
    finally:
        db.close()


def test_dispatch_threads_unread_first(session_factory):
    seed = _seed(session_factory)
    db = session_factory()
    try:
        # Tech sends two messages
        for body in ("first", "second"):
            mobile_chat.send_job_chat(
                job_id=seed["job_id"],
                payload=mobile_chat.SendChatIn(kind="text", body=body),
                request=_req(), current_user=_TECH, db=db,
            )

        resp = mobile_chat.list_dispatch_threads(
            request=_req(), current_user=_DISPATCHER, db=db,
        )
        assert resp.status_code == 200
        body = _as_json(resp)
        assert len(body["threads"]) == 1
        assert body["threads"][0]["unread_count"] == 2
        assert body["threads"][0]["job_id"] == seed["job_id"]
    finally:
        db.close()
