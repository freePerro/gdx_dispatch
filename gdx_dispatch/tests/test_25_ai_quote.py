"""Tests for AI quote generator and pricing intelligence (gdx_dispatch/core/ai_quote.py)."""
from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gdx_dispatch.core.ai_quote import (  # noqa: E402
    analyze_pricing_health,
    generate_quote,
    get_pricing_suggestions,
)

# ---------------------------------------------------------------------------
# Test 1: quote generation returns expected structure
# ---------------------------------------------------------------------------

def test_quote_generation():
    """generate_quote returns valid line_items, total, confidence, notes."""
    result = generate_quote(
        job_description="Replace broken torsion spring",
        equipment_type="double car residential",
        issue_description="door won't open, spring snapped",
    )

    assert isinstance(result, dict)
    assert "line_items" in result
    assert "total" in result
    assert "confidence" in result
    assert "notes" in result
    assert "matched_rules" in result

    assert isinstance(result["line_items"], list)
    assert len(result["line_items"]) > 0, "Should produce at least one line item for spring job"
    assert result["total"] > 0, "Total should be positive"
    assert 0.0 <= result["confidence"] <= 1.0, "Confidence must be in [0, 1]"
    assert result["matched_rules"] >= 1, "Should have matched at least one rule"

    # Torsion spring parts should appear
    part_keys = {item["part_key"] for item in result["line_items"]}
    assert "torsion_spring" in part_keys, "torsion_spring must be in line items"

    # Each line item must have required fields
    for item in result["line_items"]:
        assert "name" in item
        assert "qty" in item
        assert "unit" in item
        assert "unit_price" in item
        assert "subtotal" in item
        assert item["subtotal"] == round(item["qty"] * item["unit_price"], 2)


# ---------------------------------------------------------------------------
# Test 2: pricing suggestion returns correct structure and sensible values
# ---------------------------------------------------------------------------

def test_pricing_suggestion():
    """get_pricing_suggestions returns valid price range for known part."""
    mock_db = MagicMock()
    # Simulate no historical invoice data — triggers catalog fallback
    mock_db.execute.return_value.scalars.return_value.all.return_value = []

    result = get_pricing_suggestions("Torsion Spring", mock_db)

    assert isinstance(result, dict)
    assert result["part_name"] == "Torsion Spring"
    assert "suggested_price" in result
    assert "min_price" in result
    assert "max_price" in result
    assert "market_avg" in result
    assert "sample_count" in result
    assert "source" in result

    assert result["suggested_price"] > 0
    assert result["min_price"] <= result["suggested_price"]
    assert result["max_price"] >= result["suggested_price"]
    assert result["market_avg"] <= result["suggested_price"]
    assert result["source"] in ("historical", "catalog", "default")

    # Known catalog match: Torsion Spring is $185
    assert result["suggested_price"] == pytest.approx(185.00, abs=0.01)
    assert result["source"] == "catalog"


# ---------------------------------------------------------------------------
# Test 3: keyword matching covers all defined rules
# ---------------------------------------------------------------------------

def test_keyword_matching():
    """All KEYWORD_RULES keywords produce matching line items."""
    test_cases = [
        ("spring", "torsion_spring"),
        ("opener motor", "garage_door_opener"),
        ("cable broke", "cable_replacement"),
        ("damaged panel section", "panel_replacement"),
        ("weather seal gap", "weather_seal"),
        ("noisy roller grinding", "roller_set"),
        ("hinge replacement", "hinge_set"),
        ("keypad entry", "keypad"),
        ("remote clicker", "remote"),
    ]

    for description, expected_part in test_cases:
        result = generate_quote(job_description=description)
        part_keys = {item["part_key"] for item in result["line_items"]}
        assert expected_part in part_keys, (
            f"Expected '{expected_part}' in line items for description '{description}'. "
            f"Got: {part_keys}"
        )

    # Unknown description should produce service_call fallback
    fallback = generate_quote(job_description="routine maintenance visit")
    fallback_keys = {item["part_key"] for item in fallback["line_items"]}
    assert "service_call" in fallback_keys, "Unknown description should fall back to service_call"
    assert fallback["matched_rules"] == 0


# ---------------------------------------------------------------------------
# Test 4: pricing health analysis returns valid structure
# ---------------------------------------------------------------------------

def test_pricing_health_analysis(tenant_db):
    """analyze_pricing_health returns expected dict shape and valid values."""
    tenant_id = str(uuid.uuid4())
    result = analyze_pricing_health(tenant_id=tenant_id, db=tenant_db)

    assert isinstance(result, dict)
    assert result["tenant_id"] == tenant_id
    assert "avg_margin" in result
    assert "below_market_items" in result
    assert "above_market_items" in result
    assert "total_invoices_analyzed" in result
    assert "health_score" in result
    assert "recommendations" in result

    assert isinstance(result["avg_margin"], float)
    assert 0.0 <= result["avg_margin"] <= 1.0
    assert result["health_score"] in ("good", "fair", "poor")
    assert isinstance(result["below_market_items"], list)
    assert isinstance(result["above_market_items"], list)
    assert isinstance(result["recommendations"], list)
    assert len(result["recommendations"]) >= 1

    # With no invoices the default margin (0.35) should yield "good"
    assert result["health_score"] == "good", (
        f"Expected 'good' with default margin=0.35, got '{result['health_score']}'"
    )


# ---------------------------------------------------------------------------
# Test 5: AI quote page renders (template exists and imports are valid)
# ---------------------------------------------------------------------------

def test_quote_page_renders():
    """ai_quote.html template exists and all exported symbols are importable."""
    import importlib

    # Module must import without error
    mod = importlib.import_module("gdx_dispatch.core.ai_quote")

    # Required public symbols
    for sym in ("PARTS_CATALOG", "KEYWORD_RULES", "generate_quote",
                "get_pricing_suggestions", "analyze_pricing_health", "router"):
        assert hasattr(mod, sym), f"gdx_dispatch.core.ai_quote missing symbol: {sym}"

    # PARTS_CATALOG must have all expected keys
    expected_keys = {
        "torsion_spring", "labor_spring", "extension_spring", "labor_spring_ext",
        "garage_door_opener", "opener_install_labor", "cable_replacement", "cable_labor",
        "panel_replacement", "panel_labor", "weather_seal", "bottom_seal",
        "roller_set", "hinge_set", "keypad", "remote", "service_call",
    }
    assert expected_keys <= set(mod.PARTS_CATALOG.keys()), (
        f"Missing catalog keys: {expected_keys - set(mod.PARTS_CATALOG.keys())}"
    )

    # Template file must exist
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "ai_quote.html"
    )
    assert os.path.isfile(template_path), f"Template not found: {template_path}"

    # Router must have the three new routes
    route_paths = {r.path for r in mod.router.routes}
    assert "/api/ai/quote" in route_paths, "Missing POST /api/ai/quote route"
    assert "/api/ai/pricing/suggest" in route_paths, "Missing POST /api/ai/pricing/suggest route"
    assert "/api/ai/pricing/health" in route_paths, "Missing GET /api/ai/pricing/health route"
