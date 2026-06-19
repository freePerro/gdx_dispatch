"""Tests for AI quote generation and parts pricing intelligence.

gdx_dispatch/core/ai_quote.py  — QuoteTemplate model, generate_quote_suggestion
gdx_dispatch/core/parts_pricing.py — PartPrice model, _compute_margin
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

# Ensure the repo root is on sys.path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gdx_dispatch.core.ai_quote import QuoteSuggestion, QuoteTemplate, generate_quote_suggestion  # noqa: E402
from gdx_dispatch.core.parts_pricing import PartPrice, _compute_margin  # noqa: E402

# ---------------------------------------------------------------------------
# Test 1: quote suggestion structure
# ---------------------------------------------------------------------------

def test_quote_suggestion_structure(tenant_db):
    """generate_quote_suggestion returns a valid QuoteSuggestion with all fields."""
    result = generate_quote_suggestion("spring replacement", "90210", "tenant-123", tenant_db)

    assert isinstance(result, QuoteSuggestion)
    assert result.job_type == "spring replacement"
    assert isinstance(result.parts, list)
    assert isinstance(result.labor_hours, float)
    assert isinstance(result.labor_rate, float)
    assert isinstance(result.subtotal, float)
    assert isinstance(result.markup_pct, float)
    assert isinstance(result.total_price, float)
    assert result.total_price > 0
    assert "low" in result.price_range
    assert "high" in result.price_range
    assert result.price_range["high"] > result.price_range["low"]
    assert result.confidence in ("high", "medium", "low")
    assert isinstance(result.similar_jobs_count, int)


# ---------------------------------------------------------------------------
# Test 2: confidence=low with no history
# ---------------------------------------------------------------------------

def test_quote_confidence_low_no_history(tenant_db):
    """Unknown job type with no history returns confidence=low and count=0."""
    result = generate_quote_suggestion(
        "some unknown job type xyz999", "12345", "tenant-xyz", tenant_db
    )

    assert result.confidence == "low"
    assert result.similar_jobs_count == 0
    assert result.total_price > 0  # still returns a usable default


# ---------------------------------------------------------------------------
# Test 3: confidence=high with 12 history records
# ---------------------------------------------------------------------------

def test_quote_confidence_high_with_history(tenant_db):
    """12 QuoteTemplate records yields confidence=high."""
    now = datetime.now(timezone.utc)
    for _ in range(12):
        tenant_db.add(
            QuoteTemplate(
                tenant_id="tenant-high",
                job_type="door panel replacement",
                typical_parts=[{"name": "Panel", "estimated_cost": 150.0, "typical_qty": 1}],
                typical_labor_hours=2.5,
                typical_price_low=350.0,
                typical_price_high=500.0,
                last_used_at=now,
                use_count=3,
                created_at=now,
            )
        )
    tenant_db.commit()

    result = generate_quote_suggestion(
        "door panel replacement", "10001", "tenant-high", tenant_db
    )

    assert result.confidence == "high"
    assert result.similar_jobs_count >= 10


# ---------------------------------------------------------------------------
# Test 4: parts pricing CRUD
# ---------------------------------------------------------------------------

def test_parts_pricing_crud(tenant_db):
    """Create a PartPrice record and read it back with correct values."""
    now = datetime.now(timezone.utc)
    margin = _compute_margin(85.0, 149.0)

    part = PartPrice(
        tenant_id="tenant-crud",
        part_number="SP-001",
        part_name="Torsion Spring",
        cost_price=85.0,
        sell_price=149.0,
        margin_pct=margin,
        supplier="Acme Parts",
        price_history=[],
        created_at=now,
        last_updated_at=now,
    )
    tenant_db.add(part)
    tenant_db.commit()

    fetched = (
        tenant_db.query(PartPrice)
        .filter(PartPrice.part_number == "SP-001", PartPrice.tenant_id == "tenant-crud")
        .first()
    )

    assert fetched is not None
    assert fetched.part_name == "Torsion Spring"
    assert float(fetched.sell_price) == pytest.approx(149.0)
    assert float(fetched.cost_price) == pytest.approx(85.0)
    assert float(fetched.margin_pct) == pytest.approx(_compute_margin(85.0, 149.0), abs=0.001)
    assert fetched.supplier == "Acme Parts"


# ---------------------------------------------------------------------------
# Test 5: bulk update creates new and updates existing
# ---------------------------------------------------------------------------

def test_parts_bulk_update_creates_and_updates(tenant_db):
    """Simulate bulk update: existing part gets new price, new part is created."""
    now = datetime.now(timezone.utc)

    # Seed existing part
    existing = PartPrice(
        tenant_id="tenant-bulk",
        part_number="BULK-001",
        part_name="Cable",
        cost_price=12.0,
        sell_price=25.0,
        margin_pct=_compute_margin(12.0, 25.0),
        supplier=None,
        price_history=[],
        created_at=now,
        last_updated_at=now,
    )
    tenant_db.add(existing)
    tenant_db.commit()

    # Simulate bulk update: update BULK-001 sell price, create BULK-002
    to_update = (
        tenant_db.query(PartPrice)
        .filter(PartPrice.part_number == "BULK-001", PartPrice.tenant_id == "tenant-bulk")
        .first()
    )
    old_history = list(to_update.price_history or [])
    old_history.append({
        "date": now.isoformat(),
        "cost_price": float(to_update.cost_price),
        "sell_price": float(to_update.sell_price),
        "margin_pct": float(to_update.margin_pct),
    })
    to_update.sell_price = 30.0
    to_update.margin_pct = _compute_margin(12.0, 30.0)
    to_update.price_history = old_history
    to_update.last_updated_at = now

    new_part = PartPrice(
        tenant_id="tenant-bulk",
        part_number="BULK-002",
        part_name="Roller",
        cost_price=5.0,
        sell_price=15.0,
        margin_pct=_compute_margin(5.0, 15.0),
        supplier=None,
        price_history=[],
        created_at=now,
        last_updated_at=now,
    )
    tenant_db.add(new_part)
    tenant_db.commit()

    # Verify
    bulk001 = (
        tenant_db.query(PartPrice)
        .filter(PartPrice.part_number == "BULK-001", PartPrice.tenant_id == "tenant-bulk")
        .first()
    )
    bulk002 = (
        tenant_db.query(PartPrice)
        .filter(PartPrice.part_number == "BULK-002", PartPrice.tenant_id == "tenant-bulk")
        .first()
    )

    assert float(bulk001.sell_price) == pytest.approx(30.0)
    assert len(bulk001.price_history) >= 1  # history appended
    assert bulk002 is not None
    assert bulk002.part_name == "Roller"


# ---------------------------------------------------------------------------
# Test 6: margin analysis and suggest markup
# ---------------------------------------------------------------------------

def test_margin_analysis_and_suggest_markup(tenant_db):
    """Verify margin computation and markup suggestion math."""
    now = datetime.now(timezone.utc)

    # Part A: cost=50, sell=100 → margin=0.5
    # Part B: cost=80, sell=100 → margin=0.2
    # Part C: cost=60, sell=100 → margin=0.4
    for part_number, cost, sell in [("MA-001", 50.0, 100.0), ("MA-002", 80.0, 100.0), ("MA-003", 60.0, 100.0)]:
        tenant_db.add(
            PartPrice(
                tenant_id="tenant-margin",
                part_number=part_number,
                part_name=f"Part {part_number}",
                cost_price=cost,
                sell_price=sell,
                margin_pct=_compute_margin(cost, sell),
                supplier=None,
                price_history=[],
                created_at=now,
                last_updated_at=now,
            )
        )
    tenant_db.commit()

    # Verify _compute_margin values
    assert _compute_margin(50.0, 100.0) == pytest.approx(0.5)
    assert _compute_margin(80.0, 100.0) == pytest.approx(0.2)
    assert _compute_margin(60.0, 100.0) == pytest.approx(0.4)
    assert _compute_margin(0.0, 0.0) == 0.0  # zero sell = 0 margin

    # Fetch all 3 parts and compute avg margin
    parts = (
        tenant_db.query(PartPrice)
        .filter(PartPrice.tenant_id == "tenant-margin", PartPrice.deleted_at.is_(None))
        .all()
    )
    assert len(parts) == 3

    avg_margin = sum(float(p.margin_pct) for p in parts) / len(parts)
    assert avg_margin == pytest.approx((0.5 + 0.2 + 0.4) / 3, abs=0.001)

    # Suggest markup for cost=60 using avg_margin ≈ 0.3667
    # suggest = 60 / (1 - 0.3667) ≈ 94.74
    suggest = 60.0 / (1.0 - avg_margin)
    assert suggest > 60.0
    assert suggest == pytest.approx(94.74, abs=0.5)
