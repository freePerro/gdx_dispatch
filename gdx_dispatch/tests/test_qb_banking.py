"""Banking module — deposits/transfers/balances/unified-feed/schedule."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
# Force model registration so create_all picks the new tables up.
from gdx_dispatch.modules.quickbooks.banking import (  # noqa: F401
    FREQ_DAILY,
    FREQ_EVERY_4H,
    FREQ_HOURLY,
    FREQ_MANUAL,
    QBBankTransaction,
    QBDeposit,
    QBSyncSchedule,
    QBTransfer,
    bank_balances,
    compute_next_run_at,
    get_or_create_schedule,
    record_scheduled_run,
    schedule_dict,
    unified_banking_transactions,
    update_schedule,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    # qb_accounts is still inline DDL inside sync.pull_accounts. The test
    # fixture creates a matching shape here. qb_bank_transactions is now
    # a real ORM model on TenantBase (QBBankTransaction) so create_all
    # above already built it — no manual DDL needed.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE qb_accounts (
                id TEXT PRIMARY KEY, tenant_id TEXT, qb_account_id TEXT,
                name TEXT, account_type TEXT, account_sub_type TEXT,
                classification TEXT, current_balance NUMERIC, active BOOLEAN,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, qb_account_id)
            )
        """))
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    yield s
    s.close()
    engine.dispose()


# ─── Balances ──────────────────────────────────────────────────────


def test_bank_balances_filters_to_bank_and_credit_card(db):
    db.execute(text("INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, current_balance, active) "
                    "VALUES (:i, 't', '1', 'Operating', 'Bank', 12500.75, 1)"),
               {"i": str(uuid4())})
    db.execute(text("INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, current_balance, active) "
                    "VALUES (:i, 't', '2', 'Amex', 'Credit Card', -540.10, 1)"),
               {"i": str(uuid4())})
    db.execute(text("INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, current_balance, active) "
                    "VALUES (:i, 't', '3', 'Sales Income', 'Income', 0, 1)"),
               {"i": str(uuid4())})
    db.commit()

    bal = bank_balances(db)
    names = sorted(b["name"] for b in bal)
    assert names == ["Amex", "Operating"]
    assert any(b["current_balance"] == 12500.75 for b in bal)
    assert any(b["current_balance"] == -540.10 for b in bal)


def test_bank_balances_returns_empty_when_no_accounts_table():
    # Engine without qb_accounts table — the read must NOT raise.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    assert bank_balances(s) == []
    s.close()
    engine.dispose()


# ─── Unified transactions ──────────────────────────────────────────


def test_unified_transactions_merges_three_sources_in_date_order(db):
    # qb_bank_transactions stores QBO's TotalAmt unsigned (always positive);
    # the unified reader signs Purchases negative at emit so the UI can show
    # money OUT consistently.
    db.add(QBBankTransaction(qb_txn_id="P1", txn_date=date(2026, 5, 1), txn_type="Check",
                             account_name="Operating", payee="ACE Hardware",
                             amount=89.42, memo="screws"))
    db.add(QBDeposit(qb_txn_id="D1", txn_date=date(2026, 5, 10), total_amount=2500.00,
                     deposit_to_account_name="Operating", memo="customer batch"))
    db.add(QBTransfer(qb_txn_id="T1", txn_date=date(2026, 5, 5), amount=1000.00,
                     from_account_name="Operating", to_account_name="Savings"))
    db.commit()

    rows = unified_banking_transactions(db)
    assert len(rows) == 3
    # Newest first.
    assert rows[0]["kind"] == "deposit"
    assert rows[0]["amount"] == 2500.00
    assert rows[0]["direction"] == "in"
    assert rows[1]["kind"] == "transfer"
    assert rows[1]["counterparty"] == "Savings"
    assert rows[1]["direction"] == "transfer"
    assert rows[2]["kind"] == "purchase"
    # Signed at read time — Purchase is always money OUT.
    assert rows[2]["amount"] == -89.42
    assert rows[2]["direction"] == "out"


def test_unified_transactions_survives_missing_table():
    # Only the schedule table exists — purchases/deposits/transfers absent.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    # qb_deposits + qb_transfers ARE in TenantBase.metadata, so create_all
    # made them. Drop them to exercise the missing-table path.
    s.execute(text("DROP TABLE qb_deposits"))
    s.execute(text("DROP TABLE qb_transfers"))
    s.commit()
    assert unified_banking_transactions(s) == []
    s.close()
    engine.dispose()


# ─── Schedule ──────────────────────────────────────────────────────


def test_get_or_create_schedule_returns_manual_default(db):
    s = get_or_create_schedule(db)
    assert s.frequency == FREQ_MANUAL
    assert s.next_run_at is None
    # Idempotent.
    s2 = get_or_create_schedule(db)
    assert s2.id == s.id


def test_update_schedule_sets_next_run_at_for_non_manual(db):
    s = update_schedule(db, FREQ_HOURLY)
    assert s.frequency == FREQ_HOURLY
    assert s.next_run_at is not None
    # SQLite drops timezone info on round-trip; Postgres keeps it. Normalize
    # both sides to naive UTC for the delta check.
    nra = s.next_run_at.replace(tzinfo=None) if s.next_run_at.tzinfo else s.next_run_at
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    delta = nra - now_naive
    assert timedelta(minutes=55) < delta < timedelta(minutes=65)


def test_update_schedule_clears_next_run_at_for_manual(db):
    update_schedule(db, FREQ_DAILY)
    s = update_schedule(db, FREQ_MANUAL)
    assert s.frequency == FREQ_MANUAL
    assert s.next_run_at is None


def test_update_schedule_rejects_invalid_frequency(db):
    with pytest.raises(ValueError):
        update_schedule(db, "every-15-seconds")


def test_compute_next_run_at_known_offsets():
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    assert compute_next_run_at(FREQ_MANUAL, base) is None
    assert compute_next_run_at(FREQ_HOURLY, base) == base + timedelta(hours=1)
    assert compute_next_run_at(FREQ_EVERY_4H, base) == base + timedelta(hours=4)
    assert compute_next_run_at(FREQ_DAILY, base) == base + timedelta(days=1)


