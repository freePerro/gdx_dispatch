"""Sprint MCP-Streamable-HTTP S4 — bearer-token mint + verify for /mcp.

The MCP transport at ``<tenant-host>/mcp`` accepts JWT bearer tokens
whose claims tie the token to a single tenant. Three claims are
load-bearing for cross-tenant safety:

* ``iss``     — must equal ``https://<tenant-host>``
* ``aud``     — must equal ``https://<tenant-host>/mcp``
* ``gdx_tid`` — must equal the tenant's UUID (matches what
  ``TenantMiddleware`` resolves from the host header)

The transport-side ASGI middleware re-derives all three from the
inbound request (host + ``request.state.tenant["id"]``) and rejects
any mismatch. A token minted at ``gdx.*`` cannot be replayed against
another tenant's ``/mcp`` because both ``aud`` and ``gdx_tid`` will
fail to match. This is the load-bearing invariant the sprint plan
flagged as a verification gate.

Signing keys reuse the same env-var contract as ``gdx_dispatch/routers/auth/core.py``:

* ``RS_PRIVATE_KEY`` + ``RS_PUBLIC_KEY``  → RS256 (preferred)
* ``JWT_SECRET`` (≥ 32 bytes)             → HS256

Refusing to start when neither is configured is auth/core.py's job;
this module is a consumer, not a key configurator.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from jwt.exceptions import InvalidTokenError


# ── exceptions ──────────────────────────────────────────────────────────────


class MCPBearerError(Exception):
    """Base class for MCP-bearer mint/verify failures."""


class BearerInvalid(MCPBearerError):
    """Token is malformed, expired, signed by an unexpected key, or has
    an ``aud``/``iss``/``gdx_tid`` mismatch with the inbound request."""


class BearerKeyNotConfigured(MCPBearerError):
    """No JWT signing/verification key is configured in the environment."""


# ── key resolution (mirror of gdx_dispatch/routers/auth/core.py) ──────────────────────────


def _resolve_keys() -> tuple[str, str, str]:
    """Return ``(sign_key, verify_key, alg)`` from env. Raises if unset.

    Imported lazily so test fixtures can set ``JWT_SECRET`` before this
    module's first use without import-time failures elsewhere.
    """
    priv = os.environ.get("RS_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    pub = os.environ.get("RS_PUBLIC_KEY", "").replace("\\n", "\n").strip()
    secret = os.environ.get("JWT_SECRET", "").strip()
    if priv:
        return priv, (pub or priv), "RS256"
    if secret:
        if len(secret) < 32:
            raise BearerKeyNotConfigured(
                f"JWT_SECRET is only {len(secret)} bytes; HS256 requires ≥ 32"
            )
        return secret, secret, "HS256"
    raise BearerKeyNotConfigured(
        "no MCP-bearer signing key — set RS_PRIVATE_KEY+RS_PUBLIC_KEY "
        "(preferred) or JWT_SECRET (≥ 32 bytes)"
    )


# ── claim shape ─────────────────────────────────────────────────────────────

ACCESS_TOKEN_TTL_SECONDS = 3600  # 1h, matches /oauth/token default


@dataclass(frozen=True)
class MCPClaims:
    """Verified, host-bound claims extracted from an MCP bearer token."""

    sub: str
    tenant_id: str
    issuer: str
    audience: str
    scope: str
    jti: str
    issued_at: int
    expires_at: int
    raw: dict[str, Any]

    @property
    def has_scope(self) -> bool:
        return bool(self.scope)


# ── mint ────────────────────────────────────────────────────────────────────


def mint_mcp_access_token(
    *,
    tenant_id: str | uuid.UUID,
    subject_id: str,
    issuer: str,
    audience: str,
    scope: str = "mcp:invoke",
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
    extra: dict[str, Any] | None = None,
) -> str:
    """Mint a JWT bearer token suitable for the MCP transport.

    ``issuer`` and ``audience`` MUST be derived from the request host
    at issuance time (``https://<host>`` and ``https://<host>/mcp``).
    Mismatched issuance host is the entire bug class S4 prevents — the
    helper does not "fix up" inputs, it signs whatever the caller
    provides. Caller-side correctness is enforced by the
    ``/oauth/token`` route.
    """
    sign_key, _verify_key, alg = _resolve_keys()
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer.rstrip("/"),
        "aud": audience.rstrip("/"),
        "sub": subject_id,
        "gdx_tid": str(tenant_id),
        "scope": scope,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
        "typ": "mcp_access",
    }
    if extra:
        for k, v in extra.items():
            claims.setdefault(k, v)
    return jwt.encode(claims, sign_key, algorithm=alg)


# ── verify ──────────────────────────────────────────────────────────────────


def verify_mcp_bearer(
    token: str,
    *,
    expected_issuer: str,
    expected_audience: str,
    expected_tenant_id: str | uuid.UUID,
) -> MCPClaims:
    """Decode + verify a bearer JWT against the inbound request's host.

    Three load-bearing checks (any failure → ``BearerInvalid``):

    1. Signature valid + token unexpired (PyJWT does this).
    2. ``iss`` exactly equals ``expected_issuer``.
    3. ``aud`` exactly equals ``expected_audience`` (PyJWT does the
       comparison; we still re-check explicitly to catch the case
       where ``aud`` is a list and one entry happens to match — MCP
       tokens are single-audience by design).
    4. ``gdx_tid`` exactly equals ``expected_tenant_id`` (string compare).

    Any caller (transport middleware, future tooling) MUST pass the
    expected values derived from the request — never from the token.
    """
    _sign_key, verify_key, alg = _resolve_keys()
    try:
        claims = jwt.decode(
            token,
            verify_key,
            algorithms=[alg],
            audience=expected_audience.rstrip("/"),
            issuer=expected_issuer.rstrip("/"),
            options={"require": ["exp", "iss", "aud", "sub", "gdx_tid"]},
        )
    except InvalidTokenError as exc:
        raise BearerInvalid(f"jwt decode failed: {exc}") from exc

    aud = claims.get("aud")
    if isinstance(aud, list):
        if expected_audience.rstrip("/") not in [a.rstrip("/") for a in aud]:
            raise BearerInvalid("aud claim does not include expected audience")
        if len(aud) != 1:
            raise BearerInvalid(
                "MCP tokens must be single-audience; got list len="
                f"{len(aud)}"
            )

    token_tid = str(claims.get("gdx_tid", ""))
    expected_tid_s = str(expected_tenant_id)
    if token_tid != expected_tid_s:
        raise BearerInvalid(
            f"gdx_tid mismatch: token={token_tid!r} expected={expected_tid_s!r}"
        )

    return MCPClaims(
        sub=str(claims["sub"]),
        tenant_id=token_tid,
        issuer=str(claims["iss"]),
        audience=str(claims["aud"]),
        scope=str(claims.get("scope", "")),
        jti=str(claims.get("jti", "")),
        issued_at=int(claims["iat"]) if "iat" in claims else 0,
        expires_at=int(claims["exp"]),
        raw=claims,
    )
