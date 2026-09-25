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
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
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


def _router_paths() -> set[str]:
    """Every route the SPA can reach, as an absolute path.

    Most children declare an absolute `path:` and need no assembly. The
    `/phone-com` block declares its four children RELATIVE (`path: 'calls'`),
    so they are joined to the nearest enclosing absolute parent — a scan that
    only reads absolute paths calls `/phone-com/calls` dead, which is worse
    than a blind spot: it sends the next author to "fix" a working link.
    A bare `path: ''` is the parent's own index route, already collected.
    """
    paths: set[str] = set()
    parents: list[tuple[int, str]] = []  # (indent, absolute path)
    for line in ROUTER.read_text().splitlines():
        m = re.search(r"path:\s*'([^']*)'", line)
        if not m:
            continue
        raw, indent = m.group(1), len(line) - len(line.lstrip())
        if raw.startswith("/"):
            while parents and parents[-1][0] >= indent:
                parents.pop()
            parents.append((indent, raw))
            paths.add(raw)
        elif raw:
            parent = next((p for ind, p in reversed(parents) if ind < indent), None)
            if parent:
                paths.add(f"{parent.rstrip('/')}/{raw}")
    return paths


def _route_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Every reachable `path:` in the SPA router, as an anchored regex.

    Sorted static-before-dynamic so `_route_for` is deterministic: both
    `/billing/new` and `/billing/:id` match "/billing/new", and picking off an
    unordered set made the winner depend on PYTHONHASHSEED.
    """
    out = []
    for p in _router_paths():
        if "*" in p or "pathMatch" in p:
            continue  # the catch-all matches everything; it is the 404 page
        body = re.sub(r":[A-Za-z_]+", "[^/]+", p.rstrip("/")) or ""
        out.append((p, re.compile(f"^{body}/?$")))
    out.sort(key=lambda pr: (pr[0].count(":"), -len(pr[0]), pr[0]))
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


def test_a_failed_dispatcher_lookup_reaches_the_callers_log(chat_db, monkeypatch, caplog):
    """The tech→dispatcher push used to wrap its role lookup in a bare
    ``except Exception: pass`` under a stale comment ("role tables not
    present in this tenant DB" — both are ORM tables). Since 2026-09-17 the
    failure propagates to send_job_chat, which logs it AFTER the message is
    committed, exactly as the dispatcher→tech branch always has."""
    db, job_id, sent = chat_db

    class _BrokenDb:
        def execute(self, *_a, **_k):
            raise RuntimeError("role lookup failed")

    msg = SimpleNamespace(id="m1", body="hi", sender_role="technician")
    with pytest.raises(RuntimeError, match="role lookup failed"):
        mobile_chat._push_other_party(_BrokenDb(), job_id=job_id, msg=msg, user=_TECH, request=_req())
    assert sent == []

    # …and the caller's net is what catches it: message saved (201), failure logged.
    def _push_boom(*_a, **_k):
        raise RuntimeError("push exploded")

    monkeypatch.setattr(mobile_chat, "_push_other_party", _push_boom)
    with caplog.at_level("ERROR", logger="gdx_dispatch.routers.mobile_chat"):
        _send(db, job_id, _TECH, "still delivered")
    assert any("mobile_chat_push_failed" in r.message for r in caplog.records), [
        r.message for r in caplog.records
    ]


def test_a_missing_push_module_is_logged_not_silent(chat_db, monkeypatch, caplog):
    """If the push module itself cannot be imported, chat keeps working
    in-app (unchanged) — but "no push, ever" now leaves a trace instead of a
    bare ``return``."""
    db, job_id, sent = chat_db
    # A None entry in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "gdx_dispatch.core.push_subscriptions", None)

    msg = SimpleNamespace(id="m2", body="hi", sender_role="technician")
    with caplog.at_level("ERROR", logger="gdx_dispatch.routers.mobile_chat"):
        mobile_chat._push_other_party(db, job_id=job_id, msg=msg, user=_TECH, request=_req())  # must not raise

    assert sent == []
    assert any("mobile_chat_push_unavailable" in r.message for r in caplog.records), [
        r.message for r in caplog.records
    ]
