"""Money-audit M17 items 2-4 — the smaller payment-path holes, closed.

- **17.2** `charge_method`'s idempotency key was optional from both header and
  body, so a caller that sent neither got no double-charge protection at all: a
  double-click minted two distinct intents and two full-balance charges. The
  key now falls back to a server-derived shape like the public pay path.
  (`charge_method` and its tests were deleted 2026-10-11, GDXA-552: the portal
  router was unreachable — `CustomerUser` has no `stripe_customer_id` — and
  nothing called it.)
- **17.3** `/confirm`, `ach/charge` and the portal charge recorded
  ``pi.amount`` (what was ASKED) while the webhook records ``amount_received``
  (what MOVED). Identical under auto-capture — no mint site here sets
  ``capture_method="manual"`` — divergent the moment one does. All recording
  sites now agree on ``amount_received``. (`ach/charge` was deleted
  2026-09-16 — ACH completes in the browser and the webhook records it — and
  the portal charge 2026-10-11, so two sites remain: `/confirm` and the
  webhook.)
- **17.4** `_next_invoice_number` races: the generator is consolidated and the
  office create path retries on IntegrityError, but the mobile and deposit
  creators flushed bare — the loser of a same-instant race 500'd. They retry
  now.
"""
from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment

TENANT = "tenant-m17"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def invoice(db):
    inv = Invoice(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), job_id=uuid.uuid4(),
        invoice_number="INV-M17", billing_type="standard",
        subtotal=Decimal("300.00"), tax_amount=Decimal("0"), total=Decimal("300.00"),
        balance_due=Decimal("300.00"), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        id=uuid.uuid4(), invoice_id=inv.id, description="Panel replacement",
        quantity=1, unit_price=Decimal("300.00"), line_total=Decimal("300.00"),
        company_id=TENANT,
    ))
    db.commit()
    db.refresh(inv)
    return inv


def _pi(pid, *, amount, amount_received=None, status="succeeded"):
    return SimpleNamespace(
        id=pid, status=status, amount=amount, amount_received=amount_received,
        metadata={}, currency="usd", client_secret="cs_x",
    )


# ── 17.3: record what MOVED, not what was asked


def test_confirm_records_amount_received(db, invoice):
    """A manually-captured intent can settle for less than it asked. `amount`
    is the ask; `amount_received` is the money. The webhook already records
    the latter — /confirm now agrees."""
    from gdx_dispatch.core.payments import ConfirmPaymentRequest, confirm_payment

    pi = _pi("pi_cap", amount=30000, amount_received=20000)
    pi.metadata = {"invoice_id": str(invoice.id)}
    req = SimpleNamespace(state=SimpleNamespace(tenant={}))
    with patch("stripe.PaymentIntent.retrieve", return_value=pi), \
            patch("gdx_dispatch.core.payments._resolve_public_invoice", return_value=invoice), \
            patch("stripe.PaymentIntent.list", return_value=SimpleNamespace(data=[], has_more=False)), \
            patch("gdx_dispatch.core.payments._init_stripe"):
        confirm_payment(ConfirmPaymentRequest(invoice_token=invoice.public_token,
                                              payment_intent_id="pi_cap"), req, db=db)

    row = db.execute(select(Payment).where(Payment.reference == "pi_cap")).scalar_one()
    assert float(row.amount) == 200.00, (
        "recorded the ASK ($300), not the money that MOVED ($200)"
    )


def test_confirm_still_records_legacy_intents_without_the_field(db, invoice):
    """The fallback: an intent with no amount_received records `amount` exactly
    as before — the counterfactual that keeps today's behavior unchanged."""
    from gdx_dispatch.core.payments import ConfirmPaymentRequest, confirm_payment

    pi = _pi("pi_leg", amount=30000, amount_received=None)
    pi.metadata = {"invoice_id": str(invoice.id)}
    req = SimpleNamespace(state=SimpleNamespace(tenant={}))
    with patch("stripe.PaymentIntent.retrieve", return_value=pi), \
            patch("gdx_dispatch.core.payments._resolve_public_invoice", return_value=invoice), \
            patch("stripe.PaymentIntent.list", return_value=SimpleNamespace(data=[], has_more=False)), \
            patch("gdx_dispatch.core.payments._init_stripe"):
        confirm_payment(ConfirmPaymentRequest(invoice_token=invoice.public_token,
                                              payment_intent_id="pi_leg"), req, db=db)

    row = db.execute(select(Payment).where(Payment.reference == "pi_leg")).scalar_one()
    assert float(row.amount) == 300.00


