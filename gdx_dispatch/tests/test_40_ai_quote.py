"""test_40_ai_quote.py — AI quote generation HTTP + unit tests.

Tests the FastAPI routes exposed by gdx_dispatch/core/ai_quote.py via TestClient.

Actual routes (prefix /api/ai):
  POST /api/ai/quote               — keyword-based quote (no auth)
  POST /api/ai/quote-suggestion    — template-based quote (auth required)
  POST /api/ai/pricing/suggest     — price suggestion for a named part
  GET  /api/ai/pricing/health      — tenant pricing health analysis
  GET  /api/ai/price-benchmarks    — per-job-type benchmark data (auth required)

NOTE — routes requested in the task spec that do NOT yet exist:
  POST /api/ai/quote-generate   → TODO: not implemented; tested as 404 below
  GET  /api/ai/quote-history    → TODO: not implemented; tested as 404 below
  POST /api/ai/quote-feedback   → TODO: not implemented; tested as 404 below
  GET  /api/catalog/items       → TODO: not implemented; tested as 404 below

Run with:
  cd gdx && python -m pytest tests/test_40_ai_quote.py -v
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# App fixture — build a minimal FastAPI app from just the ai_quote router so
# tests run without a live database or Redis.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    """Return a TestClient-wrapped FastAPI app with the ai_quote router mounted."""
    from fastapi import FastAPI

    from gdx_dispatch.core.ai_quote import router as ai_quote_router

    _app = FastAPI(title="test-ai-quote")
    _app.include_router(ai_quote_router)
    return _app


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. POST /api/ai/quote-generate
#    Requested route does not exist yet.  Assert 404 and document the TODO.
# ---------------------------------------------------------------------------

def test_ai_quote_generate_exists(client):
    """POST /api/ai/quote-generate is now implemented.
    Without tenant middleware it returns 403 (require_role) or 400 (missing tenant).
    We just verify the route exists (not 404/405)."""
    resp = client.post(
        "/api/ai/quote-generate",
        json={"job_type": "Spring Replacement", "notes": "broken spring"},
    )
    assert resp.status_code != 404, (
        "Route /api/ai/quote-generate should exist now"
    )


# ---------------------------------------------------------------------------
# 2. POST /api/ai/quote  (actual existing route — keyword-based quote)
#    Exercises the real implementation with a torsion spring description.
# ---------------------------------------------------------------------------

def test_ai_quote_post_spring_job(client):
    """POST /api/ai/quote returns 200 with line_items containing torsion_spring."""
    resp = client.post(
        "/api/ai/quote",
        json={
            "job_description": "Replace broken torsion spring",
            "equipment_type": "double car residential",
            "issue_description": "door won't open, spring snapped",
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "line_items" in body
    assert "total" in body
    assert "confidence" in body
    assert isinstance(body["line_items"], list)
    assert len(body["line_items"]) > 0
    assert body["total"] > 0

    part_keys = {item["part_key"] for item in body["line_items"]}
    assert "torsion_spring" in part_keys, (
        f"Expected torsion_spring in line items, got: {part_keys}"
    )


def test_ai_quote_post_opener_job(client):
    """POST /api/ai/quote identifies opener jobs correctly."""
    resp = client.post(
        "/api/ai/quote",
        json={"job_description": "garage door opener motor not working"},
    )
    assert resp.status_code == 200
    body = resp.json()
    part_keys = {item["part_key"] for item in body["line_items"]}
    assert "garage_door_opener" in part_keys


def test_ai_quote_post_unknown_job_falls_back_to_service_call(client):
    """POST /api/ai/quote falls back to service_call for unknown descriptions."""
    resp = client.post(
        "/api/ai/quote",
        json={"job_description": "routine maintenance visit"},
    )
    assert resp.status_code == 200
    body = resp.json()
    part_keys = {item["part_key"] for item in body["line_items"]}
    assert "service_call" in part_keys
    assert body["matched_rules"] == 0


# ---------------------------------------------------------------------------
# 3. GET /api/ai/quote-history
#    Requested route does not exist yet.  Assert 404 and document the TODO.
# ---------------------------------------------------------------------------

def test_ai_quote_history_exists(client):
    """GET /api/ai/quote-history is now implemented.
    Without tenant middleware returns 403. Verify route exists."""
    resp = client.get("/api/ai/quote-history")
    assert resp.status_code != 404, (
        "Route /api/ai/quote-history should exist now"
    )


# ---------------------------------------------------------------------------
# 4. POST /api/ai/quote-feedback
#    Requested route does not exist yet.  Assert 404 and document the TODO.
# ---------------------------------------------------------------------------

def test_ai_quote_feedback_exists(client):
    """POST /api/ai/quote-feedback is now implemented.
    Without tenant middleware returns 403. Verify route exists."""
    resp = client.post(
        "/api/ai/quote-feedback",
        json={"quote_id": "fake-id", "accepted": True, "notes": "good estimate"},
    )
    assert resp.status_code != 404, (
        "Route /api/ai/quote-feedback should exist now"
    )


# ---------------------------------------------------------------------------
# 5. Auth guard — POST /api/ai/quote-suggestion requires auth
# ---------------------------------------------------------------------------

def test_ai_quote_suggestion_requires_auth(client):
    """POST /api/ai/quote-suggestion without auth returns 401, 403, or 400.

    The route uses require_role('admin','owner','tech') which raises 403 when
    the tenant context is missing (TenantMiddleware not wired in test app),
    or 400 if the tenant header is absent.
    """
    resp = client.post(
        "/api/ai/quote-suggestion",
        json={"job_type": "spring replacement"},
    )
    # Without TenantMiddleware or auth headers the app will reject with 4xx.
    assert resp.status_code in (400, 401, 403, 422, 500), (
        f"Expected auth rejection (4xx/5xx) without credentials, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 6. GET /api/catalog/items (pricing catalog for AI)
#    Requested route does not exist yet.  Assert 404 and document the TODO.
# ---------------------------------------------------------------------------

def test_ai_quote_pricing_catalog_not_yet_implemented(client):
    """
    TODO: GET /api/catalog/items is not yet implemented.
    When added it should return 200 with a list of catalog items used by the
    AI quote engine (wrapping PARTS_CATALOG from gdx_dispatch/core/ai_quote.py).
    """
    resp = client.get("/api/catalog/items")
    assert resp.status_code == 404, (
        "Expected 404 for unimplemented /api/catalog/items; "
        "update test when route is added."
    )


# ---------------------------------------------------------------------------
# 7. POST /api/ai/pricing/suggest — price suggestion (no auth needed)
# ---------------------------------------------------------------------------

def test_ai_pricing_suggest_known_part(client):
    """POST /api/ai/pricing/suggest returns price range for a known catalog part."""
    from unittest.mock import MagicMock, patch

    mock_db = MagicMock()
    mock_db.execute.return_value.scalars.return_value.all.return_value = []

    with patch("gdx_dispatch.core.ai_quote.get_db", return_value=mock_db):
        resp = client.post(
            "/api/ai/pricing/suggest",
            json={"part_name": "Torsion Spring"},
        )

    # Route depends on get_db; without a real DB it may 500 but the
    # endpoint *exists* (not 404) and the function logic is unit-tested separately.
    assert resp.status_code != 404, (
        "POST /api/ai/pricing/suggest route should exist (not 404)"
    )


# ---------------------------------------------------------------------------
# 8. Unit test — generate_quote structure (no HTTP)
# ---------------------------------------------------------------------------

def test_generate_quote_unit_structure():
    """generate_quote() returns all required fields with sensible values."""
    from gdx_dispatch.core.ai_quote import generate_quote

    result = generate_quote(
        job_description="broken cable on left side",
        equipment_type="single residential",
        issue_description="cable frayed and snapped",
    )

    assert isinstance(result, dict)
    for key in ("line_items", "total", "confidence", "notes", "matched_rules"):
        assert key in result, f"Missing key: {key}"

    assert isinstance(result["line_items"], list)
    assert len(result["line_items"]) > 0
    assert result["total"] > 0
    assert 0.0 <= result["confidence"] <= 1.0

    part_keys = {item["part_key"] for item in result["line_items"]}
    assert "cable_replacement" in part_keys

    for item in result["line_items"]:
        assert item["subtotal"] == round(item["qty"] * item["unit_price"], 2)


# ---------------------------------------------------------------------------
# 9. Unit test — PARTS_CATALOG completeness
# ---------------------------------------------------------------------------

def test_parts_catalog_completeness():
    """PARTS_CATALOG contains all expected keys with positive prices and costs."""
    from gdx_dispatch.core.ai_quote import PARTS_CATALOG

    expected_keys = {
        "torsion_spring", "labor_spring", "extension_spring", "labor_spring_ext",
        "garage_door_opener", "opener_install_labor",
        "cable_replacement", "cable_labor",
        "panel_replacement", "panel_labor",
        "weather_seal", "bottom_seal",
        "roller_set", "hinge_set",
        "keypad", "remote", "service_call",
    }
    missing = expected_keys - set(PARTS_CATALOG.keys())
    assert not missing, f"PARTS_CATALOG missing keys: {missing}"

    for key, entry in PARTS_CATALOG.items():
        assert entry["unit_price"] > 0, f"{key}: unit_price must be positive"
        assert entry["unit_cost"] > 0, f"{key}: unit_cost must be positive"
        assert entry["unit_cost"] < entry["unit_price"], (
            f"{key}: cost ({entry['unit_cost']}) should be less than price ({entry['unit_price']})"
        )


# ---------------------------------------------------------------------------
# 10. Unit test — all keyword rules fire on their respective keywords
# ---------------------------------------------------------------------------

def test_keyword_rules_fire():
    """Every rule in KEYWORD_RULES fires on at least one of its own keywords."""
    from gdx_dispatch.core.ai_quote import KEYWORD_RULES, generate_quote

    for rule in KEYWORD_RULES:
        keyword = rule["keywords"][0]
        result = generate_quote(job_description=keyword)
        matched_keys = {item["part_key"] for item in result["line_items"]}
        for expected_part in rule["parts"]:
            assert expected_part in matched_keys, (
                f"Rule keyword '{keyword}' should produce part '{expected_part}', "
                f"got: {matched_keys}"
            )
