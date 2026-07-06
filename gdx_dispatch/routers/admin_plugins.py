"""Owner-only management of the plugin_registry (ADR-013 step 5, in-app install).

The registry is the operator's desired-state list of installed plugin packages.
Writing here records intent; the plugin-host materializes it (pip install into
the /plugins volume) on its next restart. Installing a plugin is owner-only and
audited — same trust tier as adding a dependency, since the package runs with
backend access (confined to plugin-host).
"""
from __future__ import annotations

import hashlib
import logging
import os

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.plugin_consent import (
    consented_permissions,
    fetch_permissions,
    record_consent,
)
from gdx_dispatch.plugin_api.manifest import PERMISSION_RISKS
from gdx_dispatch.plugin_host.reconcile import (
    artifact_name_version,
    desired_artifact_names,
    desired_packages,
    ensure_artifact_table,
    ensure_registry_table,
    looks_like_artifact_filename,
    safe_artifact_name,
)
from gdx_dispatch.routers.auth import get_current_user

# Cap an uploaded plugin artifact — wheels/sdists are small; a big upload is a
# red flag, not a real plugin.
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

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


@router.post("/upload", status_code=201)
async def upload_artifact(
    file: UploadFile = File(...),
    user: dict = Depends(_require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Upload a private plugin wheel/sdist (not on a pip index, e.g. an internal
    plugin). Stored in plugin_artifact; plugin-host installs it on restart.
    Owner-only + audited — same trust tier as adding a dependency, since the
    package runs with backend access in plugin-host."""
    name = safe_artifact_name(file.filename or "")
    if name is None:
        raise HTTPException(400, "filename must be a .whl or .tar.gz with no path")
    # Read at most cap+1 bytes so an oversized upload can't be pulled wholesale
    # into memory before we reject it.
    content = await file.read(_MAX_ARTIFACT_BYTES + 1)
    if not content:
        raise HTTPException(400, "empty file")
    if len(content) > _MAX_ARTIFACT_BYTES:
        raise HTTPException(413, "artifact too large")
    ensure_artifact_table(db)
    digest = hashlib.sha256(content).hexdigest()
    db.execute(
        text(
            """
            INSERT INTO plugin_artifact (filename, sha256, content, uploaded_by)
            VALUES (:f, :h, :c, :by)
            ON CONFLICT (filename) DO UPDATE
              SET sha256 = EXCLUDED.sha256, content = EXCLUDED.content,
                  uploaded_by = EXCLUDED.uploaded_by, uploaded_at = now()
            """
        ),
        {"f": name, "h": digest, "c": content, "by": str(user.get("sub") or "")},
    )
    db.commit()
    return {"filename": name, "sha256": digest, "size": len(content),
            "note": "restart plugin-host to install"}


@router.get("/artifacts")
def list_artifacts(_: dict = Depends(_require_owner), db: Session = Depends(get_db)) -> list[dict]:
    """Uploaded artifacts (metadata only — never the bytes)."""
    ensure_artifact_table(db)
    rows = db.execute(
        text("SELECT filename, sha256, uploaded_at FROM plugin_artifact ORDER BY filename")
    ).fetchall()
    return [{"filename": r[0], "sha256": r[1],
             "uploaded_at": r[2].isoformat() if r[2] else None} for r in rows]


@router.delete("/artifacts/{filename}")
def delete_artifact(
    filename: str,
    _: dict = Depends(_require_owner),
    db: Session = Depends(get_db),
) -> dict:
    ensure_artifact_table(db)
    db.execute(text("DELETE FROM plugin_artifact WHERE filename = :f"), {"f": filename})
    db.commit()
    return {"filename": filename, "status": "removed",
            "note": "already-installed copy stays until plugin-host restarts"}


@router.post("", status_code=201)
def add_plugin(
    body: PluginInstall,
    user: dict = Depends(_require_owner),
    db: Session = Depends(get_db),
) -> dict:
    ensure_registry_table(db)
    # Guard the free-text package field against a wheel/sdist *filename* (issue
    # #100). A filename is not an index package spec; a private wheel belongs in the
    # Upload flow (plugin_artifact). Recording it here makes reconcile try
    # `pip install <bare filename>` on every boot, which fails and wedges
    # plugin-host /ready red. If the file was already uploaded it's installed from
    # there — report success without a bogus row; otherwise point at Upload.
    if looks_like_artifact_filename(body.package):
        ensure_artifact_table(db)
        if body.package.strip() in set(desired_artifact_names(db)):
            _, fver = artifact_name_version(body.package)
            return {
                "package": body.package,
                "version": fver,
                "status": "already-uploaded",
                "note": "installed from the uploaded file — restart plugin-host if it isn't loaded yet",
            }
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{body.package}' looks like a plugin file, not a package name. "
                'Upload it under "Upload plugin file" instead; the Package field is '
                "for an index package name like gdx-plugin-example."
            ),
        )
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


@router.get("/{key}/permissions")
def plugin_permissions(
    key: str,
    _: dict = Depends(_require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """The elevated permissions a plugin declares, each with its risk text and
    whether an owner has already consented (ADR-014). Drives the consent dialog."""
    declared = fetch_permissions(key)
    granted = consented_permissions(db, key)
    return {
        "key": key,
        "permissions": [
            {"name": p, "risk": PERMISSION_RISKS.get(p, p), "consented": p in granted}
            for p in declared
        ],
        "all_consented": bool(declared) and set(declared).issubset(granted),
    }


@router.post("/{key}/consent", status_code=201)
def consent_plugin(
    key: str,
    user: dict = Depends(_require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Owner grants consent for the plugin's currently-declared permissions.
    Records exactly what was declared now, so a later-added permission isn't
    silently covered by old consent."""
    declared = fetch_permissions(key)
    if not declared:
        raise HTTPException(status_code=400, detail="plugin declares no permissions")
    record_consent(db, key, declared, str(user.get("sub") or ""))
    return {"key": key, "consented": declared}


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