def test_all_recording_sites_agree_on_amount_received():
    """The webhook always recorded `amount_received`; /confirm and the portal
    recorded `amount`. The books must not depend on which message arrives
    first. Source-shape check across the fixed sites — the behavioral proof
    for /confirm is above. (ach/charge was deleted 2026-09-16, the portal
    charge 2026-10-11.)"""
    core = pathlib.Path(
        __import__("gdx_dispatch.core.payments", fromlist=["__file__"]).__file__
    ).read_text()
    for fn in ("def confirm_payment(",):
        i = core.index(fn)
        j = core.index("\n@", i) if "\n@" in core[i:] else len(core)
        assert 'getattr(pi, "amount_received", None) or pi.amount' in core[i:j], (
            f"{fn} records the ASK, not what moved"
        )


# ── 17.4: the same-instant sibling loses gracefully, not with a 500


def test_the_retry_helper_regenerates_once_and_flushes(db, invoice):
    """Behavioral, not source-text: a number collision rolls back, regenerates
    and flushes; the caller keeps its own row."""
    from sqlalchemy.exc import IntegrityError

    from gdx_dispatch.core.closeout_billing import flush_invoice_with_number_retry

    calls = {"n": 0}

    class FakeDB:
        def flush(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed: invoices.invoice_number"))
        def rollback(self): calls["rolled"] = True
        def add(self, obj): calls["readded"] = obj
        def execute(self, *a, **k):
            class R:
                def first(self_inner): return None
            return R()

    inv = SimpleNamespace(invoice_number="INV-000001")
    with patch("gdx_dispatch.core.closeout_billing.next_invoice_number", return_value="INV-000002"):
        out = flush_invoice_with_number_retry(FakeDB(), inv)
    assert out is inv
    assert inv.invoice_number == "INV-000002"
    assert calls["n"] == 2 and calls.get("rolled") and calls.get("readded") is inv


def test_the_retry_helper_adopts_a_concurrent_winner(db):
    """The adversarial find: rollback RELEASES the FOR UPDATE lock that
    serialized concurrent pay-clicks, so the rival may mint the deposit first.
    The helper re-checks and hands back the winner instead of double-minting."""
    from sqlalchemy.exc import IntegrityError

    from gdx_dispatch.core.closeout_billing import flush_invoice_with_number_retry

    class FakeDB:
        def __init__(self): self.n = 0
        def flush(self):
            self.n += 1
            if self.n == 1:
                raise IntegrityError("stmt", {}, Exception("invoice_number"))
            raise AssertionError("must not flush our row once the winner exists")
        def rollback(self): pass
        def add(self, obj): raise AssertionError("must not re-add over a winner")

    ours = SimpleNamespace(invoice_number="INV-000001")
    theirs = SimpleNamespace(invoice_number="INV-000001", id="winner")
    out = flush_invoice_with_number_retry(FakeDB(), ours, already_won=lambda: theirs)
    assert out is theirs


def test_the_retry_helper_leaves_foreign_integrity_errors_alone(db):
    from sqlalchemy.exc import IntegrityError

    from gdx_dispatch.core.closeout_billing import flush_invoice_with_number_retry

    class FakeDB:
        def flush(self): raise IntegrityError("stmt", {}, Exception("payments.reference"))
        def rollback(self): raise AssertionError("a foreign constraint is not ours to absorb")

    with pytest.raises(IntegrityError):
        flush_invoice_with_number_retry(FakeDB(), SimpleNamespace(invoice_number="x"))


def test_all_three_creators_route_through_the_helper():
    """Call-site pinning only — the behavior is proven above, once. The
    deposit site must ALSO adopt the winner (return early on a foreign row),
    or the lock-release fix silently degrades to a double-mint."""
    office = pathlib.Path(
        __import__("gdx_dispatch.routers.invoices", fromlist=["__file__"]).__file__
    ).read_text()
    mobile = pathlib.Path(
        __import__("gdx_dispatch.routers.mobile_invoicing", fromlist=["__file__"]).__file__
    ).read_text()
    deposits = pathlib.Path(
        __import__("gdx_dispatch.modules.deposits.service", fromlist=["__file__"]).__file__
    ).read_text()
    assert office.count("flush_invoice_with_number_retry(db, invoice)") == 1
    assert mobile.count("flush_invoice_with_number_retry(db, invoice)") == 1
    i = deposits.index("flush_invoice_with_number_retry(")
    tail = deposits[i:i + 500]
    assert "already_won=" in tail, "deposits must re-check after the lock-releasing rollback"
    assert "if flushed is not invoice:" in tail and "return flushed" in tail
