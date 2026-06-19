from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.ai_usage_logger import log_ai_usage
from gdx_dispatch.core.ai_usage_logger import router as ai_usage_router
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.data_access_logger import (
    GDPRDataAccessMiddleware,
    log_data_access,
)
from gdx_dispatch.core.data_access_logger import (
    router as gdpr_router,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.log_format import build_log_entry
from gdx_dispatch.core.log_shipper import LogShipper
from gdx_dispatch.core.performance import (
    SlowEndpointMiddleware,
    get_performance_logger,
    reset_performance_logger,
)
from gdx_dispatch.core.performance import (
    router as performance_router,
)
from gdx_dispatch.core.security_logger import log_security_event
from gdx_dispatch.core.security_logger import router as security_router
from gdx_dispatch.core.webhook_logger import log_webhook_delivery
from gdx_dispatch.core.webhook_logger import router as webhook_router


@pytest.fixture()
def tenant_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = SessionLocal()
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT,
                company_id TEXT,
                deleted_at TIMESTAMP
            )
            """
        )
    )
    db.execute(text("INSERT INTO customers (id, name, company_id, deleted_at) VALUES ('cust-1', 'Acme', 'tenant-test', NULL)"))
    db.commit()
    db.close()
    yield engine
    engine.dispose()


@pytest.fixture()
def tenant_db_session(tenant_engine) -> Session:
    SessionLocal = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _reset_performance_state() -> None:
    reset_performance_logger()


@pytest.fixture()
def client(tenant_engine) -> TestClient:
    SessionLocal = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    app = FastAPI()

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", "req-test")
        request.state.tenant = {
            "id": request.headers.get("x-tenant-id", "tenant-1"),
            "db_url": "sqlite://",
        }
        response = await call_next(request)
        return response

    app.add_middleware(SlowEndpointMiddleware, threshold_ms=1)
    app.add_middleware(GDPRDataAccessMiddleware)

    @app.get("/api/customers/{customer_id}")
    async def _get_customer(customer_id: str):
        return {"id": customer_id}

    @app.get("/slow")
    async def _slow_endpoint():
        time.sleep(0.01)
        return {"ok": True}

    app.include_router(ai_usage_router)
    app.include_router(performance_router)
    app.include_router(security_router)
    app.include_router(webhook_router)
    app.include_router(gdpr_router)

    def _override_tenant_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _override_user(request: Request):
        return {
            "user_id": request.headers.get("x-user-id", "user-1"),
            "tenant_id": request.headers.get("x-tenant-id", "tenant-1"),
            "role": request.headers.get("x-role", "admin"),
        }

    app.dependency_overrides[get_db] = _override_tenant_db
    app.dependency_overrides[get_current_user] = _override_user

    return TestClient(app, raise_server_exceptions=True)


# 1

def test_ai_usage_logged_with_cost(tenant_db_session: Session):
    row = log_ai_usage(
        tenant_db_session,
        tenant_id="tenant-1",
        user_id="user-1",
        task="general",
        model="claude-sonnet-4-20250514",
        input_tokens=120,
        output_tokens=80,
        cost_usd=0.0042,
        latency_ms=220,
        request_id="req-ai-1",
    )
    assert row["tenant_id"] == "tenant-1"
    assert row["cost_usd"] == pytest.approx(0.0042)


# 2

def test_ai_usage_aggregation_by_tenant(client: TestClient, tenant_db_session: Session):
    log_ai_usage(tenant_db_session, "tenant-a", "u1", "general", "m1", 10, 5, 0.01, 50, request_id="r1")
    log_ai_usage(tenant_db_session, "tenant-a", "u1", "general", "m1", 20, 10, 0.02, 55, request_id="r2")
    log_ai_usage(tenant_db_session, "tenant-b", "u2", "general", "m2", 99, 1, 0.10, 66, request_id="r3")

    response = client.get("/api/ai/usage", headers={"x-tenant-id": "tenant-a"})
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "tenant-a"
    assert data["totals"]["tokens"] == 45
    assert data["totals"]["cost_usd"] == pytest.approx(0.03)


# 3

def test_slow_query_detected_and_logged(tenant_db_session: Session):
    perf = get_performance_logger()
    perf.log_slow_query(
        tenant_id="tenant-1",
        request_id="req-sq-1",
        sql="SELECT * FROM customers WHERE id = :id",
        params={"id": "cust-1"},
        duration_ms=777,
    )
    events = perf.snapshot()
    assert any(e.get("event_type") == "slow_query" and e.get("duration_ms") == 777 for e in events)


# 4

def test_slow_endpoint_detected(client: TestClient):
    response = client.get("/slow", headers={"x-tenant-id": "tenant-1", "x-request-id": "req-end-1"})
    assert response.status_code == 200
    events = get_performance_logger().snapshot()
    assert any(e.get("event_type") == "slow_endpoint" and e.get("path") == "/slow" for e in events)


# 5

def test_security_event_password_change(tenant_db_session: Session):
    row = log_security_event(
        tenant_db_session,
        tenant_id="tenant-1",
        user_id="user-1",
        event_type="password_changed",
        details={"source": "settings"},
        ip_address="1.2.3.4",
        request_id="req-sec-1",
    )
    assert row["event_type"] == "password_changed"
    assert row["ip_address"] == "1.2.3.4"


# 6

def test_security_alert_on_5_failed_logins(tenant_db_session: Session):
    for idx in range(5):
        log_security_event(
            tenant_db_session,
            tenant_id="tenant-1",
            user_id=f"user-{idx}",
            event_type="login_failed",
            details={"reason": "bad_password"},
            ip_address="5.6.7.8",
            request_id=f"req-fail-{idx}",
        )

    rows = tenant_db_session.execute(
        text("SELECT event_type, details FROM security_events ORDER BY created_at ASC")
    ).mappings().all()
    assert any(r["event_type"] == "security_alert" for r in rows)


# 7

def test_webhook_delivery_success_logged(tenant_db_session: Session):
    row = log_webhook_delivery(
        tenant_db_session,
        tenant_id="tenant-1",
        webhook_id="wh-1",
        url="https://example.com/hook",
        status_code=200,
        response_time_ms=123,
        attempt=1,
        error=None,
        request_id="req-wh-1",
    )
    assert row["delivery_status"] == "sent"


# 8

def test_webhook_delivery_failure_logged(tenant_db_session: Session):
    row = log_webhook_delivery(
        tenant_db_session,
        tenant_id="tenant-1",
        webhook_id="wh-2",
        url="https://example.com/hook",
        status_code=500,
        response_time_ms=200,
        attempt=2,
        error="timeout",
        request_id="req-wh-2",
    )
    assert row["delivery_status"] == "retried"


# 9

def test_gdpr_access_logged_on_customer_view(client: TestClient):
    response = client.get("/api/customers/cust-1", headers={"x-tenant-id": "tenant-1", "x-user-id": "user-9"})
    assert response.status_code == 200

    access_logs = client.get("/api/admin/gdpr/access-log", headers={"x-role": "admin"})
    assert access_logs.status_code == 200
    items = access_logs.json()["items"]
    assert any(item["entity_type"] == "customers" and item["entity_id"] == "cust-1" for item in items)


# 10

def test_gdpr_access_export_csv(client: TestClient, tenant_db_session: Session):
    log_data_access(
        tenant_db_session,
        tenant_id="tenant-1",
        user_id="user-1",
        entity_type="customers",
        entity_id="cust-1",
        access_type="export",
        fields_accessed=["name", "email"],
        request_id="req-gdpr-csv",
    )
    response = client.get("/api/admin/gdpr/access-log/export", headers={"x-role": "admin"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "tenant_id,user_id,entity_type" in response.text


# 11

def test_log_shipper_s3_not_configured_graceful():
    shipper = LogShipper(target="s3", s3_bucket="")
    ok = shipper.ship([{"message": "hello"}])
    assert ok is False


# 12

def test_log_format_is_json():
    entry = build_log_entry(
        level="INFO",
        logger="gdx_dispatch.audit",
        request_id="req-json-1",
        tenant_id="tenant-1",
        user_id="user-1",
        action="customer_created",
        entity_type="customer",
        entity_id="cust-1",
        duration_ms=45,
        details={"name": "Acme"},
        timestamp=datetime(2026, 4, 3, 10, 0, 0, tzinfo=UTC),
    )
    encoded = json.dumps(entry)
    decoded = json.loads(encoded)
    assert decoded["timestamp"] == "2026-04-03T10:00:00Z"


# 13

def test_all_loggers_include_request_id(tenant_db_session: Session):
    ai = log_ai_usage(tenant_db_session, "tenant-1", "u1", "general", "m", 1, 1, 0.0, 10, request_id="req-all-1")
    sec = log_security_event(
        tenant_db_session,
        "tenant-1",
        "u1",
        "password_changed",
        details={},
        ip_address="1.1.1.1",
        request_id="req-all-2",
    )
    wh = log_webhook_delivery(
        tenant_db_session,
        "tenant-1",
        "wh-1",
        "https://e.com",
        200,
        10,
        1,
        None,
        request_id="req-all-3",
    )
    gdpr = log_data_access(
        tenant_db_session,
        "tenant-1",
        "u1",
        "customers",
        "cust-1",
        "view",
        ["name"],
        request_id="req-all-4",
    )
    assert ai["request_id"] and sec["request_id"] and wh["request_id"] and gdpr["request_id"]


# 14

def test_all_loggers_include_tenant_id(tenant_db_session: Session):
    ai = log_ai_usage(tenant_db_session, "tenant-xyz", "u1", "general", "m", 1, 1, 0.0, 10, request_id="req1")
    sec = log_security_event(
        tenant_db_session,
        "tenant-xyz",
        "u1",
        "password_changed",
        details={},
        ip_address="1.1.1.1",
        request_id="req2",
    )
    wh = log_webhook_delivery(
        tenant_db_session,
        "tenant-xyz",
        "wh-1",
        "https://e.com",
        200,
        10,
        1,
        None,
        request_id="req3",
    )
    gdpr = log_data_access(
        tenant_db_session,
        "tenant-xyz",
        "u1",
        "customers",
        "cust-1",
        "view",
        ["name"],
        request_id="req4",
    )
    assert ai["tenant_id"] == sec["tenant_id"] == wh["tenant_id"] == gdpr["tenant_id"] == "tenant-xyz"


# 15

def test_performance_dashboard_returns_data(client: TestClient):
    perf = get_performance_logger()
    perf.log_slow_query("tenant-1", "req-perf-1", "SELECT 1", {}, 999)
    perf.log_slow_endpoint("tenant-1", "req-perf-2", "/demo", 3456)

    response = client.get("/api/admin/performance", headers={"x-role": "admin"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["slow_queries"]
    assert payload["slow_endpoints"]
