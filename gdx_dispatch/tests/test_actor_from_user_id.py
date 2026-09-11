"""#701: who-did-it fields read claims the login dict never carries.

``get_current_user`` returns exactly ``{"user_id", "tenant_id", "role"}``. A
handler that recorded its actor from ``user.get("email")``, ``name`` or a bare
``sub`` stored NULL or a placeholder: 10/10 purchase orders and 7/19 payment
reminders on prod named nobody, and a server error "resolved by" ``system``
was resolved by a person.

Every request here carries that exact login shape — no ``sub``, no ``email``,
no ``name`` — so a handler that still reads one fails. Names come from the
users row, which is where they live.
"""
from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Every router driven below is imported at module load, before the fixture's
# create_all, so each model it touches is registered on TenantBase.metadata.
from gdx_dispatch.core import locations as locations_mod
from gdx_dispatch.core import recommendation_routes as recommendations_mod
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.bank_feeds import router as bank_feeds_mod
from gdx_dispatch.modules.error_sink import router as error_sink_mod
from gdx_dispatch.modules.forecasting import router as forecasting_mod
from gdx_dispatch.routers import ai as ai_mod
from gdx_dispatch.routers import change_orders as change_orders_mod
from gdx_dispatch.routers import collections as collections_mod
from gdx_dispatch.routers import messages as messages_mod
from gdx_dispatch.routers import purchase_orders as purchase_orders_mod
from gdx_dispatch.routers import tech_locations as tech_locations_mod
from gdx_dispatch.routers.auth import get_current_user

_LOGIN = Depends(get_current_user)
TENANT = "11111111-1111-1111-1111-111111111701"
# Exactly what finalize_login_jwt returns.
OFFICE = {"user_id": "00000000-0000-0000-0000-000000000711", "tenant_id": TENANT, "role": "admin"}
TECH = {"user_id": "00000000-0000-0000-0000-000000000712", "tenant_id": TENANT, "role": "technician"}
OFFICE_NAME = "Pat Office"


@pytest.fixture
def SessionLocal():
    from gdx_dispatch.models.tenant_models import User

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session_ = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with Session_() as db:
        db.add(User(id=UUID(OFFICE["user_id"]), email="office@example.com", name=OFFICE_NAME,
                    role="admin", company_id=TENANT, active=True))
        db.add(User(id=UUID(TECH["user_id"]), email="tech@example.com", name="Sam Tech",
                    role="technician", company_id=TENANT, active=True))
        db.commit()
    yield Session_
    engine.dispose()


def _client(SessionLocal, router, user: dict) -> TestClient:
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user

    app = FastAPI()
    app.include_router(router)

    def _like_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _like_get_db
    app.dependency_overrides[get_current_user] = lambda: user

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    return TestClient(app)


def _one(SessionLocal, model, **where):
    with SessionLocal() as db:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return db.execute(q).scalars().one()


# ── columns that hold the acting user's id ────────────────────────────────────


