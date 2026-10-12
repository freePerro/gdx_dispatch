"""The micro-deposit lock has an in-app release (GDXA-792).

A customer who typed their account number leaves the intent in
`requires_action` / `verify_with_microdeposits`, and `_refuse_if_ach_processing`
then refuses every new card or bank payment on the invoice. Stripe lifts that
only after its 10-day verification timeout
(<https://docs.stripe.com/payments/ach-direct-debit/accept-a-payment?payment-ui=direct-api>,
read 2026-10-12); before this fix nothing in the app could end it sooner.

Two releases, one core (`release_verifying_intents`): the customer's "Pay a
different way instead" on the pay page, and the office's "Cancel bank
verification". Each test runs a verifying intent through the REAL endpoint
against a stateful Stripe stand-in (a cancel changes what the next read sees)
and reads the committed audit row back, then proves the lock is gone by
driving `create_intent` and watching it mint.

`processing` is the sibling state: legitimately unreleasable (the debit is
authorized and moving), so it is refused with a message that says so.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.core.payments import (
    PROCESSING_UNRELEASABLE_DETAIL,
    VERIFICATION_RELEASED_ACTION,
    CreateIntentRequest,
    create_intent,
)
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine

TENANT = "tenant-gdxa-792"

_MICRODEPOSIT_WAIT = {
    "type": "verify_with_microdeposits",
    "verify_with_microdeposits": {"hosted_verification_url": "https://payments.stripe.com/verify/x"},
}
_CANCELLABLE = {"requires_payment_method", "requires_confirmation", "requires_action", "requires_capture"}


class FakeStripe:
    """Stripe's register for one test: list, retrieve and cancel agree."""

    def __init__(self, *intents):
        self.intents = {pi.id: pi for pi in intents}
        self.cancels: list[tuple[str, str | None]] = []
        self.minted: list[dict] = []
        # pid -> status the intent moves to on its NEXT retrieve (a race).
        self.move_on_retrieve: dict[str, str] = {}
        self.refuse_cancel = False

    def list(self, **_kw):
        return SimpleNamespace(data=list(self.intents.values()), has_more=False)

    def retrieve(self, pid, **_kw):
        pi = self.intents[pid]
        if pid in self.move_on_retrieve:
            pi.status = self.move_on_retrieve.pop(pid)
            pi.next_action = None
        return pi

    def cancel(self, pid, cancellation_reason=None, **_kw):
        pi = self.intents[pid]
        if self.refuse_cancel or pi.status not in _CANCELLABLE:
            raise stripe.InvalidRequestError(
                f"This PaymentIntent's status is {pi.status}; it cannot be canceled.", None,
            )
        self.cancels.append((pid, cancellation_reason))
        pi.status = "canceled"
        pi.next_action = None
        return pi

    def create(self, **kw):
        pid = f"pi_new_{len(self.minted)}"
        self.minted.append(kw)
        pi = SimpleNamespace(
            id=pid, status="requires_payment_method", amount=kw["amount"],
            client_secret=f"{pid}_secret", metadata=kw.get("metadata") or {},
            next_action=None, payment_method=None, amount_details=None,
            payment_method_types=kw.get("payment_method_types") or ["card"], created=0,
        )
        self.intents[pid] = pi
        return pi

    def patches(self):
        return (
            patch("stripe.PaymentIntent.list", side_effect=self.list),
            patch("stripe.PaymentIntent.retrieve", side_effect=self.retrieve),
            patch("stripe.PaymentIntent.cancel", side_effect=self.cancel),
            patch("stripe.PaymentIntent.create", side_effect=self.create),
        )


def _pi(pid, status, invoice_id, *, next_action=None, amount=20000):
    return SimpleNamespace(
        id=pid, status=status, amount=amount, metadata={"invoice_id": str(invoice_id)},
        next_action=next_action, payment_method="pm_bank", payment_method_types=["us_bank_account"],
        created=1,
    )


def _verifying(invoice_id, pid="pi_wait"):
    return _pi(pid, "requires_action", invoice_id, next_action=dict(_MICRODEPOSIT_WAIT))


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
        invoice_number="INV-792", billing_type="standard",
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


