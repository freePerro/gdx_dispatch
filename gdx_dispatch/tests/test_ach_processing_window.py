"""ACH's double-payment window is closed while a debit is in flight (M16).

ACH is a delayed-notification method — "up to 4 business days to receive
acknowledgement of success or failure"
(<https://docs.stripe.com/payments/ach-direct-debit>, read 2026-08-24). Nothing
was recorded while a debit sat in `processing`: the balance stayed full, the
pay page stayed live, and Stripe's idempotency key expires after 24h — so a
customer paying Friday and again Monday minted a second intent, and both
settled.

The fix is stateless, like M12 whose scan it reuses: a `processing` intent
bound to the invoice by `metadata.invoice_id` IS the pending marker. Three
surfaces honor it:

- the mint sites (`create-intent`, `ach/charge`, the portal) refuse with a 409
  that tells the customer their transfer is already moving;
- the pay page renders "Bank transfer processing" instead of a live form;
- the webhook notes `payment_intent.processing` on the invoice's audit trail,
  so the office can answer "why is the pay page refusing?".

Deliberate failure directions, asserted below: the page probe fails OPEN (a
Stripe outage renders the normal form — the mint gates are the hard stop), and
the mint gates also fail open (refusing to take money because we could not
check would strand a legitimate payer; the M12 sweep and the
`payment_exceeds_receivable` audit event still cover the overlap).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.core.payments import _ach_in_flight, _refuse_if_ach_processing
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine

TENANT = "tenant-m16"


def _pi(pid, status, invoice_id, amount=20000):
    return SimpleNamespace(
        id=pid, status=status, amount=amount, metadata={"invoice_id": str(invoice_id)}
    )


def _page(rows, has_more=False):
    return SimpleNamespace(data=rows, has_more=has_more)


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
        invoice_number="INV-M16", billing_type="standard",
        subtotal=Decimal("200.00"), tax_amount=Decimal("0"), total=Decimal("200.00"),
        balance_due=Decimal("200.00"), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        id=uuid.uuid4(), invoice_id=inv.id, description="Torsion spring",
        quantity=1, unit_price=Decimal("200.00"), line_total=Decimal("200.00"),
        company_id=TENANT,
    ))
    db.commit()
    db.refresh(inv)
    return inv


# ── the probe ──────────────────────────────────────────────────────────────


def test_a_processing_debit_is_found(invoice):
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_moving", "processing", invoice.id),
    ])):
        got = _ach_in_flight(invoice)
    assert got == {"intent_id": "pi_moving", "amount_cents": 20000}


@pytest.mark.parametrize("status", ["requires_payment_method", "succeeded", "canceled"])
def test_only_processing_counts(invoice, status):
    """An abandoned page, a settled debit, or a cancelled intent is not a
    payment in flight. Treating them as one would lock a payable invoice."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_x", status, invoice.id),
    ])):
        assert _ach_in_flight(invoice) is None


def test_someone_elses_debit_does_not_lock_this_invoice(invoice):
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_theirs", "processing", uuid.uuid4()),
    ])):
        assert _ach_in_flight(invoice) is None


def test_a_stripe_outage_reads_as_nothing_known(invoice):
    """Fail OPEN. Refusing to take money because we could not check would
    strand a legitimate payer for as long as the outage lasts."""
    with patch("stripe.PaymentIntent.list", side_effect=RuntimeError("down")):
        assert _ach_in_flight(invoice) is None


# ── the mint gates ─────────────────────────────────────────────────────────


def test_the_gate_refuses_while_a_debit_is_moving(invoice):
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_moving", "processing", invoice.id),
    ])), pytest.raises(HTTPException) as exc:
        _refuse_if_ach_processing(invoice, op="test")
    assert exc.value.status_code == 409
    assert "already processing" in exc.value.detail
    assert "don't need to pay again" in exc.value.detail


def test_the_gate_passes_a_clean_invoice(invoice):
    with patch("stripe.PaymentIntent.list", return_value=_page([])):
        _refuse_if_ach_processing(invoice, op="test")  # must not raise


def test_card_mint_is_gated_through_the_real_endpoint(invoice, db):
    """A card payment while the ACH debit is moving double-pays identically —
    the balance has not moved yet. Drive `create_intent` itself: no intent may
    be minted, and the customer gets told why."""
    from gdx_dispatch.core.payments import CreateIntentRequest, create_intent

    req = SimpleNamespace(state=SimpleNamespace(tenant={}))
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_moving", "processing", invoice.id),
    ])), patch("gdx_dispatch.core.payments._resolve_public_invoice", return_value=invoice), \
            patch("stripe.PaymentIntent.create") as mint, pytest.raises(HTTPException) as exc:
        create_intent(
            CreateIntentRequest(invoice_token=invoice.public_token),
            req,
            db=db,
        )
    mint.assert_not_called()
    assert exc.value.status_code == 409
    assert "already processing" in exc.value.detail


def test_ach_mint_is_gated_through_the_real_endpoint(invoice, db):
    """Drive `ach_charge` itself: resolve succeeds, the gate fires first."""
    from gdx_dispatch.core.payments import ACHChargeRequest, ach_charge

    req = SimpleNamespace(state=SimpleNamespace(tenant={}))
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_moving", "processing", invoice.id),
    ])), patch("gdx_dispatch.core.payments._resolve_public_invoice", return_value=invoice), \
            pytest.raises(HTTPException) as exc:
        ach_charge(
            ACHChargeRequest(
                invoice_token=invoice.public_token,
                setup_intent_id="seti_x",
                payment_method_id="pm_x",
                customer_email="c@example.com",
            ),
            req,
            db=db,
        )
    assert exc.value.status_code == 409
    assert "already processing" in exc.value.detail


