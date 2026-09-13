"""Chat push notifications land on the thread they are about (#657).

A chat push used to send both sides to `/mobile?job=<id>`, a query no view
reads, so the tap opened the generic Today screen.

Each test invokes the real send path and checks the link it actually produced
against the SPA's route table, so a link to a route that does not exist turns
the test red — not merely a changed string. The query each view consumes is
pinned from the other side by ChatPushDeepLinks657.spec.js.
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import gdx_dispatch.core.push_subscriptions as push_subscriptions
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (registers every table)
from gdx_dispatch.models.tenant_models import JobAssignment, TenantRole, UserRoleAssignment
from gdx_dispatch.routers import mobile_chat

ROUTER = (
    pathlib.Path(__file__).resolve().parents[1] / "frontend/src/router/index.js"
)


def _route_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Every absolute `path:` in the SPA router, as an anchored regex."""
    paths = set(re.findall(r"path:\s*'(/[^']*)'", ROUTER.read_text()))
    out = []
    for p in paths:
        if "*" in p or "pathMatch" in p:
            continue  # the catch-all matches everything; it is the 404 page
        body = re.sub(r":[A-Za-z_]+", "[^/]+", p.rstrip("/")) or ""
        out.append((p, re.compile(f"^{body}/?$")))
    return out


def _route_for(url: str) -> str | None:
    path = urlsplit(url).path
    hits = [p for p, rx in _route_patterns() if rx.match(path)]
    return hits[0] if hits else None


def test_the_route_matcher_can_fail() -> None:
    """The check below is only worth something if a dead link is caught."""
    assert _route_for("/mobile?job=1") == "/mobile"  # exists — the old bug was the query
    assert _route_for("/invoices/abc") is None
    assert _route_for("/customers/abc/schedule") is None
    assert _route_for("/billing/abc") == "/billing/:id"


# ── Chat push ───────────────────────────────────────────────────────────────

_TECH = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}
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
    job_id, customer_id = str(uuid4()), str(uuid4())
    db.execute(
        text("INSERT INTO customers (id, name, address, company_id) VALUES (:id, 'Acme', '1 Main', 'tenant-a')"),
        {"id": customer_id},
    )
    # Prod's shape (measured 2026-09-13): jobs.assigned_to holds a TECHNICIAN
    # id, and the tech's user is reached through a live job_assignments row.
    db.execute(
        text("INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, assigned_to, scheduled_at, created_at) "
             "VALUES (:id, 'tenant-a', :cid, 'Repair', 'on_site', 'tech-1', :n, :n)"),
        {"id": job_id, "cid": customer_id, "n": now},
    )
    for tech, user in (("tech-1", "user-1"), ("tech-9", "user-9")):
        db.execute(
            text("INSERT INTO technicians (id, company_id, user_id, active, created_at) "
                 "VALUES (:t, 'tenant-a', :u, 1, :n)"),
            {"t": tech, "u": user, "n": now},
        )
    db.add(JobAssignment(id=str(uuid4()), job_id=job_id, tech_id="tech-1", is_lead=True, assigned_at=now))
    # user-9 was on this job and has been removed from it.
    db.add(JobAssignment(id=str(uuid4()), job_id=job_id, tech_id="tech-9", is_lead=False,
                         assigned_at=now, deleted_at=now))
    role = TenantRole(company_id="tenant-a", name="dispatcher", permissions="[]")
    db.add(role)
    db.flush()
    db.add(UserRoleAssignment(company_id="tenant-a", user_id="user-2", role_id=role.id))
    db.commit()

    sent: list[dict] = []
    monkeypatch.setattr(
        push_subscriptions, "send_push", lambda _db, **kw: sent.append(kw), raising=True
    )
    yield db, job_id, sent
    db.close()
    engine.dispose()


def _send(db, job_id, user, body):
    resp = mobile_chat.send_job_chat(
        job_id=job_id,
        payload=mobile_chat.SendChatIn(kind="text", body=body),
        request=_req(), current_user=user, db=db,
    )
    assert resp.status_code == 201, resp.body
    return json.loads(resp.body)


def test_a_tech_message_sends_the_dispatcher_to_that_thread(chat_db):
    db, job_id, sent = chat_db
    _send(db, job_id, _TECH, "Need a cable here")

    assert [s["user_id"] for s in sent] == ["user-2"]
    url = sent[0]["url"]
    assert _route_for(url) == "/mobile/dispatch"
    # MobileDispatchView reads ?job= to open the thread with mark-read.
    assert parse_qs(urlsplit(url).query) == {"job": [job_id]}


def test_a_dispatcher_message_sends_the_tech_to_the_job_chat(chat_db):
    db, job_id, sent = chat_db
    _send(db, job_id, _DISPATCHER, "Customer called, running late?")

    by_user = {s["user_id"]: s["url"] for s in sent}
    assert "user-1" in by_user
    url = by_user["user-1"]
    assert _route_for(url) == "/mobile/jobs/:id"
    assert urlsplit(url).path == f"/mobile/jobs/{job_id}"
    # MobileJobDetailView reads ?chat=1 to open the chat dialog.
    assert parse_qs(urlsplit(url).query) == {"chat": ["1"]}


def test_a_tech_removed_from_the_job_is_not_notified(chat_db):
    """The job page refuses a soft-deleted assignee, so the chat link would
    land them on "Job not found" — they must not get the push at all."""
    db, job_id, sent = chat_db
    _send(db, job_id, _DISPATCHER, "Heads up")

    assert "user-9" not in {s["user_id"] for s in sent}
