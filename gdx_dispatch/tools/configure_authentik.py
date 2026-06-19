#!/usr/bin/env python3
"""Idempotent Authentik provider configuration for GDX.

SS-6 scope: ``gdx-spa`` (Slice A) and ``gdx-thirdparty`` (Slice C).

Both providers share:

- OAuth 2.1 authorization-code + S256 PKCE.
- Audience ``gdx-api``.
- Single fixed redirect URI — no wildcards (SS-6 audit P16).
- ``response_types=["code"]`` only (no implicit / no hybrid).

Per-provider token TTLs:

- ``gdx-spa``: access 15 minutes, refresh 30 days.
- ``gdx-thirdparty``: access 1 hour, refresh 90 days (SS-21 Zapier-class
  integrations).

Re-running produces no duplicate provider artifacts: every resource is
looked up by a deterministic ``name``/``slug`` first and only created when
absent (get-or-create semantics).

Slice B (``gdx-mcp``) is intentionally NOT implemented: MCP authenticates
via PAT bearer tokens (SS-14 issuance, SS-19 validation), not via an
Authentik OAuth provider. ``--provider`` does not accept ``gdx-mcp``.

Typical usage
-------------

    export AUTHENTIK_BOOTSTRAP_TOKEN=<admin api token>
    AUTHENTIK_BASE_URL=https://auth.example.com \\
        python gdx_dispatch/tools/configure_authentik.py --provider gdx-spa

    AUTHENTIK_BASE_URL=https://auth.example.com \\
        python gdx_dispatch/tools/configure_authentik.py --provider gdx-thirdparty

Dry-run mode (does not call the Authentik API) is useful for smoke tests::

    python gdx_dispatch/tools/configure_authentik.py --provider gdx-spa --dry-run
    python gdx_dispatch/tools/configure_authentik.py --provider gdx-thirdparty --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

try:  # pragma: no cover - import-time shim only
    import requests
except Exception:  # pragma: no cover - requests missing in sandboxed runs
    requests = None  # type: ignore[assignment]

from gdx_dispatch.tools.authentik_property_mapping_gdx_tid import (
    CLAIM_SCOPE_NAME,
    SANDBOX_EXPRESSION,
)

logger = logging.getLogger("gdx_dispatch.tools.configure_authentik")

GDX_SPA_PROVIDER_NAME = "gdx-spa"
GDX_SPA_AUDIENCE = "gdx-api"
GDX_SPA_ACCESS_TOKEN_VALIDITY = "minutes=15"  # noqa: S105  # token validity string, not a credential
GDX_SPA_REFRESH_TOKEN_VALIDITY = "days=30"  # noqa: S105  # token validity string, not a credential
GDX_SPA_AUTH_CODE_VALIDITY = "minutes=1"
GDX_SPA_REDIRECT_URI = "https://app.example.com/auth/callback"
GDX_SPA_SIGNING_KEY_NAME = "gdx-spa-signing-key"
GDX_SPA_APPLICATION_SLUG = "gdx-spa"
GDX_SPA_APPLICATION_NAME = "app-gdx-spa"

GDX_THIRDPARTY_PROVIDER_NAME = "gdx-thirdparty"
GDX_THIRDPARTY_AUDIENCE = "gdx-api"
GDX_THIRDPARTY_ACCESS_TOKEN_VALIDITY = "hours=1"  # noqa: S105  # token validity string, not a credential
GDX_THIRDPARTY_REFRESH_TOKEN_VALIDITY = "days=90"  # noqa: S105  # token validity string, not a credential
GDX_THIRDPARTY_AUTH_CODE_VALIDITY = "minutes=1"
GDX_THIRDPARTY_REDIRECT_URI = "https://integrations.example.com/oauth/callback"
GDX_THIRDPARTY_SIGNING_KEY_NAME = "gdx-thirdparty-signing-key"
GDX_THIRDPARTY_APPLICATION_SLUG = "gdx-thirdparty"
GDX_THIRDPARTY_APPLICATION_NAME = "app-gdx-thirdparty"

DEFAULT_AUTHZ_FLOW_SLUG = "default-provider-authorization-implicit-consent"
SUPPORTED_PROVIDERS = ("gdx-spa", "gdx-thirdparty")


class AuthentikClient:
    """Thin wrapper over the Authentik admin API."""

    def __init__(self, base_url: str, token: str) -> None:
        if requests is None:  # pragma: no cover - explicit guard
            raise RuntimeError(
                "python-requests is required for live Authentik configuration"
            )
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v3/{path.lstrip('/')}"

    def get_or_create(
        self,
        endpoint: str,
        search_field: str,
        search_value: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Return existing record where ``search_field == search_value`` or POST to create."""
        r = self.session.get(self._url(endpoint), params={search_field: search_value})
        r.raise_for_status()
        for result in r.json().get("results", []):
            if result.get(search_field) == search_value:
                logger.info("existing %s %s=%s found, skipping create",
                            endpoint, search_field, search_value)
                return result
        logger.info("creating %s %s=%s", endpoint, search_field, search_value)
        r = self.session.post(self._url(endpoint), json=payload)
        r.raise_for_status()
        return r.json()

    def default_authorization_flow_pk(self) -> str:
        r = self.session.get(self._url("flows/instances/"),
                             params={"slug": DEFAULT_AUTHZ_FLOW_SLUG})
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            raise RuntimeError(
                f"Authentik flow '{DEFAULT_AUTHZ_FLOW_SLUG}' not found"
            )
        return results[0]["pk"]

    def default_oidc_mapping_pks(self) -> list[str]:
        r = self.session.get(self._url("propertymappings/scope/"),
                             params={"ordering": "scope_name"})
        r.raise_for_status()
        by_scope = {m["scope_name"]: m["pk"] for m in r.json().get("results", [])}
        return [by_scope[s] for s in ("openid", "profile", "email") if s in by_scope]


