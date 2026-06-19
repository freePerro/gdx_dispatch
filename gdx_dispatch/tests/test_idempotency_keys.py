"""Tests for gdx_dispatch.core.middleware.idempotency_keys — pure helper contract."""

from gdx_dispatch.core.middleware.idempotency_keys import (
    IDEMPOTENCY_TTL_SECONDS,
    REDIS_DB,
    build_cache_key,
    is_cacheable_status,
)


def test_build_cache_key_deterministic():
    """Same inputs twice must produce the same key. Also pins constants."""
    assert IDEMPOTENCY_TTL_SECONDS == 86400
    assert REDIS_DB == 7
    a = build_cache_key("tenant-1", "identity-1", "key-abc", "/v1/work_orders")
    b = build_cache_key("tenant-1", "identity-1", "key-abc", "/v1/work_orders")
    assert a == b


def test_build_cache_key_differs_on_any_field():
    """Changing any one of the 4 inputs must change the key."""
    base = build_cache_key("tenant-1", "identity-1", "key-abc", "/v1/work_orders")

    diff_tenant = build_cache_key("tenant-2", "identity-1", "key-abc", "/v1/work_orders")
    diff_identity = build_cache_key("tenant-1", "identity-2", "key-abc", "/v1/work_orders")
    diff_key = build_cache_key("tenant-1", "identity-1", "key-xyz", "/v1/work_orders")
    diff_path = build_cache_key("tenant-1", "identity-1", "key-abc", "/v1/invoices")

    assert diff_tenant != base
    assert diff_identity != base
    assert diff_key != base
    assert diff_path != base


def test_build_cache_key_prefix_and_length():
    """Output must be `idempotency:` + 64 hex chars = 76 total."""
    key = build_cache_key("t", "i", "k", "/p")
    assert key.startswith("idempotency:")
    assert len(key) == 12 + 64  # prefix (12) + sha256 hex digest (64)
    # And the tail after the prefix is a valid 64-char lowercase hex digest.
    digest = key[len("idempotency:"):]
    assert len(digest) == 64
    int(digest, 16)  # raises if non-hex


def test_is_cacheable_status():
    """2xx is cacheable; everything else is not."""
    assert is_cacheable_status(200) is True
    assert is_cacheable_status(201) is True
    assert is_cacheable_status(299) is True

    assert is_cacheable_status(199) is False
    assert is_cacheable_status(300) is False
    assert is_cacheable_status(400) is False
    assert is_cacheable_status(500) is False
