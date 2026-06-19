"""Idempotency cache-key helper — pure, stdlib-only.

Part of SS-14 (PATs + idempotency). This module intentionally holds
ONLY the hashing contract and cacheability predicate so it can be
imported and unit-tested without pulling Starlette, Redis, or any
framework code into scope. The Starlette middleware class that
consumes these helpers lives in a sibling module (ss14-b scope).

Cache-key contract:
    key = "idempotency:" + sha256_hex(
        f"{tenant_id}:{identity_id}:{idempotency_key}:{path}"
    )

The four-field composite guarantees that replays of the same
Idempotency-Key by a different tenant, identity, or against a
different path do NOT collide in the shared Redis namespace.
"""

import hashlib


IDEMPOTENCY_TTL_SECONDS = 86400  # 24h — default TTL for cached responses
REDIS_DB = 7                      # dedicated Redis logical DB for idempotency


def build_cache_key(
    tenant_id: str,
    identity_id: str,
    idempotency_key: str,
    path: str,
) -> str:
    """Return the canonical Redis cache key for an idempotent request.

    Components are joined with ``:`` separators, UTF-8 encoded, and
    SHA-256 hashed. The ``idempotency:`` prefix keeps the namespace
    obvious when inspecting Redis.
    """
    raw = f"{tenant_id}:{identity_id}:{idempotency_key}:{path}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"idempotency:{digest}"


def is_cacheable_status(status_code: int) -> bool:
    """True iff the response status is in the 2xx success range."""
    return 200 <= status_code < 300
