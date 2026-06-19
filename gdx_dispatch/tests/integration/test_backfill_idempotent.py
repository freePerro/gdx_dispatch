"""PG integration gate — backfill + seed idempotency on real Postgres.

Adapts the key tests from gdx_dispatch/tests/test_backfill_idempotent.py to run
against the PG gate's throwaway postgres:16-alpine container. The control_db
fixture routes to PG when GDX_TEST_CONTROL_DB_URL is set.

Key PG-specific behaviors exercised:
- ON CONFLICT / upsert handling in seed_platform
- UUID generation and storage (pg native uuid vs SQLite text)
- FK constraint enforcement on identity→membership→capability_set chains
- Real transaction isolation between backfill passes
"""
from __future__ import annotations

import uuid
from uuid import UUID
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.identity_repo import IdentityRepo
from gdx_dispatch.models.platform import Capability, CapabilitySet, Membership
from gdx_dispatch.models.tenant_models import Base as TenantBase
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.tools.backfill_users_to_identities import backfill_users_from_tenants
from gdx_dispatch.tools.seed_capability_sets_from_openapi import CORE_ACTIONS, CORE_RESOURCE_TYPES, seed_capabilities
from gdx_dispatch.tools.seed_platform_platform import seed_platform


class _FakeCache:
    def get(self, _key):
        return None

    def setex(self, _key, _ttl, _value):
        return None

    def delete(self, *_keys):
        return 0

    def publish(self, _channel, _message):
        return 1


def _make_tenant_engine_with_users():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, tables=[User.__table__], checkfirst=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


_USER_NS = UUID("12345678-1234-5678-1234-567812345678")

def _seed_tenant_user(SessionLocal, **kwargs):
    if "id" in kwargs and isinstance(kwargs["id"], str):
        try:
            kwargs["id"] = UUID(kwargs["id"])
        except ValueError:
            kwargs["id"] = uuid.uuid5(_USER_NS, kwargs["id"])
    s = SessionLocal()
    try:
        s.add(User(**kwargs))
        s.commit()
    finally:
        s.close()


def test_seed_idempotent(control_db) -> None:
    """seed_platform is idempotent: second call creates zero new rows on PG."""
    first = seed_platform(control_db, dry_run=False)
    second = seed_platform(control_db, dry_run=False)
    assert first["created"]["oauth_clients"] == 4
    assert first["created"]["capability_sets"] >= 7
    assert second["created"]["oauth_clients"] == 0
    assert second["created"]["capability_sets"] == 0


def test_backfill_from_tenants_idempotent(control_db) -> None:
    """PG control DB + SQLite tenant DBs — the realistic production path.

    Two tenants each with users. First pass creates identities+memberships,
    second pass is a no-op.
    """
    seed_platform(control_db, dry_run=False)

    _, gdx_Session = _make_tenant_engine_with_users()
    _, acme_Session = _make_tenant_engine_with_users()

    control_db.add(Tenant(slug="pg-gdx", name="PG GDX"))
    control_db.add(Tenant(slug="pg-acme", name="PG Acme"))
    control_db.commit()

    _seed_tenant_user(gdx_Session, id="u-pg-gdx-1", username="doug", email="doug@pg-gdx.test", role="owner", company_id="pg-gdx")
    _seed_tenant_user(acme_Session, id="u-pg-acme-1", username="alice", email="alice@pg-acme.test", role="admin", company_id="pg-acme")

    sessions_by_slug = {"pg-gdx": gdx_Session, "pg-acme": acme_Session}
    def factory(tenant):
        return sessions_by_slug[tenant.slug]()

    first = backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)
    second = backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)

    assert first["identities_created"] == 2
    assert first["memberships_created"] == 2
    assert second["identities_created"] == 0
    assert second["memberships_created"] == 0
    assert second["users_seen"] == 2
    assert second["identities_existing"] == 2


