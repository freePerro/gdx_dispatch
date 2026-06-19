from __future__ import annotations

import uuid
from uuid import UUID, uuid4

# Fixed UUIDs for deterministic test user IDs (User.id is Uuid(as_uuid=True))
_USER_NS = UUID("12345678-1234-5678-1234-567812345678")

def _make_user_uuid(legacy_id: str) -> UUID:
    """Deterministic UUID from a legacy string id via uuid5."""
    return uuid.uuid5(_USER_NS, legacy_id)

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import ServiceAccount, Tenant
from gdx_dispatch.core.identity_repo import IdentityRepo
from gdx_dispatch.models.platform import Capability, CapabilitySet, Identity, IdentityProvider, Membership
from gdx_dispatch.models.platform_extensions import (
    AccessToken,
    BillingAccount,
    DeveloperAccount,
    Installation,
    OAuthClient,
)
from gdx_dispatch.models.tenant_models import Base as TenantBase
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.tools.backfill_users_to_identities import backfill_users, backfill_users_from_tenants
from gdx_dispatch.tools.migrate_service_accounts import migrate_service_accounts
from gdx_dispatch.tools.seed_capability_sets_from_openapi import CORE_ACTIONS, CORE_RESOURCE_TYPES, seed_capabilities
from gdx_dispatch.tools.seed_platform_platform import seed_platform
from gdx_dispatch.tests.factories.platform import tenant_uuid_from_slug


class _FakeCache:
    def get(self, _key):
        return None

    def setex(self, _key, _ttl, _value):
        return None

    def delete(self, *_keys):
        return 0

    def publish(self, _channel, _message):
        return 1


def _make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ControlBase.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            ServiceAccount.__table__,
            Identity.__table__,
            IdentityProvider.__table__,
            CapabilitySet.__table__,
            Capability.__table__,
            OAuthClient.__table__,
            DeveloperAccount.__table__,
            BillingAccount.__table__,
            Installation.__table__,
            AccessToken.__table__,
            Membership.__table__,
        ],
        checkfirst=True,
    )
    TenantBase.metadata.create_all(engine, tables=[User.__table__], checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def _make_control_only_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ControlBase.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            ServiceAccount.__table__,
            Identity.__table__,
            IdentityProvider.__table__,
            CapabilitySet.__table__,
            Capability.__table__,
            OAuthClient.__table__,
            DeveloperAccount.__table__,
            BillingAccount.__table__,
            Installation.__table__,
            AccessToken.__table__,
            Membership.__table__,
        ],
        checkfirst=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def test_seed_idempotent() -> None:
    db = _make_session()
    try:
        first = seed_platform(db, dry_run=False)
        second = seed_platform(db, dry_run=False)
        assert first["created"]["oauth_clients"] == 4
        assert first["created"]["capability_sets"] >= 7
        assert second["created"]["oauth_clients"] == 0
        assert second["created"]["capability_sets"] == 0
    finally:
        db.close()


def test_backfill_dry_run_no_writes() -> None:
    db = _make_session()
    try:
        seed_platform(db, dry_run=False)
        db.add(Tenant(slug="gdx", name="GDX"))
        db.add(
            User(
                id=_make_user_uuid("user-1"),
                username="doug",
                email="doug@example.com",
                role="owner",
                company_id="gdx",
            )
        )
        db.commit()

        stats = backfill_users(db, dry_run=True)
        identity_count = db.execute(select(func.count()).select_from(Identity)).scalar_one()
        membership_count = db.execute(select(func.count()).select_from(Membership)).scalar_one()
        assert stats["users_seen"] == 1
        assert identity_count == 1  # platform admin only
        assert membership_count == 0
    finally:
        db.close()


def test_backfill_no_users_table_is_zero_state() -> None:
    db = _make_control_only_session()
    try:
        seed_platform(db, dry_run=False)
        db.add(Tenant(slug="gdx", name="GDX"))
        db.commit()

        stats = backfill_users(db, dry_run=True)
        assert stats["legacy_users_table_present"] is False
        assert stats["users_seen"] == 0
        assert stats["errors"] == []
    finally:
        db.close()


