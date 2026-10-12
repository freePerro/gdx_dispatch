"""The invoice totals invariant must see what a flush already wrote (GDXA-678).

The commit-time guard in ``core/invoice_invariants.py`` used to build its list
from ``session.new`` and ``session.dirty`` at ``before_commit``. A flush empties
both, so any handler that flushed before committing — thirty files under
``routers/`` and ``services/`` do — committed a hand-set wrong total unchecked.
And an edit to a line alone never made the invoice dirty, so it was never
checked even without a flush.

These run the real listener against a real SQLite session: each asserts on
whether the commit landed, read back from the database, not on a mock.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.invoice_invariants import (
    _TOUCHED_KEY,
    InvoiceTotalsInvariantError,
)
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Job

TENANT = "tenant-invariant-flush"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.delenv("GDX_INVOICE_INVARIANT", raising=False)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for tbl in [Job.__table__, Customer.__table__, Invoice.__table__, InvoiceLine.__table__]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _invoice(db, *, total, line_totals=(1,), tax=0, totals_locked=False):
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:6].upper()}",
        subtotal=Decimal(str(sum(line_totals))),
        tax_amount=Decimal(str(tax)),
        total=Decimal(str(total)),
        balance_due=Decimal(str(total)),
        status="draft",
        public_token=f"tok-{uuid4().hex}",
        company_id=TENANT,
        totals_locked=totals_locked,
    )
    db.add(inv)
    lines = []
    for i, amount in enumerate(line_totals):
        line = InvoiceLine(
            id=uuid4(),
            invoice_id=inv.id,
            description=f"line {i}",
            quantity=1,
            unit_price=Decimal(str(amount)),
            line_total=Decimal(str(amount)),
            sort_order=i,
            company_id=TENANT,
        )
        db.add(line)
        lines.append(line)
    return inv, lines


def _stored_total(db, invoice_id):
    return db.execute(select(Invoice.total).where(Invoice.id == invoice_id)).scalar_one_or_none()


def test_unflushed_wrong_total_is_refused(db) -> None:
    """The baseline the guard always had."""
    _invoice(db, total=999)
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()


def test_flush_then_commit_is_refused(db) -> None:
    """The triage reproduction: total 999, one line of 1, flushed, committed."""
    inv, _ = _invoice(db, total=999)
    db.flush()
    assert not db.new and not db.dirty  # the flush drained both sets
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()
    db.rollback()
    assert _stored_total(db, inv.id) is None


def test_consistent_invoice_flushed_then_committed_lands(db) -> None:
    """No false positive: a correct invoice written across flushes commits."""
    inv, _ = _invoice(db, total=3, line_totals=(1, 2))
    db.flush()
    db.commit()
    assert _stored_total(db, inv.id) == Decimal("3")
    assert _TOUCHED_KEY not in db.info


def test_line_only_edit_is_refused(db) -> None:
    """The header is untouched, so the invoice is not dirty; its line is."""
    inv, lines = _invoice(db, total=1)
    db.commit()

    lines[0].line_total = Decimal("5")
    assert inv not in db.dirty
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()


def test_line_only_edit_flushed_is_refused(db) -> None:
    _, lines = _invoice(db, total=1)
    db.commit()

    lines[0].line_total = Decimal("5")
    db.flush()
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()


def test_added_line_on_a_clean_invoice_is_refused(db) -> None:
    inv, _ = _invoice(db, total=1)
    db.commit()

    db.add(InvoiceLine(
        id=uuid4(), invoice_id=inv.id, description="extra", quantity=1,
        unit_price=Decimal("4"), line_total=Decimal("4"), sort_order=9,
        company_id=TENANT,
    ))
    db.flush()
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()


def test_hard_deleted_line_is_refused(db) -> None:
    _, lines = _invoice(db, total=3, line_totals=(1, 2))
    db.commit()

    db.delete(lines[1])
    db.flush()
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()


def test_rollback_clears_the_tracked_set(db) -> None:
    """A rolled-back violation must not haunt the next, unrelated commit."""
    _invoice(db, total=999)
    db.flush()
    assert db.info.get(_TOUCHED_KEY)
    db.rollback()
    assert _TOUCHED_KEY not in db.info

    good, _ = _invoice(db, total=1)
    db.commit()
    assert _stored_total(db, good.id) == Decimal("1")


def test_savepoint_rollback_keeps_the_outer_work_tracked(db) -> None:
    """Rolling back a savepoint must not forget what the outer transaction
    flushed before it."""
    _invoice(db, total=999)
    db.flush()
    nested = db.begin_nested()
    nested.rollback()
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()


def test_rolled_back_savepoint_does_not_block_unrelated_good_work(db, monkeypatch) -> None:
    """A legacy invoice that already disagrees with its lines, touched only
    inside a savepoint that rolled back, must not refuse the outer commit of a
    correct invoice (second audit of this fix, 2026-10-11: HEAD stored the
    good invoice, the first version of this fix did not)."""
    monkeypatch.setenv("GDX_INVOICE_INVARIANT", "log")
    legacy, _ = _invoice(db, total=999)  # predates the guard; prod has such rows
    db.commit()
    monkeypatch.setenv("GDX_INVOICE_INVARIANT", "strict")

    good, _ = _invoice(db, total=1)
    db.flush()
    nested = db.begin_nested()
    legacy.notes = "touched in a savepoint that is then abandoned"
    db.flush()
    nested.rollback()
    db.commit()
    assert _stored_total(db, good.id) == Decimal("1")
    assert _TOUCHED_KEY not in db.info


def test_bad_invoice_in_a_contained_savepoint_is_refused_there(db) -> None:
    """The QuickBooks per-row loops and the closeout autodraft wrap one invoice
    in a savepoint inside try/except, so a bad one is refused at the release
    and the outer work still commits (third audit of this fix, 2026-10-11)."""
    good, _ = _invoice(db, total=1)
    db.flush()
    refused = False
    try:
        with db.begin_nested():
            _invoice(db, total=999)
    except InvoiceTotalsInvariantError:
        refused = True
    assert refused, "the savepoint release did not refuse the bad invoice"
    db.commit()
    assert _stored_total(db, good.id) == Decimal("1")


def test_failed_flush_in_a_savepoint_discards_its_record(db, monkeypatch) -> None:
    """A savepoint rolled back by its own failed flush (the QuickBooks per-row
    shape) arrives as a non-nested soft rollback; its writes are gone all the
    same, and a legacy invoice it touched must not block the outer commit
    (fourth audit of this fix, 2026-10-11)."""
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setenv("GDX_INVOICE_INVARIANT", "log")
    legacy, lines = _invoice(db, total=999)
    db.commit()
    monkeypatch.setenv("GDX_INVOICE_INVARIANT", "strict")

    good, _ = _invoice(db, total=1)
    db.flush()
    with pytest.raises(IntegrityError), db.begin_nested():
        legacy.notes = "touched, then the row's sync fails"
        db.flush()
        dup = InvoiceLine(id=lines[0].id, invoice_id=legacy.id, description="dup", quantity=1,
                          unit_price=Decimal("1"), line_total=Decimal("1"), company_id=TENANT)
        db.add(dup)
        db.flush()
    db.commit()
    assert _stored_total(db, good.id) == Decimal("1")


def test_rebuilt_invoice_written_before_its_savepoint_is_refused_at_release(db) -> None:
    """The closeout rebuild: the reused draft's old lines are released and the
    header zeroed OUTSIDE the autodraft savepoint, then rebuilt inside it. A bad
    rebuild must be refused inside the caller's try, not lose the closeout at
    the outer commit (fourth audit of this fix, 2026-10-11)."""
    draft, lines = _invoice(db, total=1)
    db.commit()
    for line in lines:
        db.delete(line)
    draft.subtotal = draft.total = draft.balance_due = Decimal("0")
    db.flush()
    closeout_work, _ = _invoice(db, total=2, line_totals=(2,))
    db.flush()

    refused = False
    try:
        with db.begin_nested():
            db.add(InvoiceLine(id=uuid4(), invoice_id=draft.id, description="rebuilt", quantity=1,
                               unit_price=Decimal("5"), line_total=Decimal("5"), company_id=TENANT))
            draft.total = Decimal("7")  # wrong: 5 of lines, no tax
    except InvoiceTotalsInvariantError:
        refused = True
    assert refused, "the bad rebuild escaped its savepoint"
    db.rollback()  # the caller's except path; nothing below depends on it


def test_moving_a_line_checks_the_invoice_it_left(db) -> None:
    source, lines = _invoice(db, total=3, line_totals=(1, 2))
    target, _ = _invoice(db, total=1)
    db.commit()

    lines[1].invoice_id = target.id
    target.subtotal = target.total = target.balance_due = Decimal("3")  # target restated
    db.flush()
    with pytest.raises(InvoiceTotalsInvariantError, match=source.invoice_number):
        db.commit()


def test_savepoint_release_mid_build_is_not_the_commit(db) -> None:
    """before_commit also fires on a savepoint release, and every GL post
    releases one. create_invoice flushes its lines, posts a deposit credit memo
    in a savepoint, and only then restates the header — so the check must wait
    for the outermost commit (audit of this fix, 2026-10-11)."""
    inv, _ = _invoice(db, total=999)
    db.flush()
    with db.begin_nested():  # released here, the way a GL post releases it
        pass
    inv.subtotal = inv.total = inv.balance_due = Decimal("1")
    db.commit()
    assert _stored_total(db, inv.id) == Decimal("1")


def test_savepoint_release_then_wrong_commit_is_refused(db) -> None:
    """Skipping the release does not skip the work: the outer commit checks it."""
    _invoice(db, total=999)
    db.flush()
    with db.begin_nested():
        pass
    with pytest.raises(InvoiceTotalsInvariantError):
        db.commit()


def test_totals_locked_invoice_is_still_exempt(db) -> None:
    inv, _ = _invoice(db, total=999, totals_locked=True)
    db.flush()
    db.commit()
    assert _stored_total(db, inv.id) == Decimal("999")


def test_log_mode_commits_and_logs(db, monkeypatch, caplog) -> None:
    """The existing escape hatch still works on the flushed path."""
    monkeypatch.setenv("GDX_INVOICE_INVARIANT", "log")
    inv, _ = _invoice(db, total=999)
    db.flush()
    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.invoice_invariants"):
        db.commit()
    assert _stored_total(db, inv.id) == Decimal("999")
    assert "invoice_totals_invariant_violated" in caplog.text


# ---------------------------------------------------------------------------
# The production shape the savepoint test stands in for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("posting", [False, True])
def test_office_invoice_with_discount_and_part_paid_deposit_creates(tenant_db, monkeypatch, posting) -> None:
    """create_invoice sets the header from the payload lines, flushes a
    discount line, nets the deposit (a GL credit memo, inside a savepoint when
    posting is on) and only then restates the header. With posting on, the
    first version of this fix refused it at the savepoint release."""
    from datetime import date

    from gdx_dispatch.models.tenant_models import Payment
    from gdx_dispatch.modules.deposits import create_deposit_invoice
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
    from gdx_dispatch.routers.invoices import (
        InvoiceCreateIn,
        InvoiceLineCreateIn,
        _recalculate_invoice,
        create_invoice,
    )

    monkeypatch.delenv("GDX_ENV", raising=False)
    db = tenant_db
    company = "11111111-1111-1111-1111-111111111111"
    settings = ensure_gl_seed(db, company)
    settings.ledger_posting_enabled = posting
    db.commit()

    cust = Customer(id=uuid4(), name="Flush Tracking", company_id=company)
    job = Job(id=uuid4(), customer_id=cust.id, title="Install", lifecycle_stage="scheduled",
              dispatch_status="unassigned", billing_status="unbilled", company_id=company)
    db.add_all([cust, job])
    db.commit()
    est = Estimate(id=uuid4(), job_id=job.id, customer_id=cust.id, estimate_number=f"EST-{uuid4().hex[:8]}",
                   label="door", proposal_mode=False, total=Decimal("1000"), status="accepted",
                   public_token=uuid4().hex, company_id=company)
    db.add(est)
    db.flush()
    db.add(EstimateLine(estimate_id=est.id, description="Door", quantity=1, unit_price=Decimal("1000"),
                        line_total=Decimal("1000"), sort_order=1, company_id=company))
    db.commit()
    dep = create_deposit_invoice(db, estimate=est, amount=250.0, tenant_id=company, actor="user-1", source="test")
    db.add(Payment(company_id=company, invoice_id=dep.id, amount=Decimal("100"), method="card",
                   payment_date=date.today()))
    db.flush()
    _recalculate_invoice(dep, db)
    db.commit()

    user = {"user_id": "user-1", "tenant_id": company, "role": "admin", "sub": "user-1"}
    resp = create_invoice(InvoiceCreateIn(
        job_id=job.id, customer_id=cust.id, discount=50,
        line_items=[InvoiceLineCreateIn(description="Door", quantity=1, unit_price=1000)],
    ), user, db)

    final = db.get(Invoice, UUID(resp["id"]))
    assert resp["deposit_netting"]["deposit_paid_applied"] == 100.0
    assert Decimal(str(final.total)) == Decimal("850.00")  # 1000 − 50 discount − 100 paid deposit