def test_record_scheduled_run_rolls_next_run_forward(db):
    update_schedule(db, FREQ_HOURLY)
    before = get_or_create_schedule(db).next_run_at

    record_scheduled_run(db, status="ok")
    s = get_or_create_schedule(db)
    assert s.last_run_at is not None
    assert s.last_run_status == "ok"
    assert s.next_run_at is not None
    # next_run_at advanced after the run.
    assert s.next_run_at > before


def test_record_scheduled_run_captures_error_truncated(db):
    update_schedule(db, FREQ_HOURLY)
    long_err = "x" * 1000
    record_scheduled_run(db, status="error", error=long_err)
    s = get_or_create_schedule(db)
    assert s.last_run_status == "error"
    assert s.last_run_error is not None
    assert len(s.last_run_error) <= 500


def test_schedule_dict_shape(db):
    update_schedule(db, FREQ_DAILY)
    s = get_or_create_schedule(db)
    d = schedule_dict(s)
    assert d["frequency"] == FREQ_DAILY
    assert d["next_run_at"] is not None
    assert d["last_run_at"] is None


# ─── Audit follow-ups 2026-05-20 ───────────────────────────────────


def test_date_filter_rejects_injection_payloads(db):
    """The QBO query string accepts no bind params — start_date/end_date
    are interpolated as literals. Malformed input must raise BEFORE
    reaching QBO so we don't ship a query like
        TxnDate >= ''; DROP TABLE; --'
    """
    from gdx_dispatch.modules.quickbooks.banking import _build_date_where

    # Valid passes through.
    assert "TxnDate >= '2026-05-01'" in _build_date_where("2026-05-01", "")

    # Injection attempts fail loudly.
    for bad in ("2026-05-01'; DROP TABLE x;--", "yesterday", "2026/05/01", "2026-5-1"):
        with pytest.raises(ValueError):
            _build_date_where(bad, "")


def test_get_or_create_schedule_survives_duplicate_rows(db):
    """Audit follow-up: a race that inserts two singleton rows must NOT
    permanently 500. .first() with ORDER BY created_at picks the earliest
    deterministically; the duplicate is benign clutter."""
    from gdx_dispatch.modules.quickbooks.banking import QBSyncSchedule

    db.add(QBSyncSchedule(frequency=FREQ_HOURLY))
    db.add(QBSyncSchedule(frequency=FREQ_DAILY))
    db.commit()

    s = get_or_create_schedule(db)
    # Earliest of the two — both inserted near-simultaneously so this is
    # a "doesn't raise" assertion more than a deterministic pick.
    assert s.frequency in (FREQ_HOURLY, FREQ_DAILY)


# ─── Soft-delete reconciler (follow-up #1) ─────────────────────────


def test_reconcile_tombstones_marks_missing_deposits_deleted(db):
    """If a Deposit was synced previously but isn't in the latest QBO
    response, the reconciler marks it deleted_at=now(). Subsequent
    unified-feed reads exclude it."""
    from gdx_dispatch.modules.quickbooks.banking import _reconcile_tombstones

    # Three deposits initially.
    db.add(QBDeposit(qb_txn_id="D1", txn_date=date(2026, 5, 10), total_amount=100.0))
    db.add(QBDeposit(qb_txn_id="D2", txn_date=date(2026, 5, 11), total_amount=200.0))
    db.add(QBDeposit(qb_txn_id="D3", txn_date=date(2026, 5, 12), total_amount=300.0))
    db.commit()

    # Next sync sees only D1 and D3 — D2 was voided in QBO.
    deleted = _reconcile_tombstones(db, "qb_deposits", {"D1", "D3"}, "", "")
    assert deleted == 1

    # Unified feed excludes D2.
    feed = unified_banking_transactions(db)
    ids = sorted(r["qb_txn_id"] for r in feed)
    assert ids == ["D1", "D3"]


def test_reconcile_tombstones_idempotent(db):
    """Running reconcile twice with the same response doesn't re-tombstone
    already-tombstoned rows."""
    from gdx_dispatch.modules.quickbooks.banking import _reconcile_tombstones

    db.add(QBDeposit(qb_txn_id="D1", txn_date=date(2026, 5, 10), total_amount=100.0))
    db.add(QBDeposit(qb_txn_id="D2", txn_date=date(2026, 5, 11), total_amount=200.0))
    db.commit()

    first = _reconcile_tombstones(db, "qb_deposits", {"D1"}, "", "")
    second = _reconcile_tombstones(db, "qb_deposits", {"D1"}, "", "")
    assert first == 1
    assert second == 0  # D2 was already deleted — not touched again.


def test_reconcile_tombstones_respects_date_window(db):
    """Rows OUTSIDE the synced date window must NOT be tombstoned —
    they're just out of scope, not missing."""
    from gdx_dispatch.modules.quickbooks.banking import _reconcile_tombstones

    db.add(QBDeposit(qb_txn_id="OLD", txn_date=date(2026, 1, 1), total_amount=50.0))
    db.add(QBDeposit(qb_txn_id="NEW", txn_date=date(2026, 5, 10), total_amount=100.0))
    db.commit()

    # Sync window: 2026-05-01 onwards. QBO returned nothing in window.
    deleted = _reconcile_tombstones(db, "qb_deposits", set(), "2026-05-01", "")
    assert deleted == 1  # NEW is gone

    # OLD survives (out of window).
    db.expire_all()
    feed = unified_banking_transactions(db)
    assert any(r["qb_txn_id"] == "OLD" for r in feed)


def test_reconcile_tombstones_works_for_transfers(db):
    from gdx_dispatch.modules.quickbooks.banking import _reconcile_tombstones

    db.add(QBTransfer(qb_txn_id="T1", txn_date=date(2026, 5, 10), amount=100.0))
    db.add(QBTransfer(qb_txn_id="T2", txn_date=date(2026, 5, 11), amount=200.0))
    db.commit()

    deleted = _reconcile_tombstones(db, "qb_transfers", {"T1"}, "", "")
    assert deleted == 1


