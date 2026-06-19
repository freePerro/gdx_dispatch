"""SS-35 slice E tests — erasure_executor."""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import pii_registry
from gdx_dispatch.core.erasure_executor import ERASED_LITERAL, execute_erasure


@pytest.fixture(autouse=True)
def _registry_reset():
    pii_registry.clear_registry()
    yield
    pii_registry.clear_registry()


@pytest.fixture
def db_and_tables():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    md = MetaData()
    identities = Table(
        "identities", md,
        Column("id", String, primary_key=True),
        Column("email", String),
        Column("phone", String),
    )
    addresses = Table(
        "addresses", md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("identity_id", String),
        Column("street1", String),
        Column("city", String),
    )
    payment_methods = Table(
        "payment_methods", md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("identity_id", String),
        Column("card_last4_masked", String),
    )
    md.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    s.execute(insert(identities).values(id="i-1", email="a@x.com", phone="555-0100"))
    s.execute(insert(addresses).values(
        identity_id="i-1", street1="1 First St", city="Denver"
    ))
    s.execute(insert(payment_methods).values(
        identity_id="i-1", card_last4_masked="****1234"
    ))
    s.commit()
    try:
        yield s, identities, addresses, payment_methods
    finally:
        s.close()
        engine.dispose()


def _seed():
    pii_registry.register_pii_field("identities", "email", "contact")
    pii_registry.register_pii_field("identities", "phone", "contact")
    pii_registry.register_pii_field(
        "addresses", "street1", "location", identity_fk_column="identity_id"
    )
    pii_registry.register_pii_field(
        "addresses", "city", "location", identity_fk_column="identity_id"
    )
    pii_registry.register_pii_field(
        "payment_methods", "card_last4_masked", "financial",
        identity_fk_column="identity_id", scrub_strategy="skip",
    )


def test_dry_run_returns_counts_no_values(db_and_tables):
    db, _, _, _ = db_and_tables
    _seed()
    out = execute_erasure(db, "i-1", dry_run=True)
    assert out["dry_run"] is True
    # contact: 2 (email, phone); location: 2 (street1, city); financial skipped
    assert out["by_category"]["contact"] == 2
    assert out["by_category"]["location"] == 2
    assert out["by_category"]["financial"] == 0
    assert out["affected_field_count"] == 4
    assert out["skipped_field_count"] == 1
    assert "executed_at" not in out
    # CRITICAL: must NOT contain any plaintext values.
    s = str(out)
    assert "a@x.com" not in s
    assert "1 First St" not in s


def test_real_run_scrubs_values(db_and_tables):
    db, identities, addresses, payment_methods = db_and_tables
    _seed()
    out = execute_erasure(db, "i-1", dry_run=False)
    assert out["dry_run"] is False
    assert "executed_at" in out

    # Verify identities row
    row = db.execute(select(identities).where(identities.c.id == "i-1")).one()
    assert row.email == ERASED_LITERAL
    assert row.phone == ERASED_LITERAL

    # Verify addresses row (strategy=null)
    addr = db.execute(
        select(addresses).where(addresses.c.identity_id == "i-1")
    ).one()
    assert addr.street1 is None
    assert addr.city is None

    # Financial must be untouched (skip)
    pm = db.execute(
        select(payment_methods).where(payment_methods.c.identity_id == "i-1")
    ).one()
    assert pm.card_last4_masked == "****1234"


def test_no_fields_registered_is_zero(db_and_tables):
    db, _, _, _ = db_and_tables
    out = execute_erasure(db, "i-1", dry_run=True)
    assert out["affected_field_count"] == 0
    for cat, count in out["by_category"].items():
        assert count == 0


def test_every_category_present_in_summary(db_and_tables):
    db, _, _, _ = db_and_tables
    _seed()
    out = execute_erasure(db, "i-1", dry_run=True)
    from gdx_dispatch.core.pii_registry import VALID_CATEGORIES
    for cat in VALID_CATEGORIES:
        assert cat in out["by_category"], f"missing category {cat}"


def test_skip_strategy_preserves_financial(db_and_tables):
    db, _, _, payment_methods = db_and_tables
    _seed()
    execute_erasure(db, "i-1", dry_run=False)
    pm = db.execute(
        select(payment_methods).where(payment_methods.c.identity_id == "i-1")
    ).one()
    assert pm.card_last4_masked == "****1234"