def test_backfill_idempotent() -> None:
    db = _make_session()
    try:
        seed_platform(db, dry_run=False)
        db.add(Tenant(slug="gdx", name="GDX"))
        db.add(
            User(
                id=_make_user_uuid("user-1"),
                username="doug",
                email="doug@example.com",
                role="owner",
                company_id="gdx",
            )
        )
        db.commit()

        first = backfill_users(db, dry_run=False)
        second = backfill_users(db, dry_run=False)

        assert first["identities_created"] == 1
        assert first["memberships_created"] == 1
        assert second["identities_created"] == 0
        assert second["memberships_created"] == 0

        repo = IdentityRepo(db, _FakeCache())
        identity = repo.get_identity_by_provider("legacy_local", str(_make_user_uuid("user-1")))
        assert identity is not None
        memberships = repo.get_memberships(identity.id)
        assert len(memberships) == 1
        # backfill resolves the membership's tenant_id from the actual
        # Tenant.id of the seeded "gdx" row (not a synthetic UUID).
        gdx_tenant = db.execute(select(Tenant).where(Tenant.slug == "gdx")).scalar_one()
        assert memberships[0].tenant_id == gdx_tenant.id
    finally:
        db.close()


def test_service_account_migration_preserves_secret() -> None:
    db = _make_session()
    try:
        platform = seed_platform(db, dry_run=False)
        db.add(Tenant(slug="gdx", name="GDX"))
        sa = ServiceAccount(
            id=uuid4(),
            name="scanner",
            key_hash="abc123hash",
            key_prefix="svc_live_abcd1234",
            allowed_scopes=["read:*"],
            created_by="test",
        )
        db.add(sa)
        db.commit()

        result = migrate_service_accounts(db, platform_results=platform, dry_run=False)
        assert result["access_tokens_created"] >= 1

        token = db.execute(
            select(AccessToken).where(
                AccessToken.owner_type == "service_account",
                AccessToken.owner_id == sa.id,
            )
        ).scalar_one()
        assert token.secret_hash == "abc123hash"
    finally:
        db.close()


def _make_tenant_engine_with_users() -> tuple:
    """Stand up an isolated SQLite engine holding only a tenant ``users`` table."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, tables=[User.__table__], checkfirst=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _make_tenant_engine_empty() -> tuple:
    """Tenant engine with no ``users`` table — simulates a registered-but-unprovisioned tenant."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_tenant_user(SessionLocal, **user_kwargs) -> None:
    # User.id is Uuid(as_uuid=True); convert non-UUID string ids deterministically.
    if "id" in user_kwargs and isinstance(user_kwargs["id"], str):
        try:
            user_kwargs["id"] = UUID(user_kwargs["id"])
        except ValueError:
            user_kwargs["id"] = _make_user_uuid(user_kwargs["id"])
    s = SessionLocal()
    try:
        s.add(User(**user_kwargs))
        s.commit()
    finally:
        s.close()


def test_backfill_from_tenants_sources_per_tenant_users() -> None:
    """Two tenants, each with its own users table → 2 identities + 2 memberships in control DB."""
    control_db = _make_control_only_session()
    _, gdx_Session = _make_tenant_engine_with_users()
    _, acme_Session = _make_tenant_engine_with_users()
    try:
        seed_platform(control_db, dry_run=False)
        control_db.add(Tenant(slug="gdx", name="GDX"))
        control_db.add(Tenant(slug="acme", name="Acme Doors"))
        control_db.commit()

        _seed_tenant_user(gdx_Session, id="u-gdx-1", username="doug", email="doug@gdx.test", role="owner", company_id="gdx")
        _seed_tenant_user(acme_Session, id="u-acme-1", username="alice", email="alice@acme.test", role="admin", company_id="acme")

        sessions_by_slug = {"gdx": gdx_Session, "acme": acme_Session}
        def factory(tenant):
            return sessions_by_slug[tenant.slug]()

        stats = backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)

        assert stats["tenants_seen"] == 2
        assert stats["tenants_with_users_table"] == 2
        assert stats["users_seen"] == 2
        assert stats["identities_created"] == 2
        assert stats["memberships_created"] == 2
        assert stats["per_tenant"]["gdx"]["users_seen"] == 1
        assert stats["per_tenant"]["acme"]["users_seen"] == 1
        assert stats["per_tenant"]["gdx"]["error"] is None
        assert stats["per_tenant"]["acme"]["error"] is None

        identity_count = control_db.execute(select(func.count()).select_from(Identity)).scalar_one()
        # 1 platform admin from seed_platform + 2 tenant users
        assert identity_count == 3
    finally:
        control_db.close()


