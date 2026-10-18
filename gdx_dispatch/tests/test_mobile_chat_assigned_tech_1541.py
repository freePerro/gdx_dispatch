"""Chat reads ``jobs.assigned_to`` as the technician id it is (GDXA-1541).

Prod's assigned jobs store a TECHNICIAN id in ``jobs.assigned_to``. Chat used
that column as a USER id twice: the tech's visibility gate compared it to the
caller's user id (a tech who could open the job got 404 posting chat on it),
and the dispatcher→tech push handed it straight to ``send_push`` (a push to a
user that does not exist). Separately, the tech→dispatcher push notified
holders of a soft-deleted role.

The fixture assigns the job ONLY through ``jobs.assigned_to`` — no
job_assignments row — which is the shape every one of these bugs needed.
Real SQLite schema from the ORM, real queries; only ``send_push`` itself is
replaced, to record who it was asked to notify.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import gdx_dispatch.core.push_subscriptions as push_subscriptions
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (registers every table)
from gdx_dispatch.models.tenant_models import (
    Appointment,
    JobAssignment,
    TenantRole,
    UserRoleAssignment,
)
from gdx_dispatch.routers import mobile_chat

_TECH = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}
_OTHER_TECH = {"user_id": "user-9", "role": "technician", "tenant_id": "tenant-a"}
_APPT_TECH = {"user_id": "user-5", "role": "technician", "tenant_id": "tenant-a"}
_INACTIVE_TECH = {"user_id": "user-7", "role": "technician", "tenant_id": "tenant-a"}
_DISPATCHER = {"user_id": "user-2", "role": "dispatcher", "tenant_id": "tenant-a"}


def _req() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    req.state.tenant_id = "tenant-a"
    return req


@pytest.fixture()
def chat_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chat.sqlite3'}", connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    now = datetime.now(UTC)
    job_id, removed_job_id, customer_id = str(uuid4()), str(uuid4()), str(uuid4())
    db.execute(
        text("INSERT INTO customers (id, name, address, company_id) VALUES (:id, 'Acme', '1 Main', 'tenant-a')"),
        {"id": customer_id},
    )
    for tech, user, active in (("tech-1", "user-1", 1), ("tech-9", "user-9", 1),
                               ("tech-5", "user-5", 1), ("tech-7", "user-7", 0)):
        db.execute(
            text("INSERT INTO technicians (id, company_id, user_id, active, created_at) "
                 "VALUES (:t, 'tenant-a', :u, :a, :n)"),
            {"t": tech, "u": user, "a": active, "n": now},
        )
    # The job under test: assigned to tech-1 through jobs.assigned_to ONLY.
    db.execute(
        text("INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, assigned_to, scheduled_at, created_at) "
             "VALUES (:id, 'tenant-a', :cid, 'Repair', 'on_site', 'tech-1', :n, :n)"),
        {"id": job_id, "cid": customer_id, "n": now},
    )
    # A second job tech-9 was crewed on and has been removed from.
    db.execute(
        text("INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, assigned_to, scheduled_at, created_at) "
             "VALUES (:id, 'tenant-a', :cid, 'Install', 'on_site', 'tech-1', :n, :n)"),
        {"id": removed_job_id, "cid": customer_id, "n": now},
    )
    # tech-5 is on the job by appointment only; tech-7 is crewed but deactivated.
    db.add(Appointment(company_id="tenant-a", job_id=UUID(job_id), tech_id="tech-5",
                       title="Repair", start_at=now, end_at=now + timedelta(hours=1)))
    db.add(JobAssignment(id=str(uuid4()), job_id=job_id, tech_id="tech-7", is_lead=False, assigned_at=now))
    db.add(JobAssignment(id=str(uuid4()), job_id=removed_job_id, tech_id="tech-9", is_lead=False,
                         assigned_at=now, deleted_at=now))
    live = TenantRole(company_id="tenant-a", name="dispatcher", permissions="[]")
    gone = TenantRole(company_id="tenant-a", name="admin", permissions="[]", deleted_at=now)
    db.add_all([live, gone])
    db.flush()
    db.add(UserRoleAssignment(company_id="tenant-a", user_id="user-2", role_id=live.id))
    db.add(UserRoleAssignment(company_id="tenant-a", user_id="user-3", role_id=gone.id))
    db.commit()

    sent: list[dict] = []
    monkeypatch.setattr(
        push_subscriptions, "send_push", lambda _db, **kw: sent.append(kw), raising=True
    )
    yield db, job_id, removed_job_id, sent
    db.close()
    engine.dispose()


def _send(db, job_id, user, body="hi"):
    return mobile_chat.send_job_chat(
        job_id=job_id,
        payload=mobile_chat.SendChatIn(kind="text", body=body),
        request=_req(), current_user=user, db=db,
    )


def test_the_assigned_tech_can_post_and_read_chat(chat_db):
    db, job_id, _removed, _sent = chat_db
    resp = _send(db, job_id, _TECH, "On the ladder now")
    assert resp.status_code == 201, resp.body

    resp = mobile_chat.get_job_chat(job_id=job_id, request=_req(), current_user=_TECH, db=db)
    assert resp.status_code == 200, resp.body
    assert [m["body"] for m in json.loads(resp.body)["messages"]] == ["On the ladder now"]


def test_another_tech_still_cannot_see_the_job(chat_db):
    """The fix widens nothing: an unassigned tech is refused as before."""
    db, job_id, _removed, _sent = chat_db
    assert _send(db, job_id, _OTHER_TECH).status_code == 404


def test_a_tech_removed_from_the_crew_loses_the_thread(chat_db):
    """Chat's old crew check had no ``ja.deleted_at IS NULL``."""
    db, _job, removed_job_id, _sent = chat_db
    assert _send(db, removed_job_id, _OTHER_TECH).status_code == 404


def test_a_dispatcher_message_pushes_the_techs_user_not_the_technician_id(chat_db):
    db, job_id, _removed, sent = chat_db
    assert _send(db, job_id, _DISPATCHER, "Customer called").status_code == 201
    # tech-1 by assigned_to, tech-5 by appointment; deactivated tech-7 is not pushed.
    assert sorted(s["user_id"] for s in sent) == ["user-1", "user-5"]


def test_a_soft_deleted_role_is_not_notified(chat_db):
    db, job_id, _removed, sent = chat_db
    assert _send(db, job_id, _TECH, "Need a cable").status_code == 201
    assert sorted(s["user_id"] for s in sent) == ["user-2"]


def test_a_tech_on_the_job_by_appointment_can_post(chat_db):
    db, job_id, _removed, _sent = chat_db
    assert _send(db, job_id, _APPT_TECH).status_code == 201


@pytest.mark.parametrize("deactivate", ["tech-1", "tech-7"], ids=["assigned_to", "crew"])
def test_a_deactivated_tech_is_refused_like_on_the_job_page(chat_db, deactivate):
    """The shared ownership gate matches inactive technician rows; the mobile
    routers refuse them up front with a 403 (GDXA-208), and chat must too."""
    db, job_id, _removed, _sent = chat_db
    db.execute(text("UPDATE technicians SET active = 0 WHERE id = :t"), {"t": deactivate})
    db.commit()
    user = _TECH if deactivate == "tech-1" else _INACTIVE_TECH
    with pytest.raises(HTTPException) as exc:
        _send(db, job_id, user)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        mobile_chat.get_job_chat(job_id=job_id, request=_req(), current_user=user, db=db)