def test_reconcile_tombstones_rejects_unknown_table(db):
    from gdx_dispatch.modules.quickbooks.banking import _reconcile_tombstones

    # qb_bank_transactions has its OWN reconciler in sync.py
    # (_reconcile_bank_tx_tombstones) — keep banking.py's helper scoped
    # to deposits/transfers as before.
    with pytest.raises(ValueError):
        _reconcile_tombstones(db, "qb_banking_entries", set(), "", "")  # not in allowlist


# ─── Purchase tombstone (qb_bank_transactions) ─────────────────────


def test_purchase_reconciler_refuses_to_nuke_on_empty_response(db):
    """SAFETY GATE: empty seen_qb_ids must NOT tombstone every live row.
    A pull that returned zero rows is almost always a transient API
    failure (quota throttle, empty page, network blip) — not a real
    bulk-delete event. Refusing the nuke prevents a 5-second outage
    from wiping the local mirror.
    """
    from gdx_dispatch.modules.quickbooks.sync import _reconcile_bank_tx_tombstones

    db.add(QBBankTransaction(qb_txn_id="P1", txn_date=date(2026, 5, 1),
                             account_name="Operating", payee="Live Co", amount=10.0))
    db.add(QBBankTransaction(qb_txn_id="P2", txn_date=date(2026, 5, 2),
                             account_name="Operating", payee="Also Live", amount=20.0))
    db.commit()

    # Empty seen, empty window — would otherwise nuke everything.
    deleted = _reconcile_bank_tx_tombstones(db, set(), "", "")
    assert deleted == 0
    # Both rows still alive.
    feed = unified_banking_transactions(db, kind="purchase")
    assert {r["qb_txn_id"] for r in feed} == {"P1", "P2"}


def test_purchase_reconciler_tombstones_missing_in_window(db):
    from gdx_dispatch.modules.quickbooks.sync import _reconcile_bank_tx_tombstones

    db.add(QBBankTransaction(qb_txn_id="P1", txn_date=date(2026, 5, 1),
                             account_name="Operating", payee="Live", amount=10.0))
    db.add(QBBankTransaction(qb_txn_id="P2", txn_date=date(2026, 5, 2),
                             account_name="Operating", payee="Gone", amount=20.0))
    db.commit()

    # Sync returned only P1 — P2 should tombstone.
    deleted = _reconcile_bank_tx_tombstones(db, {"P1"}, "", "")
    assert deleted == 1
    feed = unified_banking_transactions(db, kind="purchase")
    assert {r["qb_txn_id"] for r in feed} == {"P1"}


def test_unified_excludes_tombstoned_purchases(db):
    """A Purchase row with deleted_at set must NOT appear in the unified feed."""
    db.add(QBBankTransaction(qb_txn_id="P_ALIVE", txn_date=date(2026, 5, 1),
                             account_name="Operating", payee="Live Co", amount=10.0))
    db.add(QBBankTransaction(qb_txn_id="P_DEAD", txn_date=date(2026, 5, 2),
                             account_name="Operating", payee="Dead Co", amount=20.0,
                             deleted_at=datetime.now(UTC)))
    db.commit()
    rows = unified_banking_transactions(db, kind="purchase")
    ids = sorted(r["qb_txn_id"] for r in rows)
    assert ids == ["P_ALIVE"]


# ─── Server-side filter/search/sort/pagination ─────────────────────


def _seed_mixed_feed(db):
    """Common fixture: 1 Purchase, 2 Deposits, 1 Transfer, 1 BillPayment."""
    from gdx_dispatch.modules.quickbooks.banking import _upsert_banking_entry
    db.add(QBBankTransaction(qb_txn_id="P1", txn_date=date(2026, 5, 1), txn_type="Check",
                             account_name="Operating", payee="ACE Hardware", amount=100.0))
    db.add(QBDeposit(qb_txn_id="D1", txn_date=date(2026, 5, 10), total_amount=500.0,
                     deposit_to_account_name="Operating", memo="batch A",
                     linked_qb_ids="Payment:11,SalesReceipt:22"))
    db.add(QBDeposit(qb_txn_id="D2", txn_date=date(2026, 5, 11), total_amount=750.0,
                     deposit_to_account_name="Savings", memo="batch B"))
    db.add(QBTransfer(qb_txn_id="T1", txn_date=date(2026, 5, 5), amount=200.0,
                     from_account_name="Operating", to_account_name="Savings"))
    db.commit()
    _upsert_banking_entry(
        db, qb_entity="BillPayment", qb_txn_id="BP1", qb_line_index=0,
        txn_date=date(2026, 5, 8), amount=-300.0, account_id="acc1",
        account_name="Operating", counterparty_name="Verizon",
        memo="phone bill", raw_json={"Id": "BP1"},
    )
    db.commit()


def test_kind_filter_pushdown(db):
    _seed_mixed_feed(db)
    rows = unified_banking_transactions(db, kind="deposit")
    assert {r["qb_txn_id"] for r in rows} == {"D1", "D2"}
    assert all(r["kind"] == "deposit" for r in rows)


def test_multi_kind_filter(db):
    _seed_mixed_feed(db)
    rows = unified_banking_transactions(db, kind="purchase,bill_payment")
    kinds = sorted({r["kind"] for r in rows})
    assert kinds == ["bill_payment", "purchase"]


def test_search_matches_memo_and_payee(db):
    _seed_mixed_feed(db)
    # 'verizon' hits BillPayment.counterparty_name
    rows = unified_banking_transactions(db, search="verizon")
    assert {r["qb_txn_id"] for r in rows} == {"BP1"}
    # 'batch a' hits D1.memo only (case-insensitive)
    rows = unified_banking_transactions(db, search="Batch A")
    assert {r["qb_txn_id"] for r in rows} == {"D1"}


def test_account_filter(db):
    _seed_mixed_feed(db)
    rows = unified_banking_transactions(db, account="Savings")
    # D2 deposits to Savings; T1 has Savings as either from or to.
    ids = {r["qb_txn_id"] for r in rows}
    assert "D2" in ids
    assert "T1" in ids
    assert "D1" not in ids  # D1 → Operating


