"""Atomic inventory-quantity mutation.

``item.quantity = item.quantity + delta`` done as a plain read-modify-write is a
lost-update race: two requests targeting the same InventoryItem both read the
old value and both write old+delta, so one delta vanishes. This helper is the
single safe path — it locks the item row FOR UPDATE before reading, so
concurrent deltas serialize (the second blocks until the first commits, then
reads the updated quantity). On Postgres that's a real row lock; on SQLite
(tests) FOR UPDATE is a no-op, which is fine because those runs are
single-threaded.

Every quantity change (vendor-invoice receipt, PO receive, manual adjust) goes
through here and records a ``StockAdjustment`` for the audit trail.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import InventoryItem, StockAdjustment


def apply_stock_delta(
    db: Session,
    item: InventoryItem,
    *,
    delta: int,
    reason: str,
    notes: str | None = None,
    job_id: UUID | None = None,
    clamp_nonneg: bool = False,
) -> StockAdjustment:
    """Atomically add ``delta`` to ``item.quantity`` and record a
    ``StockAdjustment``. Returns the adjustment (its ``.id`` and the updated
    ``item.quantity`` are both available to the caller).

    ``clamp_nonneg=True`` floors the result at 0 (manual-adjust semantics);
    vendor-invoice receipts and PO receives pass False (allow-negative, for
    credit memos / corrections).

    Precondition: ``item`` must have NO unflushed changes. The FOR UPDATE refresh
    below reloads the row and would silently discard any pending in-memory edit
    to ``item`` — so mutate ``item`` (e.g. ``unit_cost``) AFTER this call, not
    before. Violations raise loudly instead of losing data.
    """
    if item in db.dirty:
        raise ValueError(
            "apply_stock_delta: `item` has unflushed changes that the FOR UPDATE "
            "refresh would discard — flush first, or mutate the item after this call."
        )
    # Lock + re-read the committed quantity so concurrent deltas can't lost-update.
    db.refresh(item, with_for_update=True)
    new_qty = (item.quantity or 0) + delta
    if clamp_nonneg and new_qty < 0:
        new_qty = 0
    item.quantity = new_qty

    adj = StockAdjustment(
        item_id=item.id,
        quantity_delta=delta,
        reason=reason,
        notes=notes,
        job_id=job_id,
    )
    db.add(adj)
    db.flush()
    return adj
