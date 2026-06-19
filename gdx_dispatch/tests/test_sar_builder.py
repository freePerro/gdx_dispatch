"""SS-35 slice C tests — sar_builder."""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import pii_registry
from gdx_dispatch.core.sar_builder import build_sar_export, SCHEMA_VERSION


@pytest.fixture
def db():
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
        Column("legal_name", String),
    )
    addresses = Table(
        "addresses", md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("identity_id", String),
        Column("street1", String),
        Column("city", String),
    )
    md.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    s.execute(insert(identities).values(
        id="i-1", email="a@x.com", phone="555-0100", legal_name="Alice A"
    ))
    s.execute(insert(addresses).values(
        identity_id="i-1", street1="1 First St", city="Denver"
    ))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _registry_reset():
    pii_registry.clear_registry()
    yield
    pii_registry.clear_registry()


def _seed_basic_registry():
    pii_registry.register_pii_field("identities", "email", "contact")
    pii_registry.register_pii_field("identities", "phone", "contact")
    pii_registry.register_pii_field("identities", "legal_name", "identity")
    pii_registry.register_pii_field(
        "addresses", "street1", "location", identity_fk_column="identity_id"
    )
    pii_registry.register_pii_field(
        "addresses", "city", "location", identity_fk_column="identity_id"
    )


def test_build_export_envelope(db):
    _seed_basic_registry()
    out = build_sar_export(db, "i-1", privacy_policy_url="https://gdx/privacy")
    assert out["schema"] == SCHEMA_VERSION
    assert out["gdpr_basis"] == "GDPR Art. 15"
    assert out["identity_id"] == "i-1"
    assert out["privacy_policy_url"] == "https://gdx/privacy"
    assert isinstance(out["registry_fingerprint"], str)
    assert len(out["registry_fingerprint"]) == 16


def test_every_category_is_present_even_if_empty(db):
    _seed_basic_registry()
    out = build_sar_export(db, "i-1")
    from gdx_dispatch.core.pii_registry import VALID_CATEGORIES
    for cat in VALID_CATEGORIES:
        assert cat in out["categories"], f"missing category {cat}"


def test_populated_categories_contain_values(db):
    _seed_basic_registry()
    out = build_sar_export(db, "i-1")
    emails = [r for r in out["categories"]["contact"]
              if r["column"] == "email"]
    assert len(emails) == 1
    assert emails[0]["value"] == "a@x.com"
    assert emails[0]["provenance"]["source_table"] == "identities"
    assert emails[0]["provenance"]["pii_category"] == "contact"


def test_empty_when_identity_missing(db):
    _seed_basic_registry()
    out = build_sar_export(db, "i-does-not-exist")
    assert out["field_count"] == 0
    for cat, rows in out["categories"].items():
        assert rows == []


def test_every_field_has_provenance(db):
    _seed_basic_registry()
    out = build_sar_export(db, "i-1")
    for cat, rows in out["categories"].items():
        for row in rows:
            assert "provenance" in row
            p = row["provenance"]
            assert p["source_table"] == row["table"]
            assert p["source_column"] == row["column"]
            assert p["pii_category"] == cat


def test_fingerprint_changes_when_registry_changes(db):
    _seed_basic_registry()
    fp1 = build_sar_export(db, "i-1")["registry_fingerprint"]
    pii_registry.register_pii_field("identities", "email", "contact",
                                     retention_days=99)
    fp2 = build_sar_export(db, "i-1")["registry_fingerprint"]
    assert fp1 != fp2
