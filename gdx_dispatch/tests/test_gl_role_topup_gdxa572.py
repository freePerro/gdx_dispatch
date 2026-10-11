"""GDXA-572 — a role added to ALL_ROLES reaches an existing install at its
first posting, not at the next accounting-settings save.

``seed_coa`` tops system roles up, but nothing runs it at boot: its callers
are ``ensure_gl_seed`` (the accounting-settings endpoints) and the opening
apply. Before this fix only the surcharge leg went through the first-use
top-up (``ensure_role_account``); every other posting resolved through the
strict ``resolve_role_account`` inside ``engine._resolve_lines``, so the next
new role would raise ``LedgerConfigError`` inside whatever recorded the money
on an install whose CoA predates it.

Simulated, for every role, by deleting that role's system account (SQLite —
no immutability triggers) and posting a line that needs it. Real posts and
read-backs, no mocks.
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from gdx_dispatch.modules.ledger.coa import DEFAULT_COA, LedgerConfigError, seed_coa
from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
from gdx_dispatch.modules.ledger.models import (
    ALL_ROLES,
    ROLE_AR,
    ROLE_CUSTOMER_CREDITS,
    ROLE_ROUNDING,
    GlAccount,
    GlJournalLine,
)
from gdx_dispatch.modules.ledger.rules import (
    customer_credit_balance_cents,
    invoice_ar_balance_cents,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
DAY = dt.date(2026, 7, 1)
CODE_BY_ROLE = {role: code for code, _n, _t, role, _s in DEFAULT_COA if role}


@pytest.fixture
def ledger_db(tenant_db):
    seed_coa(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _role_accounts(db, role):
    return db.scalars(select(GlAccount).where(
        GlAccount.company_id == COMPANY, GlAccount.role == role)).all()


def _drop_role(db, role):
    for acct in _role_accounts(db, role):
        db.delete(acct)
    db.commit()
    assert _role_accounts(db, role) == []


def _event(role, source_id):
    other = ROLE_ROUNDING if role != ROLE_ROUNDING else ROLE_AR
    return PostingEvent(
        company_id=COMPANY,
        source_type="manual",
        source_id=source_id,
        event="recorded",
        effective_at=DAY,
        lines=(
            PostingLine(amount_cents=1_234, role=role),
            PostingLine(amount_cents=-1_234, role=other),
        ),
    )


@pytest.mark.parametrize("role", sorted(ALL_ROLES))
def test_a_posting_recreates_a_role_account_the_install_never_had(ledger_db, role):
    _drop_role(ledger_db, role)

    entry = post_for_event(ledger_db, _event(role, f"topup-{role}"))
    ledger_db.commit()

    accounts = _role_accounts(ledger_db, role)
    assert len(accounts) == 1
    acct = accounts[0]
    assert (acct.code, acct.is_system, acct.active) == (CODE_BY_ROLE[role], True, True)
    landed = ledger_db.scalars(select(GlJournalLine).where(
        GlJournalLine.entry_id == entry.id, GlJournalLine.account_id == acct.id)).all()
    assert [ln.amount_cents for ln in landed] == [1_234]


@pytest.mark.parametrize("role", sorted(ALL_ROLES))
def test_a_deactivated_role_account_is_not_resurrected_by_a_posting(ledger_db, role):
    (acct,) = _role_accounts(ledger_db, role)
    acct.active = False
    ledger_db.commit()

    with pytest.raises(LedgerConfigError, match=role):
        post_for_event(ledger_db, _event(role, f"dead-{role}"))
    ledger_db.rollback()

    accounts = _role_accounts(ledger_db, role)
    assert [(a.code, a.active) for a in accounts] == [(CODE_BY_ROLE[role], False)]


def test_the_top_up_flushes_but_never_commits(ledger_db):
    """The new account rides the caller's transaction: if what recorded the
    money rolls back, the account goes with it."""
    _drop_role(ledger_db, ROLE_CUSTOMER_CREDITS)
    post_for_event(ledger_db, _event(ROLE_CUSTOMER_CREDITS, "rolled-back"))
    assert len(_role_accounts(ledger_db, ROLE_CUSTOMER_CREDITS)) == 1
    ledger_db.rollback()
    assert _role_accounts(ledger_db, ROLE_CUSTOMER_CREDITS) == []


def test_an_unseeded_company_stays_loud_and_gets_no_chart(tenant_db):
    """The top-up is for an install that already has a chart of accounts. A
    company with none is not seeded by whichever posting happens first."""
    with pytest.raises(LedgerConfigError, match="no active system account"):
        post_for_event(tenant_db, _event(ROLE_AR, "unseeded"))
    tenant_db.rollback()
    assert tenant_db.scalars(select(GlAccount)).all() == []


def test_a_credit_balance_read_on_a_never_seeded_role_is_zero_and_writes_nothing(ledger_db):
    _drop_role(ledger_db, ROLE_CUSTOMER_CREDITS)
    assert customer_credit_balance_cents(ledger_db, COMPANY, uuid.uuid4()) == 0
    assert _role_accounts(ledger_db, ROLE_CUSTOMER_CREDITS) == []


def test_a_credit_balance_read_on_a_deactivated_role_stays_loud(ledger_db):
    (acct,) = _role_accounts(ledger_db, ROLE_CUSTOMER_CREDITS)
    acct.active = False
    ledger_db.commit()
    with pytest.raises(LedgerConfigError, match=ROLE_CUSTOMER_CREDITS):
        customer_credit_balance_cents(ledger_db, COMPANY, uuid.uuid4())


def test_an_invoice_ar_read_on_a_never_seeded_role_is_zero_and_writes_nothing(ledger_db):
    _drop_role(ledger_db, ROLE_AR)
    invoice = SimpleNamespace(id=uuid.uuid4(), company_id=COMPANY)
    assert invoice_ar_balance_cents(ledger_db, invoice) == 0
    assert _role_accounts(ledger_db, ROLE_AR) == []