def test_backfill_from_tenants_missing_users_table_per_tenant_is_zero_state() -> None:
    """A tenant whose DB has no users table contributes zero; others still backfill."""
    control_db = _make_control_only_session()
    _, gdx_Session = _make_tenant_engine_with_users()
    _, empty_Session = _make_tenant_engine_empty()
    try:
        seed_platform(control_db, dry_run=False)
        control_db.add(Tenant(slug="gdx", name="GDX"))
        control_db.add(Tenant(slug="newco", name="New Co"))
        control_db.commit()

        _seed_tenant_user(gdx_Session, id="u-gdx-1", username="doug", email="doug@gdx.test", role="owner", company_id="gdx")

        sessions_by_slug = {"gdx": gdx_Session, "newco": empty_Session}
        def factory(tenant):
            return sessions_by_slug[tenant.slug]()

        stats = backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)

        assert stats["tenants_seen"] == 2
        assert stats["tenants_with_users_table"] == 1
        assert stats["users_seen"] == 1
        assert stats["identities_created"] == 1
        assert stats["per_tenant"]["gdx"]["users_table_present"] is True
        assert stats["per_tenant"]["newco"]["users_table_present"] is False
        assert stats["per_tenant"]["newco"]["users_seen"] == 0
    finally:
        control_db.close()


def test_backfill_from_tenants_processes_all_tenants() -> None:
    """All tenants are processed (db_url_enc column dropped in Phase D)."""
    control_db = _make_control_only_session()
    _, gdx_Session = _make_tenant_engine_with_users()
    _, pending_Session = _make_tenant_engine_with_users()  # empty — no users seeded
    try:
        seed_platform(control_db, dry_run=False)
        control_db.add(Tenant(slug="gdx", name="GDX"))
        control_db.add(Tenant(slug="pending", name="Pending Co"))
        control_db.commit()

        _seed_tenant_user(gdx_Session, id="u-gdx-1", username="doug", email="doug@gdx.test", role="owner", company_id="gdx")
        # pending has no users seeded

        factory_calls: list[str] = []
        sessions_by_slug = {"gdx": gdx_Session, "pending": pending_Session}

        def factory(tenant):
            factory_calls.append(tenant.slug)
            return sessions_by_slug[tenant.slug]()

        stats = backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)

        assert sorted(factory_calls) == ["gdx", "pending"]  # both tenants get a session
        assert stats["tenants_skipped"] == 0
        assert stats["identities_created"] == 1  # only gdx had users
    finally:
        control_db.close()


def test_backfill_from_tenants_is_idempotent_across_tenants() -> None:
    """Running twice yields zero additional writes on the second pass."""
    control_db = _make_control_only_session()
    _, gdx_Session = _make_tenant_engine_with_users()
    _, acme_Session = _make_tenant_engine_with_users()
    try:
        seed_platform(control_db, dry_run=False)
        control_db.add(Tenant(slug="gdx", name="GDX"))
        control_db.add(Tenant(slug="acme", name="Acme"))
        control_db.commit()

        _seed_tenant_user(gdx_Session, id="u-gdx-1", username="doug", email="doug@gdx.test", role="owner", company_id="gdx")
        _seed_tenant_user(acme_Session, id="u-acme-1", username="alice", email="alice@acme.test", role="admin", company_id="acme")

        sessions_by_slug = {"gdx": gdx_Session, "acme": acme_Session}
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
    finally:
        control_db.close()


def test_backfill_from_tenants_dry_run_makes_no_writes() -> None:
    """Dry-run rolls back the control DB; no identities are persisted."""
    control_db = _make_control_only_session()
    _, gdx_Session = _make_tenant_engine_with_users()
    try:
        seed_platform(control_db, dry_run=False)
        control_db.add(Tenant(slug="gdx", name="GDX"))
        control_db.commit()
        platform_identity_count = control_db.execute(
            select(func.count()).select_from(Identity)
        ).scalar_one()

        _seed_tenant_user(gdx_Session, id="u-gdx-1", username="doug", email="doug@gdx.test", role="owner", company_id="gdx")

        def factory(tenant):
            return gdx_Session()
        stats = backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=True)

        assert stats["users_seen"] == 1
        post_identity_count = control_db.execute(
            select(func.count()).select_from(Identity)
        ).scalar_one()
        assert post_identity_count == platform_identity_count
    finally:
        control_db.close()


