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
    SANCTION_ATTR,
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
    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    setattr(inv, SANCTION_ATTR, "sent")  # forged/stale stamp
    inv.status = "paid"
    with pytest.raises(ChokepointBypassError):
        db.flush()
    db.rollback()


# ── GDXA-51: a rollback listener must not assume it is the root's ──────
#
# SQLAlchemy dispatches a transaction's rollback events for every transaction
# where `_parent is None or nested` (2.0.54 orm/session.py:1360-1366) — `nested`
# is an INCLUSION, so a SAVEPOINT rollback reaches the same listener the root
# rollback does, and the money path's savepoint sites are real
# (`engine.post_event`'s idempotency-key retry, `core/payments.py`'s audit
# savepoint). The old listener popped the whole registry on any of them.
#
# ⚠ Read the honest scope before trusting these: only the FIRST test below
# fails against the old listener. The other three are regression nets — they
# pass either way, and exist so the nested branch cannot be "simplified" back
# into a false negative. And the first test's shape (a pending invoice
# chokepointed down to `draft`, which `_check_flush`'s `session.new` arm
# short-circuits past without spending the stamp) has NO shipping caller: all
# ten non-test `transition_invoice_status` call sites target sent/paid/void,
# and `SessionTransaction._take_snapshot` flushes on every `begin_nested()`,
# spending the stamp before any savepoint exists. This is a latent-shape
# repair, not a live-defect repair.


def test_savepoint_rollback_keeps_a_sanction_it_did_not_unwind(db, monkeypatch):
    """The one test here that detects the defect.

    A SAVEPOINT rollback undoes only what happened after the savepoint, so a
    sanctioned status write flushed BEFORE it survives — and its sanction has
    to survive with it, because the registry is what the guard uses to decide
    a sanction is dead. Stripping it makes the listener's verdict depend on
    savepoint depth rather than on what was rolled back.
    """
    monkeypatch.delenv("GDX_ENV", raising=False)
    _enable_posting(db)

    inv = _make_invoice(status="sent", number="INV-SPKEEP")
    db.add(inv)
    transition_invoice_status(db, inv, "draft")
    db.flush()  # the sanctioned write lands BEFORE the savepoint exists
    assert getattr(inv, SANCTION_ATTR, None) == "draft"

    sp = db.begin_nested()
    sp.rollback()  # unwound nothing of this invoice's

    assert getattr(inv, SANCTION_ATTR, None) == "draft", (
        "a SAVEPOINT rollback cleared a sanction whose status write it never "
        "touched — the listener is treating a nested rollback as the root's"
    )
    assert inv.status == "draft"  # and the write itself did survive
    db.rollback()


def test_savepoint_rollback_still_clears_the_sanction_it_did_unwind(db, monkeypatch):
    """Regression net (passes against the old listener too). A transition
    minted INSIDE the savepoint is genuinely undone by rolling it back, so its
    sanction must die — a stale one would bless a later raw write to the same
    status (audit round 1's finding, at savepoint depth). This is the false
    negative the nested branch must not acquire: `emit.py`'s `_drop_pending`
    can `return` outright on a nested rollback; this guard cannot."""
    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    sp = db.begin_nested()
    transition_invoice_status(db, inv, "sent")
    sp.rollback()

    assert getattr(inv, SANCTION_ATTR, None) is None
    assert inv.status == "draft"  # the write was unwound with the savepoint

    inv.status = "sent"  # raw write to the status the dead sanction named
    with pytest.raises(ChokepointBypassError):
        db.flush()
    db.rollback()


def test_savepoint_rollback_clears_a_sanction_on_an_expunged_invoice(db, monkeypatch):
    """Regression net for `_sanctioned_write_survived`'s expunge branch: an
    invoice created inside the savepoint is expunged to transient by the
    rollback, not merely expired, so `state.session` is None and there is no
    loaded status to compare."""
    monkeypatch.delenv("GDX_ENV", raising=False)
    _enable_posting(db)

    sp = db.begin_nested()
    inv = _make_invoice(status="sent", number="INV-SPGONE")
    db.add(inv)
    transition_invoice_status(db, inv, "draft")
    db.flush()
    assert getattr(inv, SANCTION_ATTR, None) == "draft"
    sp.rollback()  # the whole invoice goes with the savepoint

    assert getattr(inv, SANCTION_ATTR, None) is None
    db.rollback()


def test_a_poisoned_flush_rollback_still_clears_sanctions(db, monkeypatch):
    """Regression net. A failed flush rolls the whole transaction back from
    inside SQLAlchemy — a non-nested rollback, so everything staged is gone
    and no sanction may survive. This is the flavour where
    `session.in_transaction()` still reads True, which is why the listener
    discriminates on `previous_transaction.nested` instead."""
    from sqlalchemy.exc import IntegrityError

    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    transition_invoice_status(db, inv, "sent")
    db.add(_make_invoice(number=inv.invoice_number))  # duplicate → flush fails
    with pytest.raises(IntegrityError):
        db.flush()

    assert getattr(inv, SANCTION_ATTR, None) is None
    db.rollback()


def test_a_real_rollback_drains_the_registry(db, monkeypatch):
    """The registry's only drain. Nothing clears it on commit or close, so if
    the non-nested branch ever stopped popping unconditionally it would hold
    live stamps and strong Invoice references for the life of the session."""
    from gdx_dispatch.modules.ledger.service import SANCTION_REGISTRY_KEY

    monkeypatch.delenv("GDX_ENV", raising=False)
    inv = _invoice(db)
    _enable_posting(db)

    transition_invoice_status(db, inv, "sent")
    assert db.info.get(SANCTION_REGISTRY_KEY)
    db.rollback()
    assert not db.info.get(SANCTION_REGISTRY_KEY)


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


# ── #422: a void is terminal ────────────────────────────────────────
#
# void_invoice releases the invoice's parts and change orders back to the
# unbilled checklist. Nothing on the Stripe payment path refused a voided
# invoice, so a PaymentIntent that succeeded just before the void — or a
# webhook redelivered after it — booked its money and flipped void -> paid.
# The books then showed a paid invoice whose work was simultaneously unbilled.


def test_void_is_terminal_chokepoint_refuses_to_leave_it(db):
    inv = _invoice(db, status="void")
    previous = transition_invoice_status(db, inv, "paid")
    assert inv.status == "void", "a voided invoice must not be resurrected to paid"
    assert previous == "void"


def test_void_refusal_never_raises_so_the_payment_row_survives(db):
    """The money moved. Raising here would roll back the Payment being
    recorded and strand cash at the processor with nothing to refund."""
    inv = _invoice(db, status="void")
    transition_invoice_status(db, inv, "paid")  # must not raise
    db.flush()  # and must leave the session usable for the payment write


@pytest.mark.parametrize("target", ["paid", "sent", "draft"])
def test_no_status_escapes_a_void(db, target):
    inv = _invoice(db, status="void")
    assert transition_invoice_status(db, inv, target) == "void"
    assert inv.status == "void"


def test_entering_a_void_still_works(db):
    """The guard blocks leaving a void, not reaching one."""
    inv = _invoice(db, status="sent")
    assert transition_invoice_status(db, inv, "void") == "sent"
    assert inv.status == "void"


def test_ordinary_transitions_are_untouched(db):
    inv = _invoice(db, status="draft")
    assert transition_invoice_status(db, inv, "sent") == "draft"
    assert inv.status == "sent"
    assert transition_invoice_status(db, inv, "paid") == "sent"
    assert inv.status == "paid"
