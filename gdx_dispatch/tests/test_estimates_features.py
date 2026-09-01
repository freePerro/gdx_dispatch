"""Tests for the per-tenant estimates-features gate (2026-04-30).

Covers:
- EstimatesFeatures defaults to permissive.
- require_line_margin_override_allowed raises 403 when the tenant has
  estimates_allow_line_margin_override=False.
- estimates router's add_line / patch_line refuse a margin_pct_override
  payload under that flag.
- (2026-08-31, issue #351) the four invoice/receipt email template columns
  round-trip through GET/PATCH /api/estimates-features, read back through
  the service resolver, and the PATCH leaves an audit row.
"""
from __future__ import annotations

from contextlib import nullcontext

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.estimates_features import service as features_service
from gdx_dispatch.modules.estimates_features.router import (
    _COLS,
    _INT_DEFAULTS,
    _TEXT_COLS,
    FeaturesPayload,
    router,
)
from gdx_dispatch.modules.estimates_features.service import (
    EstimatesFeatures,
    require_line_margin_override_allowed,
)
from gdx_dispatch.routers.auth import get_current_user


def test_features_default_is_permissive():
    f = EstimatesFeatures()
    assert f.allow_line_margin_override is True


def test_gate_passes_when_allowed(monkeypatch):
    monkeypatch.setattr(
        features_service,
        "get_features",
        lambda tid: EstimatesFeatures(allow_line_margin_override=True),
    )
    require_line_margin_override_allowed("any-tenant")  # no raise


def test_gate_blocks_when_disabled(monkeypatch):
    monkeypatch.setattr(
        features_service,
        "get_features",
        lambda tid: EstimatesFeatures(allow_line_margin_override=False),
    )
    with pytest.raises(HTTPException) as excinfo:
        require_line_margin_override_allowed("any-tenant")
    assert excinfo.value.status_code == 403
    assert "margin override" in excinfo.value.detail.lower()


def test_router_imports_gate():
    """Surface-level guard: estimates router imports the gate. If this
    fails, the in-router enforcement was reverted by accident."""
    import gdx_dispatch.routers.estimates as est
    assert hasattr(est, "require_line_margin_override_allowed")


# ── Invoice / receipt email templates (issue #351) ──────────────────────────

TEMPLATE_COLS = (
    "invoice_email_subject_template",
    "invoice_email_body_template",
    "receipt_email_subject_template",
    "receipt_email_body_template",
)
TID = "8f3f5c1e-6d2a-4b7e-9c1d-2a4b6c8d0e1f"
SAMPLE = {
    "invoice_email_subject_template": "Bill {{invoice_number}} from {{company_name}}",
    "invoice_email_body_template": "Hi {{customer_name}},\n\nInvoice {{invoice_number}}: {{total}}{{due_line}}\n\n{{company_name}}",
    "receipt_email_subject_template": "Thanks — Invoice {{invoice_number}} from {{company_name}}",
    "receipt_email_body_template": "Hi {{customer_name}},\n\nGot it.{{paid_line}}\n\n{{company_name}}",
}


def _col_ddl(col: str) -> str:
    # Types derived from the router's own classification so this DDL cannot
    # silently lag _COLS: a column added there without a home here fails
    # loudly instead of returning a stale NULL.
    if col in _TEXT_COLS:
        return f"{col} TEXT"
    if col in _INT_DEFAULTS:
        return f"{col} INTEGER"
    return f"{col} BOOLEAN"


