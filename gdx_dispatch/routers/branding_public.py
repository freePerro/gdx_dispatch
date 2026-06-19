"""Tenant branding read — accessible to every authenticated user.

The full settings router (``gdx_dispatch/routers/settings.py``) gates the entire
``/api/settings`` prefix on admin / owner / super_admin. That's the
right rule for the rest of settings (integrations, role permissions,
etc.) but branding (company name, logo, colors) is the data the SPA
topbar and login picker need to render correctly for every signed-in
user — a tech needs to see "Example Garage Doors" in the header, not
the platform default. Pulling the read endpoint out into its own
router with a permissive role gate keeps the existing settings hard
gate intact for the write side.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.core.cache import cached
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/settings", tags=["settings-public"])


@router.get("/branding")
async def get_branding_public(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    # Lazy import to avoid pulling the whole settings router (with its
    # router-level admin gate) into module load just to reuse two helpers.
    from gdx_dispatch.routers.settings import _branding_dict, _ensure_settings

    tenant_id = company_id()
    return await cached(
        tenant_id,
        "settings:branding",
        ttl_seconds=300,
        fetcher=lambda: _branding_dict(_ensure_settings(db)),
    )


@router.get("/modules")
def get_modules_public(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Tenant module-grant list — readable by every authenticated user.

    Returns the full shape used by both the admin Settings → Modules tab
    (needs `tier`/`name`/`locked`) and `useTenantModules` (needs `key`/`enabled`).
    The same path is also registered by `gdx_dispatch/routers/settings.py:get_modules`
    behind a router-level admin gate; this public copy wins by include order
    in `gdx_dispatch/app.py` and is the authoritative read path. Write-side
    (enable/disable POSTs) stays admin-gated in `routers/settings.py`.
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    from fastapi import HTTPException

    from gdx_dispatch.core.modules import MODULES
    from gdx_dispatch.models.tenant_models import CompanyModuleGrant

    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")

    # First-GET bootstrap: seed default-enabled grants for fresh tenants.
    # Mirrors `routers/settings.get_modules` (settings.py:191-206). Idempotent.
    existing = db.query(CompanyModuleGrant).first()
    if not existing:
        now = datetime.now(timezone.utc)
        for key, cfg in MODULES.items():
            if cfg.get("default"):
                db.add(CompanyModuleGrant(
                    id=str(uuid4()),
                    company_id=tenant_id,
                    module_key=key,
                    granted_at=now,
                    created_at=now,
                ))
        db.commit()

    rows = db.query(CompanyModuleGrant.module_key).all()
    granted = {str(r[0]) for r in rows}

    payload: list[dict[str, Any]] = []
    for key, cfg in MODULES.items():
        payload.append({
            "key": key,
            "name": cfg["name"],
            "label": cfg["name"],
            "tier": str(cfg["tier"]),
            "default": bool(cfg["default"]),
            "enabled": key in granted,
            "locked": False,
            "upgrade_required": None,
        })
    payload.sort(key=lambda item: item["name"])

    return {"modules": payload}
