"""pull_bank_transactions against a REAL (ORM-shaped) table — the v1.59.0
deploy found this path had never run outside mocks: the raw INSERT omitted
NOT-NULL ``synced_at`` (no server default on the ORM shape), and the first
failure's transaction abort cascaded a 1,627-row pull into all-errors.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import text

from gdx_dispatch.modules.quickbooks import sync


class _FakeQB:
    def __init__(self, rows):
        self._rows = rows
        self.read_count = 0

    async def query(self, entity, where="", max_results=1000):
        assert entity == "Purchase"
        return self._rows


def _purchase(qb_id, amount=100.0):
    return {
        "Id": qb_id,
        "TxnDate": "2026-08-01",
        "TotalAmt": amount,
        "PaymentType": "Check",
        "PrivateNote": "test",
        "EntityRef": {"name": "Sample Vendor"},
        "AccountRef": {"name": "Checking"},
    }


def _run_pull(db, rows):
    return asyncio.run(sync.pull_bank_transactions("tenant-test", db, _FakeQB(rows)))


def test_fresh_insert_supplies_synced_at(tenant_db):
    out = _run_pull(tenant_db, [_purchase("p1"), _purchase("p2", 55.5)])
    assert out["errors"] == []
    assert out["created"] == 2
    rows = tenant_db.execute(text(
        "SELECT qb_txn_id, synced_at FROM qb_bank_transactions ORDER BY qb_txn_id"
    )).all()
    assert [r[0] for r in rows] == ["p1", "p2"]
    assert all(r[1] is not None for r in rows), "synced_at must be written on INSERT"


def test_poisoned_row_does_not_cascade(tenant_db, monkeypatch):
    """One bad row (here: a NOT NULL violation forced through a broken id)
    must error alone — the savepoint keeps the transaction alive so every
    other row still lands."""
    calls = {"n": 0}
    real_uuid4 = sync.uuid4

    class _Bomb:
        def __str__(self):
            raise ValueError("poisoned row")

    def poisoned_uuid4():
        calls["n"] += 1
        if calls["n"] == 2:  # second INSERT blows up mid-statement-build
            return _Bomb()
        return real_uuid4()

    monkeypatch.setattr(sync, "uuid4", poisoned_uuid4)
    out = _run_pull(tenant_db, [_purchase("a1"), _purchase("a2"), _purchase("a3")])
    assert out["created"] == 2
    assert len(out["errors"]) == 1 and out["errors"][0]["qb_id"] == "a2"
    survivors = tenant_db.execute(text(
        "SELECT qb_txn_id FROM qb_bank_transactions ORDER BY qb_txn_id")).scalars().all()
    assert survivors == ["a1", "a3"]


def test_resync_updates_and_untombstones(tenant_db):
    _run_pull(tenant_db, [_purchase("r1")])
    tenant_db.execute(text(
        "UPDATE qb_bank_transactions SET deleted_at = :now WHERE qb_txn_id = 'r1'"
    ), {"now": datetime(2026, 8, 2)})
    tenant_db.commit()
    out = _run_pull(tenant_db, [_purchase("r1", 77.0)])
    assert out["updated"] == 1 and out["errors"] == []
    row = tenant_db.execute(text(
        "SELECT amount, deleted_at FROM qb_bank_transactions WHERE qb_txn_id = 'r1'"
    )).one()
    assert float(row[0]) == 77.0
    assert row[1] is None
