"""SS-28 slice A tests — record_consumer_action helper.

Uses SQLite + the SS-28 stub Base to materialize the audit table in
memory. Covers:
* First row uses ZERO_HASH as prev_hash
* Second row's prev_hash matches first row's row_hash
* Cross-tenant chains are independent
* Mandatory fields raise ValueError when missing
* Tamper: mutating a committed row's action breaks verify_chain
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit_hash_chain import ZERO_HASH, verify_chain
from gdx_dispatch.core.platform_audit import record_consumer_action
from gdx_dispatch.models.platform_ss28_additions import (
    SS28Base,
    PlatformConsumerAudit,
)

T1 = "00000000-0000-0000-0000-000000000001"
TA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
P1 = "00000000-0000-0000-0000-000000000011"
PA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaab"
PB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb0"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    SS28Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_first_row_uses_zero_hash(db):
    row = record_consumer_action(
        db,
        tenant_id=T1,
        principal_identity_id=P1,
        action="api.call",
        resource_type="job",
        resource_id="j1",
        result="ok",
    )
    db.commit()
    assert row.prev_hash == ZERO_HASH
    assert len(row.row_hash) == 64


def test_second_row_chains_off_first(db):
    r1 = record_consumer_action(
        db, tenant_id=T1, principal_identity_id=P1,
        action="a1", resource_type="job", resource_id="j1", result="ok",
    )
    db.commit()
    r2 = record_consumer_action(
        db, tenant_id=T1, principal_identity_id=P1,
        action="a2", resource_type="job", resource_id="j2", result="ok",
    )
    db.commit()
    assert r2.prev_hash == r1.row_hash
    assert r2.row_hash != r1.row_hash


def test_cross_tenant_chains_independent(db):
    a1 = record_consumer_action(
        db, tenant_id=TA, principal_identity_id=PA,
        action="a", resource_type="x", resource_id="1", result="ok",
    )
    b1 = record_consumer_action(
        db, tenant_id=TB, principal_identity_id=PB,
        action="a", resource_type="x", resource_id="1", result="ok",
    )
    db.commit()
    # Both tenants should start from ZERO_HASH.
    assert a1.prev_hash == ZERO_HASH
    assert b1.prev_hash == ZERO_HASH


def test_missing_tenant_raises(db):
    with pytest.raises(ValueError):
        record_consumer_action(
            db, tenant_id="", principal_identity_id=P1,
            action="a", resource_type="x", resource_id="1", result="ok",
        )


def test_missing_action_raises(db):
    with pytest.raises(ValueError):
        record_consumer_action(
            db, tenant_id=T1, principal_identity_id=P1,
            action="", resource_type="x", resource_id="1", result="ok",
        )


def test_verify_chain_db_backed_intact(db):
    for i in range(5):
        record_consumer_action(
            db, tenant_id=T1, principal_identity_id=P1,
            action=f"a{i}", resource_type="x", resource_id=str(i), result="ok",
        )
    db.commit()
    valid, break_at = verify_chain(db, T1)
    assert valid is True
    assert break_at == -1


def test_verify_chain_db_backed_tamper(db):
    for i in range(4):
        record_consumer_action(
            db, tenant_id=T1, principal_identity_id=P1,
            action=f"a{i}", resource_type="x", resource_id=str(i), result="ok",
        )
    db.commit()
    # Mutate a committed row (not the last one) — chain must break AT that row.
    import uuid as _uuid
    t1_uuid = _uuid.UUID(T1)
    row = (
        db.query(PlatformConsumerAudit)
        .filter(PlatformConsumerAudit.tenant_id == t1_uuid)
        .order_by(PlatformConsumerAudit.created_at, PlatformConsumerAudit.id)
        .all()
    )[1]
    row.action = "a1-tampered"
    db.commit()
    valid, break_at = verify_chain(db, T1)
    assert valid is False
    assert break_at == 1


def test_details_json_roundtrip(db):
    row = record_consumer_action(
        db, tenant_id=T1, principal_identity_id=P1,
        action="a", resource_type="x", resource_id="1", result="ok",
        details={"foo": "bar", "nested": {"n": 1}},
        ip_address="10.0.0.1", user_agent="curl/8",
    )
    db.commit()
    assert row.details == {"foo": "bar", "nested": {"n": 1}}
    assert row.ip_address == "10.0.0.1"
    assert row.user_agent == "curl/8"
