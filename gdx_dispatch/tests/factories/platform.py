"""Plain-function factories for platform entities — SS-5 Slice A.

Each ``make_*`` takes a SQLAlchemy ``Session`` as its first positional arg
plus keyword overrides, instantiates the model with sensible defaults, calls
``db.add(obj)`` + ``db.flush()`` (no commit — caller controls the txn), and
returns the persisted entity.

Defaults use module-level counters so sequential calls inside one test get
unique emails / slugs / client_ids without clashing. Counters reset on
module import, which is per-session for pytest.

For any subfactory that depends on another entity (e.g. Membership requires
an Identity and a CapabilitySet), callers can either:

- Pass the dependency explicitly as a kwarg (e.g. ``identity=my_identity``).
- Let the factory auto-create one via the corresponding ``make_*`` helper.

Because defaults auto-create dependencies, a single call like
``make_installation(db)`` produces a full chain: Tenant + OAuthClient +
CapabilitySet + BillingAccount + Identity + Installation. Pass overrides
when a test needs shared entities across siblings.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5


def tenant_uuid_from_slug(slug: str) -> UUID:
    """Deterministic test-only UUID derived from a slug-style name.

    Use this in tests instead of literal slug strings whenever a
    tenant_id (or any column FK'd to ``tenants.id``) is needed. The
    same slug always produces the same UUID, so inserts and assertions
    can both round-trip through this helper without coordinating on a
    hardcoded UUID.

    See ``feedback_centralized_test_identifiers.md`` for the rationale —
    D97 (slug→UUID column-shape migration) hit ~40 test failures because
    literal slug strings were baked into both insert sites and asserts.
    Future shape evolutions should route through this helper to keep the
    flip a one-line edit instead of a multi-file slog.
    """
    return uuid5(NAMESPACE_DNS, f"d97-test-{slug}")

from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.models.platform import (
    Capability,
    CapabilitySet,
    Identity,
    IdentityProvider,
    Membership,
)
from gdx_dispatch.models.platform_extensions import (
    AccessToken,
    BillingAccount,
    DeveloperAccount,
    Installation,
    OAuthClient,
    OAuthClientKey,
    ResourceDescriptor,
    Share,
    SharedResource,
)

_identity_seq = itertools.count(1)
_tenant_seq = itertools.count(1)
_capset_seq = itertools.count(1)
_client_seq = itertools.count(1)
_provider_seq = itertools.count(1)
_resource_seq = itertools.count(1)
_descriptor_seq = itertools.count(1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Core SS-2 entities ──────────────────────────────────────────────────────


def make_tenant(db: Session, *, slug: str | None = None, **overrides: Any) -> Tenant:
    if slug is None:
        slug = f"t-ss5-{next(_tenant_seq)}"
    defaults: dict[str, Any] = {
        "slug": slug,
        "name": overrides.pop("name", f"Test Tenant {slug}"),
        "timezone": overrides.pop("timezone", "America/New_York"),
    }
    # Pop legacy columns so callers that still pass them don't blow up.
    overrides.pop("db_url_enc", None)
    overrides.pop("db_provisioned", None)
    overrides.pop("subscription_status", None)
    defaults.update(overrides)
    tenant = Tenant(**defaults)
    db.add(tenant)
    db.flush()
    return tenant


def make_identity(db: Session, *, email: str | None = None, **overrides: Any) -> Identity:
    seq = next(_identity_seq)
    if email is None:
        email = f"ss5-user{seq}@example.com"
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "email": email,
        "display_name": overrides.pop("display_name", f"SS-5 User {seq}"),
        "status": overrides.pop("status", "active"),
        "email_verified_at": overrides.pop("email_verified_at", _utcnow()),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    identity = Identity(**defaults)
    db.add(identity)
    db.flush()
    return identity


def make_identity_provider(
    db: Session,
    *,
    identity: Identity | None = None,
    **overrides: Any,
) -> IdentityProvider:
    if identity is None:
        identity = make_identity(db)
    seq = next(_provider_seq)
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "identity_id": identity.id,
        "provider_type": overrides.pop("provider_type", "authentik"),
        "provider_subject": overrides.pop("provider_subject", f"sub-{seq}"),
        "provider_email": overrides.pop("provider_email", identity.email),
        "email_verified_by_provider": overrides.pop("email_verified_by_provider", True),
        "is_authoritative_for_domain": overrides.pop("is_authoritative_for_domain", False),
        "linked_at": overrides.pop("linked_at", _utcnow()),
        "provider_metadata": overrides.pop("provider_metadata", {}),
    }
    defaults.update(overrides)
    provider = IdentityProvider(**defaults)
    db.add(provider)
    db.flush()
    return provider


def make_capability_set(
    db: Session,
    *,
    name: str | None = None,
    **overrides: Any,
) -> CapabilitySet:
    seq = next(_capset_seq)
    if name is None:
        name = f"capset-ss5-{seq}"
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "name": name,
        "description": overrides.pop("description", f"SS-5 scaffold capset {seq}"),
        "scope_type": overrides.pop("scope_type", "tenant"),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    capset = CapabilitySet(**defaults)
    db.add(capset)
    db.flush()
    return capset


def make_capability(
    db: Session,
    *,
    capability_set: CapabilitySet | None = None,
    **overrides: Any,
) -> Capability:
    if capability_set is None:
        capability_set = make_capability_set(db)
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "capability_set_id": capability_set.id,
        "action": overrides.pop("action", "read"),
        "resource_type": overrides.pop("resource_type", "job"),
        "instance_pattern": overrides.pop("instance_pattern", "*"),
        "conditions": overrides.pop("conditions", {}),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    cap = Capability(**defaults)
    db.add(cap)
    db.flush()
    return cap


def make_membership(
    db: Session,
    *,
    identity: Identity | None = None,
    tenant: Tenant | None = None,
    capability_set: CapabilitySet | None = None,
    **overrides: Any,
) -> Membership:
    if identity is None:
        identity = make_identity(db)
    if tenant is None:
        tenant = make_tenant(db)
    if capability_set is None:
        capability_set = make_capability_set(db)
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "identity_id": identity.id,
        "tenant_id": tenant.id,
        "role": overrides.pop("role", "tech"),
        "capability_set_id": capability_set.id,
        "granted_at": overrides.pop("granted_at", _utcnow()),
    }
    defaults.update(overrides)
    membership = Membership(**defaults)
    db.add(membership)
    db.flush()
    return membership


# ── SS-3 OAuth + installation surface ────────────────────────────────────────


def make_developer_account(
    db: Session,
    *,
    email: str | None = None,
    **overrides: Any,
) -> DeveloperAccount:
    seq = next(_identity_seq)  # reuse identity counter for uniqueness
    if email is None:
        email = f"dev{seq}@example.com"
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "email": email,
        "display_name": overrides.pop("display_name", f"SS-5 Developer {seq}"),
        "status": overrides.pop("status", "active"),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    dev = DeveloperAccount(**defaults)
    db.add(dev)
    db.flush()
    return dev


def make_billing_account(
    db: Session,
    *,
    owner_type: str = "tenant",
    owner_id: UUID | None = None,
    **overrides: Any,
) -> BillingAccount:
    if owner_id is None:
        owner_id = uuid4()
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "owner_type": owner_type,
        "owner_id": owner_id,
        "status": overrides.pop("status", "active"),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    ba = BillingAccount(**defaults)
    db.add(ba)
    db.flush()
    return ba


def make_oauth_client(
    db: Session,
    *,
    owner_type: str = "developer",
    owner_id: UUID | None = None,
    **overrides: Any,
) -> OAuthClient:
    seq = next(_client_seq)
    if owner_id is None:
        owner_id = uuid4()
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "client_id": overrides.pop("client_id", f"gdx_oauth_ss5_{seq}"),
        "name": overrides.pop("name", f"SS-5 OAuth Client {seq}"),
        "description": overrides.pop("description", None),
        "owner_type": owner_type,
        "owner_id": owner_id,
        "redirect_uris": overrides.pop("redirect_uris", ["http://localhost:3000/callback"]),
        "scopes_requested": overrides.pop("scopes_requested", ["read:jobs"]),
        "client_type": overrides.pop("client_type", "confidential"),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    client = OAuthClient(**defaults)
    db.add(client)
    db.flush()
    return client


def make_oauth_client_key(
    db: Session,
    *,
    oauth_client: OAuthClient | None = None,
    public_key_pem: str | None = None,
    kid: str | None = None,
    **overrides: Any,
) -> OAuthClientKey:
    if oauth_client is None:
        oauth_client = make_oauth_client(db)
    if kid is None:
        kid = f"ss5-kid-{next(_client_seq)}"
    if public_key_pem is None:
        # Placeholder PEM — tests that actually verify signatures pass a
        # real public key via the keypair fixture.
        public_key_pem = "-----BEGIN PUBLIC KEY-----\nPLACEHOLDER\n-----END PUBLIC KEY-----\n"
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "oauth_client_id": oauth_client.id,
        "kid": kid,
        "public_key_pem": public_key_pem,
        "state": overrides.pop("state", "active"),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    key = OAuthClientKey(**defaults)
    db.add(key)
    db.flush()
    return key


def make_installation(
    db: Session,
    *,
    oauth_client: OAuthClient | None = None,
    tenant: Tenant | None = None,
    installer_identity: Identity | None = None,
    capability_set: CapabilitySet | None = None,
    billing_account: BillingAccount | None = None,
    **overrides: Any,
) -> Installation:
    if tenant is None:
        tenant = make_tenant(db)
    if oauth_client is None:
        oauth_client = make_oauth_client(db)
    if installer_identity is None:
        installer_identity = make_identity(db)
    if capability_set is None:
        capability_set = make_capability_set(db)
    if billing_account is None:
        billing_account = make_billing_account(db, owner_type="tenant", owner_id=uuid4())
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "oauth_client_id": oauth_client.id,
        "tenant_id": tenant.id,
        "installer_identity_id": installer_identity.id,
        "capability_set_id": capability_set.id,
        "billing_account_id": billing_account.id,
        "status": overrides.pop("status", "active"),
        "installed_at": overrides.pop("installed_at", _utcnow()),
        "config": overrides.pop("config", {}),
        "health_status": overrides.pop("health_status", "healthy"),
    }
    defaults.update(overrides)
    install = Installation(**defaults)
    db.add(install)
    db.flush()
    return install


# ── SS-3c sharing surface ────────────────────────────────────────────────────


def make_resource_descriptor(
    db: Session,
    *,
    resource_type: str | None = None,
    **overrides: Any,
) -> ResourceDescriptor:
    seq = next(_descriptor_seq)
    if resource_type is None:
        resource_type = f"ss5.job.v{seq}"
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "resource_type": resource_type,
        "owner": overrides.pop("owner", "gdx-core"),
        "schema": overrides.pop("schema", {"type": "object"}),
        "capabilities_supported": overrides.pop(
            "capabilities_supported", ["read", "write", "share"]
        ),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    desc = ResourceDescriptor(**defaults)
    db.add(desc)
    db.flush()
    return desc


def make_shared_resource(
    db: Session,
    *,
    owner_tenant: Tenant | None = None,
    shared_via_installation: Installation | None = None,
    resource_type: str = "job",
    resource_id: str | None = None,
    **overrides: Any,
) -> SharedResource:
    if owner_tenant is None:
        owner_tenant = make_tenant(db)
    if resource_id is None:
        resource_id = str(uuid4())
    next(_resource_seq)
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "owner_tenant_id": owner_tenant.id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "shared_via_installation_id": shared_via_installation.id if shared_via_installation else None,
        "visibility": overrides.pop("visibility", "tenant"),
        "created_at": overrides.pop("created_at", _utcnow()),
    }
    defaults.update(overrides)
    sr = SharedResource(**defaults)
    db.add(sr)
    db.flush()
    return sr


def make_share(
    db: Session,
    *,
    shared_resource: SharedResource | None = None,
    target_tenant: Tenant | None = None,
    target_installation: Installation | None = None,
    capabilities: list[str] | None = None,
    **overrides: Any,
) -> Share:
    """Create a Share row.

    At least one of ``target_tenant`` / ``target_installation`` SHOULD be set
    (the DB allows both null but a share with no target is meaningless). If
    both omitted, a target_tenant is auto-created so the share has a scope.
    """
    if shared_resource is None:
        shared_resource = make_shared_resource(db)
    if target_tenant is None and target_installation is None:
        target_tenant = make_tenant(db)
    if capabilities is None:
        capabilities = ["read"]
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "shared_resource_id": shared_resource.id,
        "target_tenant_id": target_tenant.id if target_tenant else None,
        "target_installation_id": target_installation.id if target_installation else None,
        "capabilities": capabilities,
        "granted_at": overrides.pop("granted_at", _utcnow()),
    }
    defaults.update(overrides)
    share = Share(**defaults)
    db.add(share)
    db.flush()
    return share


def make_access_token(
    db: Session,
    *,
    owner_type: str = "user",
    owner_id: UUID | None = None,
    capability_set: CapabilitySet | None = None,
    installation: Installation | None = None,
    **overrides: Any,
) -> AccessToken:
    if owner_id is None:
        owner_id = uuid4()
    if capability_set is None:
        capability_set = make_capability_set(db)
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", uuid4()),
        "prefix": overrides.pop("prefix", "gdx_pat_ss5_"),
        "secret_hash": overrides.pop("secret_hash", "$2b$12$dummy"),
        "owner_type": owner_type,
        "owner_id": owner_id,
        "installation_id": installation.id if installation else None,
        "capability_set_id": capability_set.id,
        "name": overrides.pop("name", "ss5 test token"),
        "expires_at": overrides.pop("expires_at", _utcnow() + timedelta(days=90)),
        "created_at": overrides.pop("created_at", _utcnow()),
        "key_version": overrides.pop("key_version", 1),
    }
    defaults.update(overrides)
    tok = AccessToken(**defaults)
    db.add(tok)
    db.flush()
    return tok
