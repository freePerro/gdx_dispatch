"""tools/void_invoice_balance_repair.py — zeroing a void that still owes money.

GDXA-267 stops `_recalculate_invoice` writing a balance onto a void. This tool
repairs voids already carrying one, whatever wrote them (the one prod row,
$100.00, came from a raw-SQL void in tools/qb_payment_substance_repair.py
that never zeroed the balance, not from a recalc). These assert the
row that LANDS — the balance and the audit trail read back from the database —
not the arguments passed, because #661 was an audit row that mocks proved
"written" for its whole life while it never once was.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import Invoice

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import void_invoice_balance_repair as repair  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _invoice(db, *, number, status, balance, total=100.00):
    inv = Invoice(
        customer_id=uuid.uuid4(), job_id=uuid.uuid4(), invoice_number=number,
        billing_type="standard", subtotal=total, tax_amount=0, total=total,
        balance_due=balance, status=status, company_id="tenant-test",
        public_token=uuid.uuid4().hex,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_selects_only_voids_that_carry_a_balance(db):
    armed = _invoice(db, number="V-1", status="void", balance=100.00)
    _invoice(db, number="V-2", status="void", balance=0)
    _invoice(db, number="S-1", status="sent", balance=100.00)
    found = repair.fetch_armed(db)
    assert [a.invoice_id for a in found] == [str(armed.id)]


def test_apply_zeroes_the_balance_keeps_the_total_and_audits_the_old_value(db):
    inv = _invoice(db, number="V-1", status="void", balance=100.00)
    n = repair.apply_plan(db, repair.fetch_armed(db), operator="doug")
    assert n == 1

    db.expire_all()
    row = db.get(Invoice, inv.id)
    assert float(row.balance_due) == 0.0
    assert float(row.total) == 100.0
    assert row.status == "void"

    audits = db.query(AuditLog).filter(AuditLog.action == "void_invoice_balance_zeroed").all()
    assert len(audits) == 1, "the zeroing must leave its trail in the database"
    assert audits[0].user_id == "cli:doug", "a money mutation names its actor"
    assert str(inv.id) in str(audits[0].entity_id)
    assert audits[0].details["previous_balance_due"] == 100.0, "the rollback value must be recorded"


def test_a_second_run_finds_nothing(db):
    _invoice(db, number="V-1", status="void", balance=100.00)
    repair.apply_plan(db, repair.fetch_armed(db), operator="doug")
    assert repair.fetch_armed(db) == []


def test_apply_without_operator_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["void_invoice_balance_repair.py", "--apply"])
    with pytest.raises(SystemExit) as exc:
        repair.main()
    assert exc.value.code == 2
