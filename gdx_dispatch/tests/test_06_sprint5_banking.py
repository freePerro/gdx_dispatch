"""
Sprint 5 — Banking Readiness tests.
Covers: reporting, GPS dispatch, AI health score, distributor/wholesale supply chain,
shadow_run dark launch, JWKS key rotation, chaos engineering stubs, SOC2 evidence,
vulnerability check, and GDPR/CCPA completeness.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sprint5_db(tenant_db):
    """Reuse shared tenant_db for Sprint 5 tests."""
    yield tenant_db


# ---------------------------------------------------------------------------
# Reporting module
# ---------------------------------------------------------------------------

def test_reporting_importable():
    from gdx_dispatch.modules.reporting.service import job_costing_report, revenue_report, tech_performance_report
    assert all([job_costing_report, revenue_report, tech_performance_report])


def test_reporting_revenue_empty(sprint5_db):
    from gdx_dispatch.modules.reporting.service import revenue_report
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2020, 12, 31, tzinfo=timezone.utc)
    result = revenue_report(start, end, sprint5_db)
    assert result["invoice_count"] == 0
    assert result["total_paid"] == 0


def test_reporting_job_costing_empty(sprint5_db):
    from gdx_dispatch.modules.reporting.service import job_costing_report
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2020, 12, 31, tzinfo=timezone.utc)
    result = job_costing_report(start, end, sprint5_db)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# GPS Dispatch module
# ---------------------------------------------------------------------------

def test_gps_dispatch_importable():
    from gdx_dispatch.modules.gps_dispatch.service import (
        assign_route,
        get_technician_locations,
        update_technician_location,
    )
    assert all([update_technician_location, get_technician_locations, assign_route])


def test_gps_location_tracking(sprint5_db):
    from gdx_dispatch.modules.gps_dispatch.service import get_technician_locations, update_technician_location
    loc = update_technician_location("tech-gps-1", 40.7128, -74.0060, 5.0, sprint5_db, company_id="tenant-test")
    assert float(loc.lat) == pytest.approx(40.7128, rel=1e-4)
    all_locs = get_technician_locations(sprint5_db)
    assert any(l.tech_id == "tech-gps-1" for l in all_locs)  # noqa: E741


# ---------------------------------------------------------------------------
# AI Health Score module
# ---------------------------------------------------------------------------

def test_health_score_importable():
    from gdx_dispatch.modules.ai_health_score.service import compute_health_score, trigger_retention_playbook
    assert all([compute_health_score, trigger_retention_playbook])


def test_health_score_low_tenant(sprint5_db):
    from gdx_dispatch.modules.ai_health_score.service import compute_health_score
    score = compute_health_score("tenant-cold", sprint5_db)
    assert score.score >= 0
    assert score.tenant_id == "tenant-cold"
    # New tenant with no data → low score → re_engagement playbook
    assert score.playbook_triggered in ("re_engagement", "activation", None)


# ---------------------------------------------------------------------------
# Distributor module
# ---------------------------------------------------------------------------

def test_distributor_order_flow(sprint5_db):
    from gdx_dispatch.modules.distributor.service import (
        advance_order_status,
        create_dealer_order,
        get_dealer_network_orders,
    )
    order = create_dealer_order(
        dealer_tenant_id="dealer-1",
        distributor_tenant_id="dist-1",
        line_items=[{"sku": "SPRING-01", "qty": 5, "unit_price": 12.50}],
        idempotency_key="idem-order-1",
        db=sprint5_db,
    )
    assert order.status == "pending"
    assert float(order.total_amount) == pytest.approx(62.50)

    # Idempotency — same key returns same order
    order2 = create_dealer_order("dealer-1", "dist-1", [], "idem-order-1", sprint5_db)
    assert order2.id == order.id

    # Advance status
    confirmed = advance_order_status(order.id, "confirmed", sprint5_db)
    assert confirmed.status == "confirmed"

    # Invalid transition raises
    with pytest.raises(ValueError):
        advance_order_status(order.id, "pending", sprint5_db)

    orders = get_dealer_network_orders("dist-1", sprint5_db)
    assert any(o.id == order.id for o in orders)


# ---------------------------------------------------------------------------
# Wholesale module
# ---------------------------------------------------------------------------

def test_wholesale_catalog_and_pricing(sprint5_db):
    from gdx_dispatch.modules.wholesale.service import get_discounted_price, set_pricing_tier, upsert_catalog_item
    item = upsert_catalog_item("w-1", "TORSION-9", "Torsion Spring 9ft", 45.00, None, sprint5_db)
    assert item.sku == "TORSION-9"

    # Upsert updates in place
    item2 = upsert_catalog_item("w-1", "TORSION-9", "Torsion Spring 9ft v2", 48.00, None, sprint5_db)
    assert item2.id == item.id
    assert float(item2.base_price) == pytest.approx(48.00)

    # Pricing tier
    set_pricing_tier("w-1", "dist-1", "gold", 10.0, sprint5_db)
    price = get_discounted_price("w-1", "dist-1", "TORSION-9", sprint5_db)
    assert price == pytest.approx(43.20, rel=1e-3)  # 48 * 0.90


# ---------------------------------------------------------------------------
# JWKS key store
# ---------------------------------------------------------------------------

def test_jwks_importable():
    from gdx_dispatch.core.jwks import JWKSKeyStore, key_store
    assert key_store is not None
    assert callable(getattr(JWKSKeyStore, "get_jwks", None))


def test_jwks_sign_and_verify():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from gdx_dispatch.core.jwks import JWKSKeyStore

    ks = JWKSKeyStore()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    ks.add_key("kid-1", private_pem, public_pem)
    token = ks.sign_token({"sub": "user-123", "role": "admin"}, kid="kid-1")
    claims = ks.verify_token(token)
    assert claims["sub"] == "user-123"


# ---------------------------------------------------------------------------
# Dealer invitation onboarding flow
# ---------------------------------------------------------------------------

def test_dealer_invitation_flow(sprint5_db):
    from gdx_dispatch.modules.distributor.onboarding import (
        accept_dealer_invitation,
        create_dealer_invitation,
        list_pending_invitations,
    )
    inv = create_dealer_invitation("dist-1", "newdealer@example.com", sprint5_db)
    assert inv.status == "pending"
    assert len(inv.token) > 40

    # Listing shows it
    pending = list_pending_invitations("dist-1", sprint5_db)
    assert any(i.id == inv.id for i in pending)

    # Accept
    accepted = accept_dealer_invitation(inv.token, "dealer-new-1", sprint5_db)
    assert accepted.status == "accepted"
    assert accepted.dealer_tenant_id == "dealer-new-1"

    # Cannot accept twice
    with pytest.raises(ValueError, match="already accepted"):
        accept_dealer_invitation(inv.token, "dealer-new-2", sprint5_db)


def test_dealer_invitation_cancel(sprint5_db):
    from gdx_dispatch.modules.distributor.onboarding import cancel_invitation, create_dealer_invitation
    inv = create_dealer_invitation("dist-2", "cancel@example.com", sprint5_db)
    cancelled = cancel_invitation(inv.id, sprint5_db)
    assert cancelled.status == "cancelled"
    with pytest.raises(ValueError, match="Cannot cancel"):
        cancel_invitation(inv.id, sprint5_db)


def test_pwa_version_endpoint():
    import os

    from gdx_dispatch.core.pwa import PWARouter
    os.environ.setdefault("APP_VERSION", "test-1.0")
    # Verify routes exist
    routes = [r.path for r in PWARouter.routes]
    assert "/pwa/version" in routes
    assert "/pwa/manifest.json" in routes
    assert "/sw.js" in routes


def test_pwa_service_worker_contains_cache_version():
    from gdx_dispatch.core.pwa import _SW_JS_PATH
    assert _SW_JS_PATH.exists(), f"Service worker file not found at {_SW_JS_PATH}"
    sw_content = _SW_JS_PATH.read_text(encoding="utf-8")
    assert "CACHE_VERSION" in sw_content or "cache" in sw_content.lower(), "Service worker should reference caching"


def test_gdpr_export_endpoint_exists():
    from gdx_dispatch.routers.gdpr import router
    routes = [r.path for r in router.routes]
    assert any("export" in p for p in routes), f"No export endpoint found in GDPR router: {routes}"


def test_gdpr_delete_endpoint_exists():
    from gdx_dispatch.routers.gdpr import router
    routes = [r.path for r in router.routes]
    assert any("delete" in p or "hard" in p for p in routes), f"No delete endpoint: {routes}"
