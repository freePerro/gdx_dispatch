from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, log_audit_event, verify_audit_chain
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers import admin_ops, customers, stripe_connect


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    TenantBase.metadata.create_all(engine, checkfirst=True)

    db = Session()
    yield db
    db.close()
    engine.dispose()


def _request(tenant_id: str = "tenant-1", ip: str = "127.0.0.1", request_id: str = "req-1") -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": tenant_id}, request_id=request_id),
        client=SimpleNamespace(host=ip),
        headers={"x-request-id": request_id},
    )


def _seed_customer(db, name: str = "Alice") -> str:
    cust = Customer(
        name=name,
        email="alice@example.com",
        phone="555-0100",
        address="123 Main",
        customer_type="Retail",
        company_id="tenant-test",
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return str(cust.id)


@pytest.mark.anyio
async def test_audit_log_created_on_customer_create(db_session):
    out = await customers.create_customer(
        payload=customers.CustomerCreateIn(name="Alice", email="a@example.com"),
        request=_request(),
        _={"sub": "user-1"},
        db=db_session,
    )
    row = db_session.query(AuditLog).filter_by(entity_id=out.id).order_by(AuditLog.created_at.desc()).first()
    assert row is not None
    assert row.action == "customer_created"
    assert row.entity_type == "customer"


def test_audit_log_hash_chain_integrity(db_session):
    asyncio.run(
        log_audit_event(
            db=db_session,
            tenant_id="tenant-1",
            user_id="user-1",
            action="job_created",
            entity_type="job",
            entity_id="j-1",
            details={"step": 1},
        )
    )
    asyncio.run(
        log_audit_event(
            db=db_session,
            tenant_id="tenant-1",
            user_id="user-1",
            action="job_updated",
            entity_type="job",
            entity_id="j-1",
            details={"step": 2},
        )
    )
    assert verify_audit_chain(db_session, "job", "j-1") is True


def test_audit_log_immutable(db_session):
    asyncio.run(
        log_audit_event(
            db=db_session,
            tenant_id="tenant-1",
            user_id="user-1",
            action="settings_updated",
            entity_type="settings",
            entity_id="s-1",
            details={"a": 1},
        )
    )
    with pytest.raises(Exception):
        db_session.execute(text("DELETE FROM audit_logs"))
        db_session.commit()


def test_audit_log_captures_ip(db_session):
    asyncio.run(
        log_audit_event(
            db=db_session,
            tenant_id="tenant-1",
            user_id="user-1",
            action="customer_viewed",
            entity_type="customer",
            entity_id="c-1",
            ip_address="198.51.100.22",
            details={},
        )
    )
    row = db_session.query(AuditLog).filter_by(entity_id="c-1").first()
    assert row is not None
    assert row.ip_address == "198.51.100.22"


def test_audit_api_requires_admin(db_session):
    from gdx_dispatch.routers import audit as audit_router

    with pytest.raises(Exception):
        audit_router._require_admin({"role": "dispatcher", "sub": "u1"})


def test_audit_export_csv(db_session):
    from gdx_dispatch.routers import audit as audit_router

    asyncio.run(
        log_audit_event(
            db=db_session,
            tenant_id="tenant-1",
            user_id="u1",
            action="customer_created",
            entity_type="customer",
            entity_id="c-1",
            details={"name": "A"},
        )
    )
    resp = audit_router.export_audit_logs_csv(_={"role": "admin", "sub": "admin-1"}, db=db_session)
    body = resp.body.decode("utf-8")
    assert "text/csv" in resp.media_type
    assert "customer_created" in body


def test_audit_filter_by_entity(db_session):
    from gdx_dispatch.routers import audit as audit_router

    asyncio.run(log_audit_event(db=db_session, tenant_id="tenant-1", user_id="u1", action="customer_created", entity_type="customer", entity_id="c-1", details={}))
    asyncio.run(log_audit_event(db=db_session, tenant_id="tenant-1", user_id="u1", action="customer_updated", entity_type="customer", entity_id="c-2", details={}))
    body = audit_router.get_entity_audit_trail(
        entity_type="customer",
        entity_id="c-1",
        page=1,
        page_size=50,
        _={"role": "admin", "sub": "admin-1"},
        db=db_session,
    )
    assert len(body["items"]) >= 1
    assert all(i["entity_id"] == "c-1" for i in body["items"])


def test_audit_filter_by_user(db_session):
    from gdx_dispatch.routers import audit as audit_router

    asyncio.run(log_audit_event(db=db_session, tenant_id="tenant-1", user_id="u1", action="customer_created", entity_type="customer", entity_id="c-1", details={}))
    asyncio.run(log_audit_event(db=db_session, tenant_id="tenant-1", user_id="u2", action="customer_created", entity_type="customer", entity_id="c-2", details={}))
    body = audit_router.get_user_audit_trail(
        user_id="u1",
        page=1,
        page_size=50,
        _={"role": "admin", "sub": "admin-1"},
        db=db_session,
    )
    assert len(body["items"]) >= 1
    assert all(i["user_id"] == "u1" for i in body["items"])


def _build_middleware_app(db_session) -> TestClient:
    from gdx_dispatch.core.audit_middleware import AuditMiddleware

    app = FastAPI()
    SessionLocal = sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)

    @app.middleware("http")
    async def seed_tenant(request: Request, call_next):
        request.state.tenant = {"id": "tenant-1", "db_url": "sqlite://"}
        request.state.request_id = request.headers.get("x-request-id", "req-mw")
        return await call_next(request)

    app.add_middleware(AuditMiddleware, session_factory=SessionLocal)

    @app.post("/auth/login")
    async def login_ok():
        return {"ok": True}

    @app.post("/auth/login-fail")
    async def login_fail():
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=401, content={"detail": "bad creds"})

    @app.post("/auth/logout")
    async def logout_ok():
        return {"ok": True}

    return TestClient(app)


