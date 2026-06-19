from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base, Tenant
from gdx_dispatch.core.identity_repo import IdentityRepo
from gdx_dispatch.models.platform import Capability, CapabilitySet, Identity, IdentityProvider, Membership, PendingInvalidation


class _FakeCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.channels: list[tuple[str, str]] = []

    def get(self, key: str):
        return self.data.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.data[key] = value

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.data:
                del self.data[key]
                deleted += 1
        return deleted

    def publish(self, channel: str, message: str) -> int:
        self.channels.append((channel, message))
        return 1


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            Identity.__table__,
            IdentityProvider.__table__,
            CapabilitySet.__table__,
            Capability.__table__,
            Membership.__table__,
            PendingInvalidation.__table__,
        ],
        checkfirst=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# _seed_tenant_chain helper + tenant-tree tests removed (D97 031): tenants.parent_tenant_id
# is dropped, tree-traversal feature retired (zero active writers).


def test_identity_collision_provider_subject_match(db_session: Session) -> None:
    cache = _FakeCache()
    repo = IdentityRepo(db_session, cache)

    identity = Identity(email="owner@example.com")
    db_session.add(identity)
    db_session.flush()
    db_session.add(
        IdentityProvider(
            identity_id=identity.id,
            provider_type="authentik",
            provider_subject="sub-123",
            provider_email="owner@example.com",
            email_verified_by_provider=True,
        )
    )
    db_session.commit()

    resolved, decision = repo.match_for_login(
        provider_type="authentik",
        provider_subject="sub-123",
        provider_email="owner@example.com",
        is_authoritative_for_domain=True,
    )

    assert decision == "found"
    assert resolved is not None
    assert resolved.id == identity.id


def test_identity_collision_authoritative_email_match_strips_unverified_local(db_session: Session) -> None:
    cache = _FakeCache()
    repo = IdentityRepo(db_session, cache)

    identity = Identity(
        email="tech@example.com",
        email_verified_at=datetime.now(timezone.utc),
    )
    db_session.add(identity)
    db_session.flush()

    local = IdentityProvider(
        identity_id=identity.id,
        provider_type="local",
        provider_subject="legacy-local-id",
        provider_email="tech@example.com",
        email_verified_by_provider=False,
    )
    db_session.add(local)
    db_session.commit()

    resolved, decision = repo.match_for_login(
        provider_type="google",
        provider_subject="google-sub-1",
        provider_email="tech@example.com",
        is_authoritative_for_domain=True,
    )
    db_session.commit()

    db_session.refresh(local)
    assert decision == "strip_unverified"
    assert resolved is not None
    assert resolved.id == identity.id
    assert local.revoked_at is not None


def test_identity_collision_unauthoritative_no_match_returns_collision(db_session: Session) -> None:
    cache = _FakeCache()
    repo = IdentityRepo(db_session, cache)

    db_session.add(Identity(email="billing@example.com"))
    db_session.commit()

    resolved, decision = repo.match_for_login(
        provider_type="saml",
        provider_subject="saml-sub-7",
        provider_email="billing@example.com",
        is_authoritative_for_domain=False,
    )

    assert resolved is None
    assert decision == "collision"


def test_identity_collision_new_when_no_matches(db_session: Session) -> None:
    cache = _FakeCache()
    repo = IdentityRepo(db_session, cache)

    resolved, decision = repo.match_for_login(
        provider_type="saml",
        provider_subject="new-sub",
        provider_email="new@example.com",
        is_authoritative_for_domain=False,
    )
    assert resolved is None
    assert decision == "new"


def test_capability_lookup_cached(db_session: Session) -> None:
    cache = _FakeCache()
    repo = IdentityRepo(db_session, cache)

    capset = CapabilitySet(name="role:owner", scope_type="tenant")
    db_session.add(capset)
    db_session.flush()

    cap_active = Capability(capability_set_id=capset.id, action="read", resource_type="job")
    cap_revoked = Capability(
        capability_set_id=capset.id,
        action="write",
        resource_type="job",
        revoked_at=datetime.now(timezone.utc),
    )
    db_session.add_all([cap_active, cap_revoked])
    db_session.commit()

    first = repo.get_capabilities_for_capability_set(capset.id)
    second = repo.get_capabilities_for_capability_set(capset.id)

    assert [c.id for c in first] == [cap_active.id]
    assert [c.id for c in second] == [cap_active.id]
    assert f"capset:{capset.id}:capabilities" in cache.data


def test_cache_invalidation_on_membership_write(db_session: Session) -> None:
    tenant = Tenant(slug="gdx", name="GDX")
    db_session.add(tenant)
    db_session.flush()
    identity = Identity(email="ops@example.com")
    capset = CapabilitySet(name="role:viewer", scope_type="tenant")
    db_session.add_all([identity, capset])
    db_session.flush()
    membership = Membership(identity_id=identity.id, tenant_id=tenant.id, role="viewer", capability_set_id=capset.id)
    db_session.add(membership)
    db_session.commit()

    cache = _FakeCache()
    repo = IdentityRepo(db_session, cache)

    first = repo.get_memberships(identity.id)
    assert len(first) == 1

    membership.revoked_at = datetime.now(timezone.utc)
    db_session.commit()
    repo.invalidate_identity(identity.id)
    db_session.commit()

    second = repo.get_memberships(identity.id)
    assert second == []


def test_membership_allows_multiple_roles_per_identity_tenant(db_session: Session) -> None:
    tenant = Tenant(slug="tenant-x", name="Tenant X")
    db_session.add(tenant)
    db_session.flush()
    identity = Identity(email="multi@example.com")
    admin_set = CapabilitySet(name="role:admin", scope_type="tenant")
    tech_set = CapabilitySet(name="role:tech", scope_type="tenant")
    db_session.add_all([identity, admin_set, tech_set])
    db_session.flush()

    db_session.add_all(
        [
            Membership(identity_id=identity.id, tenant_id=tenant.id, role="admin", capability_set_id=admin_set.id),
            Membership(identity_id=identity.id, tenant_id=tenant.id, role="tech", capability_set_id=tech_set.id),
        ]
    )
    db_session.commit()

    memberships = IdentityRepo(db_session, _FakeCache()).get_memberships(identity.id)
    assert {m.role for m in memberships} == {"admin", "tech"}


def test_capability_set_unique_per_name_scope(db_session: Session) -> None:
    db_session.add(CapabilitySet(name="role:owner", scope_type="tenant"))
    db_session.commit()
    db_session.add(CapabilitySet(name="role:owner", scope_type="tenant"))
    with pytest.raises(IntegrityError):
        db_session.commit()
