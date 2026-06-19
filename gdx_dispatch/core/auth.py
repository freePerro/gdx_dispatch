"""SS-7 Slice E — auth-core composition surface.

Two responsibilities, intentionally narrow:

* Re-export the legacy :func:`gdx_dispatch.routers.auth.get_current_user` so existing
  router-level dependency injection keeps resolving to the real JWT decoder.
  Slice E does NOT modify that path — router migration is Slice F.
* Expose :func:`validate_principal`, a pure auth-core wrapper that composes
  Slice A's :func:`gdx_dispatch.core.auth_jwt.validate_access_token` with Slice C's
  :class:`gdx_dispatch.core.denylist.Denylist` via Slice D's optional ``denylist=``
  parameter. The function is FastAPI-free (no ``Depends``, no ``Request``,
  no ``app.state``) so it can be unit-tested without a running app and so
  Slice F can wire it into the router with explicit dependencies rather
  than ambient state.

The denylist check stays an *authentication* concern (is this token still
valid?) and is deliberately separated from the *authorization* concern
answered by :mod:`gdx_dispatch.core.policy` — this module does not import the
policy evaluator.
"""
from __future__ import annotations

from collections.abc import Mapping

from gdx_dispatch.core.auth_jwt import validate_access_token
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.principal import Principal
from gdx_dispatch.routers.auth import get_current_user  # noqa: F401  (legacy re-export)

__all__ = ["get_current_user", "validate_principal"]


def validate_principal(
    token: str,
    *,
    public_keys_by_provider: Mapping[str, bytes | str],
    leeway_seconds: int = 0,
    denylist: Denylist | None = None,
) -> Principal:
    """Validate ``token`` and return a :class:`Principal`, optionally denylist-aware.

    Thin composition layer over :func:`gdx_dispatch.core.auth_jwt.validate_access_token`:
    every argument forwards verbatim, every typed
    :class:`gdx_dispatch.core.auth_jwt.JWTValidationError` (including
    :class:`gdx_dispatch.core.auth_jwt.TokenRevoked` from the Slice D pre-check)
    propagates unchanged so middleware can keep its single ``except``
    handler.

    Parameters
    ----------
    token:
        Encoded JWT (compact serialization).
    public_keys_by_provider:
        PEM-encoded RSA public keys keyed by SS-6 provider slug. Callers
        supply the trusted keys explicitly — this slice does not fetch
        JWKS.
    leeway_seconds:
        Clock skew allowance forwarded to ``exp``/``nbf`` enforcement.
    denylist:
        Optional Slice C :class:`Denylist`. When supplied and the token
        carries a non-empty ``jti`` listed on it, validation raises
        :class:`gdx_dispatch.core.auth_jwt.TokenRevoked`. When omitted (the
        default), behaviour matches Slice A exactly — no revocation
        check occurs.

    Returns
    -------
    Principal
        Built only from cryptographically verified claims.
    """
    return validate_access_token(
        token,
        public_keys_by_provider=public_keys_by_provider,
        leeway_seconds=leeway_seconds,
        denylist=denylist,
    )