def _run_auth_middleware(
    db_session,
    *,
    path: str,
    status_code: int,
    request_id: str,
    forwarded_for: str | None = None,
) -> None:
    from gdx_dispatch.core.audit_middleware import AuditMiddleware

    SessionLocal = sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)
    app = FastAPI()
    middleware = AuditMiddleware(app=app, session_factory=SessionLocal)

    headers = [(b"x-request-id", request_id.encode("utf-8"))]
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("utf-8")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, receive)
    request.state.tenant = {"id": "tenant-1", "db_url": "sqlite://"}
    request.state.request_id = request_id

    async def call_next(_request: Request):
        return JSONResponse(status_code=status_code, content={"ok": status_code < 400})

    asyncio.run(middleware.dispatch(request, call_next))


def test_login_audit_logged(db_session):
    _run_auth_middleware(
        db_session,
        path="/auth/login",
        status_code=200,
        request_id="req-login",
        forwarded_for="203.0.113.7",
    )
    row = db_session.query(AuditLog).filter_by(action="login").order_by(AuditLog.created_at.desc()).first()
    assert row is not None
    assert row.request_id == "req-login"
    assert row.ip_address == "203.0.113.7"


def test_failed_login_audit_logged(db_session):
    _run_auth_middleware(
        db_session,
        path="/auth/login-fail",
        status_code=401,
        request_id="req-fail",
    )
    row = db_session.query(AuditLog).filter_by(action="failed_login").order_by(AuditLog.created_at.desc()).first()
    assert row is not None
    assert row.request_id == "req-fail"


@pytest.mark.anyio
async def test_gdpr_access_logged(db_session):
    customer_id = _seed_customer(db_session)
    _ = await customers.get_customer(customer_id=customer_id, request=_request(), _={"sub": "u1"}, db=db_session)
    row = db_session.query(AuditLog).filter_by(action="data_accessed", entity_id=customer_id).first()
    assert row is not None


def test_payment_event_logged(db_session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    req = _request()

    class FakeControlDB:
        tenant = SimpleNamespace(stripe_connect_account_id="acct_123")

    from unittest.mock import MagicMock, patch

    pi = MagicMock()
    pi.id = "pi_123"
    pi.client_secret = "sec"
    pi.application_fee_amount = 10
    with patch("gdx_dispatch.routers.stripe_connect.create_payment_intent", return_value=pi):
        stripe_connect.create_connect_payment_intent(
            body=stripe_connect.PaymentIntentRequest(account_id="acct_123", amount_cents=1000, fee_percent=1.0),
            request=req,
            _={"sub": "u1", "role": "admin"},
            tenant_db=db_session,
            control_db=FakeControlDB(),
        )

    row = db_session.query(AuditLog).filter_by(action="payment_intent_created", entity_type="stripe_payment_intent").first()
    assert row is not None


def test_logout_audit_logged(db_session):
    _run_auth_middleware(
        db_session,
        path="/auth/logout",
        status_code=200,
        request_id="req-logout",
    )
    row = db_session.query(AuditLog).filter_by(action="logout").order_by(AuditLog.created_at.desc()).first()
    assert row is not None
    assert row.request_id == "req-logout"


def test_gdpr_export_event_logged(db_session):
    admin_ops.full_export(_={"role": "admin", "sub": "admin-1"}, db=db_session)
    row = db_session.query(AuditLog).filter_by(action="data_exported", entity_type="tenant").first()
    assert row is not None


@pytest.mark.anyio
async def test_gdpr_deletion_event_logged(db_session):
    customer_id = _seed_customer(db_session)
    await customers.delete_customer(customer_id=customer_id, request=_request(), _={"sub": "u1"}, db=db_session)
    row = db_session.query(AuditLog).filter_by(action="data_deleted", entity_id=customer_id).first()
    assert row is not None


def test_audit_request_id_propagated(db_session):
    asyncio.run(
        log_audit_event(
            db=db_session,
            tenant_id="tenant-1",
            user_id="u1",
            action="settings_updated",
            entity_type="settings",
            entity_id="s-1",
            details={"ok": True},
            request_id="req-xyz",
        )
    )
    row = db_session.query(AuditLog).filter_by(entity_id="s-1").first()
    assert row is not None
    assert row.request_id == "req-xyz"