def test_portal_mint_is_gated():
    """Presence, not behavior — the portal handler needs a full portal
    principal to drive. `_refuse_if_ach_processing`'s behavior is proven
    above; this pins that the portal actually calls it, and the deletion
    counterfactual bites on exactly that."""
    src = __import__("pathlib").Path(
        __import__("gdx_dispatch.routers.portal", fromlist=["__file__"]).__file__
    ).read_text()
    i = src.index("stripe.api_key = os.getenv")
    j = src.index("payment_intent_id", i)
    window = src[i:j]
    # Assert the CALL, not the bare name (the import line satisfied the name
    # with the call deleted), and assert it runs BEFORE the mint — a gate
    # after `PaymentIntent.create` guards nothing.
    gate = window.find('_refuse_if_ach_processing(invoice, op="portal-pay")')
    mint = window.find("stripe.PaymentIntent.create")
    assert gate != -1, "the portal mints against the same balance and must honor the gate"
    assert mint != -1, "portal mint site moved — retarget this test"
    assert gate < mint, "the gate runs AFTER the mint — it guards nothing"


# ── the pay page ───────────────────────────────────────────────────────────


def _render_pay_page(db, invoice, list_result):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.payments import public_router

    app = FastAPI()
    app.include_router(public_router)
    app.dependency_overrides[get_db] = lambda: db
    with patch("stripe.PaymentIntent.list", **list_result), \
            patch("gdx_dispatch.core.payments.record_customer_view"), TestClient(app) as client:
        return client.get(f"/pay/{invoice.public_token}")


def test_the_pay_page_says_processing_instead_of_collecting(db, invoice):
    r = _render_pay_page(db, invoice, {"return_value": _page([
        _pi("pi_moving", "processing", invoice.id),
    ])})
    assert r.status_code == 200
    assert "ach-processing-banner" in r.text
    assert "Bank transfer processing" in r.text
    assert "you don't need to pay again" in r.text
    # The live form must be gone — a banner above a working Pay button is a
    # double-charge with extra reading.
    assert 'id="card-form"' not in r.text
    assert 'id="ach-form"' not in r.text


def test_a_clean_invoice_still_gets_the_form(db, invoice):
    r = _render_pay_page(db, invoice, {"return_value": _page([])})
    assert r.status_code == 200
    assert 'id="card-form"' in r.text
    assert "ach-processing-banner" not in r.text


def test_a_stripe_outage_renders_the_normal_form(db, invoice):
    """The page probe is best-effort. A Stripe blip must not turn every pay
    link into a dead end — the mint gates are the hard stop."""
    r = _render_pay_page(db, invoice, {"side_effect": RuntimeError("down")})
    assert r.status_code == 200
    assert 'id="card-form"' in r.text


def test_a_paid_invoice_does_not_probe_stripe(db, invoice):
    """The paid page needs no Stripe round-trip — and must never show the
    processing banner over the 'already paid' message."""
    invoice.status = "paid"
    db.commit()
    with patch("gdx_dispatch.core.payments._ach_in_flight") as probe:
        r = _render_pay_page(db, invoice, {"return_value": _page([])})
    probe.assert_not_called()
    assert "already been paid" in r.text


# ── the audit trail ────────────────────────────────────────────────────────


def test_the_processing_event_lands_on_the_invoices_trail(db, invoice):
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.payments import handle_payment_webhook

    out = handle_payment_webhook(
        {
            "type": "payment_intent.processing",
            "data": {"object": {
                "id": "pi_moving",
                "amount": 20000,
                "payment_method_types": ["us_bank_account"],
                "metadata": {"invoice_id": str(invoice.id)},
            }},
        },
        db,
    )
    assert out["status"] == "ach_processing_noted"
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "ach_payment_processing")
        .first()
    )
    assert row is not None, "a customer starting a bank transfer left no trace"
    assert row.entity_id == str(invoice.id)
    assert row.details["intent_id"] == "pi_moving"


def test_a_card_intent_in_processing_writes_no_ach_event(db, invoice):
    """A card intent also transits `processing`. An "ach_payment_processing"
    row for it would be a trail entry that lies about the method."""
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.payments import handle_payment_webhook

    out = handle_payment_webhook(
        {
            "type": "payment_intent.processing",
            "data": {"object": {
                "id": "pi_card",
                "amount": 20000,
                "payment_method_types": ["card"],
                "metadata": {"invoice_id": str(invoice.id)},
            }},
        },
        db,
    )
    assert out["status"] == "not_ach"
    assert db.query(AuditLog).filter(AuditLog.action == "ach_payment_processing").count() == 0


def test_a_processing_event_without_an_invoice_is_ignored(db):
    from gdx_dispatch.core.payments import handle_payment_webhook

    out = handle_payment_webhook(
        {"type": "payment_intent.processing", "data": {"object": {"id": "pi_x", "metadata": {}}}},
        db,
    )
    assert out == {"status": "no_invoice_id"}
