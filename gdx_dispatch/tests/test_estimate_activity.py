"""GET /api/estimates/{id}/activity — the audit trail, on the estimate.

estimate-rejection-visibility plan, PR 1. Pins: the whitelist (patch noise
stays out), scoping by estimate (another estimate's rows never leak in),
machine actors read as what they are, and the status context — the bounce
row behind "Failed Email", the decline behind "Declined" — is served from a
dedicated lookup, not from whatever fits on the first page.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, User
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router

NOW = datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, company_id TEXT, "
        "module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT, UNIQUE(company_id, module_key))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
        "VALUES ('g2', 'tenant-test', 'estimates', datetime('now'), datetime('now'))"
    ))
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1", "role": "admin", "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=True)
    tc.session_factory = Session
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _db(client):
    return client.session_factory()


def _mk_estimate(client, *, status="sent", declined_reason=None, declined_at=None):
    db = _db(client)
    try:
        cust = Customer(name="Farm Co", email="farm@example.com", company_id="tenant-test")
        db.add(cust)
        db.commit()
        est = Estimate(
            estimate_number=f"EST-{uuid4().hex[:6]}", customer_id=cust.id, status=status,
            declined_reason=declined_reason, declined_at=declined_at,
            company_id="tenant-test", public_token=uuid4().hex,
        )
        db.add(est)
        db.commit()
        return str(est.id)
    finally:
        db.close()


def _audit(client, *, entity_id, action, user_id="user-1", details=None, at=NOW,
           tenant_id=None, entity_type="estimate"):
    """Write a row the way the app does — tenant_id=None is what every
    estimate writer produces on prod (1,947 of 2,092 rows, 2026-08-31)."""
    db = _db(client)
    try:
        db.add(AuditLog(
            tenant_id=tenant_id, user_id=user_id, action=action, entity_type=entity_type,
            entity_id=str(entity_id), details=details or {}, created_at=at,
        ))
        db.commit()
    finally:
        db.close()


def _mk_user(client, name="Pat Office"):
    db = _db(client)
    try:
        u = User(email=f"{uuid4().hex[:6]}@example.com", name=name, role="admin",
                 company_id="tenant-test", password_hash="x")
        db.add(u)
        db.commit()
        return str(u.id)
    finally:
        db.close()


def test_404_for_unknown_estimate(client):
    assert client.get(f"/api/estimates/{uuid4()}/activity").status_code == 404


def test_whitelist_and_scope(client):
    """Patch noise stays out; another estimate's rows never appear; the
    tenant_id-NULL rows the app actually writes are served."""
    est = _mk_estimate(client)
    other = _mk_estimate(client)
    _audit(client, entity_id=est, action="estimate_created", at=NOW - timedelta(hours=2))
    _audit(client, entity_id=est, action="patch_estimate", at=NOW - timedelta(hours=1))
    _audit(client, entity_id=est, action="patch_estimate", at=NOW - timedelta(minutes=50))
    _audit(client, entity_id=est, action="estimate_marked_sent",
           details={"channel": "manual", "status": "sent"}, at=NOW - timedelta(minutes=30))
    _audit(client, entity_id=other, action="estimate_marked_sent", at=NOW - timedelta(minutes=10))
    _audit(client, entity_id=est, action="estimate_created", entity_type="invoice",
           at=NOW - timedelta(minutes=5))  # same id string, different entity type

    r = client.get(f"/api/estimates/{est}/activity")
    assert r.status_code == 200, r.text
    body = r.json()
    actions = [i["action"] for i in body["items"]]
    assert actions == ["estimate_marked_sent", "estimate_created"]  # newest first, no patch rows
    assert body["total"] == 2
    labels = {i["action"]: i["label"] for i in body["items"]}
    assert labels["estimate_marked_sent"] == "Marked sent"
    assert body["context"] == {"bounce": None, "decline": None}
    # The limit bounds the page, the total does not shrink with it.
    r2 = client.get(f"/api/estimates/{est}/activity?limit=1")
    assert [i["action"] for i in r2.json()["items"]] == ["estimate_marked_sent"]
    assert r2.json()["total"] == 2


def test_bounce_context_and_machine_actor(client):
    """A rejected estimate serves the bounce row that made it so — its
    failed recipient and date — and the detector reads as a system actor,
    not as an API key called 'bounce-detector'."""
    est = _mk_estimate(client, status="rejected")
    _audit(client, entity_id=est, action="estimate_marked_sent", at=NOW - timedelta(hours=11))
    _audit(client, entity_id=est, action="estimate_email_rejected", user_id="bounce-detector",
           details={"failed_recipient": "bjfarms1888@example.com",
                    "ndr_subject": "Undeliverable: Garage door",
                    "ndr_graph_message_id": "AAMk", "matched_by": "conversation_time"},
           at=NOW)
    # Plenty of later whitelisted rows: the context lookup must not depend
    # on the bounce row fitting on the page.
    for i in range(5):
        _audit(client, entity_id=est, action="estimate_attachment_uploaded",
               at=NOW + timedelta(minutes=i + 1))

    r = client.get(f"/api/estimates/{est}/activity?limit=3")
    body = r.json()
    assert [i["action"] for i in body["items"]] == ["estimate_attachment_uploaded"] * 3
    assert body["context"]["bounce"] == {
        "failed_recipient": "bjfarms1888@example.com",
        "ndr_subject": "Undeliverable: Garage door",
        "matched_by": "conversation_time",
        "at": NOW.isoformat(),
    }
    assert body["context"]["decline"] is None

    full = client.get(f"/api/estimates/{est}/activity").json()
    bounce_item = next(i for i in full["items"] if i["action"] == "estimate_email_rejected")
    assert bounce_item["user_name"] == "System — email bounce detector"
    assert bounce_item["actor_type"] == "system"
    assert bounce_item["label"].startswith("Email bounced")


def test_bounce_context_only_while_rejected(client):
    """Once the estimate is re-sent (rejected → sent) the old bounce is
    history, not context: the banner must not survive the recovery."""
    est = _mk_estimate(client, status="sent")
    _audit(client, entity_id=est, action="estimate_email_rejected", user_id="bounce-detector",
           details={"failed_recipient": "bad@example.com"}, at=NOW - timedelta(days=1))
    _audit(client, entity_id=est, action="estimate_sent", at=NOW)
    body = client.get(f"/api/estimates/{est}/activity").json()
    assert body["context"]["bounce"] is None
    assert [i["action"] for i in body["items"]] == ["estimate_sent", "estimate_email_rejected"]


def test_decline_context_names_reason_and_actor(client):
    uid = _mk_user(client, name="Pat Office")
    when = NOW - timedelta(days=3)
    est = _mk_estimate(client, status="declined", declined_reason="Went with a cheaper quote",
                       declined_at=when)
    _audit(client, entity_id=est, action="estimate_declined", user_id=uid,
           details={"reason": "Went with a cheaper quote"}, at=when)
    body = client.get(f"/api/estimates/{est}/activity").json()
    assert body["context"]["decline"] == {
        "reason": "Went with a cheaper quote",
        "at": when.isoformat(),
        "user_name": "Pat Office",
        "actor_type": "staff",
    }
    assert body["context"]["bounce"] is None


def test_public_link_and_portal_actors_read_as_the_customer(client):
    """The emailed-link decline is written as user_id="customer:public-link"
    and a portal decline as "portal:<CustomerUser id>" (neither is a staff
    row). The shared resolver would echo those as API keys; here they read
    as the customer, with the portal login's email when the row exists."""
    from gdx_dispatch.modules.customer_portal.models import CustomerUser

    est = _mk_estimate(client, status="declined")
    db = _db(client)
    try:
        from uuid import UUID as _UUID
        cust_id = db.execute(select(Estimate.customer_id).where(Estimate.id == _UUID(est))).scalar_one()
        cu = CustomerUser(customer_id=cust_id, email="pat@farm.example")
        db.add(cu)
        db.commit()
        cu_id = str(cu.id)
    finally:
        db.close()

    _audit(client, entity_id=est, action="public_estimate_declined", user_id="customer:public-link",
           details={"reason": "Too expensive"}, at=NOW - timedelta(days=2))
    _audit(client, entity_id=est, action="portal_estimate_declined", user_id=f"portal:{cu_id}",
           details={"customer_id": str(cust_id), "reason": "Went elsewhere"}, at=NOW - timedelta(days=1))
    _audit(client, entity_id=est, action="portal_estimate_declined", user_id=f"portal:{uuid4()}",
           details={"reason": "Deleted login"}, at=NOW)

    body = client.get(f"/api/estimates/{est}/activity").json()
    by_time = body["items"]  # newest first
    assert [i["user_name"] for i in by_time] == [
        "Customer (portal)",             # login row gone → generic
        "pat@farm.example (portal)",     # resolved portal login
        "Customer (email link)",         # public-link decline
    ]
    assert {i["actor_type"] for i in by_time} == {"customer"}
    assert by_time[2]["label"] == "Declined by customer (email link)"
    # context.decline = the LATEST decline, reason from details when the
    # column is empty.
    ctx = body["context"]["decline"]
    assert ctx["reason"] == "Deleted login"
    assert ctx["user_name"] == "Customer (portal)"
    assert ctx["actor_type"] == "customer"