def test_backfill_from_tenants_records_connect_errors_per_tenant() -> None:
    """A tenant whose factory raises is recorded in per_tenant stats; others still run."""
    control_db = _make_control_only_session()
    _, gdx_Session = _make_tenant_engine_with_users()
    try:
        seed_platform(control_db, dry_run=False)
        control_db.add(Tenant(slug="gdx", name="GDX"))
        control_db.add(Tenant(slug="broken", name="Broken Co"))
        control_db.commit()

        _seed_tenant_user(gdx_Session, id="u-gdx-1", username="doug", email="doug@gdx.test", role="owner", company_id="gdx")

        def factory(tenant):
            if tenant.slug == "broken":
                raise RuntimeError("simulated connect failure")
            return gdx_Session()

        stats = backfill_users_from_tenants(control_db, tenant_session_factory=factory, dry_run=False)

        assert stats["tenants_errored"] == 1
        assert "connect: RuntimeError" in stats["per_tenant"]["broken"]["error"]
        assert stats["per_tenant"]["gdx"]["error"] is None
        assert stats["identities_created"] == 1
    finally:
        control_db.close()


def test_backfill_from_tenants_error_abort_threshold_halts_iteration() -> None:
    """Once error rate exceeds threshold after the sample floor, remaining tenants are skipped."""
    control_db = _make_control_only_session()
    _, gdx_Session = _make_tenant_engine_with_users()
    try:
        seed_platform(control_db, dry_run=False)
        # 6 tenants: 5 break, 6th would succeed. With threshold 0.3 + min_samples 5,
        # the abort fires after the 5th error and the 6th tenant is recorded skipped.
        for i in range(5):
            control_db.add(Tenant(slug=f"break-{i}", name=f"Break {i}"))
        control_db.add(Tenant(slug="last", name="Last"))
        control_db.commit()

        _seed_tenant_user(gdx_Session, id="u-last", username="last", email="last@x.test", role="owner", company_id="last")

        def factory(tenant):
            if tenant.slug.startswith("break-"):
                raise RuntimeError("simulated")
            return gdx_Session()

        stats = backfill_users_from_tenants(
            control_db,
            tenant_session_factory=factory,
            dry_run=False,
            error_abort_threshold=0.3,
            min_samples_before_abort=5,
        )

        assert stats["aborted_on_error_rate"] is True
        assert stats["tenants_errored"] == 5
        assert stats["per_tenant"]["last"]["skipped"] == "aborted_error_rate"
        # 'last' tenant never opened → no user read → no identity for it
        assert stats["identities_created"] == 0
    finally:
        control_db.close()


def test_backfill_from_tenants_error_abort_respects_min_samples() -> None:
    """With only 2 tenants attempted, abort does not fire even at 100% error rate."""
    control_db = _make_control_only_session()
    try:
        seed_platform(control_db, dry_run=False)
        control_db.add(Tenant(slug="a", name="A"))
        control_db.add(Tenant(slug="b", name="B"))
        control_db.commit()

        def factory(tenant):
            raise RuntimeError("simulated")

        stats = backfill_users_from_tenants(
            control_db,
            tenant_session_factory=factory,
            dry_run=False,
            error_abort_threshold=0.5,
            min_samples_before_abort=5,
        )

        assert stats["aborted_on_error_rate"] is False
        assert stats["tenants_errored"] == 2
    finally:
        control_db.close()


def test_backfill_from_tenants_records_guards_in_output() -> None:
    """Guards metadata is echoed into stats for auditability."""
    control_db = _make_control_only_session()
    try:
        seed_platform(control_db, dry_run=False)
        control_db.commit()

        stats = backfill_users_from_tenants(
            control_db,
            tenant_session_factory=lambda _: _make_control_only_session(),  # unused — no tenants
            dry_run=True,
            statement_timeout_seconds=15,
            enforce_read_only=True,
            error_abort_threshold=0.25,
            min_samples_before_abort=10,
        )

        assert stats["guards"]["statement_timeout_seconds"] == 15
        assert stats["guards"]["enforce_read_only"] is True
        assert stats["guards"]["error_abort_threshold"] == 0.25
        assert stats["guards"]["min_samples_before_abort"] == 10
    finally:
        control_db.close()


def test_capability_seeding_role_owner_full_matrix() -> None:
    db = _make_session()
    try:
        seed_platform(db, dry_run=False)
        stats = seed_capabilities(db, dry_run=False)
        assert stats["capabilities_inserted"] > 0

        owner_set = db.execute(
            select(CapabilitySet).where(CapabilitySet.name == "role:owner")
        ).scalar_one()
        owner_caps = db.execute(
            select(Capability).where(Capability.capability_set_id == owner_set.id)
        ).scalars().all()
        expected = len(CORE_RESOURCE_TYPES) * len(CORE_ACTIONS)
        assert len(owner_caps) == expected
    finally:
        db.close()