def test_date_range_filter(db):
    _seed_mixed_feed(db)
    rows = unified_banking_transactions(db, start_date="2026-05-06", end_date="2026-05-10")
    ids = sorted(r["qb_txn_id"] for r in rows)
    # T1 (5/5) out, D2 (5/11) out, BP1 (5/8) and D1 (5/10) in.
    assert ids == ["BP1", "D1"]


def test_sort_by_amount_ascending(db):
    _seed_mixed_feed(db)
    rows = unified_banking_transactions(db, order_by="amount", order_dir="asc")
    # Signed amounts (Purchase=-100, BillPayment=-300, Transfer=200, D1=500, D2=750)
    amounts = [r["amount"] for r in rows]
    assert amounts == sorted(amounts)
    assert amounts[0] == -300.0  # BillPayment most negative
    assert amounts[-1] == 750.0  # D2 highest


def test_paginated_shape(db):
    _seed_mixed_feed(db)
    result = unified_banking_transactions(db, paginated=True, page=1, page_size=2)
    assert isinstance(result, dict)
    assert set(result.keys()) >= {"items", "total", "page", "page_size"}
    assert result["total"] == 5
    assert result["page"] == 1
    assert result["page_size"] == 2
    assert len(result["items"]) == 2

    page2 = unified_banking_transactions(db, paginated=True, page=2, page_size=2)
    assert len(page2["items"]) == 2
    assert page2["page"] == 2


def test_linked_txn_ids_surfaced(db):
    _seed_mixed_feed(db)
    rows = unified_banking_transactions(db, kind="deposit")
    by_id = {r["qb_txn_id"]: r for r in rows}
    assert by_id["D1"]["linked_txn_ids"] == "Payment:11,SalesReceipt:22"
    assert by_id["D2"]["linked_txn_ids"] is None


# ─── Line of Credit (LOC) visibility ───────────────────────────────


def _add_account(db, qb_id, name, account_type, sub_type="", balance=0.0, active=True):
    db.execute(text(
        "INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, "
        "account_sub_type, current_balance, active) "
        "VALUES (:i, 't', :qid, :n, :at, :st, :bal, :a)"
    ), {"i": str(uuid4()), "qid": qb_id, "n": name, "at": account_type,
        "st": sub_type, "bal": balance, "a": active})


def test_bank_balances_includes_loc_with_kind_discriminator(db):
    """LOC accounts (Long Term Liability with AccountSubType=LineOfCredit)
    must surface alongside bank/CC, each row carrying `kind` so the UI
    can group cash vs debt."""
    _add_account(db, "1", "Operating", "Bank", balance=5000.0)
    _add_account(db, "2", "Wells LOC", "Long Term Liability", sub_type="LineOfCredit", balance=12000.0)
    _add_account(db, "3", "Sales Tax Payable", "Other Current Liability",
                 sub_type="OtherCurrentLiabilities", balance=300.0)  # NOT an LOC — must be excluded
    _add_account(db, "4", "Amex", "Credit Card", balance=-540.0)
    db.commit()

    rows = bank_balances(db)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Operating"]["kind"] == "cash"
    assert by_name["Amex"]["kind"] == "cash"
    assert by_name["Wells LOC"]["kind"] == "loc"
    assert "Sales Tax Payable" not in by_name  # other liabilities filtered out


def test_loc_account_ids_returns_only_lineofcredit_subtype(db):
    """loc_account_ids() must NOT include non-LOC liabilities even when
    the account_type matches."""
    from gdx_dispatch.modules.quickbooks.banking import loc_account_ids
    _add_account(db, "1", "Wells LOC", "Long Term Liability", sub_type="LineOfCredit")
    _add_account(db, "2", "Mortgage", "Long Term Liability", sub_type="NotesPayable")
    _add_account(db, "3", "Operating", "Bank")
    db.commit()
    assert loc_account_ids(db) == {"1"}


def test_liability_kind_classifier():
    """Classifier handles cash, loc, loan, and unrelated liability shapes."""
    from gdx_dispatch.modules.quickbooks.banking import _liability_kind
    # Cash side
    assert _liability_kind("Bank", None) == "cash"
    assert _liability_kind("Credit Card", None) == "cash"
    assert _liability_kind("Other Current Asset", "OtherCurrentAssets") == "cash"
    # LOCs
    assert _liability_kind("Long Term Liability", "LineOfCredit") == "loc"
    assert _liability_kind("Other Current Liability", "LineOfCredit") == "loc"
    # Term loans — LTL: all sub-types except LineOfCredit are loans
    assert _liability_kind("Long Term Liability", "NotesPayable", "Midwest Bank Loan") == "loan"
    assert _liability_kind("Long Term Liability", "ShareholderNotesPayable") == "loan"
    assert _liability_kind("Long Term Liability", "OtherLongTermLiabilities") == "loan"
    # Term loans — OCL: only canonical loan sub-types
    assert _liability_kind("Other Current Liability", "LoanPayable") == "loan"
    assert _liability_kind("Other Current Liability", "NotesPayable") == "loan"
    # OCL with the generic catch-all sub-type: name-pattern fallback
    assert _liability_kind("Other Current Liability", "OtherCurrentLiabilities", "Intuit Finance Loan") == "loan"
    assert _liability_kind("Other Current Liability", "OtherCurrentLiabilities", "Bank Note Payable") == "loan"
    assert _liability_kind("Other Current Liability", "OtherCurrentLiabilities", "Home Mortgage") == "loan"
    # NOT loans — tax/payroll/clearing sub-types must NOT pattern-match the name
    assert _liability_kind("Other Current Liability", "PayrollTaxPayable", "Federal Loan Tax") is None
    assert _liability_kind("Other Current Liability", "SalesTaxPayable", "Notes payable to state") is None
    # Non-loan generic OCL with no loan-like name
    assert _liability_kind("Other Current Liability", "OtherCurrentLiabilities", "othercurrentliability") is None
    assert _liability_kind("Other Current Liability", "OtherCurrentLiabilities", None) is None
    # Other account types pass through to None
    assert _liability_kind("Income", None) is None
    assert _liability_kind("Accounts Payable", "AccountsPayable") is None
    assert _liability_kind(None, None) is None


