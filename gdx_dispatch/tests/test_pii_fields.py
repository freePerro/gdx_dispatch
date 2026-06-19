"""SS-35 slice B tests — declarative field registration."""
from __future__ import annotations

import pytest

from gdx_dispatch.core import pii_fields, pii_registry


@pytest.fixture(autouse=True)
def _clear():
    pii_registry.clear_registry()
    yield
    pii_registry.clear_registry()


def test_register_all_populates_expected_tables():
    pii_fields.register_all()
    tables = {f.table for f in pii_registry.list_pii_fields()}
    # Core tables must appear — missing any is an incomplete SAR.
    required = {
        "identities", "memberships", "addresses", "payment_methods",
        "device_bindings", "user_preferences", "activity_log",
    }
    assert required.issubset(tables), f"missing tables: {required - tables}"


def test_all_categories_valid():
    pii_fields.register_all()
    for rec in pii_registry.list_pii_fields():
        assert rec.pii_category in pii_registry.VALID_CATEGORIES


def test_identities_email_registered_as_contact():
    pii_fields.register_all()
    ids = [f for f in pii_registry.list_pii_fields(table="identities")
           if f.column == "email"]
    assert len(ids) == 1
    assert ids[0].pii_category == "contact"


def test_financial_fields_use_skip_strategy():
    """Ledger immutability — GDPR Art. 17(3)(e)."""
    pii_fields.register_all()
    fin = [f for f in pii_registry.list_pii_fields(table="payment_methods")]
    assert fin, "expected payment_methods entries"
    for rec in fin:
        assert rec.scrub_strategy == "skip", rec


def test_register_all_idempotent():
    pii_fields.register_all()
    n1 = len(pii_registry.list_pii_fields())
    pii_fields.register_all()
    n2 = len(pii_registry.list_pii_fields())
    assert n1 == n2


def test_addresses_use_identity_fk():
    pii_fields.register_all()
    for rec in pii_registry.list_pii_fields(table="addresses"):
        assert rec.identity_fk_column == "identity_id"


def test_activity_log_uses_actor_identity_fk():
    pii_fields.register_all()
    recs = pii_registry.list_pii_fields(table="activity_log")
    assert recs
    for rec in recs:
        assert rec.identity_fk_column == "actor_identity_id"
