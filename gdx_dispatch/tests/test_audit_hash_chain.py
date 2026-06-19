"""SS-28 slice B tests — tamper-evident audit hash chain.

Covers:
* canonical_json is byte-stable across equivalent dict orderings
* compute_row_hash produces a 64-char sha256 hex
* verify_chain returns (True, -1) for an intact chain
* verify_chain identifies the FIRST broken row when a field is mutated
* hashes_equal uses constant-time compare

Runs in pure-memory mode — no DB session needed. The DB-backed branch
of verify_chain is exercised in test_platform_audit.py where an
SQLite session is stood up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from gdx_dispatch.core.audit_hash_chain import (
    HASHED_FIELDS,
    ZERO_HASH,
    canonical_json,
    compute_row_hash,
    hashes_equal,
    verify_chain,
)


def _row(**overrides):
    base = {
        "id": uuid4(),
        "tenant_id": "tenant-a",
        "principal_identity_id": "ident-1",
        "action": "api.call",
        "resource_type": "job",
        "resource_id": "job-1",
        "result": "ok",
        "details": {"k": 1},
        "ip_address": "10.0.0.1",
        "user_agent": "ua",
        "created_at": datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def test_zero_hash_is_64_hex_zeros():
    assert ZERO_HASH == "0" * 64
    assert len(ZERO_HASH) == 64


def test_canonical_json_is_order_independent():
    a = canonical_json({"b": 2, "a": 1, "c": {"y": 2, "x": 1}})
    b = canonical_json({"a": 1, "c": {"x": 1, "y": 2}, "b": 2})
    assert a == b


def test_canonical_json_handles_uuid_datetime():
    out = canonical_json(
        {"id": uuid4(), "ts": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    )
    # Must be a string; exact contents not asserted (depends on UUID4).
    assert isinstance(out, str)
    # Datetime normalized to UTC then emitted without tz offset (so the
    # digest is identical on sqlite round-trips, which drop tzinfo).
    assert "2026-01-01T00:00:00" in out
    assert "+00:00" not in out


def test_compute_row_hash_is_sha256_hex():
    h = compute_row_hash(_row(), ZERO_HASH)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_row_hash_deterministic():
    r = _row()
    h1 = compute_row_hash(r, ZERO_HASH)
    h2 = compute_row_hash(r, ZERO_HASH)
    assert hashes_equal(h1, h2)


def test_compute_row_hash_changes_with_prev():
    r = _row()
    h1 = compute_row_hash(r, ZERO_HASH)
    h2 = compute_row_hash(r, "a" * 64)
    assert not hashes_equal(h1, h2)


def test_compute_row_hash_rejects_bad_prev_hash():
    with pytest.raises(ValueError):
        compute_row_hash(_row(), "short")


def test_hashed_fields_stable_set():
    # Regression guard: a future edit that adds/removes a field from the
    # hash set breaks every prior verification. If you intentionally
    # rotate the schema, bump this list and ship a re-hash migration.
    assert set(HASHED_FIELDS) == {
        "id",
        "tenant_id",
        "principal_identity_id",
        "action",
        "resource_type",
        "resource_id",
        "result",
        "details",
        "ip_address",
        "user_agent",
        "created_at",
        "prev_hash",
    }


def _build_chain(n: int, tenant="tenant-a"):
    """Build an in-memory intact chain of n rows."""
    rows = []
    prev = ZERO_HASH
    for i in range(n):
        r = _row(tenant_id=tenant, resource_id=f"r-{i}")
        r["prev_hash"] = prev
        r["row_hash"] = compute_row_hash(r, prev)
        rows.append(r)
        prev = r["row_hash"]
    return rows


def test_verify_chain_empty_is_valid():
    valid, break_at = verify_chain(None, "tenant-a", rows=[])
    assert valid is True
    assert break_at == -1


def test_verify_chain_intact():
    rows = _build_chain(5)
    valid, break_at = verify_chain(None, "tenant-a", rows=rows)
    assert valid is True
    assert break_at == -1


def test_verify_chain_detects_field_mutation():
    """Tamper-detection: mutate row 2's action, verify_chain flags it."""
    rows = _build_chain(5)
    # Attacker mutates the stored field but NOT the row_hash.
    rows[2]["action"] = "api.call.tampered"
    valid, break_at = verify_chain(None, "tenant-a", rows=rows)
    assert valid is False
    assert break_at == 2


def test_verify_chain_detects_prev_hash_mutation():
    rows = _build_chain(4)
    rows[1]["prev_hash"] = "f" * 64
    valid, break_at = verify_chain(None, "tenant-a", rows=rows)
    assert valid is False
    assert break_at == 1


def test_verify_chain_detects_row_hash_mutation():
    rows = _build_chain(3)
    rows[2]["row_hash"] = "e" * 64
    valid, break_at = verify_chain(None, "tenant-a", rows=rows)
    assert valid is False
    assert break_at == 2


def test_hashes_equal_constant_time():
    # Not a true timing test — just verifies semantics.
    assert hashes_equal("a" * 64, "a" * 64) is True
    assert hashes_equal("a" * 64, "b" * 64) is False
    assert hashes_equal(None, "a" * 64) is False
    assert hashes_equal("a" * 64, None) is False