def _client(db):
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.payments import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _release_rows(db):
    db.rollback()  # only what was COMMITTED counts
    return db.scalars(select(AuditLog).where(AuditLog.action == VERIFICATION_RELEASED_ACTION)).all()


def _mint_card(db, invoice):
    req = SimpleNamespace(state=SimpleNamespace(tenant={}))
    with patch("gdx_dispatch.core.payments._resolve_public_invoice", return_value=invoice):
        return create_intent(CreateIntentRequest(invoice_token=invoice.public_token), req, db=db)


# ── the customer release ───────────────────────────────────────────────────


def test_the_customer_releases_the_wait_and_can_pay_by_card(db, invoice):
    fake = FakeStripe(_verifying(invoice.id))
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        # Locked first: the gate refuses a card while the deposit is unconfirmed.
        with pytest.raises(HTTPException) as locked:
            _mint_card(db, invoice)
        assert locked.value.status_code == 409
        assert fake.minted == []

        r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
        assert r.status_code == 200, r.text
        assert r.json() == {"released": ["pi_wait"]}
        assert fake.cancels == [("pi_wait", "abandoned")]

        # Released: the same card press now mints.
        out = _mint_card(db, invoice)
    assert len(fake.minted) == 1
    assert out["amount"] == 20000

    rows = _release_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == "stripe-pay-page"
    # Unauthenticated: the address is what tells the customer from a forwarded link.
    assert row.ip_address, "the pay-page release must record the caller's address"
    assert row.entity_type == "invoice"
    assert row.entity_id in (str(invoice.id), invoice.id.hex)
    details = row.details if isinstance(row.details, dict) else __import__("json").loads(row.details)
    assert details["intent_id"] == "pi_wait"
    assert details["by"] == "customer"
    assert details["status"] == "canceled"


def test_releasing_twice_is_harmless(db, invoice):
    fake = FakeStripe(_verifying(invoice.id))
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        client = _client(db)
        first = client.post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
        second = client.post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
    assert first.json() == {"released": ["pi_wait"]}
    assert second.status_code == 200
    assert second.json() == {"released": []}
    assert len(fake.cancels) == 1
    assert len(_release_rows(db)) == 1


def test_an_unknown_token_is_a_plain_404(db, invoice):
    fake = FakeStripe(_verifying(invoice.id))
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": "nope" * 8})
    assert r.status_code == 404
    assert r.json()["detail"] == "Invoice not found"
    assert fake.cancels == []


def test_the_legacy_invoice_id_shape_is_not_accepted(db, invoice):
    """Token only: an invoice id is enumerable, the token is the credential."""
    r = _client(db).post("/api/payments/release-verifying", json={"invoice_id": str(invoice.id)})
    assert r.status_code == 422


# ── the office release ─────────────────────────────────────────────────────


def _office(db, invoice, user):
    from gdx_dispatch.routers.invoices import cancel_bank_verification

    req = SimpleNamespace(state=SimpleNamespace(tenant={}))
    return cancel_bank_verification(invoice.id, req, user=user, db=db)


def test_the_office_releases_the_wait_as_the_acting_user(db, invoice):
    fake = FakeStripe(_verifying(invoice.id))
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        out = _office(db, invoice, {"sub": "office-user-7"})
        assert out == {"released": ["pi_wait"]}
        # The bank rail opens again too.
        req = SimpleNamespace(state=SimpleNamespace(tenant={}))
        with patch("gdx_dispatch.core.payments._resolve_public_invoice", return_value=invoice):
            create_intent(CreateIntentRequest(invoice_token=invoice.public_token, method="ach"), req, db=db)
    assert fake.cancels == [("pi_wait", "abandoned")]
    assert len(fake.minted) == 1
    assert fake.minted[0]["payment_method_types"] == ["us_bank_account"]

    rows = _release_rows(db)
    assert [r.user_id for r in rows] == ["office-user-7"]
    details = rows[0].details if isinstance(rows[0].details, dict) else __import__("json").loads(rows[0].details)
    assert details["by"] == "office"


def test_the_office_release_refuses_an_anonymous_actor(db, invoice):
    fake = FakeStripe(_verifying(invoice.id))
    p = fake.patches()
    with p[0], p[1], p[2], p[3], pytest.raises(HTTPException) as exc:
        _office(db, invoice, {})
    assert exc.value.status_code == 401
    assert fake.cancels == []


