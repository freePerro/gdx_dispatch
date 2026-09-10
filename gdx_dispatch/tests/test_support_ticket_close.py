"""Feedback tickets: the body is readable and a ticket can be closed (#622),
and the submission's audit row actually lands (#700's support instance).

The harness matters here. ``test_support_router.py`` hands every request the
SAME session and never closes it, so a row flushed after the commit survives
in that session and a test there cannot see it vanish. Production's
``get_db()`` yields a fresh session and closes it WITHOUT committing — so this
file does exactly that, per request, and reads results from a separate session.
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
from gdx_dispatch.models.tenant_models import SupportTicket

TENANT = "11111111-1111-1111-1111-111111111aaa"
# The shape prod's get_current_user returns (finalize_login_jwt): no `sub`, no
# `email` claim. Emails come from the users table, as they do on prod.
REPORTER = {"user_id": "00000000-0000-0000-0000-000000000aaa", "tenant_id": TENANT, "role": "technician"}
ADMIN = {"user_id": "00000000-0000-0000-0000-000000000bbb", "tenant_id": TENANT, "role": "admin"}
OTHER_TECH = {"user_id": "00000000-0000-0000-0000-000000000eee", "tenant_id": TENANT, "role": "technician"}
EMAILS = {
    REPORTER["user_id"]: "tech@example.com",
    ADMIN["user_id"]: "office@example.com",
    OTHER_TECH["user_id"]: "other.tech@example.com",
}


def _seed_users(Session) -> None:
    from uuid import UUID

    from gdx_dispatch.models.tenant_models import User

    with Session() as db:
        for uid, email in EMAILS.items():
            role = "admin" if uid == ADMIN["user_id"] else "technician"
            db.add(User(id=UUID(uid), email=email, role=role, company_id=TENANT, active=True))
        db.commit()


@pytest.fixture
def SessionLocal():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _seed_users(Session)
    yield Session
    engine.dispose()


def _client(SessionLocal, user: dict) -> TestClient:
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user
    from gdx_dispatch.routers.support import router

    app = FastAPI()
    app.include_router(router)

    def _like_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()  # no commit — exactly what core.database.get_db does

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _like_get_db

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    return TestClient(app)


def _file_bug(SessionLocal) -> str:
    r = _client(SessionLocal, REPORTER).post(
        "/api/support/bug",
        json={
            "subject": "Save button does nothing",
            "body": "Clicked Save on the estimate, nothing happened.\n\n---\nPage: /estimates/123\nBrowser: Chrome",
            "priority": "high",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["ticket_id"]


def _audit_rows(SessionLocal, ticket_id: str) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.execute(
            select(AuditLog).where(AuditLog.entity_type == "support_ticket", AuditLog.entity_id == ticket_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).scalars())


def test_a_submission_leaves_an_audit_row(SessionLocal):
    """#700: the row used to be flushed AFTER the commit, and get_db() closes
    without committing — 7 tickets on prod, 0 audit rows."""
    ticket_id = _file_bug(SessionLocal)
    rows = _audit_rows(SessionLocal, ticket_id)
    assert [r.action for r in rows] == ["create"]
    assert rows[0].user_id == REPORTER["user_id"]
    assert rows[0].details["subject"] == "Save button does nothing"


def test_the_team_reads_what_the_reporter_wrote(SessionLocal):
    ticket_id = _file_bug(SessionLocal)
    items = _client(SessionLocal, ADMIN).get("/api/support/my").json()["items"]
    row = next(i for i in items if i["id"] == ticket_id)
    assert row["body"].startswith("Clicked Save on the estimate")
    assert "Page: /estimates/123" in row["body"]
    assert row["opened_by_email"] == "tech@example.com"


def test_a_reporter_reads_their_own_report(SessionLocal):
    ticket_id = _file_bug(SessionLocal)
    items = _client(SessionLocal, REPORTER).get("/api/support/my").json()["items"]
    row = next(i for i in items if i["id"] == ticket_id)
    assert row["body"].startswith("Clicked Save on the estimate")


def test_anyone_else_sees_the_list_but_not_the_report(SessionLocal):
    """#622 audit: /my answers every signed-in role, and a body can carry
    customer details (the bug button files from any page). Another tech sees
    the row as the list always showed it — no body, no reporter."""
    ticket_id = _file_bug(SessionLocal)
    r = _client(SessionLocal, OTHER_TECH).get("/api/support/my")
    assert r.status_code == 200
    row = next(i for i in r.json()["items"] if i["id"] == ticket_id)
    assert row["subject"] == "Save button does nothing"
    assert row["body"] is None
    assert row["opened_by_email"] is None


def test_an_admin_closes_a_ticket_with_a_resolution(SessionLocal):
    ticket_id = _file_bug(SessionLocal)
    r = _client(SessionLocal, ADMIN).post(
        f"/api/support/tickets/{ticket_id}/close",
        json={"resolution_summary": "  Fixed in v1.118.8 — the save handler no longer swallows the 422.  "},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "closed"
    assert body["closed_at"]
    assert body["resolution_summary"] == "Fixed in v1.118.8 — the save handler no longer swallows the 422."

    with SessionLocal() as db:
        row = db.get(SupportTicket, ticket_id)
        assert row.status == "closed" and row.closed_at is not None

    closed = [r for r in _audit_rows(SessionLocal, ticket_id) if r.action == "support_ticket_closed"]
    assert len(closed) == 1
    assert closed[0].user_id == ADMIN["user_id"]
    assert closed[0].details["from_status"] == "open"
    assert closed[0].details["resolution_summary"].startswith("Fixed in v1.118.8")


def test_a_reporter_cannot_close_a_ticket(SessionLocal):
    ticket_id = _file_bug(SessionLocal)
    r = _client(SessionLocal, REPORTER).post(
        f"/api/support/tickets/{ticket_id}/close", json={"resolution_summary": "nevermind"}
    )
    assert r.status_code == 403
    with SessionLocal() as db:
        assert db.get(SupportTicket, ticket_id).status == "open"


def test_closing_needs_a_resolution_and_happens_once(SessionLocal):
    ticket_id = _file_bug(SessionLocal)
    admin = _client(SessionLocal, ADMIN)
    assert admin.post(f"/api/support/tickets/{ticket_id}/close", json={"resolution_summary": ""}).status_code == 422
    assert admin.post(f"/api/support/tickets/{ticket_id}/close", json={"resolution_summary": "   "}).status_code == 422
    assert admin.post(f"/api/support/tickets/{ticket_id}/close", json={"resolution_summary": "Done."}).status_code == 200
    again = admin.post(f"/api/support/tickets/{ticket_id}/close", json={"resolution_summary": "Done again."})
    assert again.status_code == 409
    with SessionLocal() as db:
        assert db.get(SupportTicket, ticket_id).resolution_summary == "Done."


def test_closing_an_unknown_ticket_is_404(SessionLocal):
    r = _client(SessionLocal, ADMIN).post(
        "/api/support/tickets/00000000-0000-0000-0000-000000000000/close",
        json={"resolution_summary": "x"},
    )
    assert r.status_code == 404


def _add_user(SessionLocal, user_id: str, email: str) -> None:
    from uuid import UUID

    from gdx_dispatch.models.tenant_models import User

    with SessionLocal() as db:
        db.add(User(id=UUID(user_id), email=email, company_id=TENANT))
        db.commit()


def test_a_reporter_without_an_email_claim_is_named_from_their_user_row(SessionLocal):
    """The JWT carries no email claim, so 5 of the 7 tickets on prod were filed
    as anonymous@unknown — while each carried the reporter's user id."""
    no_claim = {"user_id": "00000000-0000-0000-0000-000000000ccc", "tenant_id": TENANT, "role": "technician"}
    _add_user(SessionLocal, no_claim["user_id"], "real.tech@example.com")
    r = _client(SessionLocal, no_claim).post(
        "/api/support/bug", json={"subject": "Map will not load", "body": "Blank map on the dispatch board."}
    )
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        assert db.get(SupportTicket, r.json()["ticket_id"]).opened_by_email == "real.tech@example.com"


