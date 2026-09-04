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

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.cache import cached
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant import company_id
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


@router.get("/integrations/google-maps")
def get_google_maps_key_public(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Tenant Google Maps JS API key — readable by every authenticated user.

    The gated twin in ``routers/settings.py:get_google_maps_key`` documents
    "reachable by any authenticated user", but the router-level
    admin/owner/superadmin dependency silently overrode that: technicians got
    403 and the tech-mobile map view never rendered (2026-07-16, reported
    from a tech's device via /api/feedback/client-error). Same pattern as
    ``/modules`` above — this public copy wins by include order in
    ``gdx_dispatch/app.py``; the PATCH (write side) stays admin-gated.

    Exposing the key to signed-in users is by design: it ships in the
    ``<script src=…&key=…>`` URL of every browser that loads a map, so the
    real control is the HTTP-referrer restriction on the key itself in
    Google Cloud Console.
    """
    from gdx_dispatch.routers.settings import _ensure_settings

    row = _ensure_settings(db)
    key = (row.google_maps_api_key or "").strip()
    return {"key": key, "configured": bool(key)}


@router.get("/modules")
def get_modules_public(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Tenant module-grant list — readable by every authenticated user.

    Returns the shape used by both the admin Settings → Modules tab and
    `useTenantModules`: `key` / `name` / `enabled`. (`tier`, `locked` and
    `upgrade_required` are still emitted for compatibility; since 2026-09-03
    nothing in the SPA reads them — the plan-tier grouping is gone and
    `locked` was only ever hard-coded False here.)
    This is the authoritative read path (the admin-gated twin that once
    shadowed it in `routers/settings.py` is gone). Write-side (enable/disable
    POSTs) stays admin-gated in `routers/settings.py`.
    """
    from fastapi import HTTPException

    from gdx_dispatch.core.modules import MODULES
    from gdx_dispatch.models.tenant_models import CompanyModuleGrant

    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")

    # First-GET bootstrap. Tier-7 audit catch: this used to seed only
    # `default: True` modules while core/modules._seed_default_modules seeds
    # EVERY module (the single-tenant decision — the owner owns the whole
    # install). Whichever seeder a fresh tenant hit first decided which
    # modules existed: if this GET won, google_maps/reports_advanced/
    # equipment_tracking got no rows → explicit enabled:false → nav hidden
    # (exactly what the empty-granted demo DB would have hit). One seeder now.
    from gdx_dispatch.core.modules import _seed_default_modules

    _seed_default_modules(db, tenant_id)
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


@router.get("/branding/logo/{filename}", include_in_schema=False)
def serve_branding_logo(filename: str):
    """Serve the uploaded company logo — deliberately unauthenticated.

    The sidebar renders ``branding.logo_url`` in a plain ``<img>`` tag, which
    cannot attach a Bearer header, so this route must be public. That is safe
    because the strict filename pattern below matches ONLY files minted by
    ``routers/settings.py:upload_branding_logo`` (branding-logo-<uuid4hex>.png/
    jpg) — no other document in the flat upload dir is addressable here, and
    the uuid4 segment makes names unguessable. A company logo is public
    marketing material by nature.
    """
    from fastapi.responses import FileResponse

    from gdx_dispatch.core.branding_logo import branding_logo_file

    path = branding_logo_file(filename)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media_type = "image/png" if filename.endswith(".png") else "image/jpeg"
    # Filenames are unique per upload, so the content behind one never
    # changes — safe to let browsers cache for a day.
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
    )
