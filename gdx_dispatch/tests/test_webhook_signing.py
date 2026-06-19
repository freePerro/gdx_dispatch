"""
gdx_dispatch/tests/test_webhook_signing.py — SS-21 webhook signing tests.

Covers:
  - compute_v1_signature: deterministic, matches stdlib hmac expectation
  - build_signature_header: single secret, dual-active (old+new)
  - parse_signature_header: roundtrip
  - verify_signature: accepts either secret during rotation
  - verify_signature: rejects tampered body, wrong secret, old timestamp
  - constant-time comparison (smoke — hard to prove, but verify rejects)
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from gdx_dispatch.core.webhook_signing import (
    SIGNATURE_HEADER,
    SigningSecret,
    build_signature_header,
    compute_v1_signature,
    parse_signature_header,
    verify_signature,
)


def test_compute_v1_signature_deterministic():
    s = b"shhh"
    body = b'{"event":"job.created"}'
    sig1 = compute_v1_signature(s, 1700000000, body)
    sig2 = compute_v1_signature(s, 1700000000, body)
    assert sig1 == sig2
    # matches manual hmac
    expected = hmac.new(s, b"1700000000." + body, hashlib.sha256).hexdigest()
    assert sig1 == expected


def test_compute_rejects_str_secret():
    with pytest.raises(TypeError):
        compute_v1_signature("not-bytes", 1, b"body")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_v1_signature(b"s", 1, "not-bytes")  # type: ignore[arg-type]


def test_build_header_single_secret():
    sec = SigningSecret(kid="k1", raw=b"s1")
    body = b"hello"
    header, ts = build_signature_header([sec], body, timestamp=1700000000)
    assert header.startswith("t=1700000000")
    assert header.count("v1=") == 1


def test_build_header_dual_active():
    secs = [SigningSecret(kid="old", raw=b"old-secret"), SigningSecret(kid="new", raw=b"new-secret")]
    header, _ts = build_signature_header(secs, b"body", timestamp=1700000000)
    assert header.count("v1=") == 2


def test_parse_roundtrip():
    secs = [SigningSecret(kid="a", raw=b"x"), SigningSecret(kid="b", raw=b"y")]
    header, _ = build_signature_header(secs, b"body", timestamp=1700000000)
    ts, sigs = parse_signature_header(header)
    assert ts == 1700000000
    assert len(sigs) == 2


def test_parse_handles_bad_ts():
    ts, sigs = parse_signature_header("t=not-a-number,v1=abc")
    assert ts is None
    assert sigs == ["abc"]


def test_verify_accepts_either_secret_during_rotation():
    old = SigningSecret(kid="old", raw=b"old-secret")
    new = SigningSecret(kid="new", raw=b"new-secret")
    body = b'{"e":1}'
    # Signed with BOTH
    header, _ = build_signature_header([old, new], body)
    # Receiver has only OLD yet
    assert verify_signature(header, body, [old]) is True
    # Receiver has rotated to NEW
    assert verify_signature(header, body, [new]) is True
    # Receiver has BOTH (worst case)
    assert verify_signature(header, body, [old, new]) is True


def test_verify_rejects_tampered_body():
    sec = SigningSecret(kid="k", raw=b"s")
    body = b"legit"
    header, _ = build_signature_header([sec], body)
    assert verify_signature(header, b"tampered", [sec]) is False


def test_verify_rejects_wrong_secret():
    sec = SigningSecret(kid="k", raw=b"s")
    header, _ = build_signature_header([sec], b"body")
    evil = SigningSecret(kid="evil", raw=b"other")
    assert verify_signature(header, b"body", [evil]) is False


def test_verify_rejects_replay_window():
    sec = SigningSecret(kid="k", raw=b"s")
    # Sign with an old timestamp
    old_ts = int(time.time()) - 10_000
    header, _ = build_signature_header([sec], b"body", timestamp=old_ts)
    assert verify_signature(header, b"body", [sec], max_age_seconds=300) is False


def test_verify_rejects_missing_t():
    sec = SigningSecret(kid="k", raw=b"s")
    assert verify_signature("v1=deadbeef", b"body", [sec]) is False


def test_verify_rejects_no_sigs():
    sec = SigningSecret(kid="k", raw=b"s")
    assert verify_signature(f"t={int(time.time())}", b"body", [sec]) is False
