"""admin_ops read_router — RBAC boundary for GET /api/admin/update-check.

Per /audit: update-check moved from admin/owner (`_require_admin`) to a
read-only router gated on `settings.read`, so a read-only user can poll for an
available update without tripping the settings.write wall + logging a 403. This
locks that *intended* widening so it can't silently drift (e.g. back to a write
gate, or wider to a role without settings.read). Endpoint-level 403 enforcement
of require_permission() is covered by test_role_permissions_enforcement.py.
"""
from __future__ import annotations

from gdx_dispatch.core.permissions import BUILTIN_ROLES
from gdx_dispatch.routers.admin_ops import read_router


def test_read_router_exposes_update_check():
    paths = {getattr(r, "path", "") for r in read_router.routes}
    assert "/api/admin/update-check" in paths, paths


def test_update_check_boundary_settings_read():
    # settings.read holders CAN reach it — incl. the read-only `viewer` auditor.
    assert "settings.read" in BUILTIN_ROLES["viewer"]
    assert "settings.read" in BUILTIN_ROLES["admin"]
    # technician (field tech) lacks settings.read → blocked. This is the line the
    # /audit asked us to assert: the widening reaches viewer, NOT every role.
    assert "settings.read" not in BUILTIN_ROLES["technician"]
    assert "settings.read" not in BUILTIN_ROLES["dispatcher"]
    # owner is structurally different — it reaches everything via the wildcard,
    # not a literal settings.read grant. Assert that explicitly so this role
    # isn't silently skipped.
    assert BUILTIN_ROLES["owner"] == ["*"]