def test_a_purchase_order_names_its_creator(SessionLocal):
    """Prod: 10 of 10 purchase orders had created_by NULL."""
    r = _client(SessionLocal, purchase_orders_mod.router, OFFICE).post(
        "/api/purchase-orders", json={"vendor_name": "Door supply", "lines": [{"description": "Spring"}]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == OFFICE["user_id"]


def test_a_change_order_names_its_creator_and_approver(SessionLocal):
    client = _client(SessionLocal, change_orders_mod.router, OFFICE)
    created = client.post("/api/change-orders", json={"title": "Add opener", "amount": 250})
    assert created.status_code == 201, created.text
    assert created.json()["created_by"] == OFFICE["user_id"]
    approved = client.post(f"/api/change-orders/{created.json()['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_by"] == OFFICE["user_id"]


def test_a_manual_reminder_names_its_sender(SessionLocal):
    """Prod: the 7 manual sends of 19 reminders had sent_by NULL."""
    r = _client(SessionLocal, collections_mod.router, OFFICE).post(
        "/api/collections/reminders",
        json={"invoice_id": "22222222-2222-2222-2222-222222222701", "channel": "phone"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["sent_by"] == OFFICE["user_id"]


def test_a_manual_recurring_stream_names_its_creator(SessionLocal):
    from gdx_dispatch.modules.forecasting.models import RecurringStream

    r = _client(SessionLocal, forecasting_mod.router, OFFICE).post(
        "/api/forecast/recurring/streams",
        json={"label": "Truck lease", "payee_pattern": "ALLY", "amount_min": 400,
              "amount_max": 450, "cadence": "monthly", "start_date": "2026-09-01"},
    )
    assert r.status_code == 201, r.text
    stream = _one(SessionLocal, RecurringStream, label="Truck lease")
    assert stream.created_by_user_id == UUID(OFFICE["user_id"])


def test_a_manual_next_action_names_its_creator_and_reaches_their_queue(SessionLocal):
    """The key was always "anonymous": a manual action named nobody, and one
    assigned to a real user would have been hidden from every queue."""
    from gdx_dispatch.core.next_action import NextAction

    client = _client(SessionLocal, recommendations_mod.router, TECH)
    r = client.post("/api/next-actions", json={"title": "Call about the spring", "action_type": "follow_up"})
    assert r.status_code in (200, 201), r.text
    row = _one(SessionLocal, NextAction, title="Call about the spring")
    assert row.user_id == TECH["user_id"]
    queue = client.get("/api/next-actions")
    assert queue.status_code == 200, queue.text
    assert "Call about the spring" in [a.get("title") for a in queue.json()]


# ── columns that hold a display name ──────────────────────────────────────────


def test_a_team_message_carries_the_senders_name(SessionLocal):
    from gdx_dispatch.models.tenant_models import TeamMessage

    r = _client(SessionLocal, messages_mod.router, OFFICE).post(
        "/api/messages", json={"body": "Truck 2 is back", "recipient_ids": [TECH["user_id"]]},
    )
    assert r.status_code == 201, r.text
    assert _one(SessionLocal, TeamMessage, body="Truck 2 is back").sender_name == OFFICE_NAME


def test_a_resolved_server_error_names_the_person(SessionLocal):
    """Prod: one error resolved by a person reads resolved_by = 'system'."""
    with SessionLocal() as db:
        db.execute(text(
            "CREATE TABLE IF NOT EXISTS server_errors (id TEXT PRIMARY KEY, tenant_id TEXT, "
            "group_fingerprint TEXT, resolved_at TIMESTAMP, resolved_by TEXT, resolution_note TEXT)"
        ))
        db.execute(text("INSERT INTO server_errors (id, tenant_id) VALUES ('err-701', :t)"), {"t": TENANT})
        db.commit()
    client = _client(SessionLocal, error_sink_mod.router, OFFICE)
    r = client.patch("/api/admin/errors/err-701/resolve", json={"note": "fixed in the deploy"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        # The id: unique, and within the column's 64 characters on Postgres.
        assert db.execute(text("SELECT resolved_by FROM server_errors WHERE id='err-701'")).scalar() == OFFICE["user_id"]
    # The page shows the person.
    detail = client.get("/api/admin/errors/err-701")
    assert detail.status_code == 200, detail.text
    assert detail.json()["resolved_by"] == OFFICE_NAME


def test_an_in_person_signature_names_who_requested_it(SessionLocal):
    from gdx_dispatch.routers import signatures as signatures_mod
    from gdx_dispatch.routers.signatures import DocumentSignature

    r = _client(SessionLocal, signatures_mod.admin_router, OFFICE).post(
        "/api/signatures",
        json={"document_type": "estimate", "document_id": "est-701",
              "signature_data": "data:image/png;base64,AAAA", "signed_by": "Casey Customer"},
    )
    assert r.status_code == 201, r.text
    assert _one(SessionLocal, DocumentSignature, document_id="est-701").requested_by == OFFICE_NAME


def test_a_job_chat_message_carries_the_senders_name(SessionLocal):
    """Prod has no chat rows yet; every one would have had a NULL sender."""
    from gdx_dispatch.models.tenant_models import JobChatMessage
    from gdx_dispatch.routers import mobile_chat as mobile_chat_mod

    job_id = "33333333-3333-3333-3333-333333333701"
    with SessionLocal() as db:
        # Raw insert on purpose: the visibility check matches jobs.id as text.
        db.execute(text(
            "INSERT INTO jobs (id, title, lifecycle_stage, dispatch_status, billing_status, "
            "is_return_visit, company_id, created_at) VALUES "
            "(:id, 'Spring swap', 'scheduled', 'unassigned', 'unbilled', 0, :t, CURRENT_TIMESTAMP)"
        ), {"id": job_id, "t": TENANT})
        db.commit()
    r = _client(SessionLocal, mobile_chat_mod.router, OFFICE).post(
        f"/api/mobile/jobs/{job_id}/chat", json={"kind": "text", "body": "On my way"},
    )
    assert r.status_code == 201, r.text
    assert _one(SessionLocal, JobChatMessage, body="On my way").sender_name == OFFICE_NAME


def test_an_invite_names_who_sent_it(SessionLocal):
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.routers import admin_ops as admin_ops_mod

    r = _client(SessionLocal, admin_ops_mod.router, OFFICE).post(
        "/api/admin/users/invite", json={"email": "new.tech@example.com", "role": "technician"},
    )
    assert r.status_code == 201, r.text
    row = _one(SessionLocal, AuditLog, action="user_invited")
    assert row.details["invited_by"] == OFFICE_NAME


def test_a_server_error_on_a_require_role_route_names_the_user_and_shows_an_email(SessionLocal):
    """The sink read state.user alone; a require_role route leaves its
    principal on state.current_user. The page's User column read an email
    claim nobody carries — it now comes from the users row."""
    from gdx_dispatch.modules.error_sink import service as sink

    with SessionLocal() as db:
        db.execute(text(
            "CREATE TABLE IF NOT EXISTS server_errors (id TEXT PRIMARY KEY, tenant_id TEXT, "
            "request_id TEXT, method TEXT, path TEXT, status_code INTEGER, exception_class TEXT, "
            "exception_message TEXT, traceback TEXT, user_id TEXT, user_email TEXT, query_string TEXT, "
            "referer TEXT, user_agent TEXT, git_sha TEXT, group_fingerprint TEXT, occurred_at TIMESTAMP, "
            "resolved_at TIMESTAMP, resolved_by TEXT, resolution_note TEXT)"
        ))
        db.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(tenant={"id": TENANT}, current_user={"sub": TECH["user_id"], "role": "technician"}),
        method="GET", url=SimpleNamespace(path="/api/next-actions", query=""), headers={},
    )
    import gdx_dispatch.modules.error_sink.service as sink_mod

    original = sink_mod.SessionLocal
    sink_mod.SessionLocal = SessionLocal
    try:
        sink.record_server_error(request=request, exc=RuntimeError("boom"), status_code=500)
    finally:
        sink_mod.SessionLocal = original
    with SessionLocal() as db:
        assert db.execute(text("SELECT user_id FROM server_errors")).scalar() == TECH["user_id"]
    listed = _client(SessionLocal, error_sink_mod.router, OFFICE).get("/api/admin/errors")
    assert listed.status_code == 200, listed.text
    assert [i["user_email"] for i in listed.json()["items"]] == ["tech@example.com"]


def test_a_bank_line_expense_and_its_gl_entry_name_the_user(tenant_db, monkeypatch):
    """Money: the expense's GL entry and the match that records it named nobody
    — `sub` read off a login dict that has none."""
    from datetime import date

    from starlette.requests import Request as StarletteRequest

    from gdx_dispatch.modules.bank_feeds.statement_models import (
        TIE_OUT_PASSED,
        BankAccount,
        BankMatch,
        BankStatementImport,
        BankStatementLine,
    )
    from gdx_dispatch.modules.ledger.models import GlJournalEntry
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed

    monkeypatch.delenv("GDX_ENV", raising=False)
    db = tenant_db
    ensure_gl_seed(db, TENANT).ledger_posting_enabled = True
    account = BankAccount(name="Business Checking", kind="checking", institution="Bank", last4="2204")
    db.add(account)
    db.commit()
    imp = BankStatementImport(
        bank_account_id=account.id, file_sha256="1" * 64, period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31), beginning_balance_cents=0, ending_balance_cents=0,
        tie_out_status=TIE_OUT_PASSED,
    )
    db.add(imp)
    db.commit()
    line = BankStatementLine(
        bank_account_id=account.id, import_id=imp.id, txn_date=date(2026, 8, 15),
        amount_cents=-4200, description="MENARDS", section="debit", line_hash="b" * 64,
    )
    db.add(line)
    db.commit()
    request = StarletteRequest({"type": "http", "method": "POST", "path": "/", "headers": [],
                                "state": {"tenant": {"id": TENANT}}})
    out = bank_feeds_mod.create_expense_from_line(
        str(line.id),
        bank_feeds_mod.CreateExpenseFromLineIn(account_id=str(account.id), vendor="Menards", category="Fuel"),
        request, current_user=dict(OFFICE), _perm=None, db=db,
    )
    assert db.get(BankMatch, UUID(str(out["id"]))).created_by == OFFICE["user_id"]
    entry = db.execute(
        select(GlJournalEntry).where(GlJournalEntry.source_type == "expense",
                                     GlJournalEntry.source_id == str(out["expense_id"]))
    ).scalars().one()
    assert entry.created_by == OFFICE["user_id"]


def test_a_gdpr_data_access_names_who_read_the_record(SessionLocal):
    """Prod: all 4,065 gdpr_data_access_logs rows said user '-'."""
    from gdx_dispatch.core.data_access_logger import GDPRDataAccessMiddleware
    from gdx_dispatch.core.database import get_db

    app = FastAPI()
    app.add_middleware(GDPRDataAccessMiddleware)

    def _login(request: Request) -> dict:
        request.state.user = dict(OFFICE)  # what finalize_login_jwt stashes
        return dict(OFFICE)

    @app.get("/api/customers/{customer_id}")
    def read_customer(customer_id: str, user: dict = _LOGIN):
        return {"id": customer_id}

    def _like_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_current_user] = _login
    app.dependency_overrides[get_db] = _like_get_db

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    assert TestClient(app).get("/api/customers/cust-701").status_code == 200
    with SessionLocal() as db:
        rows = db.execute(text("SELECT user_id, entity_id FROM gdpr_data_access_logs")).all()
    assert [tuple(r) for r in rows] == [(OFFICE["user_id"], "cust-701")]


# ── identities built from the login dict ──────────────────────────────────────


def test_the_ai_principal_is_the_user_not_a_random_id():
    """`id or sub or uuid4()` — neither is carried, so every AI action was
    delegated by a fresh random id."""
    principal = ai_mod.get_current_principal_for_ai(user=dict(OFFICE))
    assert principal.identity_id == UUID(OFFICE["user_id"])


def test_bank_feed_actions_name_the_user():
    """Match confirm/reject/create and bank-line expenses (and their GL entry)
    read `sub` alone."""
    assert bank_feeds_mod._actor_id(dict(OFFICE)) == OFFICE["user_id"]


def test_a_tech_ping_carries_the_technician_row_id(SessionLocal):
    from gdx_dispatch.models.tenant_models import Technician

    with SessionLocal() as db:
        db.add(Technician(id="tech-row-701", company_id=TENANT, user_id=TECH["user_id"], active=True))
        db.commit()
        assert tech_locations_mod._technician_id(db, TECH["user_id"]) == "tech-row-701"
        assert tech_locations_mod._technician_id(db, OFFICE["user_id"]) is None


def test_a_technician_sees_the_locations_they_are_assigned(SessionLocal):
    """`user["sub"]` raised KeyError — a 500 for every non-admin."""
    from uuid import uuid4

    with SessionLocal() as db:
        mine, other = locations_mod.ServiceLocation(name="North shop"), locations_mod.ServiceLocation(name="South shop")
        for loc in (mine, other):
            loc.tenant_id = TENANT
            db.add(loc)
        db.flush()
        db.add(locations_mod.UserLocation(id=uuid4(), user_id=TECH["user_id"], location_id=mine.id))
        db.commit()
    r = _client(SessionLocal, locations_mod.router, TECH).get("/api/locations/")
    assert r.status_code == 200, r.text
    assert [loc["name"] for loc in r.json()] == ["North shop"]


def test_the_recommendation_key_reads_the_stashed_principal():
    """In production require_role stashes the RAW JWT claims — sub, no user_id."""
    raw_claims = {"sub": TECH["user_id"], "tenant_id": TENANT, "role": "technician", "jti": "j", "typ": "access"}
    request = SimpleNamespace(state=SimpleNamespace(current_user=raw_claims))
    assert recommendations_mod._get_user_id(request) == TECH["user_id"]
    login_dict_route = SimpleNamespace(state=SimpleNamespace(user=dict(TECH)))
    assert recommendations_mod._get_user_id(login_dict_route) == TECH["user_id"]
    assert recommendations_mod._get_user_id(SimpleNamespace(state=SimpleNamespace())) == "anonymous"
