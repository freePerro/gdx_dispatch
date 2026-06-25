"""Canonical role names + normalization — single source of truth.

The same role has historically been spelled multiple ways:

  * DB (``users.role``) stores SHORT legacy forms: ``"tech"``, ``"dispatch"``.
  * RBAC catalog (:data:`core.permissions.BUILTIN_ROLES`) and ``tenant_roles.name``
    use LONG forms: ``"technician"``, ``"dispatcher"``.
  * Superadmin has appeared as ``"super_admin"`` / ``"superadmin"`` / ``"super-admin"``.

Every IN-MEMORY role comparison should normalize first via :func:`normalize_role`
so callers never special-case variants. Canonical = the LONG RBAC form (matches
BUILTIN_ROLES keys).

IMPORTANT: SQL that filters ``users.role`` or ``tenant_roles.name`` must still use
that column's STORED form (short for users.role, long for tenant_roles.name) —
normalize in Python, not in the query. See the wiki "Role naming conventions".

This module is dependency-free (no imports from permissions/models) so anything
can import it without cycles.
"""
from __future__ import annotations

from typing import Final

# ── Canonical (long) role names — must match BUILTIN_ROLES keys ──────────────
OWNER: Final = "owner"
ADMIN: Final = "admin"
DISPATCHER: Final = "dispatcher"
TECHNICIAN: Final = "technician"
SALES: Final = "sales"
ACCOUNTING: Final = "accounting"
VIEWER: Final = "viewer"
MANAGER: Final = "manager"
SUPER_ADMIN: Final = "super_admin"

# Every known spelling → its canonical form. Lowercased keys; normalize_role
# lowercases input before lookup.
ROLE_ALIASES: Final[dict[str, str]] = {
    "tech": TECHNICIAN,
    "technician": TECHNICIAN,
    "dispatch": DISPATCHER,
    "dispatcher": DISPATCHER,
    "superadmin": SUPER_ADMIN,
    "super_admin": SUPER_ADMIN,
    "super-admin": SUPER_ADMIN,
}


def normalize_role(raw: object) -> str:
    """Collapse any known spelling of a role to its canonical (long) form.

    Unknown roles pass through lowercased/stripped (never raises) so callers
    that compare against canonical constants degrade safely.
    """
    r = str(raw or "").strip().lower()
    return ROLE_ALIASES.get(r, r)


# ── Role-group predicates (normalize, then test) ─────────────────────────────
# Roles permitted to act on OTHER users' records (dispatch-manager tier).
# Replaces the scattered per-router _DISPATCH_ROLES / DISPATCH_ROLES frozensets.
DISPATCH_MANAGER_ROLES: Final[frozenset[str]] = frozenset(
    {OWNER, ADMIN, DISPATCHER, MANAGER, SUPER_ADMIN}
)

# Roles whose ASSIGNMENT of the admin/owner tier is permitted (owner-exclusive
# privilege; admin == owner for ops but may NOT grant the admin/owner role).
ROLE_ADMIN_ACTORS: Final[frozenset[str]] = frozenset({OWNER, SUPER_ADMIN})

# Full-access tier (owner/admin/superadmin) — see everything, bypass gates.
ADMIN_TIER_ROLES: Final[frozenset[str]] = frozenset({OWNER, ADMIN, SUPER_ADMIN})


def is_dispatch_manager(role: object) -> bool:
    """True if the role may act on other users' records (dispatch/admin tier)."""
    return normalize_role(role) in DISPATCH_MANAGER_ROLES


def is_role_admin_actor(role: object) -> bool:
    """True if the role may grant/change the admin/owner tier (owner/superadmin)."""
    return normalize_role(role) in ROLE_ADMIN_ACTORS


def is_admin_tier(role: object) -> bool:
    """True for owner / admin / superadmin (full-access tier)."""
    return normalize_role(role) in ADMIN_TIER_ROLES


def is_technician(role: object) -> bool:
    """True for the field technician role (either 'tech' or 'technician' spelling)."""
    return normalize_role(role) == TECHNICIAN


if __name__ == "__main__":  # pragma: no cover — runnable self-check
    assert normalize_role("tech") == "technician"
    assert normalize_role("TECHNICIAN") == "technician"
    assert normalize_role("dispatch") == "dispatcher"
    assert normalize_role("super-admin") == normalize_role("superadmin") == "super_admin"
    assert normalize_role(None) == "" and normalize_role("  Owner ") == "owner"
    assert is_dispatch_manager("dispatch") and is_dispatch_manager("dispatcher")
    assert not is_dispatch_manager("tech") and not is_technician("dispatcher")
    assert is_role_admin_actor("superadmin") and not is_role_admin_actor("admin")
    assert is_admin_tier("owner") and is_admin_tier("super-admin") and not is_admin_tier("sales")
    print("core/roles.py self-check OK")