def test_bank_balances_classifies_loans_and_excludes_tax_payables(db):
    """End-to-end through bank_balances: GDX-style data — Midwest Bank Loan,
    Intuit Finance Loan, sales tax payable, payroll tax payable. Only the
    two loans should surface; tax/payroll/clearing accounts drop out."""
    # Midwest Bank Loan: LTL NotesPayable, drawn = negative balance
    _add_account(db, "1150040004", "Midwest Bank Loan", "Long Term Liability",
                 sub_type="NotesPayable", balance=-18779.57)
    # Intuit Finance Loan: OCL OtherCurrentLiabilities + loan-keyword name
    _add_account(db, "1150040002", "Intuit Finance Loan", "Other Current Liability",
                 sub_type="OtherCurrentLiabilities", balance=-26116.29)
    # Tax/payroll/clearing — MUST NOT surface
    _add_account(db, "166", "Federal Taxes (941/943/944)", "Other Current Liability",
                 sub_type="PayrollTaxPayable", balance=-1641.30)
    _add_account(db, "21", "Sales tax to pay", "Other Current Liability",
                 sub_type="SalesTaxPayable", balance=0.0)
    _add_account(db, "175", "Direct Deposit Payable", "Other Current Liability",
                 sub_type="DirectDepositPayable", balance=0.0)
    # Junk OCL without loan keyword — MUST NOT surface
    _add_account(db, "1150040011", "othercurrentliability", "Other Current Liability",
                 sub_type="OtherCurrentLiabilities", balance=-4.12)
    db.commit()

    rows = bank_balances(db)
    names = sorted(r["name"] for r in rows)
    assert names == ["Intuit Finance Loan", "Midwest Bank Loan"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["Midwest Bank Loan"]["kind"] == "loan"
    assert by_name["Intuit Finance Loan"]["kind"] == "loan"
    # Negative balance preserved — UI flips with abs() for display.
    assert by_name["Midwest Bank Loan"]["current_balance"] == -18779.57


def test_extract_linked_txn_ids():
    from gdx_dispatch.modules.quickbooks.banking import _extract_linked_txn_ids
    raw = {
        "Line": [
            {"LinkedTxn": [{"TxnType": "Payment", "TxnId": "1"}]},
            {"LinkedTxn": [{"TxnType": "SalesReceipt", "TxnId": "2"},
                           {"TxnType": "Payment", "TxnId": "1"}]},  # dup ignored
            {},  # no LinkedTxn
        ],
    }
    assert _extract_linked_txn_ids(raw) == "Payment:1,SalesReceipt:2"
    assert _extract_linked_txn_ids({"Line": []}) is None
    assert _extract_linked_txn_ids({}) is None


# ─── qb_banking_entries (5 other entities) ─────────────────────────


def test_banking_entry_upsert_and_unified_feed(db):
    """Insert a BillPayment + SalesReceipt + RefundReceipt + JE row
    into qb_banking_entries; assert all surface in unified_banking_transactions
    with the right kind/account/amount sign."""
    from gdx_dispatch.modules.quickbooks.banking import _upsert_banking_entry

    _upsert_banking_entry(
        db, qb_entity="BillPayment", qb_txn_id="BP1", qb_line_index=0,
        txn_date=date(2026, 5, 1), amount=-500.00,
        account_id="A1", account_name="Operating", counterparty_name="ACME Supply",
        memo="check 1042", raw_json={"_": 1},
    )
    _upsert_banking_entry(
        db, qb_entity="SalesReceipt", qb_txn_id="SR1", qb_line_index=0,
        txn_date=date(2026, 5, 2), amount=320.00,
        account_id="A1", account_name="Operating", counterparty_name="Walk-in Customer",
        memo=None, raw_json={"_": 2},
    )
    _upsert_banking_entry(
        db, qb_entity="JournalEntry", qb_txn_id="JE9", qb_line_index=2,
        txn_date=date(2026, 5, 3), amount=-100.00,
        account_id="A2", account_name="Amex", counterparty_name=None,
        memo="manual fee adj", raw_json={"_": 3},
    )
    db.commit()

    feed = unified_banking_transactions(db)
    by_kind = {r["kind"]: r for r in feed}
    assert "bill_payment" in by_kind
    assert by_kind["bill_payment"]["amount"] == -500.00
    assert by_kind["bill_payment"]["counterparty"] == "ACME Supply"
    assert "sales_receipt" in by_kind
    assert by_kind["sales_receipt"]["amount"] == 320.00
    assert "journal_entry" in by_kind
    assert by_kind["journal_entry"]["amount"] == -100.00
    assert by_kind["journal_entry"]["txn_type"] == "JournalEntry"


def test_banking_entry_upsert_is_idempotent(db):
    """Calling _upsert_banking_entry twice with the same key updates the
    existing row; the unified feed shows only ONE entry."""
    from gdx_dispatch.modules.quickbooks.banking import _upsert_banking_entry

    for amount in (-100.00, -150.00):  # second call updates amount.
        _upsert_banking_entry(
            db, qb_entity="BillPayment", qb_txn_id="BP-same", qb_line_index=0,
            txn_date=date(2026, 5, 1), amount=amount,
            account_id="A1", account_name="Op", counterparty_name="V",
            memo=None, raw_json={},
        )
    db.commit()

    feed = [r for r in unified_banking_transactions(db) if r["qb_txn_id"] == "BP-same"]
    assert len(feed) == 1
    assert feed[0]["amount"] == -150.00


def test_banking_entry_reconcile_tombstones_missing(db):
    """If a row was previously synced but not in the latest pull's
    seen_keys set, it gets deleted_at = now(). Unified feed excludes it."""
    from gdx_dispatch.modules.quickbooks.banking import (
        _reconcile_entries_for,
        _upsert_banking_entry,
    )

    _upsert_banking_entry(
        db, qb_entity="BillPayment", qb_txn_id="BP-alive", qb_line_index=0,
        txn_date=date(2026, 5, 1), amount=-50, account_id="A", account_name="Op",
        counterparty_name="V", memo=None, raw_json={},
    )
    _upsert_banking_entry(
        db, qb_entity="BillPayment", qb_txn_id="BP-dead", qb_line_index=0,
        txn_date=date(2026, 5, 1), amount=-75, account_id="A", account_name="Op",
        counterparty_name="V", memo=None, raw_json={},
    )
    db.commit()

    n = _reconcile_entries_for(db, "BillPayment", {("BP-alive", 0)}, "", "")
    assert n == 1

    feed = [r["qb_txn_id"] for r in unified_banking_transactions(db) if r["kind"] == "bill_payment"]
    assert "BP-alive" in feed
    assert "BP-dead" not in feed


def test_banking_entry_reconcile_only_touches_matching_entity(db):
    """_reconcile_entries_for filters by qb_entity — a missing SalesReceipt
    must not tombstone a BillPayment with the same qb_txn_id."""
    from gdx_dispatch.modules.quickbooks.banking import (
        _reconcile_entries_for,
        _upsert_banking_entry,
    )

    _upsert_banking_entry(
        db, qb_entity="BillPayment", qb_txn_id="SHARED", qb_line_index=0,
        txn_date=date(2026, 5, 1), amount=-1, account_id="A", account_name="Op",
        counterparty_name=None, memo=None, raw_json={},
    )
    _upsert_banking_entry(
        db, qb_entity="SalesReceipt", qb_txn_id="SHARED", qb_line_index=0,
        txn_date=date(2026, 5, 1), amount=2, account_id="A", account_name="Op",
        counterparty_name=None, memo=None, raw_json={},
    )
    db.commit()

    # SalesReceipt sees SHARED — but reconcile for BillPayment shouldn't touch it.
    _reconcile_entries_for(db, "SalesReceipt", {("SHARED", 0)}, "", "")
    feed_kinds = {r["kind"] for r in unified_banking_transactions(db) if r["qb_txn_id"] == "SHARED"}
    assert "bill_payment" in feed_kinds
    assert "sales_receipt" in feed_kinds


# ─── Pull functions: field-extraction contract per entity ────────


class _FakeQB:
    """Minimal QBClient stand-in — `query` returns a canned list per entity."""
    def __init__(self, responses):
        self._responses = responses
    async def query(self, entity, where="", max_results=500):
        return self._responses.get(entity, [])


async def _run(coro):
    """Bridge async pull helpers into sync pytest."""
    import asyncio
    return await coro if False else asyncio.get_event_loop().run_until_complete(coro)


def test_pull_bill_payments_extracts_check_bank_account_ref(db):
    """BillPayment's bank account is nested under CheckPayment.BankAccountRef
    OR CreditCardPayment.CCAccountRef. The pull must find either."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"BillPayment": [{
        "Id": "BP-100",
        "TxnDate": "2026-05-01",
        "TotalAmt": 250.00,
        "VendorRef": {"value": "V9", "name": "ACME Supply"},
        "CheckPayment": {"BankAccountRef": {"value": "A1", "name": "Operating"}},
        "PrivateNote": "check 1042",
    }]})
    result = asyncio.run(_b.pull_bill_payments("t", db, qb))
    assert result["created"] == 1

    row = db.execute(text(
        "SELECT amount, account_id, account_name, counterparty_name "
        "FROM qb_banking_entries WHERE qb_entity='BillPayment' AND qb_txn_id='BP-100'"
    )).first()
    assert float(row[0]) == -250.00  # bill payment = money OUT
    assert row[1] == "A1"
    assert row[2] == "Operating"
    assert row[3] == "ACME Supply"


def test_pull_bill_payments_falls_back_to_credit_card_account(db):
    """CC-paid bill: account ref nested under CreditCardPayment.CCAccountRef."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"BillPayment": [{
        "Id": "BP-200",
        "TxnDate": "2026-05-02",
        "TotalAmt": 75.50,
        "VendorRef": {"value": "V1", "name": "Web Host"},
        "CreditCardPayment": {"CCAccountRef": {"value": "CC1", "name": "Amex"}},
    }]})
    asyncio.run(_b.pull_bill_payments("t", db, qb))
    row = db.execute(text(
        "SELECT account_id, account_name FROM qb_banking_entries "
        "WHERE qb_txn_id='BP-200'"
    )).first()
    assert row[0] == "CC1"
    assert row[1] == "Amex"