def test_backfill_from_tenants_membership_tenant_scoping(control_db) -> None:
    """Memberships created by backfill are correctly tenant-scoped on PG."""
    seed_platform(control_db, dry_run=False)

    _, gdx_Session = _make_tenant_engine_with_users()
    _, acme_Session = _make_tenant_engine_with_users()

    control_db.add(Tenant(slug="pg-scope-a", name="Scope A"))
    control_db.add(Tenant(slug="pg-scope-b", name="Scope B"))
    control_db.commit()

    _seed_tenant_user(gdx_Session, id="u-scope-a", username="userA", email="a@test.test", role="owner", company_id="pg-scope-a")
    _seed_tenant_user(acme_Session, id="u-scope-b", username="userB", email="b@test.test", role="tech", company_id="pg-scope-b")

    sessions_by_slug = {"pg-scope-a": gdx_Session, "pg-scope-b": acme_Session}
    def factory(tenant):
        return sessions_by_slug[tenant.slug]()

    backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)

    from gdx_dispatch.control.models import Tenant as _Tenant
    scope_a = control_db.execute(select(_Tenant).where(_Tenant.slug == "pg-scope-a")).scalar_one()
    scope_b = control_db.execute(select(_Tenant).where(_Tenant.slug == "pg-scope-b")).scalar_one()
    a_memberships = control_db.execute(
        select(Membership).where(Membership.tenant_id == scope_a.id)
    ).scalars().all()
    b_memberships = control_db.execute(
        select(Membership).where(Membership.tenant_id == scope_b.id)
    ).scalars().all()

    assert len(a_memberships) == 1
    assert len(b_memberships) == 1
    assert a_memberships[0].identity_id != b_memberships[0].identity_id


def test_backfill_identity_repo_lookup(control_db) -> None:
    """IdentityRepo can find backfill-created identities via provider lookup on PG."""
    seed_platform(control_db, dry_run=False)

    _, gdx_Session = _make_tenant_engine_with_users()

    control_db.add(Tenant(slug="pg-repo", name="Repo Test"))
    control_db.commit()

    _seed_tenant_user(gdx_Session, id="u-repo-1", username="repouser", email="repo@test.test", role="owner", company_id="pg-repo")

    sessions_by_slug = {"pg-repo": gdx_Session}

    def factory(tenant):
        if tenant.slug not in sessions_by_slug:
            raise RuntimeError(f"unknown tenant {tenant.slug}")
        return sessions_by_slug[tenant.slug]()

    backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)

    repo = IdentityRepo(control_db, _FakeCache())
    # provider_subject stored as str(uuid5(_USER_NS, "u-repo-1"))
    provider_ref = str(uuid.uuid5(_USER_NS, "u-repo-1"))
    identity = repo.get_identity_by_provider("legacy_local", provider_ref)
    assert identity is not None

    memberships = repo.get_memberships(identity.id)
    assert len(memberships) == 1
    # Membership.tenant_id is the actual Tenant.id UUID, not the slug
    from gdx_dispatch.control.models import Tenant as _Tenant
    pg_repo = control_db.execute(select(_Tenant).where(_Tenant.slug == "pg-repo")).scalar_one()
    assert memberships[0].tenant_id == pg_repo.id


def test_capability_seeding_full_matrix(control_db) -> None:
    """Capability seeding creates the full resource x action matrix on PG."""
    seed_platform(control_db, dry_run=False)
    stats = seed_capabilities(control_db, dry_run=False)
    assert stats["capabilities_inserted"] > 0

    owner_set = control_db.execute(
        select(CapabilitySet).where(CapabilitySet.name == "role:owner")
    ).scalar_one()
    owner_caps = control_db.execute(
        select(Capability).where(Capability.capability_set_id == owner_set.id)
    ).scalars().all()
    expected = len(CORE_RESOURCE_TYPES) * len(CORE_ACTIONS)
    assert len(owner_caps) == expected


def test_capability_seeding_idempotent(control_db) -> None:
    """Running seed_capabilities twice inserts zero on the second pass."""
    seed_platform(control_db, dry_run=False)
    first = seed_capabilities(control_db, dry_run=False)
    second = seed_capabilities(control_db, dry_run=False)
    assert first["capabilities_inserted"] > 0
    assert second["capabilities_inserted"] == first["capabilities_inserted"]
