"""Evidence-derived opening balances (modules/ledger/opening.py).

The statement archive's chained balances ARE the opening evidence: the
latest contiguous run's ending balance is the opening bank balance at the
first month boundary after it. Apply is one idempotent initialization:
reverse era-contradicting entries, set cutover, map/rename bank GL
accounts, post the opening entry, lock the pre-ledger era, attest.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from gdx_dispatch.modules.bank_feeds.statement_models import (
    BankAccount,
    BankStatementImport,
)
from gdx_dispatch.modules.ledger.models import (
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
    ROLE_OPENING_EQUITY,
)
from gdx_dispatch.modules.ledger.opening import (
    OpeningDerivationError,
    apply_opening,
    propose_opening,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed

COMPANY = "11111111-1111-1111-1111-111111111111"


def _mk_import(db, account, start, end, beg, end_bal, n):
    imp = BankStatementImport(
        bank_account_id=account.id,
        period_start=start,
        period_end=end,
        beginning_balance_cents=beg,
        ending_balance_cents=end_bal,
        original_filename=f"stmt-{n}.pdf",
        file_sha256=f"{n:064d}",
        tie_out_status="passed",
    )
    db.add(imp)
    return imp


@pytest.fixture
def world(tenant_db):
    ensure_gl_seed(tenant_db, COMPANY)
    account = BankAccount(
        name="Business Checking", kind="checking",
        institution="Primary Bank", last4="2204",
    )
    tenant_db.add(account)
    tenant_db.flush()
    # Jan–Jun chain exactly like prod: balances link period to period.
    chain = [
        (date(2026, 1, 1), date(2026, 2, 1), 1001059, 331490),
        (date(2026, 2, 2), date(2026, 3, 1), 331490, 357353),
        (date(2026, 3, 2), date(2026, 3, 31), 357353, 753815),
        (date(2026, 4, 1), date(2026, 4, 30), 753815, 382571),
        (date(2026, 5, 1), date(2026, 5, 31), 382571, 546698),
        (date(2026, 6, 1), date(2026, 6, 30), 546698, 448893),
    ]
    for n, (s, e, b, eb) in enumerate(chain):
        _mk_import(tenant_db, account, s, e, b, eb, n)
    tenant_db.commit()
    return tenant_db, account


def test_proposal_derives_cutover_and_opening(world):
    db, account = world
    out = propose_opening(db, COMPANY)
    assert out["cutover"] == "2026-07-01"
    acct = out["accounts"][0]
    assert acct["usable"] is True
    assert acct["opening_cents"] == 448893
    assert acct["chain_from"] == "2026-01-01"
    assert acct["chain_statements"] == 6


def test_broken_chain_uses_latest_contiguous_run(world):
    db, account = world
    # Corrupt the March→April link: April now claims a different beginning.
    imp = db.scalars(select(BankStatementImport).where(
        BankStatementImport.period_start == date(2026, 4, 1))).one()
    imp.beginning_balance_cents = 999999
    db.commit()
    out = propose_opening(db, COMPANY)
    acct = out["accounts"][0]
    assert acct["usable"] is True
    assert acct["chain_from"] == "2026-04-01"  # run restarts at the break
    assert acct["chain_statements"] == 3
    assert acct["opening_cents"] == 448893  # ending anchor unchanged


def test_non_month_end_chain_is_unusable(tenant_db):
    ensure_gl_seed(tenant_db, COMPANY)
    account = BankAccount(name="Mid", kind="checking", institution="B", last4="1111")
    tenant_db.add(account)
    tenant_db.flush()
    _mk_import(tenant_db, account, date(2026, 6, 1), date(2026, 6, 15), 100, 200, 0)
    tenant_db.commit()
    with pytest.raises(OpeningDerivationError, match="not a month end"):
        propose_opening(tenant_db, COMPANY)


def test_apply_full_initialization_and_idempotency(world):
    db, account = world
    # An era-contradicting live entry (like prod's May invoice posting).
    from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
    from gdx_dispatch.modules.ledger.models import ROLE_AR, ROLE_SALES_FALLBACK

    stray = post_for_event(db, PostingEvent(
        company_id=COMPANY, source_type="invoice", source_id="inv-era",
        event="issued", effective_at=date(2026, 5, 12),
        lines=(PostingLine(amount_cents=1000, role=ROLE_AR),
               PostingLine(amount_cents=-1000, role=ROLE_SALES_FALLBACK)),
    ))
    db.commit()

    out = apply_opening(db, COMPANY, actor="doug", expected_cutover=date(2026, 7, 1), expected_reversals=1)
    db.commit()

    assert out["cutover"] == "2026-07-01"
    assert stray.entry_no in out["reversed_entry_nos"]
    assert out["lock_date"] == "2026-06-30"
    assert len(out["opening_entries"]) == 1
    assert out["opening_entries"][0]["cents"] == 448893

    settings = ensure_gl_seed(db, COMPANY)
    assert settings.cutover_month == date(2026, 7, 1)
    assert settings.opening_bank_attested_by == "doug"

    # The bank account maps to the renamed Operating Bank role account.
    db.refresh(account)
    gl = db.get(GlAccount, account.gl_account_id)
    assert gl.role is not None
    assert gl.name == "Business Checking …2204"

    # Opening entry: debit the bank account, credit Opening Balance Equity.
    entry = db.scalars(select(GlJournalEntry).where(
        GlJournalEntry.source_type == "bank_account")).one()
    lines = {l.account_id: int(l.amount_cents) for l in db.scalars(
        select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)).all()}
    assert lines[gl.id] == 448893
    equity = db.scalars(select(GlAccount).where(
        GlAccount.company_id == COMPANY, GlAccount.role == ROLE_OPENING_EQUITY)).one()
    assert lines[equity.id] == -448893

    # Era lock in place.
    assert db.scalars(select(GlPeriodLock).where(
        GlPeriodLock.lock_date == date(2026, 6, 30))).first() is not None

    # Idempotent: re-apply changes nothing.
    out2 = apply_opening(db, COMPANY, actor="doug", expected_cutover=date(2026, 7, 1), expected_reversals=0)
    db.commit()
    assert out2["reversed_entry_nos"] == []
    entries = db.scalars(select(GlJournalEntry).where(
        GlJournalEntry.source_type == "bank_account")).all()
    assert len(entries) == 1


def test_apply_refuses_conflicting_existing_cutover(world):
    db, account = world
    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = date(2026, 6, 1)
    db.commit()
    with pytest.raises(OpeningDerivationError, match="cannot move"):
        apply_opening(db, COMPANY, actor="doug")


def test_apply_binding_refuses_changed_evidence(world):
    """TOCTOU guard: apply is bound to the proposal the operator SAW."""
    db, account = world
    with pytest.raises(OpeningDerivationError, match="refresh and review"):
        apply_opening(db, COMPANY, actor="doug",
                      expected_cutover=date(2026, 8, 1), expected_reversals=0)


def test_corrected_archive_reapply_reverses_stale_opening(world):
    """Gate-audit finding 1: office voids June, imports a corrected June
    with a different ending balance, re-applies — the stale opening entry
    must be REVERSED, never stacked (double-counted bank)."""
    from gdx_dispatch.core.audit import utcnow as _utcnow

    db, account = world
    apply_opening(db, COMPANY, actor="doug",
                  expected_cutover=date(2026, 7, 1), expected_reversals=0)
    db.commit()

    june = db.scalars(select(BankStatementImport).where(
        BankStatementImport.period_start == date(2026, 6, 1))).one()
    june.voided_at = _utcnow()
    _mk_import(db, account, date(2026, 6, 1), date(2026, 6, 30), 546698, 500000, 99)
    db.commit()

    out = apply_opening(db, COMPANY, actor="doug",
                        expected_cutover=date(2026, 7, 1), expected_reversals=0)
    db.commit()
    assert out["opening_entries"][0]["cents"] == 500000
    assert out["stale_opening_reversed"], "the superseded opening entry must reverse"

    live = db.scalars(select(GlJournalEntry).where(
        GlJournalEntry.source_type == "bank_account",
        GlJournalEntry.status == "posted",
        GlJournalEntry.reverses_entry_id.is_(None),
    )).all()
    assert len(live) == 1
    lines = db.scalars(select(GlJournalLine).where(
        GlJournalLine.entry_id == live[0].id)).all()
    assert max(int(l.amount_cents) for l in lines) == 500000


def test_checking_claims_operating_bank_before_savings(tenant_db):
    """Gate-audit finding 5: a savings account must never claim the
    Operating Bank role by DB ordering luck."""
    ensure_gl_seed(tenant_db, COMPANY)
    # Savings inserted FIRST — alphabetically and physically ahead.
    savings = BankAccount(name="A Savings", kind="savings", institution="B", last4="0001")
    checking = BankAccount(name="Z Checking", kind="checking", institution="B", last4="0002")
    tenant_db.add_all([savings, checking])
    tenant_db.flush()
    for n, acct in enumerate([savings, checking]):
        _mk_import(tenant_db, acct, date(2026, 6, 1), date(2026, 6, 30), 0, 12345 + n, 50 + n)
    tenant_db.commit()

    apply_opening(tenant_db, COMPANY, actor="doug",
                  expected_cutover=date(2026, 7, 1), expected_reversals=0)
    tenant_db.commit()

    tenant_db.refresh(checking)
    gl = tenant_db.get(GlAccount, checking.gl_account_id)
    assert gl.role is not None, "checking must hold the Operating Bank role"
    tenant_db.refresh(savings)
    gl_s = tenant_db.get(GlAccount, savings.gl_account_id)
    assert gl_s.role is None and gl_s.code == "1010"