def build_gdx_spa_provider_payload(
    *,
    signing_key_pk: str,
    property_mapping_pks: list[str],
    authorization_flow_pk: str,
) -> dict[str, Any]:
    """Return the provider payload for gdx-spa.

    Exposed as a pure function so dry-run and tests can inspect the shape
    without hitting the network.
    """
    return {
        "name": GDX_SPA_PROVIDER_NAME,
        "authorization_flow": authorization_flow_pk,
        "client_type": "confidential",
        "client_id": "gdx-spa",
        "access_code_validity": GDX_SPA_AUTH_CODE_VALIDITY,
        "access_token_validity": GDX_SPA_ACCESS_TOKEN_VALIDITY,
        "refresh_token_validity": GDX_SPA_REFRESH_TOKEN_VALIDITY,
        "include_claims_in_id_token": True,
        "issuer_mode": "global",
        "sub_mode": "user_id",
        "signing_key": signing_key_pk,
        "redirect_uris": GDX_SPA_REDIRECT_URI,
        "property_mappings": list(property_mapping_pks),
        "pkce_mode": "required",
        "response_types": ["code"],
        "audience": GDX_SPA_AUDIENCE,
    }


def build_gdx_thirdparty_provider_payload(
    *,
    signing_key_pk: str,
    property_mapping_pks: list[str],
    authorization_flow_pk: str,
) -> dict[str, Any]:
    """Return the provider payload for gdx-thirdparty.

    SS-6 Slice C contract — OAuth 2.1 authorization-code + S256 PKCE with
    1-hour access tokens and 90-day refresh tokens for Zapier-class
    integrations (SS-21). Audience is ``gdx-api`` (same API as the SPA —
    SS-7 validates both via shared JWKS); signing key, redirect URI, and
    client_id are distinct from gdx-spa so the two providers can rotate
    independently and fail independently.
    """
    return {
        "name": GDX_THIRDPARTY_PROVIDER_NAME,
        "authorization_flow": authorization_flow_pk,
        "client_type": "confidential",
        "client_id": "gdx-thirdparty",
        "access_code_validity": GDX_THIRDPARTY_AUTH_CODE_VALIDITY,
        "access_token_validity": GDX_THIRDPARTY_ACCESS_TOKEN_VALIDITY,
        "refresh_token_validity": GDX_THIRDPARTY_REFRESH_TOKEN_VALIDITY,
        "include_claims_in_id_token": True,
        "issuer_mode": "global",
        "sub_mode": "user_id",
        "signing_key": signing_key_pk,
        "redirect_uris": GDX_THIRDPARTY_REDIRECT_URI,
        "property_mappings": list(property_mapping_pks),
        "pkce_mode": "required",
        "response_types": ["code"],
        "audience": GDX_THIRDPARTY_AUDIENCE,
    }


def configure_gdx_spa(client: AuthentikClient) -> dict[str, Any]:
    """Idempotently configure the ``gdx-spa`` provider + supporting artifacts."""
    signing_key = client.get_or_create(
        "crypto/certificatekeypairs/",
        "name",
        GDX_SPA_SIGNING_KEY_NAME,
        {"name": GDX_SPA_SIGNING_KEY_NAME, "managed": GDX_SPA_PROVIDER_NAME},
    )

    property_mapping = client.get_or_create(
        "propertymappings/scope/",
        "name",
        CLAIM_SCOPE_NAME,
        {
            "name": CLAIM_SCOPE_NAME,
            "scope_name": CLAIM_SCOPE_NAME,
            "description": "Emits gdx_tid for the user's active tenant (D-5).",
            "expression": SANDBOX_EXPRESSION,
        },
    )

    authz_flow_pk = client.default_authorization_flow_pk()
    mapping_pks = [property_mapping["pk"]] + client.default_oidc_mapping_pks()

    provider_payload = build_gdx_spa_provider_payload(
        signing_key_pk=signing_key["pk"],
        property_mapping_pks=mapping_pks,
        authorization_flow_pk=authz_flow_pk,
    )
    provider = client.get_or_create(
        "providers/oauth2/",
        "name",
        GDX_SPA_PROVIDER_NAME,
        provider_payload,
    )

    application = client.get_or_create(
        "core/applications/",
        "slug",
        GDX_SPA_APPLICATION_SLUG,
        {
            "name": GDX_SPA_APPLICATION_NAME,
            "slug": GDX_SPA_APPLICATION_SLUG,
            "provider": provider["pk"],
            "policy_engine_mode": "any",
        },
    )

    return {
        "provider": GDX_SPA_PROVIDER_NAME,
        "signing_key_pk": signing_key["pk"],
        "property_mapping_pk": property_mapping["pk"],
        "provider_pk": provider["pk"],
        "application_pk": application["pk"],
    }


