"""The card surcharge in the books (2026-09-16).

A customer pays $514.50 by credit card on a $500 invoice: $500 settles the
invoice, $14.50 is the processing fee they agreed to on the pay page. One
settlement, one entry: the bank account receives 514.50, AR is relieved by
500.00, and 14.50 is income on 4950 Card Surcharge Income — never on the
invoice, never a customer credit. Stripe's own fee is an expense on the other
side, so the two net on the P&L.
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment
from gdx_dispatch.modules.ledger import reports
from gdx_dispatch.modules.ledger.coa import LedgerConfigError, seed_coa
from gdx_dispatch.modules.ledger.models import ROLE_SURCHARGE_INCOME, GlAccount
from gdx_dispatch.modules.ledger.rules import build_payment_lines, post_payment_received
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()
    return tenant_db


def _invoice(db, total="500.00"):
    inv = Invoice(
        id=uuid4(), customer_id=uuid4(), invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft", subtotal=Decimal(total), tax_amount=Decimal("0"), total=Decimal(total),
        balance_due=Decimal(total), invoice_date=date(2026, 9, 10),
        public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, description="Door service", quantity=1,
                       unit_price=Decimal(total), line_total=Decimal(total), company_id=COMPANY))
    db.flush()
    transition_invoice_status(db, inv, "sent", actor="t")
    db.flush()
    return inv


def _pay(db, invoice, *, amount, surcharge=None, method="card"):
    payment = Payment(
        id=uuid4(), invoice_id=invoice.id, amount=Decimal(amount), method=method,
        surcharge_amount=Decimal(surcharge) if surcharge else None,
        payment_date=date(2026, 9, 16), company_id=COMPANY,
    )
    db.add(payment)
    db.flush()
    post_payment_received(db, payment, invoice, actor="t")
    db.flush()
    return payment


def test_the_surcharge_account_is_seeded_by_role_on_existing_installs(db):
    """`seed_coa` tops up SYSTEM rows by role on every run — that is how a
    release that adds a role reaches prod without a hand step. Prove it: drop
    the account, seed again, it is back."""
    acct = db.scalars(select(GlAccount).where(
        GlAccount.company_id == COMPANY, GlAccount.role == ROLE_SURCHARGE_INCOME)).one()
    assert (acct.code, acct.name, acct.type, acct.is_system) == ("4950", "Card Surcharge Income", "revenue", True)
    db.delete(acct)
    db.flush()
    assert seed_coa(db, COMPANY) == 1
    again = db.scalars(select(GlAccount).where(
        GlAccount.company_id == COMPANY, GlAccount.role == ROLE_SURCHARGE_INCOME)).one()
    assert again.code == "4950"


def test_a_surcharged_payment_posts_the_fee_to_income_not_to_the_invoice(db):
    inv = _invoice(db, total="500.00")
    _pay(db, inv, amount="500.00", surcharge="14.50")

    tb = reports.trial_balance(db, COMPANY, as_of=date(2026, 9, 30))
    assert tb["totals"]["zero_proof_cents"] == 0
    by_code = {r["code"]: r for r in tb["rows"]}
    assert by_code["1050"]["debit_cents"] == 514_50, "the bank got amount + fee"
    # AR was debited 500 by the invoice and credited 500 by the payment: it
    # nets to zero, and the trial balance may omit a zero-net account.
    ar = by_code.get("1200")
    assert ar is None or ar["debit_cents"] == ar["credit_cents"], "AR relieved by the amount only"
    assert by_code["4950"]["credit_cents"] == 14_50, "the fee is income on 4950"
    assert "2300" not in by_code or by_code["2300"]["credit_cents"] == 0, "never a customer credit"


def test_the_fee_leg_is_in_the_same_entry_so_a_void_reverses_both(db):
    inv = _invoice(db, total="500.00")
    pay = _pay(db, inv, amount="500.00", surcharge="14.50")
    lines = build_payment_lines(db, pay, inv)
    by_role = {ln.role: ln.amount_cents for ln in lines}
    assert by_role["UNDEPOSITED"] == 514_50
    assert by_role["AR"] == -500_00
    assert by_role[ROLE_SURCHARGE_INCOME] == -14_50
    assert sum(by_role.values()) == 0


def test_a_payment_without_a_surcharge_posts_exactly_as_before(db):
    inv = _invoice(db, total="500.00")
    pay = _pay(db, inv, amount="200.00", method="check")
    lines = build_payment_lines(db, pay, inv)
    assert [(ln.role, ln.amount_cents) for ln in lines] == [("UNDEPOSITED", 200_00), ("AR", -200_00)]


def test_an_install_seeded_before_the_role_existed_gets_4950_at_first_use(db):
    """Prod's chart of accounts predates 4950, and nothing runs the seed
    between a deploy and the next accounting mutation — `ensure_gl_seed` is
    called only from the accounting settings endpoints (audit round 2, run
    against prod read-only: posting on, 216 entries, no row for the role).
    So the first surcharged receipt tops the role up itself; the alternative
    was a LedgerConfigError inside the recorder with the card already charged
    and no Payment row."""
    acct = db.scalars(select(GlAccount).where(
        GlAccount.company_id == COMPANY, GlAccount.role == ROLE_SURCHARGE_INCOME)).one()
    db.delete(acct)
    db.flush()
    inv = _invoice(db, total="500.00")
    _pay(db, inv, amount="500.00", surcharge="14.50")  # raised before the top-up

    tb = reports.trial_balance(db, COMPANY, as_of=date(2026, 9, 30))
    assert tb["totals"]["zero_proof_cents"] == 0
    by_code = {r["code"]: r for r in tb["rows"]}
    assert by_code["1050"]["debit_cents"] == 514_50
    assert by_code["4950"]["credit_cents"] == 14_50
    again = db.scalars(select(GlAccount).where(
        GlAccount.company_id == COMPANY, GlAccount.role == ROLE_SURCHARGE_INCOME)).one()
    assert (again.code, again.is_system, again.active) == ("4950", True, True)


def test_a_deactivated_surcharge_account_is_not_resurrected_at_first_use(db):
    """The top-up is for a role the install never had, not for one the office
    switched off: the seed keys on role across inactive rows too, so a
    deactivated owner stays deactivated and the posting stays loud."""
    acct = db.scalars(select(GlAccount).where(
        GlAccount.company_id == COMPANY, GlAccount.role == ROLE_SURCHARGE_INCOME)).one()
    acct.active = False
    db.flush()
    inv = _invoice(db, total="500.00")
    with pytest.raises(LedgerConfigError, match="SURCHARGE_INCOME"):
        _pay(db, inv, amount="500.00", surcharge="14.50")
