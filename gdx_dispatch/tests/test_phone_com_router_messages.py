"""pc-s10 — phone_com messages router tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.client import BASE_URL
from gdx_dispatch.modules.phone_com.models import PhoneComMessage
from gdx_dispatch.modules.phone_com.router import router as ops_router


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )


def _engines():
    ce = create_engine("sqlite:///:memory:",
                       connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
    for n in ("tenants", "tenant_settings"):
        if n in ControlBase.metadata.tables:
            ControlBase.metadata.tables[n].create(ce, checkfirst=True)
    te = create_engine("sqlite:///:memory:",
                       connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
    TenantBase.metadata.create_all(te)
    return ce, te


def _app(ce, te, tid, role="admin"):
    app = FastAPI()
    app.include_router(ops_router)
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tsm = sessionmaker(bind=te, expire_on_commit=False)

    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": str(uuid4()), "role": role, "tenant_id": str(tid),
    }
    app.dependency_overrides[get_db] = lambda: (yield from _gen(csm))
    app.dependency_overrides[get_tenant_db] = lambda: (yield from _gen(tsm))
    app.dependency_overrides[require_module("phone_com")] = lambda: None
    return app, csm, tsm


def _gen(sm):
    s = sm()
    try:
        yield s
    finally:
        s.close()


def _seed_tenant(csm):
    s = csm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="t1", name="T"))
    s.commit()
    s.close()
    return tid


def test_list_threads_empty():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).get("/api/phone-com/messages/threads")
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_list_threads_groups_by_thread_key():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    base = datetime.now(timezone.utc)
    # Two messages in thread A, one in thread B.
    s.add(PhoneComMessage(
        phone_com_message_id="m1", thread_key="+1|+2", direction="in",
        from_number="+1", to_number="+2", body="hi", sent_at=base,
        attachments=[], raw_payload={},
    ))
    s.add(PhoneComMessage(
        phone_com_message_id="m2", thread_key="+1|+2", direction="out",
        from_number="+2", to_number="+1", body="hey", sent_at=base + timedelta(seconds=10),
        attachments=[], raw_payload={},
    ))
    s.add(PhoneComMessage(
        phone_com_message_id="m3", thread_key="+3|+4", direction="in",
        from_number="+3", to_number="+4", body="alone", sent_at=base + timedelta(seconds=20),
        attachments=[], raw_payload={},
    ))
    s.commit()
    s.close()

    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).get("/api/phone-com/messages/threads")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2  # two unique thread_keys
    keys = {it["thread_key"] for it in body["items"]}
    assert keys == {"+1|+2", "+3|+4"}


def test_get_thread_returns_messages_in_order():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    base = datetime.now(timezone.utc)
    for i in range(5):
        s.add(PhoneComMessage(
            phone_com_message_id=f"m-{i}", thread_key="+1|+2",
            direction="in" if i % 2 == 0 else "out",
            from_number="+1", to_number="+2", body=f"msg-{i}",
            sent_at=base + timedelta(seconds=i),
            attachments=[], raw_payload={},
        ))
    s.commit()
    s.close()

    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).get("/api/phone-com/messages/threads/+1|+2")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    bodies = [m["body"] for m in body["items"]]
    assert bodies == [f"msg-{i}" for i in range(5)]


@respx.mock
def test_send_message_persists_outbound_row():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    cs = csm()
    key_storage.set_token(cs, tid, "phc-token")
    cs.close()
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    ts = tsm()
    ts.add(AppSettings(phone_com_voip_id="1000000",
                       phone_com_default_caller_id="+18005550199"))
    ts.commit()
    ts.close()

    respx.post(
        f"{BASE_URL}/accounts/1000000/messages"
    ).mock(
        return_value=httpx.Response(200, json={"id": "msg-out-1", "status": "queued"})
    )

    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).post(
        "/api/phone-com/messages",
        json={"to": "+13202959628", "body": "hello"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone_com_message_id"] == "msg-out-1"
    assert body["delivery_status"] == "queued"

    s = tsm()
    rows = s.query(PhoneComMessage).all()
    assert len(rows) == 1
    assert rows[0].direction == "out"
    assert rows[0].body == "hello"
    s.close()


def test_send_message_400_when_no_default_caller():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    cs = csm()
    key_storage.set_token(cs, tid, "phc-token")
    cs.close()
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    ts = tsm()
    ts.add(AppSettings(phone_com_voip_id="1000000"))  # no caller_id
    ts.commit()
    ts.close()

    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).post(
        "/api/phone-com/messages",
        json={"to": "+13202959628", "body": "hello"},
    )
    assert r.status_code == 400


def test_send_message_rejects_garbage_to():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).post(
        "/api/phone-com/messages",
        json={"to": "not-a-number", "body": "x"},
    )
    assert r.status_code == 422


def test_send_message_rejects_oversize_body():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    app, _, _ = _app(ce, te, tid)
    r = TestClient(app).post(
        "/api/phone-com/messages",
        json={"to": "+13202959628", "body": "x" * 1601},
    )
    assert r.status_code == 422


# --- local unread state (2026-07-24) ----------------------------------------

def _seed_msg(s, *, mid, thread, direction, sent_at, read_at=None, conversation_id=None):
    s.add(PhoneComMessage(
        phone_com_message_id=mid, thread_key=thread, direction=direction,
        from_number="+1", to_number="+2", body=f"body-{mid}", sent_at=sent_at,
        read_at=read_at, phone_com_conversation_id=conversation_id,
        attachments=[], raw_payload={},
    ))


def test_unread_count_counts_inbound_unread_only():
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    base = datetime.now(timezone.utc)
    _seed_msg(s, mid="m1", thread="+1|+2", direction="in", sent_at=base)                    # unread
    _seed_msg(s, mid="m2", thread="+1|+2", direction="out", sent_at=base)                   # outbound: never counts
    _seed_msg(s, mid="m3", thread="+1|+2", direction="in", sent_at=base)                    # unread
    _seed_msg(s, mid="m4", thread="+3|+4", direction="in", sent_at=base, read_at=base)      # already read
    s.commit()
    s.close()

    app, _, _ = _app(ce, te, tid)
    client = TestClient(app)
    assert client.get("/api/phone-com/messages/unread-count").json() == {"count": 2}

    threads = {t["thread_key"]: t for t in client.get("/api/phone-com/messages/threads").json()["items"]}
    assert threads["+1|+2"]["unread_count"] == 2
    assert threads["+3|+4"]["unread_count"] == 0


def test_open_thread_stamps_read():
    """GET-ing a conversation marks its inbound messages locally read — the
    badge and thread dot must clear just by reading the thread."""
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    base = datetime.now(timezone.utc)
    _seed_msg(s, mid="m1", thread="+1|+2", direction="in", sent_at=base)
    _seed_msg(s, mid="m2", thread="+1|+2", direction="in", sent_at=base)
    _seed_msg(s, mid="m3", thread="+3|+4", direction="in", sent_at=base)  # other thread untouched
    s.commit()
    s.close()

    app, _, _ = _app(ce, te, tid)
    client = TestClient(app)
    assert client.get("/api/phone-com/messages/threads/+1|+2").status_code == 200
    assert client.get("/api/phone-com/messages/unread-count").json() == {"count": 1}


def test_mark_thread_read_clears_local_without_conversation_ids():
    """Rows from before P2.9 have no phone_com_conversation_id, so the
    upstream sync no-ops — the LOCAL read marker must still clear."""
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    base = datetime.now(timezone.utc)
    _seed_msg(s, mid="m1", thread="+1|+2", direction="in", sent_at=base)
    s.commit()
    s.close()

    app, _, _ = _app(ce, te, tid)
    client = TestClient(app)
    r = client.post("/api/phone-com/messages/threads/+1|+2/mark-read")
    assert r.status_code == 204
    assert client.get("/api/phone-com/messages/unread-count").json() == {"count": 0}


def test_open_thread_stamps_beyond_current_page():
    """The read stamp covers the WHOLE thread, not just the fetched page —
    a long conversation opened at per_page=2 must still zero the badge."""
    ce, te = _engines()
    csm = sessionmaker(bind=ce, expire_on_commit=False)
    tid = _seed_tenant(csm)
    tsm = sessionmaker(bind=te, expire_on_commit=False)
    s = tsm()
    base = datetime.now(timezone.utc)
    for i in range(5):
        _seed_msg(s, mid=f"m{i}", thread="+1|+2", direction="in",
                  sent_at=base + timedelta(seconds=i))
    s.commit()
    s.close()

    app, _, _ = _app(ce, te, tid)
    client = TestClient(app)
    r = client.get("/api/phone-com/messages/threads/+1|+2?per_page=2")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2
    assert client.get("/api/phone-com/messages/unread-count").json() == {"count": 0}
