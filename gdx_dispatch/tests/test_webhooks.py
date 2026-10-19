"""Tests for the outbound webhook subscription router."""
from __future__ import annotations

import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.webhooks import router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    setup.execute(
        text("INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
             "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
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
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "u1", "sub": "u1", "role": "admin", "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _payload(**overrides):
    base = {
        "name": "Zapier test hook",
        "url": "https://hooks.zapier.com/hooks/catch/xyz",
        "events": ["job.completed", "invoice.paid"],
    }
    base.update(overrides)
    return base


def test_create_subscription(client):
    r = client.post("/api/webhooks/subscriptions", json=_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Zapier test hook"
    assert body["events"] == ["job.completed", "invoice.paid"]
    assert body["active"] is True


def test_unknown_event_rejected(client):
    r = client.post("/api/webhooks/subscriptions", json=_payload(events=["job.invented"]))
    assert r.status_code == 422


def test_non_https_url_rejected(client):
    r = client.post("/api/webhooks/subscriptions", json=_payload(url="ftp://bad.host/x"))
    assert r.status_code == 422


def test_list_tenant_scoped():
    tc_a = _make_client("tenant-a")
    tc_b = _make_client("tenant-b")
    try:
        tc_a.post("/api/webhooks/subscriptions", json=_payload(name="hook-a"))
        tc_b.post("/api/webhooks/subscriptions", json=_payload(name="hook-b"))

        r = tc_a.get("/api/webhooks/subscriptions")
        names_a = [s["name"] for s in r.json()]
        assert "hook-a" in names_a
        assert "hook-b" not in names_a
    finally:
        tc_a.app.dependency_overrides.clear()
        tc_a._engine.dispose()
        tc_b.app.dependency_overrides.clear()
        tc_b._engine.dispose()


def test_patch_updates_events(client):
    created = client.post("/api/webhooks/subscriptions", json=_payload()).json()
    r = client.patch(
        f"/api/webhooks/subscriptions/{created['id']}",
        json={"events": ["estimate.sent"]},
    )
    assert r.status_code == 200
    assert r.json()["events"] == ["estimate.sent"]


def test_soft_delete(client):
    created = client.post("/api/webhooks/subscriptions", json=_payload()).json()
    r = client.delete(f"/api/webhooks/subscriptions/{created['id']}")
    assert r.status_code == 204
    r = client.get(f"/api/webhooks/subscriptions/{created['id']}")
    assert r.status_code == 404


def test_event_catalogue_endpoint(client):
    r = client.get("/api/webhooks/events")
    assert r.status_code == 200
    events = r.json()
    assert "job.completed" in events
    assert "invoice.paid" in events


@patch("gdx_dispatch.routers.webhooks.validating_opener.open")
def test_test_send_records_success(mock_open, client):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"ok":true}'
    mock_open.return_value.__enter__.return_value = mock_resp

    created = client.post("/api/webhooks/subscriptions", json=_payload()).json()
    r = client.post(f"/api/webhooks/subscriptions/{created['id']}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status_code"] == 200
    assert body["error"] is None
    assert body["delivery_id"]

    # Delivery log shows the row
    r = client.get(f"/api/webhooks/subscriptions/{created['id']}/deliveries")
    assert r.status_code == 200
    deliveries = r.json()
    assert len(deliveries) == 1
    assert deliveries[0]["response_status"] == 200


@patch("gdx_dispatch.routers.webhooks.validating_opener.open")
def test_test_send_records_http_error(mock_open, client):
    import urllib.error

    # HTTPError needs positional args (url, code, msg, hdrs, fp)
    from io import BytesIO
    err = urllib.error.HTTPError(
        "https://bad.example", 500, "Server Error", {}, BytesIO(b"boom")
    )
    mock_open.return_value.__enter__.side_effect = err
    mock_open.side_effect = err

    created = client.post("/api/webhooks/subscriptions", json=_payload()).json()
    r = client.post(f"/api/webhooks/subscriptions/{created['id']}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["status_code"] == 500


# ---------------------------------------------------------------------------
# GDXA-1703 — the test-ping re-validates redirect hops (real sockets, no mock)
# ---------------------------------------------------------------------------

@contextmanager
def _local_server(status: int, body: bytes = b"", location: str | None = None):
    """A real HTTP server on 127.0.0.1 answering every request the same way;
    yields (port, hits) where hits records each request's method and path."""
    hits: list[tuple[str, str]] = []

    class _Handler(BaseHTTPRequestHandler):
        def _answer(self):
            hits.append((self.command, self.path))
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer  # noqa: N815

        def log_message(self, *a):  # noqa: ANN002
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], hits
    finally:
        server.shutdown()
        server.server_close()


def test_validating_opener_refuses_redirect_to_blocked_host(monkeypatch):
    from gdx_dispatch.core.ssrf_guard import OutboundURLBlocked, validating_opener

    monkeypatch.setenv("GDX_WEBHOOK_PRIVATE_ALLOW", "localhost")
    with _local_server(200, b"internal secret") as (inner_port, inner_hits), \
         _local_server(302, location=f"http://127.0.0.1:{inner_port}/secret") as (outer_port, _):
        req = urllib.request.Request(f"http://localhost:{outer_port}/hook", data=b"{}", method="POST")
        with pytest.raises(OutboundURLBlocked):
            validating_opener.open(req, timeout=5)
    assert inner_hits == []  # the blocked hop was never requested


def test_test_send_redirect_to_blocked_host_is_recorded_as_error(client, monkeypatch):
    monkeypatch.setenv("GDX_WEBHOOK_PRIVATE_ALLOW", "localhost")
    with _local_server(200, b"internal secret") as (inner_port, inner_hits), \
         _local_server(302, location=f"http://127.0.0.1:{inner_port}/secret") as (outer_port, outer_hits):
        created = client.post(
            "/api/webhooks/subscriptions", json=_payload(url=f"http://localhost:{outer_port}/hook"),
        )
        assert created.status_code == 201, created.text
        sub_id = created.json()["id"]
        r = client.post(f"/api/webhooks/subscriptions/{sub_id}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status_code"] is None
    assert "disallowed address 127.0.0.1" in body["error"]
    assert outer_hits == [("POST", "/hook")]
    assert inner_hits == []

    deliveries = client.get(f"/api/webhooks/subscriptions/{sub_id}/deliveries").json()
    assert len(deliveries) == 1
    assert deliveries[0]["error"] == body["error"]
    assert deliveries[0]["response_body"] == ""  # nothing from the internal host stored


def test_test_send_without_redirect_records_response(client, monkeypatch):
    """Control: the same real path with no redirect still delivers and logs."""
    monkeypatch.setenv("GDX_WEBHOOK_PRIVATE_ALLOW", "localhost")
    with _local_server(200, b'{"ok":true}') as (port, hits):
        created = client.post(
            "/api/webhooks/subscriptions", json=_payload(url=f"http://localhost:{port}/hook"),
        )
        assert created.status_code == 201, created.text
        sub_id = created.json()["id"]
        r = client.post(f"/api/webhooks/subscriptions/{sub_id}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status_code"] == 200
    assert body["error"] is None
    assert hits == [("POST", "/hook")]

    deliveries = client.get(f"/api/webhooks/subscriptions/{sub_id}/deliveries").json()
    assert deliveries[0]["response_status"] == 200
    assert deliveries[0]["response_body"] == '{"ok":true}'