def configure_gdx_thirdparty(client: AuthentikClient) -> dict[str, Any]:
    """Idempotently configure the ``gdx-thirdparty`` provider + supporting artifacts.

    Reuses the shared ``gdx_tid`` scope mapping (same tenant claim as
    ``gdx-spa``) but provisions a dedicated signing key and Application so
    third-party token issuance is isolated from the SPA path — rotating or
    revoking the third-party key must not invalidate SPA-issued tokens.
    """
    signing_key = client.get_or_create(
        "crypto/certificatekeypairs/",
        "name",
        GDX_THIRDPARTY_SIGNING_KEY_NAME,
        {
            "name": GDX_THIRDPARTY_SIGNING_KEY_NAME,
            "managed": GDX_THIRDPARTY_PROVIDER_NAME,
        },
    )

    property_mapping = client.get_or_create(
        "propertymappings/scope/",
        "name",
        CLAIM_SCOPE_NAME,
        {
            "name": CLAIM_SCOPE_NAME,
            "scope_name": CLAIM_SCOPE_NAME,
            "description": "Emits gdx_tid for the user's active tenant (D-5).",
            "expression": SANDBOX_EXPRESSION,
        },
    )

    authz_flow_pk = client.default_authorization_flow_pk()
    mapping_pks = [property_mapping["pk"]] + client.default_oidc_mapping_pks()

    provider_payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk=signing_key["pk"],
        property_mapping_pks=mapping_pks,
        authorization_flow_pk=authz_flow_pk,
    )
    provider = client.get_or_create(
        "providers/oauth2/",
        "name",
        GDX_THIRDPARTY_PROVIDER_NAME,
        provider_payload,
    )

    application = client.get_or_create(
        "core/applications/",
        "slug",
        GDX_THIRDPARTY_APPLICATION_SLUG,
        {
            "name": GDX_THIRDPARTY_APPLICATION_NAME,
            "slug": GDX_THIRDPARTY_APPLICATION_SLUG,
            "provider": provider["pk"],
            "policy_engine_mode": "any",
        },
    )

    return {
        "provider": GDX_THIRDPARTY_PROVIDER_NAME,
        "signing_key_pk": signing_key["pk"],
        "property_mapping_pk": property_mapping["pk"],
        "provider_pk": provider["pk"],
        "application_pk": application["pk"],
    }


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        required=True,
        choices=SUPPORTED_PROVIDERS,
        help=(
            "Which provider to configure. SS-6 supports gdx-spa (Slice A) and "
            "gdx-thirdparty (Slice C). gdx-mcp is PAT-only (SS-14/SS-19) and "
            "is not an accepted choice."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "AUTHENTIK_BASE_URL", "https://auth.example.com"
        ),
        help="Authentik base URL (default: $AUTHENTIK_BASE_URL).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the provider payload that would be POSTed; no HTTP calls.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log each get-or-create decision.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.provider not in SUPPORTED_PROVIDERS:  # pragma: no cover - argparse guard
        print(f"ERROR: unsupported --provider {args.provider!r}", file=sys.stderr)
        return 2

    payload_builders = {
        "gdx-spa": build_gdx_spa_provider_payload,
        "gdx-thirdparty": build_gdx_thirdparty_provider_payload,
    }
    configurators = {
        "gdx-spa": configure_gdx_spa,
        "gdx-thirdparty": configure_gdx_thirdparty,
    }

    if args.dry_run:
        payload = payload_builders[args.provider](
            signing_key_pk="<dry-run-signing-key>",
            property_mapping_pks=["<dry-run-gdx_tid-pk>"],
            authorization_flow_pk="<dry-run-authz-flow-pk>",
        )
        print(json.dumps({"provider_payload": payload}, indent=2, sort_keys=True))
        return 0

    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN")
    if not token:
        print("ERROR: AUTHENTIK_BOOTSTRAP_TOKEN not set in environment",
              file=sys.stderr)
        return 1

    client = AuthentikClient(args.base_url, token)
    result = configurators[args.provider](client)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
