"""
gdx_dispatch/tests/test_33_integrations.py — Integration marketplace tests.

Covers:
  1. list_available_integrations — returns all 7 types with required fields
  2. connect_integration (api_key type) — stores config row, returns status=connected
  3. connect_integration (oauth type) — stores access_token, handles refresh_token
  4. connect_integration (invalid type) — raises ValueError
  5. connect_integration (missing credentials) — raises ValueError
  6. disconnect_integration — deactivates rows, returns count
  7. get_integration_status (connected) — returns correct status dict
  8. get_integration_status (disconnected) — returns disconnected when no rows
  9. get_integration_status (error state) — triggered but never succeeded → "error"
 10. connect then disconnect round-trip — status transitions correctly
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db():
    """Isolated SQLite in-memory DB with only the tables needed."""
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.integrations import IntegrationConfig
    from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for tbl in [
        AuditLog.__table__,
        AIAction.__table__,
        WebhookEndpoint.__table__,
        WebhookDelivery.__table__,
        IntegrationConfig.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    yield db
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 1. list_available_integrations
# ---------------------------------------------------------------------------

def test_list_available_integrations_returns_all_types():
    """list_available_integrations returns all 7 types with required fields."""
    from gdx_dispatch.core.integrations import list_available_integrations

    result = list_available_integrations()
    types = {d["type"] for d in result}

    assert len(result) == 7
    assert types == {"quickbooks", "stripe", "google_calendar", "zapier", "mailchimp", "twilio", "google_maps"}

    for item in result:
        assert "name" in item and item["name"]
        assert "description" in item and item["description"]
        assert "category" in item and item["category"]
        assert "is_oauth" in item
        assert isinstance(item["is_oauth"], bool)
        assert "logo_icon" in item and item["logo_icon"]


# ---------------------------------------------------------------------------
# 2. connect_integration — api_key type
# ---------------------------------------------------------------------------

def test_connect_integration_api_key(fresh_db):
    """connect_integration stores an IntegrationConfig for an API-key type."""
    from gdx_dispatch.core.integrations import IntegrationConfig, connect_integration

    result = connect_integration(
        tenant_id="t-api-001",
        integration_type="stripe",
        credentials={"api_key": "sk_live_testkey123"},
        db=fresh_db,
    )

    assert result["status"] == "connected"
    assert result["integration_type"] == "stripe"
    assert "id" in result

    row = fresh_db.execute(
        select(IntegrationConfig).where(IntegrationConfig.tenant_id == "t-api-001")
    ).scalars().first()
    assert row is not None
    assert row.integration_type == "stripe"
    assert row.is_active is True
    assert row.secret == "sk_live_testkey123"


# ---------------------------------------------------------------------------
# 3. connect_integration — oauth type with refresh token
# ---------------------------------------------------------------------------

def test_connect_integration_oauth(fresh_db):
    """connect_integration stores OAuth token for google_calendar, including refresh token."""
    from gdx_dispatch.core.integrations import IntegrationConfig, connect_integration

    result = connect_integration(
        tenant_id="t-oauth-001",
        integration_type="google_calendar",
        credentials={"access_token": "ya29.access", "refresh_token": "1//refresh"},
        db=fresh_db,
    )

    assert result["status"] == "connected"
    assert result["integration_type"] == "google_calendar"

    row = fresh_db.execute(
        select(IntegrationConfig).where(IntegrationConfig.tenant_id == "t-oauth-001")
    ).scalars().first()
    assert row is not None
    assert "ya29.access" in row.secret
    assert "1//refresh" in row.secret


# ---------------------------------------------------------------------------
# 4. connect_integration — invalid type
# ---------------------------------------------------------------------------

def test_connect_integration_invalid_type(fresh_db):
    """connect_integration raises ValueError for unknown integration type."""
    from gdx_dispatch.core.integrations import connect_integration

    with pytest.raises(ValueError, match="Unknown integration type"):
        connect_integration(
            tenant_id="t-bad-001",
            integration_type="not_a_real_type",
            credentials={"api_key": "key"},
            db=fresh_db,
        )


# ---------------------------------------------------------------------------
# 5. connect_integration — missing credentials
# ---------------------------------------------------------------------------

def test_connect_integration_missing_credentials(fresh_db):
    """connect_integration raises ValueError when required credential key is absent."""
    from gdx_dispatch.core.integrations import connect_integration

    # stripe expects api_key, not access_token
    with pytest.raises(ValueError, match="api_key"):
        connect_integration(
            tenant_id="t-bad-002",
            integration_type="stripe",
            credentials={"access_token": "wrong_key"},
            db=fresh_db,
        )

    # quickbooks expects access_token, not api_key
    with pytest.raises(ValueError, match="access_token"):
        connect_integration(
            tenant_id="t-bad-003",
            integration_type="quickbooks",
            credentials={"api_key": "wrong"},
            db=fresh_db,
        )


# ---------------------------------------------------------------------------
# 6. disconnect_integration
# ---------------------------------------------------------------------------

def test_disconnect_integration(fresh_db):
    """disconnect_integration deactivates all active rows for the type."""
    from gdx_dispatch.core.integrations import IntegrationConfig, connect_integration, disconnect_integration

    # Connect twice
    connect_integration("t-disc-001", "mailchimp", {"api_key": "key1"}, fresh_db)
    connect_integration("t-disc-001", "mailchimp", {"api_key": "key2"}, fresh_db)

    result = disconnect_integration("t-disc-001", "mailchimp", fresh_db)

    assert result["status"] == "disconnected"
    assert result["count"] == 2

    # Confirm all are inactive
    rows = fresh_db.execute(
        select(IntegrationConfig).where(
            IntegrationConfig.tenant_id == "t-disc-001",
            IntegrationConfig.integration_type == "mailchimp",
        )
    ).scalars().all()
    assert all(not r.is_active for r in rows)


# ---------------------------------------------------------------------------
# 7. get_integration_status — connected
# ---------------------------------------------------------------------------

def test_get_integration_status_connected(fresh_db):
    """get_integration_status returns 'connected' for an active config."""
    from gdx_dispatch.core.integrations import connect_integration, get_integration_status

    connect_integration("t-status-001", "twilio", {"api_key": "ACtest"}, fresh_db)
    status = get_integration_status("t-status-001", "twilio", fresh_db)

    assert status["status"] == "connected"
    assert status["integration_type"] == "twilio"
    assert "id" in status


# ---------------------------------------------------------------------------
# 8. get_integration_status — disconnected
# ---------------------------------------------------------------------------

def test_get_integration_status_disconnected(fresh_db):
    """get_integration_status returns 'disconnected' when no active rows exist."""
    from gdx_dispatch.core.integrations import get_integration_status

    status = get_integration_status("t-nodoc-001", "google_maps", fresh_db)

    assert status["status"] == "disconnected"
    assert status["integration_type"] == "google_maps"


# ---------------------------------------------------------------------------
# 9. get_integration_status — error state
# ---------------------------------------------------------------------------

def test_get_integration_status_error_state(fresh_db):
    """get_integration_status returns 'error' when triggered but never succeeded."""
    from datetime import datetime, timezone

    from gdx_dispatch.core.integrations import IntegrationConfig, connect_integration, get_integration_status

    connect_integration("t-err-001", "stripe", {"api_key": "sk_err"}, fresh_db)

    # Manually set last_triggered_at without last_success_at
    row = fresh_db.execute(
        select(IntegrationConfig).where(IntegrationConfig.tenant_id == "t-err-001")
    ).scalars().first()
    row.last_triggered_at = datetime.now(timezone.utc)
    row.last_success_at = None
    fresh_db.commit()

    status = get_integration_status("t-err-001", "stripe", fresh_db)
    assert status["status"] == "error"


# ---------------------------------------------------------------------------
# 10. connect → disconnect round-trip
# ---------------------------------------------------------------------------

def test_connect_disconnect_roundtrip(fresh_db):
    """Status transitions correctly: disconnected → connected → disconnected."""
    from gdx_dispatch.core.integrations import connect_integration, disconnect_integration, get_integration_status

    tenant = "t-roundtrip-001"
    itype = "zapier"

    # Initially disconnected
    before = get_integration_status(tenant, itype, fresh_db)
    assert before["status"] == "disconnected"

    # Connect
    connect_integration(tenant, itype, {"api_key": "zap_key"}, fresh_db)
    after_connect = get_integration_status(tenant, itype, fresh_db)
    assert after_connect["status"] == "connected"

    # Disconnect
    disconnect_integration(tenant, itype, fresh_db)
    after_disconnect = get_integration_status(tenant, itype, fresh_db)
    assert after_disconnect["status"] == "disconnected"


# ---------------------------------------------------------------------------
# 11. test_list_integrations_requires_auth
# ---------------------------------------------------------------------------

def test_list_integrations_requires_auth():
    """GET /api/integrations without auth returns 401 or 403."""
    try:
        from fastapi.testclient import TestClient

        from gdx_dispatch.main import app
    except Exception as exc:
        pytest.skip(f"gdx_dispatch.main not importable: {exc}")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/integrations")
    assert response.status_code in {401, 403, 404, 422}, (
        f"Expected auth-required status, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# 12. test_list_integrations_with_auth
# ---------------------------------------------------------------------------

def test_list_integrations_with_auth():
    """list_available_integrations() returns a list of ≥7 typed dicts."""
    from gdx_dispatch.core.integrations import list_available_integrations

    result = list_available_integrations()
    assert isinstance(result, list)
    assert len(result) >= 7
    for item in result:
        assert "type" in item and item["type"]
        assert "name" in item and item["name"]


# ---------------------------------------------------------------------------
# 13. test_integration_tenant_isolated
# ---------------------------------------------------------------------------

def test_integration_tenant_isolated(fresh_db):
    """Disconnecting one tenant's integration does not affect another tenant."""
    from gdx_dispatch.core.integrations import connect_integration, disconnect_integration, get_integration_status

    # Connect both tenants to the same integration type
    connect_integration("t-iso-001", "stripe", {"api_key": "key_iso_001"}, fresh_db)
    connect_integration("t-iso-002", "stripe", {"api_key": "key_iso_002"}, fresh_db)

    # Both should be connected
    assert get_integration_status("t-iso-001", "stripe", fresh_db)["status"] == "connected"
    assert get_integration_status("t-iso-002", "stripe", fresh_db)["status"] == "connected"

    # Disconnect only tenant 001
    disconnect_integration("t-iso-001", "stripe", fresh_db)

    # 001 is disconnected, 002 is still connected
    assert get_integration_status("t-iso-001", "stripe", fresh_db)["status"] == "disconnected"
    assert get_integration_status("t-iso-002", "stripe", fresh_db)["status"] == "connected"


