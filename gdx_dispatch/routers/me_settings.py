"""Read-only resolved-settings endpoint for the authenticated user.

Mirror of `/api/admin/feature-settings/tech-mobile` minus the admin gate
and minus the catalog/overrides metadata. Tech app reads this at
mount-time to drive feature toggles (e.g. vehicle_inspection mode).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant_mobile_settings import load_tenant_mobile_settings
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(tags=["me-settings"])


@router.get("/api/me/tech-mobile-settings")
def get_my_tech_mobile_settings(
    request: Request,
    _user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the resolved tech-mobile settings dict for this tenant.

    Resolution: tenant override → catalog default. Authenticated tech can
    read the values that drive their UI without going through the admin
    write endpoint. `tenant_timezone` is included so the frontend can
    format times in the tenant's local zone instead of the browser's.
    """
    row = db.query(AppSettings).first()
    tz = (row.timezone if row and row.timezone else "America/New_York")
    return {
        "settings": load_tenant_mobile_settings(db, request=request),
        "tenant_timezone": tz,
    }
