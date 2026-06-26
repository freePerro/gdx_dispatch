"""Canonical role normalization — core/roles.py (role-name canonicalization #2)."""
from __future__ import annotations

import pytest

from gdx_dispatch.core import roles as R
from gdx_dispatch.core.permissions import BUILTIN_ROLES


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("tech", "technician"), ("technician", "technician"), ("TECH", "technician"),
        ("dispatch", "dispatcher"), ("dispatcher", "dispatcher"),
        ("superadmin", "super_admin"), ("super_admin", "super_admin"), ("super-admin", "super_admin"),
        ("  Owner ", "owner"), ("admin", "admin"),
        (None, ""), ("", ""), ("unknown_role", "unknown_role"),
    ],
)
def test_normalize_role(raw, expected):
    assert R.normalize_role(raw) == expected


@pytest.mark.parametrize(
    "role,expected",
    [
        ("owner", True), ("admin", True), ("dispatch", True), ("dispatcher", True),
        ("manager", True), ("superadmin", True), ("super_admin", True), ("super-admin", True),
        ("tech", False), ("technician", False), ("viewer", False), ("sales", False),
        ("user", False), ("", False),
    ],
)
def test_is_dispatch_manager(role, expected):
    assert R.is_dispatch_manager(role) is expected


@pytest.mark.parametrize(
    "role,expected",
    [
        ("owner", True), ("superadmin", True), ("super-admin", True),
        ("admin", False), ("dispatcher", False), ("tech", False),
    ],
)
def test_is_role_admin_actor(role, expected):
    assert R.is_role_admin_actor(role) is expected


def test_is_technician_both_spellings():
    assert R.is_technician("tech") and R.is_technician("technician")
    assert not R.is_technician("dispatcher")


def test_is_admin_tier():
    for r in ("owner", "admin", "super_admin", "super-admin", "superadmin"):
        assert R.is_admin_tier(r)
    for r in ("dispatcher", "tech", "sales", "viewer"):
        assert not R.is_admin_tier(r)


def test_canonical_constants_match_builtin_role_keys():
    # Canonical (long) constants must be exactly the BUILTIN_ROLES keys so the
    # RBAC catalog and the normalizer never drift apart.
    canonical = {R.OWNER, R.ADMIN, R.DISPATCHER, R.TECHNICIAN, R.SALES, R.ACCOUNTING, R.VIEWER}
    assert canonical == set(BUILTIN_ROLES.keys())


def test_aliases_resolve_to_canonical_constants():
    # Every alias target must be a defined canonical constant (no typos).
    valid = {
        R.OWNER, R.ADMIN, R.DISPATCHER, R.TECHNICIAN, R.SALES,
        R.ACCOUNTING, R.VIEWER, R.MANAGER, R.SUPER_ADMIN,
    }
    for target in R.ROLE_ALIASES.values():
        assert target in valid, f"alias target {target!r} is not a canonical constant"


# ── #45: write paths normalize users.role to ONE canonical (long) vocabulary ──

def test_user_write_models_canonicalize_role():
    from gdx_dispatch.routers.users import InviteIn, RoleChangeIn, UserCreateIn
    pw = "CorrectHorse1!"
    # Short input → long canonical on every user-write model.
    assert UserCreateIn(email="a@b.co", name="X", password=pw, role="tech").role == "technician"
    assert UserCreateIn(email="a@b.co", name="X", password=pw, role="dispatch").role == "dispatcher"
    assert RoleChangeIn(role="tech").role == "technician"
    assert InviteIn(email="a@b.co", name="X", role="dispatch").role == "dispatcher"
    # Long input passes through unchanged (idempotent).
    assert UserCreateIn(email="a@b.co", name="X", password=pw, role="technician").role == "technician"
    assert RoleChangeIn(role="dispatcher").role == "dispatcher"
    # admin/owner/sales identical in both vocabularies.
    for r in ("admin", "owner", "sales"):
        assert UserCreateIn(email="a@b.co", name="X", password=pw, role=r).role == r
