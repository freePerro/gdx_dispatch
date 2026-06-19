"""SS-28 slice C tests — ConsumerAuditMiddleware.

Starlette TestClient + an SS-28 SQLite session factory. Verifies:
* A tenant-scoped request writes exactly one audit row
* The row reflects method + path + status in the action/details
* Skip prefixes bypass audit cleanly
* No tenant on request.state → no row, no error
* Fail-closed: if the audit write raises, response becomes 500
  {"error":"audit_write_failed"}
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gdx_dispatch.core.middleware.consumer_audit_middleware import (
    ConsumerAuditMiddleware,
)
from gdx_dispatch.models.platform_ss28_additions import (
    SS28Base,
    PlatformConsumerAudit,
)

TENANT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PRINCIPAL_ID = "p1"


@pytest.fixture
def db_factory():
    # Shared in-memory SQLite across threads — TestClient runs the ASGI
    # handler in a worker thread, the fixture reads results in the main
    # thread. StaticPool + check_same_thread=False keeps both on the
    # same connection.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS28Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine))
    yield Session
    Session.remove()
    engine.dispose()


def _app(db_factory, *, tenant_id=TENANT_UUID, principal_id=PRINCIPAL_ID, handler=None):
    async def default_handler(request: Request):
        return JSONResponse({"ok": True})

    async def tenant_setter(request: Request, call_next):
        if tenant_id is not None:
            request.state.tenant_id = tenant_id
            request.state.principal_identity_id = principal_id
        return await call_next(request)

    routes = [Route("/api/test", handler or default_handler)]
    routes.append(Route("/healthz", lambda r: PlainTextResponse("ok")))

    async def raise_handler(request: Request):
        raise RuntimeError("boom")
    routes.append(Route("/api/boom", raise_handler))

    from starlette.middleware.base import BaseHTTPMiddleware

    class TenantSetter(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if tenant_id is not None:
                request.state.tenant_id = tenant_id
                request.state.principal_identity_id = principal_id
            return await call_next(request)

    return Starlette(
        routes=routes,
        middleware=[
            # Order matters: the audit middleware runs OUTSIDE (sees state
            # set by tenant_setter which runs INSIDE).
            Middleware(
                ConsumerAuditMiddleware,
                db_session_factory=db_factory,
            ),
            Middleware(TenantSetter),
        ],
    )


def _audit_count(db_factory):
    s = db_factory()
    try:
        return s.query(PlatformConsumerAudit).count()
    finally:
        s.close()


def _audit_rows(db_factory):
    s = db_factory()
    try:
        return s.query(PlatformConsumerAudit).all()
    finally:
        s.close()


def test_writes_one_row_for_tenant_request(db_factory):
    app = _app(db_factory)
    client = TestClient(app)
    r = client.get("/api/test")
    assert r.status_code == 200
    assert _audit_count(db_factory) == 1
    row = _audit_rows(db_factory)[0]
    import uuid as _uuid
    assert row.tenant_id == _uuid.UUID(TENANT_UUID)
    assert row.principal_identity_id == PRINCIPAL_ID
    assert "GET /api/test" in row.action
    assert row.result == "ok"
    assert row.resource_id == "/api/test"


def test_skip_prefix_bypasses_audit(db_factory):
    app = _app(db_factory)
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert _audit_count(db_factory) == 0


def test_no_tenant_no_row(db_factory):
    app = _app(db_factory, tenant_id=None)
    client = TestClient(app)
    r = client.get("/api/test")
    assert r.status_code == 200
    assert _audit_count(db_factory) == 0


def test_non_2xx_records_denied_or_error(db_factory):
    from starlette.responses import Response

    async def h(request):
        return Response(status_code=403)

    app = _app(db_factory, handler=h)
    client = TestClient(app)
    r = client.get("/api/test")
    assert r.status_code == 403
    rows = _audit_rows(db_factory)
    assert len(rows) == 1
    assert rows[0].result == "denied"


def test_fail_closed_returns_500_on_write_error(db_factory):
    """If record_consumer_action raises, the middleware must 500 with
    audit_write_failed — NEVER pass the request through silently."""

    def broken_factory():
        class BrokenSession:
            def add(self, *a, **kw):
                raise RuntimeError("db unreachable")

            def query(self, *a, **kw):
                raise RuntimeError("db unreachable")

            def flush(self):
                raise RuntimeError("db unreachable")

            def commit(self):
                raise RuntimeError("db unreachable")

            def close(self):
                pass

        return BrokenSession()

    app = _app(broken_factory)
    client = TestClient(app)
    r = client.get("/api/test")
    assert r.status_code == 500
    assert r.json() == {"error": "audit_write_failed"}