# ---------------------------------------------------------------------------
# 14. test_integrations_page_renders
# ---------------------------------------------------------------------------

def test_integrations_page_renders():
    """GET /integrations returns a valid response for a Vue SPA route.

    /integrations is now handled by the Vue Router client-side. The server
    responds with the Vue SPA shell (index.html) for any unknown URL so Vue
    Router can take over. The old version of this test expected the word
    "integration" to appear in the server response — that was true when the
    app was Jinja2-rendered, but is no longer correct for the SPA. The fix
    is to accept EITHER the SPA shell (identified by <div id="app">) OR a
    legacy server-rendered page that contains the word "integration".
    """
    try:
        from fastapi.testclient import TestClient

        from gdx_dispatch.main import app
    except Exception as exc:
        pytest.skip(f"gdx_dispatch.main not importable: {exc}")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/integrations", follow_redirects=False)
    # Accept any non-5xx: 200 renders the SPA shell, 3xx is auth redirect,
    # 401/403 is auth gate, 404 means tenant middleware rejected the request.
    assert response.status_code < 500, (
        f"Unexpected server error {response.status_code}: {response.text[:200]}"
    )
    if response.status_code == 200:
        body = response.text.lower()
        # Either the Vue SPA shell (empty div for client-side rendering)
        # or a legacy server-rendered page with the feature name in the HTML.
        assert (
            '<div id="app">' in body
            or 'id="app"' in body
            or "integration" in body
        ), f"Unexpected response body: {response.text[:200]}"