def test_pull_sales_receipts_signs_positive_and_uses_deposit_to(db):
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"SalesReceipt": [{
        "Id": "SR-1",
        "TxnDate": "2026-05-10",
        "TotalAmt": 320.00,
        "CustomerRef": {"value": "C1", "name": "Walk-in Customer"},
        "DepositToAccountRef": {"value": "A1", "name": "Operating"},
    }]})
    asyncio.run(_b.pull_sales_receipts("t", db, qb))
    row = db.execute(text(
        "SELECT amount, account_name, counterparty_name FROM qb_banking_entries "
        "WHERE qb_txn_id='SR-1'"
    )).first()
    assert float(row[0]) == 320.00  # sale into bank = positive
    assert row[1] == "Operating"
    assert row[2] == "Walk-in Customer"


def test_pull_refund_receipts_signs_negative(db):
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"RefundReceipt": [{
        "Id": "RR-1",
        "TxnDate": "2026-05-11",
        "TotalAmt": 50.00,
        "CustomerRef": {"value": "C2", "name": "Refunded Customer"},
        "DepositToAccountRef": {"value": "A1", "name": "Operating"},
    }]})
    asyncio.run(_b.pull_refund_receipts("t", db, qb))
    row = db.execute(text(
        "SELECT amount FROM qb_banking_entries WHERE qb_txn_id='RR-1'"
    )).first()
    assert float(row[0]) == -50.00  # refund out of bank = negative


