"""SS-35 slice A tests — pii_registry core primitives."""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, Table, MetaData, create_engine, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import pii_registry as R


@pytest.fixture(autouse=True)
def _clear():
    R.clear_registry()
    yield
    R.clear_registry()


def test_register_and_list():
    R.register_pii_field("identities", "email", "contact")
    R.register_pii_field("identities", "phone", "contact")
    R.register_pii_field("addresses", "street1", "location",
                         identity_fk_column="identity_id")
    fields = R.list_pii_fields()
    assert len(fields) == 3
    assert fields[0].table == "addresses"
    assert {f.column for f in fields} == {"email", "phone", "street1"}


def test_register_filter_by_table():
    R.register_pii_field("identities", "email", "contact")
    R.register_pii_field("addresses", "street1", "location",
                         identity_fk_column="identity_id")
    ids = R.list_pii_fields(table="identities")
    assert len(ids) == 1
    assert ids[0].column == "email"


def test_register_bad_category_raises():
    with pytest.raises(ValueError):
        R.register_pii_field("identities", "email", "bogus-category")


def test_register_bad_scrub_strategy_raises():
    with pytest.raises(ValueError):
        R.register_pii_field(
            "identities", "email", "contact", scrub_strategy="zap"
        )


def test_duplicate_registration_overwrites():
    R.register_pii_field("identities", "email", "contact", retention_days=30)
    R.register_pii_field("identities", "email", "contact", retention_days=90)
    recs = R.list_pii_fields(table="identities")
    assert len(recs) == 1
    assert recs[0].retention_days == 90


# ───────────────────────── get_pii_for_identity ─────────────────────────


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    md = MetaData()
    identities = Table(
        "identities",
        md,
        Column("id", String, primary_key=True),
        Column("email", String),
        Column("phone", String),
    )
    addresses = Table(
        "addresses",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("identity_id", String),
        Column("street1", String),
        Column("city", String),
    )
    md.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    s.execute(insert(identities).values(
        id="i-1", email="a@x.com", phone="555-0100"
    ))
    s.execute(insert(identities).values(
        id="i-2", email="b@x.com", phone="555-0200"
    ))
    s.execute(insert(addresses).values(
        identity_id="i-1", street1="1 First St", city="Denver"
    ))
    s.execute(insert(addresses).values(
        identity_id="i-1", street1="2 Second Ave", city="Aurora"
    ))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_get_pii_for_identity_walks_registry(db):
    R.register_pii_field("identities", "email", "contact")
    R.register_pii_field("identities", "phone", "contact")
    R.register_pii_field("addresses", "street1", "location",
                         identity_fk_column="identity_id")
    R.register_pii_field("addresses", "city", "location",
                         identity_fk_column="identity_id")

    records = R.get_pii_for_identity(db, "i-1")
    # 2 from identities + 2 columns × 2 address rows = 6
    assert len(records) == 6
    cols = sorted({(r["table"], r["column"]) for r in records})
    assert ("addresses", "street1") in cols
    assert ("identities", "email") in cols
    # Only i-1 values returned
    emails = [r["value"] for r in records
              if r["table"] == "identities" and r["column"] == "email"]
    assert emails == ["a@x.com"]


def test_get_pii_for_identity_empty_when_no_rows(db):
    R.register_pii_field("identities", "email", "contact")
    records = R.get_pii_for_identity(db, "i-nonexistent")
    assert records == []


def test_missing_table_returns_empty(db):
    R.register_pii_field("nonexistent_table", "foo", "contact",
                         identity_fk_column="identity_id")
    records = R.get_pii_for_identity(db, "i-1")
    assert records == []


def test_soft_deleted_rows_excluded_from_sar(db):
    """0.9-s A1: tables with ``deleted_at`` hide logically-deleted rows."""
    from sqlalchemy import Column, Integer, String, Table, MetaData, DateTime, insert
    from datetime import datetime, timezone

    md = MetaData()
    soft_table = Table(
        "soft_pii_tbl",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("identity_id", String),
        Column("ssn", String),
        Column("deleted_at", DateTime),
    )
    md.create_all(db.get_bind())
    db.execute(insert(soft_table).values(
        identity_id="i-1", ssn="111-11-1111", deleted_at=None,
    ))
    db.execute(insert(soft_table).values(
        identity_id="i-1", ssn="999-99-9999",
        deleted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    db.commit()

    R.register_pii_field(
        "soft_pii_tbl", "ssn", "identity",
        identity_fk_column="identity_id",
    )
    records = R.get_pii_for_identity(db, "i-1")
    ssns = sorted(r["value"] for r in records if r["column"] == "ssn")
    assert ssns == ["111-11-1111"], (
        f"soft-deleted ssn leaked into SAR: {ssns}"
    )


def test_per_table_row_cap_truncates_and_warns(db, caplog):
    """0.9-s A8: per-table limit bounds SAR memory."""
    import logging

    R.register_pii_field("addresses", "street1", "location",
                         identity_fk_column="identity_id")
    # Fixture seeded 2 rows on addresses for i-1. Cap at 1 should truncate.
    caplog.set_level(logging.WARNING, logger="gdx_dispatch.core.pii_registry")
    records = R.get_pii_for_identity(db, "i-1", per_table_limit=1)
    assert len(records) == 1
    assert any("per_table_limit" in m for m in caplog.messages), (
        f"expected truncation warning, got: {caplog.messages}"
    )


def test_inconsistent_fk_column_raises(db):
    # Same table, two different fk columns → ambiguous.
    R.register_pii_field("addresses", "street1", "location",
                         identity_fk_column="identity_id")
    R.register_pii_field("addresses", "city", "location",
                         identity_fk_column="owner_id")
    with pytest.raises(ValueError, match="inconsistent"):
        R.get_pii_for_identity(db, "i-1")


def test_validate_sql_identifier_accepts_valid():
    """Plain ASCII identifiers pass through."""
    assert R._validate_sql_identifier("users", "table") == "users"
    assert R._validate_sql_identifier("identity_id", "fk_column") == "identity_id"
    assert R._validate_sql_identifier("Column_42", "column") == "Column_42"


def test_validate_sql_identifier_rejects_injection():
    """Anything resembling SQL injection is rejected."""
    import pytest
    bad_inputs = [
        "users; DROP TABLE secrets--",
        "users'--",
        "users UNION SELECT *",
        "1users",         # leading digit
        "",               # empty
        "users name",     # space
        "users-name",     # dash
        "users.name",     # dot
        "a" * 64,         # too long
        "'; DELETE FROM users; --",
    ]
    for bad in bad_inputs:
        with pytest.raises(ValueError, match="refusing to interpolate"):
            R._validate_sql_identifier(bad, "table")


def test_fetch_column_rows_rejects_nonidentifier_table():
    """_fetch_column_rows refuses a malicious table name before touching the db."""
    import pytest
    class FakeDb:
        def execute(self, *args, **kwargs):
            raise AssertionError("db.execute must NOT be called for bad identifier")
    with pytest.raises(ValueError):
        R._fetch_column_rows(FakeDb(), "users; DROP TABLE x", "identity_id", "abc", ["email"])
