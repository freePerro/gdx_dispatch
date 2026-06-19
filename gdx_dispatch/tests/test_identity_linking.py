"""SS-31 slice D — reconcile_federated_identity tests."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.platform  # noqa: F401
import gdx_dispatch.models.platform_extensions  # noqa: F401
from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.core.federation import identity_linking as il
from gdx_dispatch.models.platform import Identity, IdentityProvider


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def test_new_external_user_creates_identity_and_link(db):
    events = []

    def emit(name, payload):
        events.append((name, payload))

    result = il.reconcile_federated_identity(
        db,
        provider_id="okta-1",
        external_subject="abc-123",
        profile={"email": "ada@example.com", "email_verified": True, "name": "Ada"},
        emit_event=emit,
    )
    assert result.outcome == il.Outcome.CREATED
    ident = db.query(Identity).one()
    assert ident.email == "ada@example.com"
    link = db.query(IdentityProvider).one()
    assert link.provider_type == "fed:okta-1"
    assert link.provider_subject == "abc-123"
    assert link.email_verified_by_provider is True
    assert events[0][0] == "gdx_dispatch.federation.identity_linked.v1"


def test_existing_linked_updates_not_duplicates(db):
    il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="abc",
        profile={"email": "ada@example.com", "email_verified": True, "name": "Ada"},
    )
    result = il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="abc",
        profile={"email": "ada@example.com", "email_verified": True, "name": "Ada Lovelace"},
    )
    assert result.outcome == il.Outcome.UPDATED
    assert db.query(Identity).count() == 1
    assert db.query(IdentityProvider).count() == 1
    link = db.query(IdentityProvider).one()
    assert link.provider_metadata["last_profile"]["name"] == "Ada Lovelace"


def test_email_collision_does_not_auto_merge(db):
    # Pre-seed an unrelated Identity with the same email (e.g. local user).
    from uuid import uuid4

    existing = Identity(id=uuid4(), email="ada@example.com", status="active")
    db.add(existing)
    db.flush()

    events = []

    def emit(name, payload):
        events.append((name, payload))

    with pytest.raises(il.IdentityCollisionError) as ei:
        il.reconcile_federated_identity(
            db,
            provider_id="okta-1",
            external_subject="new-sub",
            profile={"email": "ada@example.com", "email_verified": True},
            emit_event=emit,
        )
    assert ei.value.existing_identity_id == str(existing.id)
    # only the collision event; no link created
    assert events[0][0] == "gdx_dispatch.federation.identity_collision.v1"
    assert db.query(IdentityProvider).count() == 0


def test_orphan_revokes_existing_link(db):
    r = il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="abc",
        profile={"email": "a@b.co", "email_verified": True},
    )
    link = db.query(IdentityProvider).one()
    assert link.revoked_at is None

    r2 = il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="abc", profile={}, orphan=True
    )
    assert r2.outcome == il.Outcome.ORPHANED
    db.refresh(link)
    assert link.revoked_at is not None
    # Identity row itself retained — admin decides.
    assert db.query(Identity).count() == 1


def test_orphan_noop_when_no_prior_link(db):
    r = il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="never-seen",
        profile={}, orphan=True,
    )
    assert r.outcome == il.Outcome.ORPHANED
    assert r.identity_id == ""


def test_relinking_after_orphan_clears_revoke(db):
    il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="abc",
        profile={"email": "a@b.co", "email_verified": True},
    )
    il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="abc", profile={}, orphan=True,
    )
    il.reconcile_federated_identity(
        db, provider_id="okta-1", external_subject="abc",
        profile={"email": "a@b.co", "email_verified": True},
    )
    link = db.query(IdentityProvider).one()
    assert link.revoked_at is None


def test_missing_required_args_raises(db):
    with pytest.raises(ValueError):
        il.reconcile_federated_identity(db, provider_id="", external_subject="x", profile={})
    with pytest.raises(ValueError):
        il.reconcile_federated_identity(db, provider_id="p", external_subject="", profile={})


def test_different_providers_same_subject_are_isolated(db):
    # provider_type partitioning means ("fed:a", "sub") and
    # ("fed:b", "sub") are two separate rows.
    il.reconcile_federated_identity(
        db, provider_id="a", external_subject="sub",
        profile={"email": "x1@example.com", "email_verified": True},
    )
    il.reconcile_federated_identity(
        db, provider_id="b", external_subject="sub",
        profile={"email": "x2@example.com", "email_verified": True},
    )
    assert db.query(IdentityProvider).count() == 2
