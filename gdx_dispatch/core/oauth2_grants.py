"""
gdx_dispatch/core/oauth2_grants.py — SS-21 OAuth2 authorization-code storage + PKCE verifier.

SS 0.9-h (2026-04-20): authorization-code store is now Redis-backed. The public
surface (``mint_authorization_code`` / ``consume_authorization_code``) is
unchanged — the swap from the in-process TTL dict to Redis is contained in
``_RedisCodeStore``. Tests may inject ``fakeredis.FakeRedis(decode_responses=True)``
via :func:`get_code_store` override.

Responsibilities:
    * Mint + persist authorization codes with a 60s TTL.
    * Atomically consume (single-use) — double redemption is a replay attack
      and MUST fail on the second attempt per RFC 6749 §10.5.
    * PKCE S256 verification per RFC 7636 §4.6 (S256 ONLY — plain rejected
      upstream at /authorize).

Key scheme:
    gdx:oauth:code:<code_value>  →  JSON(AuthCodeRecord)   TTL=60s (SETEX)

Atomicity:
    * ``consume`` uses ``GETDEL`` (redis-py ≥ 4.1; our pin is redis>=7.4.0) for a
      single-round-trip atomic get+delete. A second redemption therefore
      returns ``None`` — replay rejected by construction.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

# SS 0.9-h — Redis swap complete. Kept as module-level flag so deploy gates can
# grep for it. (Was False under the in-memory implementation.)
_PRODUCTION_READY = True

# ---------------------------------------------------------------------------
# PKCE — RFC 7636
# ---------------------------------------------------------------------------

PKCE_METHOD_S256 = "S256"


def compute_s256_challenge(verifier: str) -> str:
    """Return the S256 code_challenge for a given code_verifier.

    challenge = BASE64URL(SHA256(ASCII(code_verifier)))  -- no padding.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    """Constant-time compare of the recomputed challenge against the stored one."""
    if not verifier or not challenge:
        return False
    computed = compute_s256_challenge(verifier)
    return hmac.compare_digest(computed, challenge)


# ---------------------------------------------------------------------------
# Authorization code record
# ---------------------------------------------------------------------------


@dataclass
class AuthCodeRecord:
    code: str
    client_id: str
    redirect_uri: str
    scope: str
    # Installation / consent context
    tenant_id: str | None
    subject_id: str | None  # user / principal id (None for admin-consent)
    # PKCE
    code_challenge: str | None
    code_challenge_method: str | None
    # Lifecycle
    expires_at: float
    consumed: bool = False
    # Extra data (admin_consent flag, etc)
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Redis client — lazy singleton
# ---------------------------------------------------------------------------

_redis_client = None


