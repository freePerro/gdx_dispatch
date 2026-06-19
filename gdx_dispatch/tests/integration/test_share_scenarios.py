"""Dual-tenant share-scenario scaffold — SS-5 Slice A.

Minimal scaffold validating that two-tenant setups compose through the
factories. Slice B+ layers on real ``shared_resources`` + ``shares`` table
exercises once SS-6/SS-7 expose share-grant paths.
"""
from __future__ import annotations

import jwt

from gdx_dispatch.models.platform_extensions import Installation
from gdx_dispatch.tests.factories import (
    make_capability_set,
    make_tenant,
)
from gdx_dispatch.tests.fixtures.keypairs import mint_installation_token


def test_two_tenants_two_installations_distinct(
    control_db,
    make_installation_with_key,
) -> None:
    """Two tenants each get their own installation; rows don't collide."""
    tenant_a = make_tenant(control_db, slug="ss5-share-a")
    tenant_b = make_tenant(control_db, slug="ss5-share-b")

    install_a, key_a = make_installation_with_key(tenant=tenant_a)
    install_b, key_b = make_installation_with_key(tenant=tenant_b)

    assert install_a.tenant_id == tenant_a.id
    assert install_b.tenant_id == tenant_b.id
    assert install_a.id != install_b.id
    assert key_a.oauth_client_id != key_b.oauth_client_id

    # The platform has two installation rows scoped to distinct tenants.
    rows = control_db.query(Installation).order_by(Installation.tenant_id).all()
    tenants_seen = {r.tenant_id for r in rows}
    assert {tenant_a.id, tenant_b.id} <= tenants_seen


def test_installation_token_round_trips_through_keypair(
    control_db,
    test_app_keypair,
    make_installation_with_key,
) -> None:
    """Token minted by the test keypair verifies against the registered public PEM."""
    tenant = make_tenant(control_db, slug="ss5-share-token")
    capset = make_capability_set(control_db, name="ss5-share-token-capset")
    install, key = make_installation_with_key(tenant=tenant, capability_set=capset)

    token = mint_installation_token(
        keypair=test_app_keypair,
        installation_id=str(install.id),
        tenant_id=tenant.slug,
        oauth_client_id=str(install.oauth_client_id),
        capability_set_id=str(capset.id),
    )

    # Verify the JWT against the registered public key — proves the fixture
    # writes the right PEM AND that mint_installation_token uses the matching
    # private key.
    decoded = jwt.decode(
        token,
        key.public_key_pem,
        algorithms=["RS256"],
        audience="gdx-platform",
    )
    assert decoded["sub"] == str(install.id)
    assert decoded["tenant_id"] == tenant.slug
    assert decoded["capability_set_id"] == str(capset.id)
    assert jwt.get_unverified_header(token)["kid"] == test_app_keypair["kid"]


# test_share_scenario_isolates_identities_by_tenant removed (D97 031):
# `tenants.parent_tenant_id` drops with zero active writers. The chain
# fixture used here is gone. Multi-tier sharing is now expressed via
# `tenant_relationships` (control/relationships.py) using UUID FKs.
