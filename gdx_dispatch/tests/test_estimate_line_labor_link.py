"""S97 slice 4 — EstimateLine ↔ LaborPriceItem wiring.

Verifies:
  1. New columns exist on estimate_lines: labor_price_item_id (UUID, nullable),
     estimated_man_hours (Numeric, nullable).
  2. FK to labor_price_items.id resolves; ondelete='SET NULL' is configured.
  3. A line can be linked to a labor row, snapshot man-hours, and survive the
     matrix row being deactivated (active=False) — the link still resolves.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine


def _mk_estimate(db, *, num="EST-S97-1"):
    est = Estimate(
        id=uuid4(),
        estimate_number=num,
        public_token=f"tok-{num}",
        company_id="t-test",
        status="draft",
    )
    db.add(est)
    db.commit()
    return est


def _mk_labor_row(db, **overrides):
    base = dict(
        id=uuid4(),
        description="10x8 Sectional Install",
        service_type="install",
        width_ft=10,
        height_ft=8,
        flat_price=Decimal("500.00"),
        assumed_man_hours=Decimal("5.00"),
    )
    base.update(overrides)
    row = LaborPriceItem(**base)
    db.add(row)
    db.commit()
    return row


def test_estimate_line_has_labor_link_columns(tenant_db):
    cols = {c.name for c in EstimateLine.__table__.columns}
    assert "labor_price_item_id" in cols
    assert "estimated_man_hours" in cols
    # Both nullable — labor link is optional, lines can be free-form.
    assert EstimateLine.__table__.c.labor_price_item_id.nullable is True
    assert EstimateLine.__table__.c.estimated_man_hours.nullable is True


def test_estimate_line_fk_targets_labor_price_items(tenant_db):
    fks = list(EstimateLine.__table__.c.labor_price_item_id.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "labor_price_items"
    assert fk.column.name == "id"
    assert fk.ondelete == "SET NULL"


def test_estimate_line_links_to_labor_row(tenant_db):
    est = _mk_estimate(tenant_db)
    row = _mk_labor_row(tenant_db)

    line = EstimateLine(
        id=uuid4(),
        estimate_id=est.id,
        description=row.description,
        unit_price=row.flat_price,
        quantity=1,
        line_total=row.flat_price,
        labor_price_item_id=row.id,
        estimated_man_hours=row.assumed_man_hours,
        company_id="t-test",
    )
    tenant_db.add(line)
    tenant_db.commit()

    fetched = tenant_db.query(EstimateLine).filter_by(id=line.id).one()
    assert fetched.labor_price_item_id == row.id
    assert fetched.estimated_man_hours == Decimal("5.00")


def test_estimate_line_survives_matrix_row_archive(tenant_db):
    """Archiving (active=False) is soft-delete; the FK link must still resolve."""
    est = _mk_estimate(tenant_db, num="EST-S97-2")
    row = _mk_labor_row(tenant_db)
    line = EstimateLine(
        id=uuid4(),
        estimate_id=est.id,
        description=row.description,
        unit_price=row.flat_price,
        quantity=1,
        line_total=row.flat_price,
        labor_price_item_id=row.id,
        estimated_man_hours=row.assumed_man_hours,
        company_id="t-test",
    )
    tenant_db.add(line)
    tenant_db.commit()

    row.active = False
    tenant_db.commit()

    fetched = tenant_db.query(EstimateLine).filter_by(id=line.id).one()
    assert fetched.labor_price_item_id == row.id
    assert fetched.estimated_man_hours == Decimal("5.00")


def test_estimate_line_link_is_optional(tenant_db):
    """Free-form lines without a labor link still work."""
    est = _mk_estimate(tenant_db, num="EST-S97-3")
    line = EstimateLine(
        id=uuid4(),
        estimate_id=est.id,
        description="Custom: trip charge",
        unit_price=Decimal("75.00"),
        quantity=1,
        line_total=Decimal("75.00"),
        company_id="t-test",
    )
    tenant_db.add(line)
    tenant_db.commit()

    fetched = tenant_db.query(EstimateLine).filter_by(id=line.id).one()
    assert fetched.labor_price_item_id is None
    assert fetched.estimated_man_hours is None