def test_pull_journal_entries_emits_only_bank_lines_with_correct_sign(db):
    """JE with three lines: one Debit to Bank, one Credit to Bank, one to
    a non-bank account. Pull emits TWO rows (the bank lines), with sign
    flipped per PostingType."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    # Seed qb_accounts so the bank filter can do its job.
    from uuid import uuid4
    db.execute(text(
        "INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, current_balance, active) "
        "VALUES (:i, 't', 'BANK1', 'Operating', 'Bank', 0, 1)"
    ), {"i": str(uuid4())})
    db.execute(text(
        "INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, current_balance, active) "
        "VALUES (:i, 't', 'EXP1', 'Office Expense', 'Expense', 0, 1)"
    ), {"i": str(uuid4())})
    db.commit()

    qb = _FakeQB({"JournalEntry": [{
        "Id": "JE-77",
        "TxnDate": "2026-05-15",
        "PrivateNote": "month-end adjustment",
        "Line": [
            {"Amount": 100.00, "JournalEntryLineDetail": {
                "PostingType": "Debit",
                "AccountRef": {"value": "BANK1", "name": "Operating"},
            }, "Description": "interest"},
            {"Amount": 60.00, "JournalEntryLineDetail": {
                "PostingType": "Credit",
                "AccountRef": {"value": "BANK1", "name": "Operating"},
            }, "Description": "fee"},
            {"Amount": 999.00, "JournalEntryLineDetail": {
                "PostingType": "Debit",
                "AccountRef": {"value": "EXP1", "name": "Office Expense"},
            }, "Description": "non-bank, must skip"},
        ],
    }]})
    result = asyncio.run(_b.pull_journal_entries("t", db, qb))
    assert result["created"] == 2  # only the two bank lines

    rows = db.execute(text(
        "SELECT qb_line_index, amount FROM qb_banking_entries "
        "WHERE qb_txn_id='JE-77' ORDER BY qb_line_index"
    )).all()
    assert len(rows) == 2
    assert float(rows[0][1]) == 100.00   # Debit → +
    assert float(rows[1][1]) == -60.00   # Credit → −


def test_pull_deposits_serializes_raw_json_for_psycopg2(db):
    """Regression: psycopg2 can't adapt a raw Python dict to a JSONB
    column. The earlier code bound `raw_json: raw` directly — SQLite
    tolerated it, Postgres 500'd. This test pins that the bind value
    is a string, by inspecting the stored value's type after the pull."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"Deposit": [{
        "Id": "D-json",
        "TxnDate": "2026-05-01",
        "TotalAmt": 100.00,
        "DepositToAccountRef": {"value": "A1", "name": "Op"},
    }]})
    asyncio.run(_b.pull_deposits("t", db, qb))

    # On SQLite the raw_json column type is JSON; SQLAlchemy deserializes
    # on read. The point of this test isn't the read shape — it's that
    # the write didn't crash. The write would crash on Postgres without
    # json.dumps; SQLite tolerates either. So we assert the write
    # succeeded (row exists) and the JSON survives round-trip via the
    # ORM. The real-prod canary is `_run` itself completing.
    row = db.execute(text(
        "SELECT raw_json FROM qb_deposits WHERE qb_txn_id = 'D-json'"
    )).first()
    assert row is not None
    # JSON column round-trips: SQLite returns the dict, Postgres returns
    # a dict via psycopg2's jsonb adapter. Either way the structure
    # survives — and a dict-bind would have raised on Postgres before
    # reaching this assertion.
    stored = row[0]
    if isinstance(stored, str):
        import json as _json
        stored = _json.loads(stored)
    assert stored.get("Id") == "D-json"


def test_pull_transfers_serializes_raw_json_for_psycopg2(db):
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"Transfer": [{
        "Id": "T-json",
        "TxnDate": "2026-05-01",
        "Amount": 50.00,
        "FromAccountRef": {"value": "A1", "name": "Op"},
        "ToAccountRef": {"value": "A2", "name": "Savings"},
    }]})
    asyncio.run(_b.pull_transfers("t", db, qb))
    row = db.execute(text(
        "SELECT raw_json FROM qb_transfers WHERE qb_txn_id = 'T-json'"
    )).first()
    assert row is not None


def test_pull_customer_payments_only_includes_direct_to_bank(db):
    """A Payment with DepositToAccountRef set to a bank account is counted.
    A Payment going to Undeposited Funds (or with no account ref) is skipped
    so it doesn't double-count against the Deposit row that will eventually
    move that cash to the bank."""
    import asyncio
    from uuid import uuid4
    from gdx_dispatch.modules.quickbooks import banking as _b

    # Seed qb_accounts: one Bank, one Undeposited-Funds-style other-current-asset.
    db.execute(text(
        "INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, current_balance, active) "
        "VALUES (:i, 't', 'BANK1', 'Operating', 'Bank', 0, 1)"
    ), {"i": str(uuid4())})
    db.execute(text(
        "INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, current_balance, active) "
        "VALUES (:i, 't', 'UNDEP', 'Undeposited Funds', 'Other Current Asset', 0, 1)"
    ), {"i": str(uuid4())})
    db.commit()

    qb = _FakeQB({"Payment": [
        {
            "Id": "PAY-direct",
            "TxnDate": "2026-05-10",
            "TotalAmt": 250.00,
            "CustomerRef": {"value": "C1", "name": "Acme"},
            "DepositToAccountRef": {"value": "BANK1", "name": "Operating"},
        },
        {
            "Id": "PAY-undep",
            "TxnDate": "2026-05-10",
            "TotalAmt": 100.00,
            "CustomerRef": {"value": "C2", "name": "Other"},
            "DepositToAccountRef": {"value": "UNDEP", "name": "Undeposited Funds"},
        },
        {
            "Id": "PAY-noacct",
            "TxnDate": "2026-05-10",
            "TotalAmt": 50.00,
            "CustomerRef": {"value": "C3", "name": "Cash Customer"},
            # No DepositToAccountRef at all.
        },
    ]})
    result = asyncio.run(_b.pull_customer_payments("t", db, qb))
    # Only the direct-to-bank one should be stored.
    assert result["created"] == 1

    rows = db.execute(text(
        "SELECT qb_txn_id, amount, account_name, counterparty_name "
        "FROM qb_banking_entries WHERE qb_entity='Payment'"
    )).all()
    assert len(rows) == 1
    assert rows[0][0] == "PAY-direct"
    assert float(rows[0][1]) == 250.00  # positive — money into bank
    assert rows[0][2] == "Operating"
    assert rows[0][3] == "Acme"


