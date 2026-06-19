"""SS-10 Slice E — tests for ``require_fresh_mfa`` + ``require_token_binding``.

Covers the behaviours the slice owes:

1. Fail-open when the principal has no MFA fields at all (``Principal``
   is not widened in this slice, so bare principals must keep passing).
2. 401 with the CAE challenge header when MFA is *explicitly* false.
3. 401 when ``mfa_verified_at`` is older than ``within_seconds`` (both
   datetime and POSIX-timestamp shapes).
4. Pass when ``mfa_verified_at`` is fresh.
5. ``raw_claims`` fallback is consulted when attributes are absent.
6. Exact ``WWW-Authenticate`` header verbatim for both the explicit-false
   and stale-timestamp branches.
7. ``require_token_binding`` returns a callable that no-ops for all
   documented ``bind_to`` values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from gdx_dispatch.core.cae import require_fresh_mfa
from gdx_dispatch.core.token_binding import require_token_binding

_EXPECTED_HEADER = 'Bearer error="insufficient_claims", required="mfa"'


@dataclass
class _PrincipalStub:
    """Stand-in for ``gdx_dispatch.core.principal.Principal`` that can carry MFA fields.

    Deliberately not ``frozen`` and not a subclass — the real ``Principal``
    does not expose MFA attributes in this slice, and using a separate
    stub lets the tests model both shapes (attribute-present and
    attribute-absent) without touching the production dataclass.
    """

    raw_claims: dict[str, Any] = field(default_factory=dict)


class _BareObject:
    """Opaque object with no MFA-related attributes at all."""


# ── fail-open paths ─────────────────────────────────────────────────────────


def test_none_principal_passes():
    require_fresh_mfa(None)


def test_principal_without_mfa_fields_passes():
    require_fresh_mfa(_BareObject())


def test_principal_with_empty_raw_claims_passes():
    require_fresh_mfa(_PrincipalStub(raw_claims={}))


def test_principal_with_unrelated_raw_claims_passes():
    require_fresh_mfa(_PrincipalStub(raw_claims={"sub": "u1", "scope": "read"}))


# ── explicit-false / stale 401 paths ────────────────────────────────────────


def test_explicit_false_mfa_attribute_raises_401():
    p = _PrincipalStub()
    p.mfa_verified = False  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as exc:
        require_fresh_mfa(p)
    assert exc.value.status_code == 401
    assert exc.value.detail == "insufficient_claims"
    assert exc.value.headers == {"WWW-Authenticate": _EXPECTED_HEADER}


def test_explicit_false_mfa_in_raw_claims_raises_401():
    p = _PrincipalStub(raw_claims={"mfa_verified": False})
    with pytest.raises(HTTPException) as exc:
        require_fresh_mfa(p)
    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"] == _EXPECTED_HEADER


def test_stale_mfa_verified_at_datetime_raises_401():
    stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
    p = _PrincipalStub()
    p.mfa_verified_at = stale  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as exc:
        require_fresh_mfa(p, within_seconds=300)
    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"] == _EXPECTED_HEADER


def test_stale_mfa_verified_at_posix_timestamp_raises_401():
    stale_epoch = (
        datetime.now(timezone.utc) - timedelta(seconds=3600)
    ).timestamp()
    p = _PrincipalStub(raw_claims={"mfa_verified_at": stale_epoch})
    with pytest.raises(HTTPException) as exc:
        require_fresh_mfa(p, within_seconds=300)
    assert exc.value.status_code == 401


def test_naive_datetime_is_treated_as_utc_and_respects_age():
    now_utc = datetime.now(timezone.utc)
    naive_stale = (now_utc - timedelta(seconds=3600)).replace(tzinfo=None)
    p = _PrincipalStub()
    p.mfa_verified_at = naive_stale  # type: ignore[attr-defined]
    with pytest.raises(HTTPException):
        require_fresh_mfa(p, within_seconds=300)


# ── fresh pass paths ────────────────────────────────────────────────────────


def test_fresh_mfa_verified_at_datetime_passes():
    fresh = datetime.now(timezone.utc) - timedelta(seconds=10)
    p = _PrincipalStub()
    p.mfa_verified_at = fresh  # type: ignore[attr-defined]
    require_fresh_mfa(p, within_seconds=300)


def test_fresh_mfa_verified_at_posix_timestamp_in_claims_passes():
    fresh_epoch = (
        datetime.now(timezone.utc) - timedelta(seconds=10)
    ).timestamp()
    p = _PrincipalStub(raw_claims={"mfa_verified_at": fresh_epoch})
    require_fresh_mfa(p, within_seconds=300)


def test_mfa_verified_true_without_timestamp_passes():
    p = _PrincipalStub()
    p.mfa_verified = True  # type: ignore[attr-defined]
    require_fresh_mfa(p)


def test_malformed_verified_at_is_ignored_and_passes():
    p = _PrincipalStub(raw_claims={"mfa_verified_at": "not-a-date"})
    require_fresh_mfa(p)


def test_attribute_wins_over_raw_claims():
    # attr says fresh; claims say stale — attr lookup runs first
    fresh = datetime.now(timezone.utc) - timedelta(seconds=5)
    stale_epoch = (
        datetime.now(timezone.utc) - timedelta(seconds=3600)
    ).timestamp()
    p = _PrincipalStub(raw_claims={"mfa_verified_at": stale_epoch})
    p.mfa_verified_at = fresh  # type: ignore[attr-defined]
    require_fresh_mfa(p, within_seconds=300)


# ── token-binding stub ──────────────────────────────────────────────────────


def test_require_token_binding_returns_callable():
    dep = require_token_binding()
    assert callable(dep)


def test_require_token_binding_default_is_noop():
    dep = require_token_binding()
    assert dep() is None


@pytest.mark.parametrize("bind_to", ["ip", "cnf", "dpop", "mtls"])
def test_require_token_binding_variants_all_noop(bind_to: str):
    dep = require_token_binding(bind_to=bind_to)
    assert dep() is None
    assert dep("anything", keyword="also fine") is None


def test_require_token_binding_name_reflects_bind_to():
    dep = require_token_binding(bind_to="cnf")
    assert dep.__name__ == "require_token_binding_cnf"


# ── exact header verbatim ───────────────────────────────────────────────────


def test_challenge_header_value_is_verbatim():
    p = _PrincipalStub()
    p.mfa_verified = False  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as exc:
        require_fresh_mfa(p)
    assert (
        exc.value.headers["WWW-Authenticate"]
        == 'Bearer error="insufficient_claims", required="mfa"'
    )
