"""apply_stock_delta — the single atomic inventory-quantity mutation path."""
from __future__ import annotations

from decimal import Decimal

from gdx_dispatch.models.tenant_models import InventoryItem, StockAdjustment
from gdx_dispatch.modules.inventory.stock import apply_stock_delta


def _item(db, qty=0):
    item = InventoryItem(part_name="Widget", quantity=qty, unit_cost=Decimal("0"))
    db.add(item)
    db.flush()
    return item


def test_increment_and_logs_adjustment(tenant_db):
    item = _item(tenant_db, qty=5)
    adj = apply_stock_delta(tenant_db, item, delta=3, reason="test", notes="n")
    assert item.quantity == 8
    assert adj.quantity_delta == 3
    assert adj.reason == "test"
    assert adj.item_id == item.id
    assert tenant_db.query(StockAdjustment).count() == 1


def test_negative_delta_allowed_without_clamp(tenant_db):
    item = _item(tenant_db, qty=2)
    apply_stock_delta(tenant_db, item, delta=-5, reason="credit")
    assert item.quantity == -3  # credit memo / correction can go negative


def test_clamp_nonneg_floors_at_zero(tenant_db):
    item = _item(tenant_db, qty=2)
    adj = apply_stock_delta(tenant_db, item, delta=-5, reason="adjust", clamp_nonneg=True)
    assert item.quantity == 0
    # The adjustment records the REQUESTED delta, not the clamped result.
    assert adj.quantity_delta == -5


def test_rejects_item_with_unflushed_changes(tenant_db):
    """The FOR UPDATE refresh would discard a pending edit — the helper refuses
    loudly instead of silently losing it."""
    import pytest
    item = _item(tenant_db, qty=5)
    item.unit_cost = Decimal("99")  # unflushed change → item is dirty
    with pytest.raises(ValueError):
        apply_stock_delta(tenant_db, item, delta=1, reason="x")


def test_repeated_deltas_on_same_item_accumulate(tenant_db):
    """The receive_po multi-line case: two deltas to the same item accumulate
    (each locks + re-reads, so no lost update)."""
    item = _item(tenant_db, qty=0)
    apply_stock_delta(tenant_db, item, delta=2, reason="po_receive")
    apply_stock_delta(tenant_db, item, delta=3, reason="po_receive")
    assert item.quantity == 5
    assert tenant_db.query(StockAdjustment).count() == 2