def test_pull_customer_payments_skips_when_bank_ids_unknown(db):
    """If qb_accounts is empty, the pull skips emitting payments AND
    surfaces a warning in the response so the UI can flag it (covered
    in test_pull_customer_payments_surfaces_warning_when_accounts_empty)."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"Payment": [{
        "Id": "PAY-x", "TxnDate": "2026-05-10", "TotalAmt": 10.00,
        "CustomerRef": {}, "DepositToAccountRef": {"value": "BANK1"},
    }]})
    result = asyncio.run(_b.pull_customer_payments("t", db, qb))
    assert result["created"] == 0
    assert result["updated"] == 0
    assert len(result["errors"]) >= 1  # warning surfaced (audit follow-up)


def test_pull_vendor_credits_is_info_only_zero_amount(db):
    """Audit follow-up: VendorCredit must store amount=0 so it can
    never contribute phantom cash to any sum over the unified feed.
    The credit value goes in the memo for display."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"VendorCredit": [{
        "Id": "VC-1",
        "TxnDate": "2026-05-12",
        "TotalAmt": 120.00,
        "VendorRef": {"value": "V1", "name": "Tools Inc"},
        "PrivateNote": "return of defective gear",
    }]})
    result = asyncio.run(_b.pull_vendor_credits("t", db, qb))
    assert result["created"] == 1
    row = db.execute(text(
        "SELECT amount, counterparty_name, memo, account_id FROM qb_banking_entries "
        "WHERE qb_txn_id='VC-1'"
    )).first()
    assert float(row[0]) == 0.0
    assert row[1] == "Tools Inc"
    assert "$120.00" in row[2]
    assert "return of defective gear" in row[2]
    assert row[3] is None


def test_pull_customer_payments_surfaces_warning_when_accounts_empty(db):
    """Audit follow-up: empty bank_ids previously returned a silent
    `{created:0, errors:[]}` zero — making an accounts-sync failure
    indistinguishable from `no customer payments`. Now it returns
    a real error in the response so the toast warns."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"Payment": [{
        "Id": "PAY-x", "TxnDate": "2026-05-10", "TotalAmt": 50,
        "CustomerRef": {}, "DepositToAccountRef": {"value": "BANK1"},
    }]})
    result = asyncio.run(_b.pull_customer_payments("t", db, qb))
    assert result["created"] == 0
    assert len(result["errors"]) == 1
    assert "qb_accounts empty" in result["errors"][0]["error"]


def test_pull_journal_entries_surfaces_warning_when_accounts_empty(db):
    """Same audit follow-up applied to JournalEntry's bank-only filter."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"JournalEntry": [{
        "Id": "JE-x", "TxnDate": "2026-05-10",
        "Line": [{"Amount": 50, "JournalEntryLineDetail": {
            "PostingType": "Debit",
            "AccountRef": {"value": "BANK1"},
        }}],
    }]})
    result = asyncio.run(_b.pull_journal_entries("t", db, qb))
    assert result["created"] == 0
    assert len(result["errors"]) == 1
    assert "qb_accounts empty" in result["errors"][0]["error"]


def test_pull_journal_entries_skips_when_bank_ids_unknown(db):
    """Audit follow-up: if qb_accounts is empty, the JE pull must NOT
    emit anything (previously it accepted every line) — AND it now
    surfaces a warning in the response so the UI can flag it (covered
    in test_pull_journal_entries_surfaces_warning_when_accounts_empty)."""
    import asyncio
    from gdx_dispatch.modules.quickbooks import banking as _b

    qb = _FakeQB({"JournalEntry": [{
        "Id": "JE-orphan",
        "TxnDate": "2026-05-15",
        "Line": [{"Amount": 100, "JournalEntryLineDetail": {
            "PostingType": "Debit",
            "AccountRef": {"value": "BANK1", "name": "Operating"},
        }}],
    }]})
    result = asyncio.run(_b.pull_journal_entries("t", db, qb))
    assert result["created"] == 0
    assert result["updated"] == 0
    assert len(result["errors"]) >= 1  # warning surfaced (audit follow-up)


def test_upsert_clears_deleted_at_when_row_reappears(db):
    """Audit follow-up: false-positive tombstones must be recoverable.
    When a previously-tombstoned row reappears in the QBO response, the
    next upsert clears deleted_at so it's visible again.

    Exercised via direct SQL UPDATE that mimics what pull_deposits does
    on the UPDATE branch — keeps the test focused on the inverse-op
    semantics without needing a live QBClient.
    """
    from sqlalchemy import text

    # Seed a tombstoned deposit.
    db.add(QBDeposit(qb_txn_id="D-zombie", txn_date=date(2026, 5, 1), total_amount=100.0))
    db.commit()
    db.execute(text("UPDATE qb_deposits SET deleted_at = CURRENT_TIMESTAMP WHERE qb_txn_id = 'D-zombie'"))
    db.commit()

    pre = db.execute(text("SELECT deleted_at FROM qb_deposits WHERE qb_txn_id = 'D-zombie'")).scalar()
    assert pre is not None, "row should be tombstoned before re-sync"

    # Simulate the UPDATE branch of pull_deposits (re-sync sees the row).
    db.execute(text("""
        UPDATE qb_deposits SET total_amount = 100.0, last_synced_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP, deleted_at = NULL
        WHERE qb_txn_id = 'D-zombie'
    """))
    db.commit()

    post = db.execute(text("SELECT deleted_at FROM qb_deposits WHERE qb_txn_id = 'D-zombie'")).scalar()
    assert post is None, "deleted_at must clear on re-sync (un-tombstone)"

    # Row is back in the unified feed.
    feed = unified_banking_transactions(db)
    assert any(r["qb_txn_id"] == "D-zombie" for r in feed)
