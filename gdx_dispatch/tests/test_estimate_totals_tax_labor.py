"""Unit tests for compute_estimate_totals with the TaxConfig.tax_labor flag.

Locks in the rule Doug confirmed 2026-05-05: when category == 'labor'
(case-insensitive — labor-matrix picks set "Labor", manual lines can use
the same dropdown value), those lines are excluded from the taxable
subtotal unless TaxConfig.tax_labor is True.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
from gdx_dispatch.modules.tax.models import TaxConfig


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _make_estimate(
    db,
    *,
    lines: list[dict],
    estimate_tax_rate: float | None = None,
    discount: float | None = None,
):
    est = Estimate(
        estimate_number=f"E-{uuid4().hex[:8]}",
        status="sent",
        company_id="tenant-test",
        public_token=uuid4().hex,
        tax_rate=Decimal(str(estimate_tax_rate)) if estimate_tax_rate is not None else None,
        discount=Decimal(str(discount)) if discount is not None else None,
        total=Decimal("0"),
    )
    db.add(est)
    db.flush()
    sub = Decimal("0")
    for ln in lines:
        qty = ln.get("quantity", 1)
        unit = Decimal(str(ln["unit_price"]))
        line_total = unit * qty
        sub += line_total
        db.add(EstimateLine(
            estimate_id=est.id,
            description=ln.get("description", "line"),
            category=ln.get("category"),
            quantity=qty,
            unit_price=unit,
            line_total=line_total,
            company_id="tenant-test",
        ))
    est.total = sub
    db.commit()
    db.refresh(est)
    return est


def _set_config(db, *, default_rate=0.0, tax_labor=False):
    cfg = TaxConfig(name="Default", default_rate=Decimal(str(default_rate)), tax_labor=tax_labor)
    db.add(cfg)
    db.commit()


def test_default_tax_labor_false_excludes_labor_lines(db):
    _set_config(db, default_rate=0.10, tax_labor=False)
    est = _make_estimate(db, lines=[
        {"category": "Doors", "unit_price": "1000.00"},
        {"category": "Labor", "unit_price": "500.00"},
    ])
    t = compute_estimate_totals(est, db)
    assert t["subtotal"] == 1500.00
    assert t["labor_subtotal"] == 500.00
    assert t["taxable_subtotal"] == 1000.00  # labor excluded
    assert t["tax"] == 100.00  # 10% of $1000
    assert t["total"] == 1600.00  # subtotal + tax (no discount)
    assert t["tax_labor"] is False


def test_tax_labor_true_taxes_everything(db):
    _set_config(db, default_rate=0.10, tax_labor=True)
    est = _make_estimate(db, lines=[
        {"category": "Doors", "unit_price": "1000.00"},
        {"category": "Labor", "unit_price": "500.00"},
    ])
    t = compute_estimate_totals(est, db)
    assert t["taxable_subtotal"] == 1500.00
    assert t["tax"] == 150.00  # 10% of full subtotal
    assert t["total"] == 1650.00
    assert t["tax_labor"] is True


def test_labor_match_is_case_insensitive(db):
    _set_config(db, default_rate=0.10, tax_labor=False)
    est = _make_estimate(db, lines=[
        {"category": "labor", "unit_price": "200"},  # lowercase
        {"category": "LABOR", "unit_price": "100"},  # uppercase
        {"category": " Labor ", "unit_price": "50"},  # whitespace
        {"category": "Materials", "unit_price": "300"},
    ])
    t = compute_estimate_totals(est, db)
    assert t["labor_subtotal"] == 350.00  # 200+100+50
    assert t["taxable_subtotal"] == 300.00
    assert t["tax"] == 30.00


def test_estimate_level_tax_rate_wins_over_default(db):
    _set_config(db, default_rate=0.10, tax_labor=False)
    est = _make_estimate(db, estimate_tax_rate=0.05, lines=[
        {"category": "Materials", "unit_price": "1000"},
        {"category": "Labor", "unit_price": "500"},
    ])
    t = compute_estimate_totals(est, db)
    assert t["tax_rate"] == 0.05
    assert t["taxable_subtotal"] == 1000.00  # labor still excluded
    assert t["tax"] == 50.00  # 5% of $1000


def test_discount_applies_to_materials_after_labor_removed(db):
    _set_config(db, default_rate=0.10, tax_labor=False)
    est = _make_estimate(db, discount=100, lines=[
        {"category": "Materials", "unit_price": "1000"},
        {"category": "Labor", "unit_price": "500"},
    ])
    t = compute_estimate_totals(est, db)
    # subtotal=1500, labor=500, taxable_pre_discount=1000, taxable=900,
    # tax=90, total = (1500−100) + 90 = 1490
    assert t["taxable_subtotal"] == 900.00
    assert t["tax"] == 90.00
    assert t["total"] == 1490.00


def test_uncategorized_lines_are_taxed(db):
    _set_config(db, default_rate=0.10, tax_labor=False)
    est = _make_estimate(db, lines=[
        {"category": None, "unit_price": "1000"},
        {"category": "Labor", "unit_price": "500"},
    ])
    t = compute_estimate_totals(est, db)
    # Null-category lines aren't labor → taxed
    assert t["taxable_subtotal"] == 1000.00
    assert t["tax"] == 100.00


def test_no_taxconfig_row_treats_labor_as_exempt(db):
    # Fresh tenant — no TaxConfig row yet. Default behavior should still
    # exclude labor (tax_labor is False by default).
    est = _make_estimate(db, estimate_tax_rate=0.10, lines=[
        {"category": "Labor", "unit_price": "500"},
        {"category": "Materials", "unit_price": "1000"},
    ])
    t = compute_estimate_totals(est, db)
    assert t["taxable_subtotal"] == 1000.00
    assert t["tax"] == 100.00
