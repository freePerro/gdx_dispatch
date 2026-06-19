"""Tests for gdx_dispatch.core.pat_validation (SS-14 slice C)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import pytest

from gdx_dispatch.core.pat_validation import (
    PAT_PREFIXES,
    PatPrincipal,
    has_pat_prefix,
    validate_pat,
)
from gdx_dispatch.tests.factories.platform import (
    make_access_token,
    make_capability,
    make_capability_set,
    make_identity,
    make_membership,
    make_tenant,
)


def _hash(token: str) -> str:
    return bcrypt.hashpw(token.encode(), bcrypt.gensalt(rounds=4)).decode()


def _seed_user_pat(
    db,
    *,
    token: str,
    prefix: str = "gdx_pat_live_",
    expires_at=None,
    revoked_at=None,
    capabilities=(("read", "job"),),
):
    """Create identity + tenant + membership + capset + PAT row."""
    identity = make_identity(db)
    tenant = make_tenant(db)
    capset = make_capability_set(db)
    for action, resource in capabilities:
        make_capability(db, capability_set=capset, action=action, resource_type=resource)
    make_membership(db, identity=identity, tenant=tenant, capability_set=capset)
    pat = make_access_token(
        db,
        owner_type="user",
        owner_id=identity.id,
        capability_set=capset,
        prefix=prefix,
        secret_hash=_hash(token),
        expires_at=expires_at
        if expires_at is not None
        else datetime.now(timezone.utc) + timedelta(days=30),
        revoked_at=revoked_at,
    )
    db.commit()
    return identity, tenant, pat


def test_has_pat_prefix_accepts_known_prefixes():
    assert has_pat_prefix("gdx_pat_live_abc")
    assert has_pat_prefix("gdx_pat_test_abc")
    assert has_pat_prefix("gdx_sk_live_abc")
    assert has_pat_prefix("gdx_sk_test_abc")


def test_has_pat_prefix_rejects_others():
    assert not has_pat_prefix("")
    assert not has_pat_prefix("Bearer gdx_pat_live_x")
    assert not has_pat_prefix("ghp_xxx")
    assert not has_pat_prefix("gdx_pat_")  # incomplete
    assert not has_pat_prefix("gdx_pat_invalid_x")


def test_pat_prefixes_constant_stable():
    """Guardrail: taxonomy is load-bearing, pin it."""
    assert set(PAT_PREFIXES) == {
        "gdx_pat_live_",
        "gdx_pat_test_",
        "gdx_sk_live_",
        "gdx_sk_test_",
    }


def test_validate_pat_unknown_prefix_returns_none(control_db):
    assert validate_pat("ghp_notours", control_db) is None
    assert validate_pat("", control_db) is None


def test_validate_pat_valid_token(control_db):
    token = "gdx_pat_live_" + "a" * 40
    identity, tenant, pat = _seed_user_pat(control_db, token=token)

    result = validate_pat(token, control_db)
    assert isinstance(result, PatPrincipal)
    assert result.identity_id == str(identity.id)
    assert result.tenant_id == str(tenant.id)
    assert result.role == "pat"
    assert result.auth_method == "pat"
    assert result.pat_id == str(pat.id)
    assert result.owner_type == "user"
    assert {(c["action"], c["resource_type"]) for c in result.capabilities} == {("read", "job")}


def test_validate_pat_wrong_secret_returns_none(control_db):
    token = "gdx_pat_live_" + "a" * 40
    _seed_user_pat(control_db, token=token)

    # Different secret body, same prefix: no match.
    assert validate_pat("gdx_pat_live_" + "b" * 40, control_db) is None


def test_validate_pat_expired_returns_none(control_db):
    token = "gdx_pat_live_" + "a" * 40
    _seed_user_pat(
        control_db,
        token=token,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert validate_pat(token, control_db) is None


def test_validate_pat_revoked_returns_none(control_db):
    token = "gdx_pat_live_" + "a" * 40
    _seed_user_pat(
        control_db,
        token=token,
        revoked_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    assert validate_pat(token, control_db) is None


def test_validate_pat_updates_last_used_at(control_db):
    token = "gdx_pat_live_" + "a" * 40
    _, _, pat = _seed_user_pat(control_db, token=token)
    assert pat.last_used_at is None

    result = validate_pat(token, control_db)
    assert result is not None

    control_db.refresh(pat)
    assert pat.last_used_at is not None


def test_validate_pat_no_membership_returns_none(control_db):
    """A user PAT whose owner has no active membership cannot be resolved."""
    token = "gdx_pat_live_" + "a" * 40
    identity = make_identity(control_db)
    capset = make_capability_set(control_db)
    make_capability(control_db, capability_set=capset)
    make_access_token(
        control_db,
        owner_type="user",
        owner_id=identity.id,
        capability_set=capset,
        prefix="gdx_pat_live_",
        secret_hash=_hash(token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    control_db.commit()

    assert validate_pat(token, control_db) is None


def test_validate_pat_test_prefix_recognised(control_db):
    token = "gdx_pat_test_" + "c" * 40
    identity, tenant, _ = _seed_user_pat(control_db, token=token, prefix="gdx_pat_test_")
    result = validate_pat(token, control_db)
    assert result is not None
    assert result.tenant_id == str(tenant.id)


def test_validate_pat_prefix_isolation(control_db):
    """Two PATs with identical secret body but different prefixes: only the matching prefix validates."""
    body = "x" * 40
    live_token = "gdx_pat_live_" + body
    test_token = "gdx_pat_test_" + body

    _seed_user_pat(control_db, token=live_token, prefix="gdx_pat_live_")
    _seed_user_pat(control_db, token=test_token, prefix="gdx_pat_test_")

    r_live = validate_pat(live_token, control_db)
    r_test = validate_pat(test_token, control_db)
    assert r_live is not None
    assert r_test is not None
    assert r_live.pat_id != r_test.pat_id
