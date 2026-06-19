"""Catalog invariants — slice 1.1 of sprint_role_permissions."""
from __future__ import annotations

import re

from gdx_dispatch.core.permissions import (
    AVAILABLE_PERMISSIONS,
    BUILTIN_DESCRIPTIONS,
    BUILTIN_ROLES,
    PERMISSION_CATEGORIES,
    PERMISSIONS,
    WILDCARD,
    is_known_permission,
)

# One or more dotted lowercase segments. Pre-2026-05-15 this allowed
# exactly ONE dot, so multi-segment keys like `pricing.labor_matrix.read`
# (added S97) silently failed this invariant test. Broadened to accept
# depth >=2 segments — covers leads.read, customers.read_all, AND
# pricing.labor_matrix.read; still rejects bare/trailing-dot/caps keys.
KEY_SHAPE = re.compile(r"^[a-z][a-z_]*(?:\.[a-z][a-z_]*)+$")


def test_no_duplicate_keys():
    keys = [k for (k, _l, _c) in PERMISSIONS]
    assert len(keys) == len(set(keys)), f"duplicate permission keys: {keys}"


def test_key_shape():
    for key in AVAILABLE_PERMISSIONS:
        assert KEY_SHAPE.match(key), f"bad permission key shape: {key!r}"


def test_categories_non_empty():
    by_cat: dict[str, list[str]] = {}
    for k, _l, c in PERMISSIONS:
        by_cat.setdefault(c, []).append(k)
    for cat in PERMISSION_CATEGORIES:
        assert by_cat.get(cat), f"category {cat!r} has no permissions"


def test_owner_has_wildcard():
    assert BUILTIN_ROLES["owner"] == [WILDCARD]


def test_builtin_perms_are_known():
    for role, perms in BUILTIN_ROLES.items():
        for p in perms:
            assert is_known_permission(p), f"role {role!r} grants unknown permission {p!r}"


def test_every_builtin_has_description():
    for role in BUILTIN_ROLES:
        assert BUILTIN_DESCRIPTIONS.get(role), f"missing description for {role!r}"


def test_required_builtin_roles_present():
    # Names locked 2026-05-02 — do NOT rename without migration plan.
    expected = {"owner", "admin", "dispatcher", "technician", "sales", "accounting", "viewer"}
    assert expected.issubset(BUILTIN_ROLES.keys())


def test_own_all_split_present():
    # Q1 answer: tenant's choice → both keys must exist for each split resource.
    for resource in ("jobs", "scheduling", "customers", "estimates", "invoices"):
        assert f"{resource}.read_own" in AVAILABLE_PERMISSIONS, f"missing {resource}.read_own"
        assert f"{resource}.read_all" in AVAILABLE_PERMISSIONS, f"missing {resource}.read_all"


def test_technician_is_own_scoped():
    perms = BUILTIN_ROLES["technician"]
    assert "jobs.read_own" in perms
    assert "jobs.read_all" not in perms
    assert "scheduling.read_own" in perms
    assert "scheduling.read_all" not in perms


def test_admin_excludes_billing_write():
    assert "billing.write" not in BUILTIN_ROLES["admin"]
    assert "billing.read" in BUILTIN_ROLES["admin"]


def test_viewer_is_read_only():
    perms = BUILTIN_ROLES["viewer"]
    for p in perms:
        assert ".read" in p, f"viewer grants non-read permission: {p!r}"
