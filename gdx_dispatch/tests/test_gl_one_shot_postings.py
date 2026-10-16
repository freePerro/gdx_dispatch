"""GDXA-1238: a one-shot GL posting stays one entry, and the duplicates
already on the books can be found, reversed and restored.

The engine keys an entry by its content. The one-shot posters (P8 anchor,
opening-era payment-void compensator, credit memo, refund, credit
application) run again on every recalc and replay, so before GDXA-1238 an
input that moved since (an invoice's job, its total, the 2300 balance a
refund split against) minted a second live entry beside the first: AR the
invoice did not owe. Each test drives the real posting rules on a real SQLite
session and reads the books back; the repair tests read the audit row that
landed, not the arguments passed (#661).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.models.tenant_models import InvoiceAdjustment
from gdx_dispatch.modules.ledger import backfill, reports
from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
from gdx_dispatch.modules.ledger.models import (
    ENTRY_STATUS_POSTED,
    ROLE_AR,
    ROLE_OPENING_EQUITY,
    GlJournalEntry,
    GlPeriodLock,
)
from gdx_dispatch.modules.ledger.rules import (
    EVENT_OPENING,
    EVENT_OPENING_PAYMENT_VOID,
    post_credit_application,
    post_credit_memo,
    post_opening_balance,
    post_refund,
    resettle_invoice_payments,
    settle_opening_on_void,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import _recalculate_invoice
from gdx_dispatch.tests.test_gl_cutover_backfill import (
    COMPANY,
    CUTOVER,
    PRE,
    _account_balance,
    _enable,
    _invoice,
    _payment,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import gl_one_shot_duplicate_repair as repair  # noqa: E402


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.cutover_month = CUTOVER
    tenant_db.commit()
    return tenant_db


def _live(db, source_type: str, source_id, event: str) -> list[GlJournalEntry]:
    return db.scalars(
        select(GlJournalEntry).where(
            GlJournalEntry.source_type == source_type,
            GlJournalEntry.source_id == str(source_id),
            GlJournalEntry.status == ENTRY_STATUS_POSTED,
            GlJournalEntry.idempotency_key.like(f"{source_type}:{source_id}:{event}:%"),
        )
    ).all()


def _anchored(db, total="400.00"):
    invoice = _invoice(db, total=total)
    post_opening_balance(db, invoice, actor="t")
    _enable(db)
    return invoice


# ---------------------------------------------------------------------------
# The posting rules: a re-run returns the live entry
# ---------------------------------------------------------------------------

def test_recalc_after_an_era_invoice_changes_job_keeps_one_anchor(db):
    # Before GDXA-1238: control 800.00 against a 400.00 balance.
    invoice = _anchored(db)
    invoice.job_id = uuid4()
    _recalculate_invoice(invoice, db, actor="t")
    db.flush()

    assert len(_live(db, "invoice", invoice.id, EVENT_OPENING)) == 1
    out = reports.ar_control(db, COMPANY)
    assert (out["control_cents"], out["subledger_cents"], out["tied"]) == (400_00, 400_00, True)


def test_recalc_after_an_era_invoice_total_changes_keeps_one_anchor(db):
    # The anchor is posted once (spec §5.7: anchors are never reversed and
    # reposted). Before GDXA-1238 a second, full anchor posted: control
    # 900.00 against 500.00. Now the 100.00 the total moved posts nowhere and
    # the control says so, untied, on this invoice. Posting that difference
    # is out of this change's scope and is recorded as such.
    invoice = _anchored(db)
    invoice.totals_locked = True
    invoice.total = invoice.subtotal = Decimal("500.00")
    _recalculate_invoice(invoice, db, actor="t")
    db.flush()

    assert len(_live(db, "invoice", invoice.id, EVENT_OPENING)) == 1
    out = reports.ar_control(db, COMPANY)
    assert (out["control_cents"], out["subledger_cents"], out["tied"]) == (400_00, 500_00, False)
    [row] = out["drift"]
    assert (row["invoice_id"], row["drift_cents"]) == (str(invoice.id), -100_00)


def test_opening_payment_void_compensator_posts_once_after_the_job_changes(db):
    invoice = _invoice(db, total="1000.00")
    pay = _payment(db, invoice, amount="200.00", created_at=PRE)
    post_opening_balance(db, invoice, actor="t")
    _enable(db)
    pay.voided_at = datetime(2026, 7, 12, 9, 0)
    db.flush()
    resettle_invoice_payments(db, invoice, actor="t")
    assert _account_balance(db, ROLE_AR) == 1000_00

    invoice.job_id = uuid4()  # moves the compensator's content key
    db.flush()
    resettle_invoice_payments(db, invoice, actor="t")

    assert len(_live(db, "payment", pay.id, EVENT_OPENING_PAYMENT_VOID)) == 1
    assert _account_balance(db, ROLE_AR) == 1000_00


@pytest.mark.parametrize(
    ("kind", "poster", "event"),
    [
        ("credit_memo", post_credit_memo, "credit_memo"),
        ("refund", post_refund, "refund"),
        ("credit_applied", post_credit_application, "credit_applied"),
    ],
)
def test_an_adjustment_posting_re_run_after_its_inputs_move_returns_the_first(
    db, kind, poster, event
):
    invoice = _invoice(db, total="300.00", invoice_date=date(2026, 7, 8))
    _enable(db)
    adj = InvoiceAdjustment(
        id=uuid4(), invoice_id=invoice.id, kind=kind, amount=Decimal("50.00"),
        reason="goodwill", refund_method="check", company_id=COMPANY,
    )
    db.add(adj)
    db.flush()
    first = poster(db, adj, invoice, actor="t")
    assert first is not None

    invoice.job_id = uuid4()
    adj.amount = Decimal("70.00")
    db.flush()
    again = poster(db, adj, invoice, actor="t")

    assert again.id == first.id
    assert len(_live(db, "adjustment", adj.id, event)) == 1


# ---------------------------------------------------------------------------
# The repair: duplicates already on the books
# ---------------------------------------------------------------------------

def _duplicate_anchor(db, invoice, cents: int) -> GlJournalEntry:
    """What a pre-GDXA-1238 recalc left: a second live P8 with new content."""
    [keeper] = _live(db, "invoice", invoice.id, EVENT_OPENING)
    dup = post_for_event(
        db,
        PostingEvent(
            company_id=COMPANY, source_type="invoice", source_id=str(invoice.id),
            event=EVENT_OPENING, effective_at=keeper.effective_at,
            lines=(
                PostingLine(amount_cents=cents, role=ROLE_AR, memo="dup"),
                PostingLine(amount_cents=-cents, role=ROLE_OPENING_EQUITY, memo="dup"),
            ),
            created_by="t",
        ),
    )
    db.flush()
    assert dup.id != keeper.id
    return dup


@pytest.fixture
def tenant(monkeypatch):
    monkeypatch.setattr(repair, "company_id", lambda: COMPANY)


def test_the_repair_reverses_a_duplicate_anchor_audits_it_and_rolls_back(db, tenant):
    invoice = _anchored(db)
    [keeper] = _live(db, "invoice", invoice.id, EVENT_OPENING)
    dup = _duplicate_anchor(db, invoice, 400_00)
    db.commit()
    assert reports.ar_control(db, COMPANY)["control_cents"] == 800_00

    [group] = backfill.duplicate_one_shot_entries(db, COMPANY)
    assert (group["keeper_entry_id"], group["duplicate_entry_ids"]) == (
        str(keeper.id), [str(dup.id)],
    )
    assert (group["invoice_number"], group["ar_cents"]) == (invoice.invoice_number, 400_00)

    result = repair.apply_repair(db, operator="tester")

    db.expire_all()
    assert [e.id for e in _live(db, "invoice", invoice.id, EVENT_OPENING)] == [keeper.id]
    out = reports.ar_control(db, COMPANY)
    assert (out["control_cents"], out["tied"]) == (400_00, True)
    [rev_id] = result.rows[0]["reversal_entry_ids"]
    rev = db.get(GlJournalEntry, UUID(rev_id))
    assert (rev.reverses_entry_id, rev.created_by) == (dup.id, "cli:tester")
    [row] = db.scalars(
        select(AuditLog).where(AuditLog.action == "gl_one_shot_duplicate_reversed")
    ).all()
    assert (row.user_id, row.entity_id) == ("cli:tester", str(dup.id))
    assert row.details["reversal_entry_id"] == rev_id
    assert row.details["kept_entry_id"] == str(keeper.id)
    # Re-runnable: nothing left to select.
    assert backfill.duplicate_one_shot_entries(db, COMPANY) == []

    restored = repair.rollback(db, [rev_id], operator="tester")
    db.expire_all()
    assert len(restored) == 1
    assert reports.ar_control(db, COMPANY)["control_cents"] == 800_00
    [back] = db.scalars(
        select(AuditLog).where(AuditLog.action == "gl_one_shot_duplicate_restored")
    ).all()
    assert back.user_id == "cli:tester"
    assert back.details["reversal_entry_id"] == rev_id


def test_the_repair_skips_a_locked_period_whole_and_dates_into_an_open_one(db, tenant):
    invoice = _anchored(db)
    dup = _duplicate_anchor(db, invoice, 150_00)
    db.add(GlPeriodLock(
        company_id=COMPANY, lock_date=dup.effective_at + timedelta(days=1),
        created_by="t",
    ))
    db.commit()

    result = repair.apply_repair(db, operator="tester")
    assert result.rows == [] and len(result.locked) == 1
    assert invoice.invoice_number in result.locked[0]
    assert reports.ar_control(db, COMPANY)["control_cents"] == 550_00

    open_day = dup.effective_at + timedelta(days=2)
    result = repair.apply_repair(db, operator="tester", effective_at=open_day)
    assert result.locked == [] and result.rows[0]["reversed_on"] == str(open_day)
    assert reports.ar_control(db, COMPANY)["control_cents"] == 400_00


def test_the_repair_refuses_an_anonymous_operator(db):
    with pytest.raises(ValueError):
        backfill.reverse_duplicate_one_shot_entries(db, COMPANY, actor=" ")
    with pytest.raises(ValueError):
        repair.apply_repair(db, operator="")


def test_rollback_refuses_an_entry_that_is_not_a_repair_reversal(db, tenant):
    invoice = _anchored(db)
    [keeper] = _live(db, "invoice", invoice.id, EVENT_OPENING)
    with pytest.raises(ValueError):
        repair.rollback(db, [str(keeper.id)], operator="tester")


def test_the_repair_holds_a_duplicate_a_void_settlement_already_absorbed(db, tenant, capsys):
    """The audit's falsifier. Voiding a pre-cutover invoice settles whatever AR
    the ledger shows, the duplicate included, so the invoice nets to zero;
    reversing the duplicate then would leave it at -400.00."""
    from gdx_dispatch.modules.ledger.rules import invoice_ar_balance_cents

    invoice = _anchored(db)
    _duplicate_anchor(db, invoice, 400_00)
    transition_invoice_status(db, invoice, "void", actor="t")
    settle_opening_on_void(db, invoice, actor="t")
    invoice.balance_due = Decimal("0")
    db.commit()
    assert invoice_ar_balance_cents(db, invoice) == 0

    [group] = backfill.duplicate_one_shot_entries(db, COMPANY)
    assert group["reversible"] is False and group["invoice_drift_cents"] == 0
    repair.print_plan([group])
    assert "HELD" in capsys.readouterr().out

    result = repair.apply_repair(db, operator="tester")
    assert result.rows == [] and len(result.refused) == 1
    assert invoice_ar_balance_cents(db, invoice) == 0
    assert db.scalars(
        select(AuditLog).where(AuditLog.action == "gl_one_shot_duplicate_reversed")
    ).all() == []


def test_the_repair_holds_a_group_whose_later_anchor_is_the_one_that_matches(db, tenant):
    """The second audit's falsifier: anchor 400, a later 300 duplicate, and
    the invoice now opens at 300. Keeping the earliest would reverse the
    right anchor and keep the stale one."""
    invoice = _anchored(db)
    _duplicate_anchor(db, invoice, 300_00)
    invoice.totals_locked = True
    invoice.total = invoice.subtotal = invoice.balance_due = Decimal("300.00")
    db.commit()

    [group] = backfill.duplicate_one_shot_entries(db, COMPANY)
    assert group["reversible"] is False and group["later_entry_matches"] is True

    result = repair.apply_repair(db, operator="tester")
    assert result.rows == [] and len(result.refused) == 1
    assert reports.ar_control(db, COMPANY)["control_cents"] == 700_00
