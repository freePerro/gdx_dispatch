"""GL Phase 1 (S4) — chokepoint pass-through + flush-guard tripwire.

Plan gate: flag off = no behavior change; flag on = raw Invoice.status
writes trip the guard (raise in dev/test, log-only in prod-like GDX_ENV)
while chokepointed writes pass.
"""
from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice
from gdx_dispatch.modules.ledger.guard import (
    ChokepointBypassError,
    install_flush_guard,
)
from gdx_dispatch.modules.ledger.models import GlJournalEntry
from gdx_dispatch.modules.ledger.service import (
    ensure_gl_seed,
    transition_invoice_status,
)

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _guard_installed():
    install_flush_guard()  # idempotent; global for the test process


@pytest.fixture
def db(tenant_db):
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _make_invoice(status="draft", company_id=COMPANY, number=None):
    return Invoice(
        id=uuid4(),
        job_id=None,
        customer_id=uuid4(),  # NOT NULL; FK not enforced in the sqlite fixture
        invoice_number=number or f"INV-{uuid4().hex[:8].upper()}",
        status=status,
        total=Decimal("100.00"),
        amount_paid=Decimal("0.00"),
        invoice_date=dt.date(2026, 7, 1),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=company_id,
    )


def _invoice(db, status="draft"):
    from gdx_dispatch.models.tenant_models import InvoiceLine

    inv = _make_invoice(status)
    db.add(inv)
    db.flush()
    # P1 (S5) refuses invoices whose total doesn't reconcile with lines —
    # give the fixture invoice one matching line so guard tests can issue it.
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="Test work", quantity=1,
            unit_price=Decimal("100.00"), line_total=Decimal("100.00"),
            company_id=COMPANY,
        )
    )
    db.commit()
    return inv


def _enable_posting(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


# ---------------------------------------------------------------------------
# flag OFF (shipped default) — identical behavior
# ---------------------------------------------------------------------------

def test_flag_off_raw_write_still_allowed(db):
    inv = _invoice(db)
    inv.status = "sent"  # the old way — must keep working until cutover
    db.commit()
    assert db.get(Invoice, inv.id).status == "sent"


def test_flag_off_chokepoint_is_pure_passthrough(db):
    inv = _invoice(db)
    old = transition_invoice_status(db, inv, "sent")
    db.commit()
    assert old == "draft"
    assert db.get(Invoice, inv.id).status == "sent"
    assert db.scalars(select(GlJournalEntry)).all() == []  # nothing posts


def test_transition_to_same_status_is_noop(db):
    inv = _invoice(db, status="sent")
    assert transition_invoice_status(db, inv, "sent") == "sent"
    db.commit()


# ---------------------------------------------------------------------------
# flag ON — the tripwire
# ---------------------------------------------------------------------------

def test_flag_on_raw_write_trips_guard(db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    inv.status = "sent"  # bypass
    with pytest.raises(ChokepointBypassError, match="bypassing the ledger"):
        db.flush()
    db.rollback()
    assert db.get(Invoice, inv.id).status == "draft"


def test_flag_on_chokepoint_write_passes(db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    transition_invoice_status(db, inv, "sent")
    db.commit()
    assert db.get(Invoice, inv.id).status == "sent"
    # S5 registered the P1 rule: a flag-on issuance posts one balanced entry
    # (test_gl_invoice_posting.py covers the composition in depth).
    entries = db.scalars(select(GlJournalEntry)).all()
    assert len(entries) == 1 and entries[0].status == "posted"


def test_sanction_is_single_use(db, monkeypatch):
    """One chokepoint call sanctions ONE flush of ONE transition — a later
    raw write on the same instance must still trip."""
    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    transition_invoice_status(db, inv, "sent")
    db.commit()
    inv.status = "paid"  # raw, after a legitimate transition
    with pytest.raises(ChokepointBypassError):
        db.flush()
    db.rollback()


def test_flag_on_nondraft_birth_trips_guard(db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    _enable_posting(db)
    db.add(_make_invoice(status="sent", number="INV-BORNSENT"))
    with pytest.raises(ChokepointBypassError, match="born 'sent'"):
        db.flush()
    db.rollback()


def test_flag_on_draft_birth_is_fine(db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    _enable_posting(db)
    inv = _invoice(db)  # draft birth commits cleanly
    assert db.get(Invoice, inv.id).status == "draft"


def test_prod_env_logs_instead_of_raising(db, monkeypatch, caplog):
    monkeypatch.setenv("GDX_ENV", "production")
    inv = _invoice(db)
    _enable_posting(db)

    inv.status = "sent"
    with caplog.at_level("ERROR"):
        db.commit()  # must NOT raise in prod-like env
    assert db.get(Invoice, inv.id).status == "sent"
    assert any("gl_chokepoint_bypass" in r.message for r in caplog.records)


def test_unset_env_defaults_to_log_only(db, monkeypatch, caplog):
    """Prod runs with GDX_ENV UNSET (app.py convention) — the guard must
    log, never 500 a paying user's request (audit round 1: the original
    tuple check inverted this)."""
    monkeypatch.delenv("GDX_ENV", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    inv.status = "sent"
    with caplog.at_level("ERROR"):
        db.commit()  # must NOT raise
    assert db.get(Invoice, inv.id).status == "sent"
    assert any("gl_chokepoint_bypass" in r.message for r in caplog.records)


def test_rolled_back_transition_leaves_no_sanction(db, monkeypatch):
    """Audit round 1: the sanction is a plain instance attribute, which
    session.rollback() does NOT expire — a rolled-back transition must not
    bless a later raw write (even one to the same status)."""
    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    transition_invoice_status(db, inv, "sent")
    db.rollback()  # transition abandoned

    inv.status = "sent"  # raw write to the SAME status the sanction named
    with pytest.raises(ChokepointBypassError):
        db.flush()
    db.rollback()


def test_stale_sanction_does_not_bless_a_different_status(db, monkeypatch):
    """The sanction carries the target status — it only blesses that exact
    write."""
    from gdx_dispatch.modules.ledger.service import SANCTION_ATTR

    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    setattr(inv, SANCTION_ATTR, "sent")  # forged/stale stamp
    inv.status = "paid"
    with pytest.raises(ChokepointBypassError):
        db.flush()
    db.rollback()


def test_hard_delete_under_flag_trips(db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    db.delete(inv)
    with pytest.raises(ChokepointBypassError, match="hard-deleted"):
        db.flush()
    db.rollback()


def test_celery_module_installs_guard(monkeypatch):
    """Workers write invoice status too (QB sync) — importing the celery app
    module must arm the tripwire in worker processes."""
    monkeypatch.setenv("GDX_ENV", "dev")
    import gdx_dispatch.core.celery_app  # noqa: F401
    import gdx_dispatch.modules.ledger.guard as guard

    assert guard._installed is True


def test_other_company_flag_does_not_leak(db, monkeypatch):
    """The flag is per-company: enabling it for COMPANY must not police
    another company's invoices."""
    monkeypatch.delenv("GDX_ENV", raising=False)
    _enable_posting(db)
    other = _make_invoice(company_id="22222222-2222-2222-2222-222222222222", number="INV-OTHERCO")
    db.add(other)
    db.commit()
    other.status = "sent"  # raw write, but that company never enabled posting
    db.commit()
    assert db.get(Invoice, other.id).status == "sent"
