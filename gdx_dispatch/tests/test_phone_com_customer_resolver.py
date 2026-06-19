"""pc-s6b — customer resolver: E.164 normalize + phone_hash blind-index lookup.

Customer.@validates("name", "email", "phone") at tenant_models.py:107 auto-
populates phone_hash on insert/update via HashColumn.hash_for_search. The
resolver MUST use the same hash function for lookups to succeed.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.models.tenant_models import Base, Customer
from gdx_dispatch.modules.phone_com.customer_resolver import (
    match_caller_id,
    match_phone_to_customer,
    normalize_e164,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()


def test_normalize_e164_us_10digit():
    assert normalize_e164("(320) 295-9628") == "+13202959628"


def test_normalize_e164_us_11digit():
    assert normalize_e164("1-320-295-9628") == "+13202959628"


def test_normalize_e164_already_e164():
    assert normalize_e164("+13202959628") == "+13202959628"


def test_normalize_e164_unparseable():
    assert normalize_e164("555-1234") is None
    assert normalize_e164("") is None
    assert normalize_e164(None) is None


def test_resolver_matches_validates_populated_hash(db_session):
    """@validates hashes phone on insert; resolver must find it."""
    cust = Customer(name="Test User", company_id="t1", phone="+13202959628")
    db_session.add(cust)
    db_session.commit()

    matched = match_phone_to_customer(db_session, "(320) 295-9628")
    assert matched is not None
    assert matched.id == cust.id


def test_match_unknown_returns_none(db_session):
    db_session.add(Customer(name="Existing", company_id="c1", phone="+15555555555"))
    db_session.commit()
    assert match_phone_to_customer(db_session, "+19999999999") is None


def test_match_caller_id_wrapper(db_session):
    cust = Customer(name="Caller", company_id="c1", phone="+13202959628")
    db_session.add(cust)
    db_session.commit()

    result_id = match_caller_id(db_session, "+13202959628")
    assert result_id == cust.id
    assert isinstance(result_id, UUID)