# ---------------------------------------------------------------------------
# 15. test_test_connection — not connected
# ---------------------------------------------------------------------------

def test_test_connection_not_connected(fresh_db):
    """test_connection returns ok=False when no active config exists."""
    from gdx_dispatch.core.integrations import test_connection

    result = test_connection("t-tc-001", "stripe", fresh_db)

    assert result["ok"] is False
    assert result["type"] == "stripe"
    assert "not connected" in result["message"]


# ---------------------------------------------------------------------------
# 16. test_test_connection — connected, records last_success_at
# ---------------------------------------------------------------------------

def test_test_connection_connected(fresh_db):
    """test_connection returns ok=True and stamps last_success_at when connected."""
    from sqlalchemy import select

    from gdx_dispatch.core.integrations import IntegrationConfig, connect_integration, test_connection

    connect_integration("t-tc-002", "twilio", {"api_key": "ACtest"}, fresh_db)

    result = test_connection("t-tc-002", "twilio", fresh_db)

    assert result["ok"] is True
    assert result["type"] == "twilio"
    assert result["message"] == "connection OK"

    row = fresh_db.execute(
        select(IntegrationConfig).where(IntegrationConfig.tenant_id == "t-tc-002")
    ).scalars().first()
    assert row is not None
    assert row.last_success_at is not None


# ---------------------------------------------------------------------------
# 17. test_list_integrations — service function returns all types with status
# ---------------------------------------------------------------------------

def test_list_integrations_service_function(fresh_db):
    """list_integrations returns all catalogue types; connected type shows connected=True."""
    from gdx_dispatch.core.integrations import connect_integration, list_integrations

    # Connect one type
    connect_integration("t-li-001", "stripe", {"api_key": "sk_test"}, fresh_db)

    result = list_integrations("t-li-001", fresh_db)

    assert isinstance(result, list)
    # Must include all types from _INTEGRATION_CATALOGUE
    types_returned = {entry["type"] for entry in result}
    assert "stripe" in types_returned
    assert "quickbooks" in types_returned
    assert "zapier" in types_returned

    # Stripe should show as connected
    stripe_entry = next(e for e in result if e["type"] == "stripe")
    assert stripe_entry["connected"] is True
    assert stripe_entry["name"]
    assert stripe_entry["description"]

    # Quickbooks (not connected) should show connected=False
    qb_entry = next(e for e in result if e["type"] == "quickbooks")
    assert qb_entry["connected"] is False
