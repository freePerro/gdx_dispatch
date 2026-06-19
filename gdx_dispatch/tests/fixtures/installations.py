"""Installation-with-key fixture — SS-5 Slice A.

Composes an OAuthClient + OAuthClientKey (registered with the test session's
real RSA public key) + Installation in one call. Used by downstream tests
that need to mint JWTs and have the platform recognize the signing key.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.models.platform import CapabilitySet, Identity
from gdx_dispatch.models.platform_extensions import (
    Installation,
    OAuthClient,
    OAuthClientKey,
)
from gdx_dispatch.tests.factories import (
    make_billing_account,
    make_capability_set,
    make_identity,
    make_installation,
    make_oauth_client,
    make_oauth_client_key,
    make_tenant,
)


@pytest.fixture
def make_installation_with_key(
    control_db: Session,
    test_app_keypair: dict[str, str],
) -> Callable[..., tuple[Installation, OAuthClientKey]]:
    """Factory: returns (installation, registered_oauth_client_key).

    The OAuthClientKey is populated with the session keypair's public PEM
    under the keypair's ``kid`` so JWTs minted via ``mint_installation_token``
    verify correctly against this client.

    Optional kwargs:
      tenant, oauth_client, installer_identity, capability_set
        — all auto-created if omitted.
    """
    def _make(
        *,
        tenant: Tenant | None = None,
        oauth_client: OAuthClient | None = None,
        installer_identity: Identity | None = None,
        capability_set: CapabilitySet | None = None,
    ) -> tuple[Installation, OAuthClientKey]:
        if tenant is None:
            tenant = make_tenant(control_db)
        if oauth_client is None:
            oauth_client = make_oauth_client(control_db)
        if installer_identity is None:
            installer_identity = make_identity(control_db)
        if capability_set is None:
            capability_set = make_capability_set(control_db)

        key = make_oauth_client_key(
            control_db,
            oauth_client=oauth_client,
            public_key_pem=test_app_keypair["public_pem"],
            kid=test_app_keypair["kid"],
        )
        billing = make_billing_account(control_db, owner_type="tenant")
        install = make_installation(
            control_db,
            oauth_client=oauth_client,
            tenant=tenant,
            installer_identity=installer_identity,
            capability_set=capability_set,
            billing_account=billing,
        )
        return install, key

    return _make
