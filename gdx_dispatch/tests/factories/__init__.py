"""Platform test factories — SS-5 Slice A.

Plain-function factories that create platform entities against a provided
SQLAlchemy Session. Each factory persists via ``db.flush()`` (no commit) so
callers control transaction boundaries. Optional keyword overrides for every
field let tests pin specific values when the default sequence would be
ambiguous.

Imported at the package level so tests can do::

    from gdx_dispatch.tests.factories import make_identity, make_installation
"""
from gdx_dispatch.tests.factories.platform import (
    make_access_token,
    make_billing_account,
    make_capability,
    make_capability_set,
    make_developer_account,
    make_identity,
    make_identity_provider,
    make_installation,
    make_membership,
    make_oauth_client,
    make_oauth_client_key,
    make_resource_descriptor,
    make_share,
    make_shared_resource,
    make_tenant,
)

__all__ = [
    "make_access_token",
    "make_billing_account",
    "make_capability",
    "make_capability_set",
    "make_developer_account",
    "make_identity",
    "make_identity_provider",
    "make_installation",
    "make_membership",
    "make_oauth_client",
    "make_oauth_client_key",
    "make_resource_descriptor",
    "make_share",
    "make_shared_resource",
    "make_tenant",
]