def test_tickets_already_filed_as_anonymous_show_their_reporter(SessionLocal):
    """Display-time, so the rows on prod read correctly without being rewritten."""
    from datetime import UTC, datetime

    reporter_id = "00000000-0000-0000-0000-000000000ddd"
    _add_user(SessionLocal, reporter_id, "office@example.com")
    with SessionLocal() as db:
        db.add(SupportTicket(
            id="legacy-1", tenant_id=TENANT, opened_by_email="anonymous@unknown",
            opened_by_user_id=reporter_id, subject="Old one", body="Old body text",
            category="bug", priority="medium", status="open", created_at=datetime.now(UTC),
        ))
        db.commit()
    items = _client(SessionLocal, ADMIN).get("/api/support/my").json()["items"]
    assert next(i for i in items if i["id"] == "legacy-1")["opened_by_email"] == "office@example.com"
    with SessionLocal() as db:
        assert db.get(SupportTicket, "legacy-1").opened_by_email == "anonymous@unknown"  # not rewritten


def test_two_people_closing_at_once_do_not_both_win(tmp_path, monkeypatch):
    """#622 audit: the close read the status, then wrote it. If a colleague
    closed the ticket in between, both got 200 and the second resolution
    silently replaced the first. Now the loser gets a 409 and the first
    resolution stands. File-backed so the colleague's session is a separate
    connection that really commits between this request's read and write."""
    from sqlalchemy.orm import Session as _Session

    engine = create_engine(f"sqlite:///{tmp_path / 'race.sqlite3'}", connect_args={"check_same_thread": False})
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _seed_users(Session)
    ticket_id = _file_bug(Session)

    real_get = _Session.get
    raced = {"done": False}

    def get_then_colleague_closes(self, entity, ident, *a, **kw):
        obj = real_get(self, entity, ident, *a, **kw)
        if entity is SupportTicket and not raced["done"]:
            raced["done"] = True
            with Session() as other:
                row = real_get(other, SupportTicket, ident)
                row.status, row.resolution_summary = "closed", "Colleague got there first"
                other.commit()
        return obj

    monkeypatch.setattr(_Session, "get", get_then_colleague_closes)
    r = _client(Session, ADMIN).post(
        f"/api/support/tickets/{ticket_id}/close", json={"resolution_summary": "Mine"}
    )
    monkeypatch.undo()
    assert raced["done"]
    assert r.status_code == 409, r.text
    with Session() as db:
        assert db.get(SupportTicket, ticket_id).resolution_summary == "Colleague got there first"
    assert not [x for x in _audit_rows(Session, ticket_id) if x.action == "support_ticket_closed"]
    engine.dispose()