def _get_redis():
    """Lazy real-Redis singleton. Tests override by supplying their own store
    to :func:`get_code_store` / passing ``store=`` to the public helpers."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    import redis as redis_lib  # local import — keeps module import cheap

    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    _redis_client = redis_lib.from_url(
        url, decode_responses=True, socket_connect_timeout=2
    )
    return _redis_client


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


_CODE_KEY_PREFIX = "gdx:oauth:code:"


def _code_key(code: str) -> str:
    return f"{_CODE_KEY_PREFIX}{code}"


class _RedisCodeStore:
    """Redis-backed single-use authorization-code store.

    Constructor-injection of the Redis client (BYO-Redis) per the pattern used
    by ``gdx_dispatch.core.middleware.idempotency.IdempotencyMiddleware``. Pass any
    object implementing ``setex`` / ``get`` / ``getdel`` / ``delete`` /
    ``keys`` (real redis-py client OR ``fakeredis.FakeRedis(decode_responses=True)``).
    """

    def __init__(self, redis_client) -> None:
        if redis_client is None:
            raise ValueError("redis_client is required")
        self._r = redis_client

    def put(self, rec: AuthCodeRecord) -> None:
        ttl = max(1, int(rec.expires_at - time.time()))
        self._r.setex(_code_key(rec.code), ttl, json.dumps(asdict(rec)))

    def get(self, code: str) -> AuthCodeRecord | None:
        raw = self._r.get(_code_key(code))
        if raw is None:
            return None
        return AuthCodeRecord(**json.loads(raw))

    def consume(self, code: str) -> AuthCodeRecord | None:
        """Atomic single-use consume via Redis GETDEL.

        Second call for the same code returns None — replay rejected.
        """
        raw = self._r.getdel(_code_key(code))
        if raw is None:
            return None
        rec = AuthCodeRecord(**json.loads(raw))
        # TTL would have evicted expired entries; defensive time check too.
        if rec.expires_at < time.time():
            return None
        rec.consumed = True
        return rec

    def clear(self) -> None:
        """Wipe every code key. Test-helper only."""
        for k in list(self._r.scan_iter(match=f"{_CODE_KEY_PREFIX}*")):
            self._r.delete(k)


# Module-level singleton — constructed lazily so importing this module does
# NOT require a live Redis. Tests inject a fakeredis client with
# ``set_code_store_for_tests``.
_default_store: _RedisCodeStore | None = None


def get_code_store() -> _RedisCodeStore:
    global _default_store
    if _default_store is None:
        _default_store = _RedisCodeStore(_get_redis())
    return _default_store


def set_code_store_for_tests(store: _RedisCodeStore | None) -> None:
    """Override the module-level store (tests only)."""
    global _default_store
    _default_store = store


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

DEFAULT_CODE_TTL_SECONDS = 60


def mint_authorization_code(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    tenant_id: str | None = None,
    subject_id: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    ttl_seconds: int = DEFAULT_CODE_TTL_SECONDS,
    extra: dict | None = None,
    store: _RedisCodeStore | None = None,
) -> str:
    """Create and persist an authorization code. Returns the opaque code string."""
    code = secrets.token_urlsafe(32)
    rec = AuthCodeRecord(
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        tenant_id=tenant_id,
        subject_id=subject_id,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        expires_at=time.time() + ttl_seconds,
        extra=extra or {},
    )
    (store or get_code_store()).put(rec)
    return code


def consume_authorization_code(
    code: str, store: _RedisCodeStore | None = None
) -> AuthCodeRecord | None:
    return (store or get_code_store()).consume(code)


def peek_authorization_code(
    code: str, store: _RedisCodeStore | None = None
) -> AuthCodeRecord | None:
    return (store or get_code_store()).get(code)


# ---------------------------------------------------------------------------
# Token-exchange helpers
# ---------------------------------------------------------------------------


def validate_redemption(
    rec: AuthCodeRecord,
    *,
    client_id: str,
    redirect_uri: str,
    code_verifier: str | None,
) -> tuple[bool, str | None]:
    """Return (ok, error) where error is an RFC 6749 error code string.

    Checks (in order):
      1. client_id matches
      2. redirect_uri matches exactly
      3. PKCE verifier matches (S256 only; missing verifier when challenge was
         supplied → error)
    """
    if rec.client_id != client_id:
        return False, "invalid_client"
    if rec.redirect_uri != redirect_uri:
        return False, "invalid_grant"
    if rec.code_challenge:
        # PKCE was used at /authorize — verifier is REQUIRED at /token.
        if rec.code_challenge_method != PKCE_METHOD_S256:
            # Defense-in-depth: /authorize should have rejected non-S256 already
            return False, "invalid_request"
        if not code_verifier:
            return False, "invalid_grant"
        if not verify_pkce_s256(code_verifier, rec.code_challenge):
            return False, "invalid_grant"
    return True, None


__all__ = [
    "AuthCodeRecord",
    "DEFAULT_CODE_TTL_SECONDS",
    "PKCE_METHOD_S256",
    "_PRODUCTION_READY",
    "_RedisCodeStore",
    "compute_s256_challenge",
    "consume_authorization_code",
    "get_code_store",
    "mint_authorization_code",
    "peek_authorization_code",
    "set_code_store_for_tests",
    "validate_redemption",
    "verify_pkce_s256",
]
