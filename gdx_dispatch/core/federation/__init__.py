"""SS-31 Federation package.

Prepares the platform to accept identities from EXTERNAL identity
providers (customer-brought Authentik / Okta / Azure AD) via SAML 2.0
(SP-initiated) and OIDC (Authorization Code + PKCE).

Modules:
  * ``trust_bundle``      — load / validate IdP trust material (signing
                            certs, JWKS URLs, metadata URLs). Caches with
                            TTL, refreshes in background, serves from
                            cache if refresh fails.
  * ``oidc_provider``     — external OIDC discovery + ID token
                            verification (signature via JWKS, full claim
                            validation).
  * ``saml_provider``     — SP-side SAML 2.0 helpers: AuthnRequest
                            builder + Response / assertion parser
                            (``defusedxml`` ONLY — never bare etree).
  * ``identity_linking``  — ``reconcile_federated_identity`` match-or-
                            create with collision detection. Never
                            auto-merges on email collision; emits an
                            event and surfaces 409 to the caller.

INTEGRATION_TODO (do NOT do in this sprint):
  * Wire ``gdx_dispatch.routers.federation`` into ``gdx_dispatch.main``.
  * Teach ``gdx_dispatch.core.auth.get_current_user`` to accept a session cookie
    / bearer minted by a federation callback.
  * Re-chain the SS-31 migration after the live alembic head.
"""
from __future__ import annotations

__all__ = [
    "trust_bundle",
    "oidc_provider",
    "saml_provider",
    "identity_linking",
]
