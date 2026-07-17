"""A tech fixing a customer's contact details must not break finding them.

Two contracts the mobile contact endpoints stand on:

1. **Writes go through the ORM so the *_hash sidecars stay true.** Customer's
   name/email/phone are @validates-hooked to recompute name_hash/email_hash/
   phone_hash. Those hashes are not decoration: tasks/email_poller.py finds a
   customer by email_hash and modules/phone_com/customer_resolver.py matches an
   inbound call by phone_hash (E.164-normalized). A raw UPDATE would store the
   value, skip the hash, and that customer's own replies and calls would stop
   matching them — silently, forever, with the data looking perfectly fine.

2. **A field tech gets contact details, never money.** customers.write also
   carries pricing_class, margin_override_pct and payment_terms_days, so the
   technician role holds the narrower customers.contact_write instead and the
   endpoints accept only name/phone/email.
"""
from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.pii import HashColumn
from gdx_dispatch.models.tenant_models import Customer


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Customer.__table__.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _customer(db: Session, **kw) -> Customer:
    c = Customer(id=uuid.uuid4(), company_id="tenant-a", name=kw.pop("name", "Acme"), **kw)
    db.add(c)
    db.commit()
    return c


# ── Contract 1: the hash follows the value ──────────────────────────────────


def test_adding_an_email_makes_the_customer_findable_by_the_email_poller(db: Session) -> None:
    """219 of 382 customers here have no email. When a tech finally gets one,
    the poller has to be able to match the reply that comes back."""
    cust = _customer(db, email=None)
    assert cust.email_hash is None

    cust.email = "paul@example.com"
    db.commit()

    # Exactly what tasks/email_poller.py computes for an inbound sender.
    assert cust.email_hash == HashColumn.hash_for_search("paul@example.com")


def test_fixing_a_phone_makes_the_customer_findable_by_an_inbound_call(db: Session) -> None:
    """phone_hash is keyed on the E.164 form so a call from +1XXXXXXXXXX matches
    a row stored as "(XXX) XXX-XXXX". Both sides must normalize identically or
    the caller shows up as a stranger."""
    from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164

    cust = _customer(db, phone=None)

    cust.phone = "(218) 555-0134"
    db.commit()

    expected = HashColumn.hash_for_search(normalize_e164("(218) 555-0134") or "(218) 555-0134")
    assert cust.phone_hash == expected
    # And the number as typed is what a human reads back.
    assert cust.phone == "(218) 555-0134"


def test_correcting_an_email_moves_the_hash_with_it(db: Session) -> None:
    """A typo'd address that gets fixed must stop matching the old one — a stale
    hash silently routes the customer's mail to the wrong record."""
    cust = _customer(db, email="typo@example.com")
    old = cust.email_hash

    cust.email = "right@example.com"
    db.commit()

    assert cust.email_hash != old
    assert cust.email_hash == HashColumn.hash_for_search("right@example.com")


def test_clearing_an_email_clears_its_hash(db: Session) -> None:
    cust = _customer(db, email="paul@example.com")
    assert cust.email_hash is not None

    cust.email = None
    db.commit()

    assert cust.email_hash is None, "a hash outliving its value matches a ghost"


# ── Contract 2: the tech's grant is contact details, not money ──────────────


def test_technician_can_write_contact_details_but_not_customers(db: Session) -> None:
    from gdx_dispatch.core.permissions import BUILTIN_ROLES

    tech = BUILTIN_ROLES["technician"]
    assert "customers.contact_write" in tech
    assert "customers.write" not in tech, (
        "customers.write also carries pricing_class, margin_override_pct and "
        "payment_terms_days — a field tech must not hold it"
    )


def test_office_roles_keep_the_narrow_key_too(db: Session) -> None:
    """A gate requiring contact_write must not lock out the people who already
    had customers.write."""
    from gdx_dispatch.core.permissions import BUILTIN_ROLES

    for role in ("dispatcher", "sales", "admin"):
        assert "customers.contact_write" in BUILTIN_ROLES[role], role


def test_the_patch_schema_cannot_reach_a_money_field() -> None:
    """The gate is one half; the schema is the other. Even holding the key, the
    endpoint's body must not accept pricing/terms — an over-broad model is how a
    narrow permission quietly becomes a wide one."""
    from gdx_dispatch.routers.mobile import CustomerContactPatch

    fields = set(CustomerContactPatch.model_fields)
    assert fields == {"name", "phone", "email"}, fields
    for money in ("pricing_class", "margin_override_pct", "payment_terms_days", "customer_type"):
        assert money not in fields