@pytest.fixture()
def features_env():
    """SQLite-backed TestClient over the estimates-features router alone.

    tenant_settings is a control-plane (baseline) table, not ORM metadata, so
    it is created by hand with exactly the router's _COLS; TenantBase.create_all
    supplies audit_logs for the PATCH audit row."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tenant_settings (tenant_id TEXT PRIMARY KEY, "
            + ", ".join(_col_ddl(c) for c in _COLS)
            + ")"
        ))
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    user = {"user_id": "user-1", "sub": "user-1", "role": "admin", "tenant_id": TID}
    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": TID}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: dict(user)
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, Session, user
    app.dependency_overrides.clear()
    engine.dispose()


def test_template_columns_are_wired_through_the_router():
    """The Settings save writes every _COLS field; a column missing from any
    of the three sets is either never persisted or read back as a bool."""
    for col in TEMPLATE_COLS:
        assert col in _COLS, col
        assert col in FeaturesPayload.model_fields, col
        assert col in _TEXT_COLS, col
        assert FeaturesPayload.model_fields[col].default == "", col


def test_service_defaults_are_blank_meaning_platform_default():
    f = EstimatesFeatures()
    for col in TEMPLATE_COLS:
        assert getattr(f, col) == "", col


def test_get_returns_blank_templates_for_a_fresh_tenant(features_env):
    tc, _Session, _user = features_env
    r = tc.get("/api/estimates-features")
    assert r.status_code == 200, r.text
    body = r.json()
    for col in TEMPLATE_COLS:
        assert body[col] == "", col
    # The pre-existing estimate pair is untouched by the widening.
    assert body["estimate_email_subject_template"] == ""
    assert body["estimate_deposit_pct"] == 50


def test_patch_round_trips_the_four_templates(features_env):
    """PATCH persists all four (newlines intact) and GET reads them back —
    the silent-write shape (200 but nothing stored) is what this catches."""
    tc, _Session, _user = features_env
    base = tc.get("/api/estimates-features").json()
    r = tc.patch("/api/estimates-features", json={**base, **SAMPLE})
    assert r.status_code == 200, r.text
    for col, val in SAMPLE.items():
        assert r.json()[col] == val, col
    again = tc.get("/api/estimates-features").json()
    for col, val in SAMPLE.items():
        assert again[col] == val, col
    assert "\n\n" in again["invoice_email_body_template"], "newlines must survive the round trip"


def test_patch_blank_clears_a_template_back_to_platform_default(features_env):
    """Blank is a real value the UI must be able to send — it is the only way
    back to the platform default once a tenant has typed something."""
    tc, _Session, _user = features_env
    base = tc.get("/api/estimates-features").json()
    tc.patch("/api/estimates-features", json={**base, **SAMPLE})
    r = tc.patch(
        "/api/estimates-features",
        json={**base, **SAMPLE, "receipt_email_subject_template": ""},
    )
    assert r.status_code == 200, r.text
    assert r.json()["receipt_email_subject_template"] == ""
    assert r.json()["invoice_email_subject_template"] == SAMPLE["invoice_email_subject_template"]


def test_patch_omitted_template_keys_default_to_blank(features_env):
    """A client that predates the four keys (or a partial payload) must not
    500 — the payload defaults them to "", i.e. platform default."""
    tc, _Session, _user = features_env
    r = tc.patch("/api/estimates-features", json={"estimate_deposit_pct": 30})
    assert r.status_code == 200, r.text
    for col in TEMPLATE_COLS:
        assert r.json()[col] == "", col
    assert r.json()["estimate_deposit_pct"] == 30


def test_patch_requires_admin_or_owner(features_env):
    tc, _Session, user = features_env
    user["role"] = "technician"
    r = tc.patch("/api/estimates-features", json=SAMPLE)
    assert r.status_code == 403, r.text
    assert tc.get("/api/estimates-features").json()["invoice_email_subject_template"] == ""


def test_patch_writes_an_audit_row_naming_the_changed_columns(features_env):
    """Invariant #1: who changed the customer-facing copy, and to what. Only
    the columns that moved are recorded; the actor is the signed-in user."""
    tc, Session, _user = features_env
    base = tc.get("/api/estimates-features").json()
    r = tc.patch(
        "/api/estimates-features",
        json={**base, "invoice_email_subject_template": SAMPLE["invoice_email_subject_template"]},
    )
    assert r.status_code == 200, r.text
    db = Session()
    try:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "estimates_features_updated")
            .order_by(AuditLog.created_at.desc())
            .all()
        )
    finally:
        db.close()
    assert len(rows) == 1, "one PATCH, one audit row"
    row = rows[0]
    assert row.user_id == "user-1"
    assert row.entity_type == "tenant_settings"
    assert row.entity_id == TID
    changed = row.details["changed"]
    assert changed == {"invoice_email_subject_template": SAMPLE["invoice_email_subject_template"]}, (
        "only the column that moved belongs in the diff"
    )


def test_service_reads_the_four_templates_from_tenant_settings(features_env, monkeypatch):
    """get_features is what routers/invoices reads at send time. Point its
    session factory at the test engine and prove the columns come through —
    the router round-trip alone would not catch a SELECT that forgot one."""
    tc, Session, _user = features_env
    base = tc.get("/api/estimates-features").json()
    tc.patch("/api/estimates-features", json={**base, **SAMPLE})
    monkeypatch.setattr(features_service, "SessionLocal", Session)
    monkeypatch.setattr(features_service, "tenant_context", nullcontext)

    f = features_service.get_features(TID)
    for col, val in SAMPLE.items():
        assert getattr(f, col) == val, col
    # Unknown tenant → dataclass defaults, i.e. blank → platform default.
    g = features_service.get_features("00000000-0000-4000-8000-000000000000")
    for col in TEMPLATE_COLS:
        assert getattr(g, col) == "", col