# ── what it must NOT release ───────────────────────────────────────────────


@pytest.mark.parametrize("who", ["customer", "office"])
def test_a_processing_debit_is_refused_with_a_reason(db, invoice, who):
    """The sibling state. The debit is authorized and moving; its settle or
    fail is the release, so both doors refuse and say why."""
    fake = FakeStripe(_pi("pi_moving", "processing", invoice.id))
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        if who == "customer":
            r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
            status, detail = r.status_code, r.json()["detail"]
        else:
            with pytest.raises(HTTPException) as exc:
                _office(db, invoice, {"sub": "office-user-7"})
            status, detail = exc.value.status_code, exc.value.detail
    assert status == 409
    assert detail == PROCESSING_UNRELEASABLE_DETAIL
    assert fake.cancels == []
    assert _release_rows(db) == []


def test_a_deposit_confirmed_meanwhile_is_not_cancelled(db, invoice):
    """The register said `verifying`; by the re-read the customer has
    confirmed the deposit and the debit is moving. Leave it alone."""
    fake = FakeStripe(_verifying(invoice.id))
    fake.move_on_retrieve["pi_wait"] = "processing"
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
    assert r.status_code == 409
    assert r.json()["detail"] == PROCESSING_UNRELEASABLE_DETAIL
    assert fake.cancels == []
    assert _release_rows(db) == []


def test_a_cancel_refused_after_the_deposit_was_confirmed_says_processing(db, invoice):
    fake = FakeStripe(_verifying(invoice.id))
    fake.refuse_cancel = True
    p = fake.patches()

    def retrieve(pid, **kw):
        # First read (the re-check) still waits; the read after the refused
        # cancel finds the debit moving.
        pi = FakeStripe.retrieve(fake, pid, **kw)
        fake.move_on_retrieve[pid] = "processing"
        return pi

    with p[0], patch("stripe.PaymentIntent.retrieve", side_effect=retrieve), p[2], p[3]:
        r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
    assert r.status_code == 409
    assert r.json()["detail"] == PROCESSING_UNRELEASABLE_DETAIL
    assert _release_rows(db) == []


def test_a_card_3ds_wait_is_not_a_bank_verification(db, invoice):
    """`requires_action` is also a card inside 3D Secure. Not ours to cancel."""
    card = _pi("pi_3ds", "requires_action", invoice.id, next_action={"type": "use_stripe_sdk"})
    fake = FakeStripe(card)
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
    assert r.json() == {"released": []}
    assert fake.cancels == []


def test_someone_elses_wait_is_untouched(db, invoice):
    fake = FakeStripe(_verifying(uuid.uuid4(), pid="pi_other"))
    p = fake.patches()
    with p[0], p[1], p[2], p[3]:
        r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
    assert r.json() == {"released": []}
    assert fake.cancels == []


def test_a_stripe_outage_is_not_reported_as_nothing_to_release(db, invoice):
    """Fail closed: "released: []" would send the customer back to a page that
    still refuses them."""
    with patch("stripe.PaymentIntent.list", side_effect=RuntimeError("down")):
        r = _client(db).post("/api/payments/release-verifying", json={"invoice_token": invoice.public_token})
    assert r.status_code == 502


# ── the pay page shows the way out ─────────────────────────────────────────


def _render(db, invoice, rows):
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.payments import public_router

    app = FastAPI()
    app.include_router(public_router)
    app.dependency_overrides[get_db] = lambda: db
    with patch("stripe.PaymentIntent.list", return_value=SimpleNamespace(data=rows, has_more=False)), \
            patch("gdx_dispatch.core.payments.record_customer_view"), TestClient(app) as client:
        return client.get(f"/pay/{invoice.public_token}")


def test_the_verifying_page_offers_pay_a_different_way(db, invoice):
    r = _render(db, invoice, [_verifying(invoice.id)])
    assert r.status_code == 200
    assert 'data-testid="ach-release"' in r.text
    assert "/api/payments/release-verifying" in r.text


def test_the_processing_page_offers_no_release(db, invoice):
    r = _render(db, invoice, [_pi("pi_moving", "processing", invoice.id)])
    assert r.status_code == 200
    assert 'data-testid="ach-release"' not in r.text
    assert "/api/payments/release-verifying" not in r.text
