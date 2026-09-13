"""The dispatch thread list says who cleared a thread (#656).

Read state is one `read_at` / `read_by_user_id` stamp per message, shared by
every dispatcher, admin and owner: the first to open a thread clears its
"N new" badge for all of them. The owner kept that office-wide meaning
(2026-09-13) and asked for the badge to say who read it, so a cleared thread
is never mistaken for one nobody acted on.

Everything runs through the real endpoints — send, mark-read, list — and the
readers are real `users` rows, so the name comes from the same resolver the
app uses (including its SQLite 32-hex UUID handling).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (registers every table)
from gdx_dispatch.models.tenant_models import JobChatMessage, User
from gdx_dispatch.routers import mobile_chat

_TECH = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}


def _req() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    req.state.tenant_id = "tenant-a"
    return req


def _body(resp):
    return json.loads(resp.body)


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'seen.sqlite3'}", connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    now = datetime.now(UTC)
    job_id, customer_id = str(uuid4()), str(uuid4())
    db.execute(
        text("INSERT INTO customers (id, name, address, company_id) VALUES (:id, 'Acme', '1 Main', 'tenant-a')"),
        {"id": customer_id},
    )
    db.execute(
        text("INSERT INTO technicians (id, company_id, user_id, active, created_at) VALUES ('tech-1', 'tenant-a', 'user-1', 1, :n)"),
        {"n": now},
    )
    db.execute(
        text("INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, assigned_to, scheduled_at, created_at) "
             "VALUES (:id, 'tenant-a', :cid, 'Repair', 'on_site', 'user-1', :n, :n)"),
        {"id": job_id, "cid": customer_id, "n": now},
    )
    dana, sam, nameless = uuid4(), uuid4(), uuid4()
    db.add_all([
        User(id=dana, company_id="tenant-a", role="dispatcher", name="Dana Dispatch", email="dana@example.test"),
        User(id=sam, company_id="tenant-a", role="owner", name="Sam Owner", email="sam@example.test"),
        User(id=nameless, company_id="tenant-a", role="admin", email="office@example.test"),
    ])
    db.commit()
    users = {
        "dana": {"user_id": str(dana), "role": "dispatcher", "tenant_id": "tenant-a"},
        "sam": {"user_id": str(sam), "role": "owner", "tenant_id": "tenant-a"},
        "nameless": {"user_id": str(nameless), "role": "admin", "tenant_id": "tenant-a"},
    }
    yield db, job_id, users
    db.close()
    engine.dispose()


def _tech_says(db, job_id, body):
    resp = mobile_chat.send_job_chat(
        job_id=job_id, payload=mobile_chat.SendChatIn(kind="text", body=body),
        request=_req(), current_user=_TECH, db=db,
    )
    assert resp.status_code == 201, resp.body
    return _body(resp)["id"]


def _read(db, message_id, who):
    resp = mobile_chat.mark_chat_read(message_id=message_id, request=_req(), current_user=who, db=db)
    assert resp.status_code == 200, resp.body


def _thread(db, job_id, who):
    resp = mobile_chat.list_dispatch_threads(request=_req(), current_user=who, db=db)
    assert resp.status_code == 200, resp.body
    return next(t for t in _body(resp)["threads"] if t["job_id"] == job_id)


def test_an_unread_thread_names_no_reader(env):
    db, job_id, users = env
    _tech_says(db, job_id, "Need a cable")

    t = _thread(db, job_id, users["dana"])
    assert t["unread_count"] == 1
    assert t["last_read_by_name"] is None
    assert t["last_read_at"] is None


def test_a_cleared_thread_says_who_cleared_it_to_everyone(env):
    db, job_id, users = env
    first = _tech_says(db, job_id, "Need a cable")
    second = _tech_says(db, job_id, "Found one, never mind")
    _read(db, first, users["dana"])
    _read(db, second, users["dana"])

    # Office-wide: Sam sees the badge cleared — and that Dana did it.
    t = _thread(db, job_id, users["sam"])
    assert t["unread_count"] == 0
    assert t["last_read_by_name"] == "Dana Dispatch"
    assert t["last_read_at"] is not None


def test_the_most_recent_reader_is_the_one_named(env):
    """Latest read_at wins — not the latest message. The reads happen in the
    opposite order to the messages, so "whichever row came back last" (which
    is insertion order on SQLite) would name the wrong person."""
    db, job_id, users = env
    first = _tech_says(db, job_id, "One")
    second = _tech_says(db, job_id, "Two")
    _read(db, second, users["sam"])
    _read(db, first, users["dana"])

    t = _thread(db, job_id, users["sam"])
    assert t["last_read_by_name"] == "Dana Dispatch"


def test_a_reader_with_no_name_falls_back_to_their_email(env):
    db, job_id, users = env
    msg = _tech_says(db, job_id, "Hello")
    _read(db, msg, users["nameless"])

    assert _thread(db, job_id, users["dana"])["last_read_by_name"] == "office@example.test"


def test_a_former_employee_is_still_named(env):
    """Users are soft-deleted. The stamp records who read it at the time, so a
    reader who has since left is still the person named."""
    db, job_id, users = env
    msg = _tech_says(db, job_id, "Hello")
    _read(db, msg, users["dana"])
    db.execute(text("UPDATE users SET deleted_at = :n"), {"n": datetime.now(UTC)})
    db.commit()

    assert _thread(db, job_id, users["sam"])["last_read_by_name"] == "Dana Dispatch"


def test_a_receipt_outside_the_thread_window_names_nobody(env):
    """The unread count only looks at the last 7 days. A receipt on an older
    message must not put "Seen by" beside a count that never included it."""
    db, job_id, users = env
    old = _tech_says(db, job_id, "Last week")
    _read(db, old, users["dana"])
    # Through the ORM: SQLite stores the Uuid id as 32-hex, so a raw
    # `WHERE id = '<dashed>'` would silently match nothing.
    db.query(JobChatMessage).filter(JobChatMessage.id == UUID(old)).update(
        {"created_at": datetime.now(UTC) - timedelta(days=9)}
    )
    _tech_says(db, job_id, "Today, unread")
    db.commit()

    t = _thread(db, job_id, users["sam"])
    assert t["total_count"] == 1, "the old message must have left the window"
    assert t["unread_count"] == 1
    assert t["last_read_by_name"] is None
    assert t["last_read_at"] is None


def test_a_reader_whose_user_row_is_gone_still_shows_the_time(env):
    """The stamp outlives the user; the list must not 500 or invent a name."""
    db, job_id, users = env
    msg = _tech_says(db, job_id, "Hello")
    _read(db, msg, {"user_id": str(uuid4()), "role": "admin", "tenant_id": "tenant-a"})

    t = _thread(db, job_id, users["dana"])
    assert t["unread_count"] == 0
    assert t["last_read_by_name"] is None
    assert t["last_read_at"] is not None
