"""
Tests for the tenant self-serve admin UI routes (tenant_ui.py).

Tests:
  1. Dashboard requires authentication (redirects when no user on request.state)
  2. Dashboard renders with mocked DB (HTML response, 200)
  3. Settings page renders with mocked DB (200, expected fields present)
  4. Settings POST saves data correctly
  5. Team page renders with mocked DB (200, table present)
  6. Team invite POST creates user and redirects
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core import tenant_ui

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_tenant_db(rows_by_query: dict | None = None):
    """Return a MagicMock SQLAlchemy session with configurable query results."""
    db = MagicMock()
    scalar_vals = [0, 0, 0, 0]  # jobs_30d, revenue, active_customers, technicians
    db.execute.return_value.scalar.side_effect = lambda: scalar_vals.pop(0) if scalar_vals else 0
    db.execute.return_value.mappings.return_value.all.return_value = []
    db.execute.return_value.mappings.return_value.first.return_value = None
    db.execute.return_value.fetchone.return_value = None
    return db


def _authed_user() -> dict:
    return {"user_id": "u1", "tenant_id": "t1", "role": "owner", "email": "owner@example.com"}


def _make_app(db: MagicMock | None = None, user: dict | None = None) -> TestClient:
    """Build a minimal FastAPI app with tenant_ui router and dependency overrides."""
    app = FastAPI()
    app.include_router(tenant_ui.router)

    mock_db = db or _mock_tenant_db()
    mock_user = user  # None = unauthenticated

    def override_tenant_db():
        yield mock_db

    app.dependency_overrides[tenant_ui.get_db] = override_tenant_db

    # Inject current_user onto request.state via middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    class FakeAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.current_user = mock_user
            request.state.tenant = {"id": "t1", "db_url": "sqlite://", "name": "Test Co", "stripe_customer_id": ""}
            request.state.flash_messages = []
            return await call_next(request)

    app.add_middleware(FakeAuthMiddleware)
    return TestClient(app, follow_redirects=False)


# ── Test 1: Dashboard requires auth ──────────────────────────────────────────

def test_dashboard_requires_auth():
    """Unauthenticated request to /dashboard should raise 302 redirect."""
    client = _make_app(user=None)
    resp = client.get("/dashboard")
    # _require_auth raises HTTPException(302) which FastAPI converts
    assert resp.status_code in (302, 401, 403), f"Expected redirect/auth error, got {resp.status_code}"


# ── Test 2: Dashboard renders for authenticated user ─────────────────────────

def test_dashboard_renders():
    """Authenticated request renders dashboard HTML with KPI section."""
    # Patch the templates so we don't need actual template files on disk
    with patch.object(tenant_ui.templates, "TemplateResponse") as mock_resp:
        from starlette.responses import HTMLResponse
        mock_resp.return_value = HTMLResponse(content="<html>dashboard</html>", status_code=200)

        client = _make_app(user=_authed_user())
        resp = client.get("/dashboard")

    assert resp.status_code == 200
    mock_resp.assert_called_once()
    call_args = mock_resp.call_args
    assert call_args[0][0] == "tenant_dashboard.html"
    ctx = call_args[0][1]
    assert "stats" in ctx
    assert "recent_jobs" in ctx
    assert "onboarding" in ctx
    assert ctx["current_user"]["role"] == "owner"


# ── Test 3: Settings page renders ────────────────────────────────────────────

def test_settings_page_renders():
    """Settings page returns 200 and passes settings dict to template."""
    with patch.object(tenant_ui.templates, "TemplateResponse") as mock_resp:
        from starlette.responses import HTMLResponse
        mock_resp.return_value = HTMLResponse(content="<html>settings</html>", status_code=200)

        client = _make_app(user=_authed_user())
        resp = client.get("/settings")

    assert resp.status_code == 200
    call_args = mock_resp.call_args
    assert call_args[0][0] == "tenant_settings.html"
    ctx = call_args[0][1]
    assert "settings" in ctx
    settings = ctx["settings"]
    # All expected keys present
    for key in ("company_name", "phone", "timezone", "term_job", "webhook_url"):
        assert key in settings, f"Missing key in settings: {key}"


# ── Test 4: Settings POST saves and redirects ─────────────────────────────────

def test_settings_save_redirects():
    """POST /settings with valid data commits to DB and redirects to /settings?saved=1."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchone.return_value = None  # no existing record
    mock_db.commit.return_value = None

    client = _make_app(db=mock_db, user=_authed_user())
    resp = client.post("/settings", data={
        "company_name": "Test Co",
        "phone": "555-1234",
        "timezone": "America/Chicago",
        "address": "123 Main St",
        "term_job": "Work Order",
        "term_customer": "Client",
        "term_technician": "Installer",
        "term_estimate": "Quote",
        "term_invoice": "Invoice",
        "term_dispatcher": "Scheduler",
        "webhook_url": "https://example.com/hook",
    })
    assert resp.status_code == 303
    assert "saved=1" in resp.headers.get("location", "")
    mock_db.commit.assert_called_once()


