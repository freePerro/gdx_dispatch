"""Regression: settings _require_admin must accept owner/superadmin, not only
admin. Gating on role == "admin" 403'd the owner (highest role, seeded account)
out of every /api/settings endpoint."""
import pytest
from fastapi import HTTPException

from gdx_dispatch.routers.settings import _require_admin


def test_admin_tier_allowed():
    for role in ("admin", "owner", "superadmin"):
        _require_admin({"role": role})  # must not raise


def test_non_admin_denied():
    for role in ("technician", "dispatcher", "user", ""):
        with pytest.raises(HTTPException) as exc:
            _require_admin({"role": role})
        assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        _require_admin({})  # missing role
