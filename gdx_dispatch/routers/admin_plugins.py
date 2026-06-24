"""Owner-only management of the plugin_registry (ADR-013 step 5, in-app install).

The registry is the operator's desired-state list of installed plugin packages.
Writing here records intent; the plugin-host materializes it (pip install into
the /plugins volume) on its next restart. Installing a plugin is owner-only and
audited — same trust tier as adding a dependency, since the package runs with
backend access (confined to plugin-host).
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.plugin_host.reconcile import desired_packages, ensure_registry_table
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/admin/plugins", tags=["admin-plugins"])

_OWNER_ROLES = {"owner", "superadmin"}


def _require_owner(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Installing plugins is owner-only")
    return user


class PluginInstall(BaseModel):
    package: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, max_length=50)


@router.get("")
def list_registry(_: dict = Depends(_require_owner), db: Session = Depends(get_db)) -> list[dict]:
    ensure_registry_table(db)
    return [{"package": p, "version": v} for p, v in desired_packages(db)]


@router.post("", status_code=201)
def add_plugin(
    body: PluginInstall,
    user: dict = Depends(_require_owner),
    db: Session = Depends(get_db),
) -> dict:
    ensure_registry_table(db)
    db.execute(
        text(
            """
            INSERT INTO plugin_registry (package, version, added_by)
            VALUES (:p, :v, :by)
            ON CONFLICT (package) DO UPDATE SET version = EXCLUDED.version
            """
        ),
        {"p": body.package, "v": body.version, "by": str(user.get("sub") or "")},
    )
    db.commit()
    return {
        "package": body.package,
        "version": body.version,
        "status": "registered",
        "note": "restart the plugin-host container to apply",
    }


@router.post("/restart", status_code=202)
def restart_plugin_host(_: dict = Depends(_require_owner)) -> dict:
    """Trigger a plugin-host restart so pending installs/removals take effect.
    Safe from inside the app: plugin-host is a separate container, so the core
    app keeps serving while it cycles (unlike app self-update). Best-effort —
    plugin-host may already be cycling or not deployed; the UI polls
    /api/plugins to confirm it comes back."""
    url = os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")
    try:
        httpx.post(f"{url}/internal/restart", timeout=5.0)
    except Exception:
        logging.getLogger(__name__).warning("plugin-host restart trigger failed (may be cycling)")
    return {"status": "restart requested"}


@router.delete("/{package}")
def remove_plugin(
    package: str,
    _: dict = Depends(_require_owner),
    db: Session = Depends(get_db),
) -> dict:
    ensure_registry_table(db)
    db.execute(text("DELETE FROM plugin_registry WHERE package = :p"), {"p": package})
    db.commit()
    return {"package": package, "status": "unregistered"}