def test_settings_save_rejects_non_https_webhook():
    """POST /settings with HTTP webhook URL returns 422."""
    mock_db = MagicMock()
    client = _make_app(db=mock_db, user=_authed_user())
    resp = client.post("/settings", data={
        "company_name": "Test Co",
        "webhook_url": "http://insecure.example.com/hook",
    })
    assert resp.status_code == 422


# ── Test 5: Team page renders ─────────────────────────────────────────────────

def test_team_page_renders():
    """Team page returns 200 and passes team_members list to template."""
    mock_db = MagicMock()
    mock_db.execute.return_value.mappings.return_value.all.return_value = [
        {"id": "u1", "name": "Alice Owner", "email": "alice@co.com", "role": "owner",
         "last_login": "2026-03-20", "active": 1},
        {"id": "u2", "name": "Bob Tech", "email": "bob@co.com", "role": "technician",
         "last_login": None, "active": 1},
    ]

    with patch.object(tenant_ui.templates, "TemplateResponse") as mock_resp:
        from starlette.responses import HTMLResponse
        mock_resp.return_value = HTMLResponse(content="<html>team</html>", status_code=200)

        client = _make_app(db=mock_db, user=_authed_user())
        resp = client.get("/team")

    assert resp.status_code == 200
    call_args = mock_resp.call_args
    assert call_args[0][0] == "tenant_team.html"
    ctx = call_args[0][1]
    assert "team_members" in ctx
    assert len(ctx["team_members"]) == 2
    assert ctx["team_members"][0]["role"] == "owner"


# ── Test 6: Team invite POST creates user ─────────────────────────────────────

def test_team_invite_creates_user_and_redirects():
    """POST /team/invite with valid data inserts user and redirects to /team?invited=1."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchone.return_value = None  # user does not exist yet
    mock_db.commit.return_value = None

    with patch.object(tenant_ui, "_send_invite_email") as mock_email:
        client = _make_app(db=mock_db, user=_authed_user())
        resp = client.post("/team/invite", data={
            "email": "newtech@example.com",
            "name": "New Tech",
            "role": "technician",
        })

    assert resp.status_code == 303
    assert "invited=1" in resp.headers.get("location", "")
    mock_db.commit.assert_called_once()
    mock_email.assert_called_once()
    assert "newtech@example.com" in str(mock_email.call_args)


def test_team_invite_rejects_invalid_role():
    """POST /team/invite with invalid role returns 422."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchone.return_value = None
    client = _make_app(db=mock_db, user=_authed_user())
    resp = client.post("/team/invite", data={
        "email": "hacker@example.com",
        "role": "superadmin",
    })
    assert resp.status_code == 422


def test_team_invite_rejects_duplicate_email():
    """POST /team/invite with existing email returns 409."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchone.return_value = ("existing-id",)  # user exists
    client = _make_app(db=mock_db, user=_authed_user())
    resp = client.post("/team/invite", data={
        "email": "existing@example.com",
        "role": "dispatcher",
    })
    assert resp.status_code == 409
