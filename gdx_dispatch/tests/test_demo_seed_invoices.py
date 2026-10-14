"""The demo seed's invoices reconcile (GDXA-951).

The public demo opened on INV-2026-1001: $612.40 + $50.52 sales tax, no job,
no lines, and the paid invoices were "paid" by a zeroed balance with no
payment behind it. This runs the real ``seed()`` against an in-memory DB and
checks every seeded invoice against its lines, its job's tax lane, its
payments, and the app's own recalculation.
"""
from __future__ import annotations

import importlib.util
import itertools
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, Payment
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.service import is_install_lane
from gdx_dispatch.tests.conftest import make_fresh_db, production_sessionmaker

SEED_PATH = Path(__file__).resolve().parents[1] / "docker" / "demo" / "seed_demo.py"
TENANT = "00000000-0000-0000-0000-0000000000d1"


def _cents(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@pytest.fixture
def seeded(monkeypatch):
    spec = importlib.util.spec_from_file_location("seed_demo_under_test", SEED_PATH)
    seed_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_demo)

    engine = make_fresh_db()
    Session = production_sessionmaker(engine)
    numbers = itertools.count(1)
    monkeypatch.setattr(seed_demo, "SessionLocal", Session)
    monkeypatch.setattr(seed_demo, "company_id", lambda: TENANT)
    monkeypatch.setattr(
        seed_demo, "allocate_job_number",
        lambda _tenant, customer_name=None: f"J-{next(numbers):04d}",
    )
    seed_demo.seed()

    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _invoices(db):
    return db.execute(select(Invoice).order_by(Invoice.sequence_number)).scalars().all()


def test_every_seeded_invoice_reconciles_to_its_lines_job_and_payments(seeded):
    db = seeded
    invoices = _invoices(db)
    assert len(invoices) == 8

    default_rate = Decimal(str(db.execute(select(TaxConfig)).scalar_one().default_rate))
    assert default_rate == Decimal("0.0825")
    exempt = {
        row.customer_id for row in db.execute(select(TaxExemption)).scalars()
        if row.exempt
    }

    lanes = set()
    for inv in invoices:
        label = inv.invoice_number
        job = db.get(Job, inv.job_id) if inv.job_id else None
        assert job is not None, f"{label} bills no job"
        assert job.customer_id == inv.customer_id, f"{label} bills another customer's job"

        lines = db.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
        ).scalars().all()
        assert lines, f"{label} has no lines"
        for ln in lines:
            assert _cents(ln.line_total) == _cents(Decimal(str(ln.quantity)) * Decimal(str(ln.unit_price)))
            if (ln.category or "").lower() == "labor":
                assert ln.taxable is False, f"{label}: labor taxed with tax_labor off"
        assert _cents(inv.subtotal) == _cents(sum(Decimal(str(ln.line_total)) for ln in lines)), label

        if is_install_lane(job.job_type):
            expected_rate, lane = Decimal("0"), "install"
        elif inv.customer_id in exempt:
            expected_rate, lane = Decimal("0"), "exempt"
        else:
            expected_rate, lane = default_rate, "taxed"
        lanes.add(lane)
        taxable = sum(Decimal(str(ln.line_total)) for ln in lines if ln.taxable)
        assert Decimal(str(inv.tax_rate)) == expected_rate, f"{label} ({lane})"
        assert _cents(inv.tax_amount) == _cents(taxable * expected_rate), f"{label} ({lane})"
        assert _cents(inv.total) == _cents(inv.subtotal) + _cents(inv.tax_amount), label

        paid = sum(
            Decimal(str(p.amount)) for p in db.execute(
                select(Payment).where(Payment.invoice_id == inv.id, Payment.voided_at.is_(None))
            ).scalars()
        )
        assert _cents(inv.balance_due) == _cents(Decimal(str(inv.total)) - paid), label
        if inv.status == "paid":
            assert paid == Decimal(str(inv.total)) and inv.paid_at is not None, label
        else:
            assert paid == 0 and inv.paid_at is None, label

    # The demo shows every branch of the rule, so a reviewer can see it.
    assert lanes == {"taxed", "install", "exempt"}


def test_app_recalculation_leaves_every_seeded_invoice_unchanged(seeded):
    """The office's own recalculation is the falsifier: a seed whose figures
    drift from what an edit would produce shows one total and bills another."""
    from gdx_dispatch.routers.invoices import _recalculate_invoice

    db = seeded
    for inv in _invoices(db):
        before = (inv.status, _cents(inv.subtotal), _cents(inv.tax_amount),
                  _cents(inv.total), _cents(inv.balance_due))
        _recalculate_invoice(inv, db)
        after = (inv.status, _cents(inv.subtotal), _cents(inv.tax_amount),
                 _cents(inv.total), _cents(inv.balance_due))
        assert after == before, inv.invoice_number
    db.rollback()
