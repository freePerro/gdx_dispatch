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
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_or_rollback, audit_ready_db, resolve_audit_actor
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

log = logging.getLogger(__name__)

_OWNER_ROLES = {"owner", "superadmin"}


def _require_owner(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in _OWNER_ROLES:
        raise HTTPException(status_code=403, detail="Installing plugins is owner-only")
    return user


def _audit(db: Session, request: Request, user: dict, action: str, **kw: object) -> None:
    """Record a plugin-surface mutation, or roll the mutation back.

    Installing a plugin is code execution with backend access, so "who did it,
    what changed, when" is not optional here (invariant #1). An audit write that
    fails must take the change down with it — a silently unaudited install is
    exactly the trace we would need and not have.
    """
    audit_or_rollback(db, action=action, actor=user, request=request, **kw)  # type: ignore[arg-type]


def _actor(user: dict) -> str:
    """Who to record as having done this, for the plugin tables' own
    provenance columns (`uploaded_by`, `added_by`, `consented_by`).

    These used `user.get("sub")`, which is blank for a token that carries the
    principal as `user_id` instead — so on a real owner session the columns
    recorded nothing while the audit row (which resolves sub/user_id/id) named
    the right person. Two records of the same action disagreeing is worse than
    either alone, so both now go through the same resolver.
    """
    return resolve_audit_actor(user)


class PluginInstall(BaseModel):
    package: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, max_length=50)


@router.get("")
def list_registry(_: dict = Depends(_require_owner), db: Session = Depends(audit_ready_db)) -> list[dict]:
    ensure_registry_table(db)
    return [{"package": p, "version": v} for p, v in desired_packages(db)]


@router.post("/upload", status_code=201)
async def upload_artifact(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
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
        {"f": name, "h": digest, "c": content, "by": _actor(user)},
    )
    _audit(
        db,
        request,
        user,
        "plugin.artifact_uploaded",
        entity_type="plugin_artifact",
        entity_id=name,
        details={"filename": name, "sha256": digest, "size": len(content)},
    )
    db.commit()
    return {"filename": name, "sha256": digest, "size": len(content),
            "note": "restart plugin-host to install"}


@router.get("/artifacts")
def list_artifacts(_: dict = Depends(_require_owner), db: Session = Depends(audit_ready_db)) -> list[dict]:
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
    request: Request,
    user: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
) -> dict:
    ensure_artifact_table(db)
    # Same shape as remove_plugin below: report what actually happened. An
    # unconditional DELETE that returns "removed" and writes an audit row for a
    # file that was never there is a success response for work not done, plus a
    # false entry in an append-only trail.
    result = db.execute(
        text("DELETE FROM plugin_artifact WHERE filename = :f"), {"f": filename}
    )
    if (result.rowcount or 0) == 0:
        db.rollback()
        raise HTTPException(404, f"No uploaded plugin file named '{filename}'.")
    _audit(
        db,
        request,
        user,
        "plugin.artifact_deleted",
        entity_type="plugin_artifact",
        entity_id=filename,
        details={"filename": filename},
    )
    db.commit()
    return {"filename": filename, "status": "removed",
            "note": "already-installed copy stays until plugin-host restarts"}


@router.post("", status_code=201)
def add_plugin(
    body: PluginInstall,
    request: Request,
    user: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
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
        {"p": body.package, "v": body.version, "by": _actor(user)},
    )
    _audit(
        db,
        request,
        user,
        "plugin.registered",
        entity_type="plugin",
        entity_id=body.package,
        details={"package": body.package, "version": body.version},
    )
    db.commit()
    return {
        "package": body.package,
        "version": body.version,
        "status": "registered",
        "note": "restart the plugin-host container to apply",
    }


class StorefrontInstall(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=50)


def _running_versions() -> dict[str, str]:
    """{canonical distribution: version} plugin-host reports as LOADED.

    The catalog is the only honest source for "what is running" — install
    metadata says what was *meant* to be installed, which can differ from the
    code on disk. Best-effort: plugin-host may be restarting, and the store must
    still render.
    """
    from gdx_dispatch.plugin_host.reconcile import _canon

    url = os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")
    try:
        from gdx_dispatch.core.plugin_consent import internal_auth_headers

        r = httpx.get(f"{url}/api/plugins", timeout=5.0, headers=internal_auth_headers())
        r.raise_for_status()
        return {
            _canon(p["distribution"]): p["version"]
            for p in r.json()
            if p.get("distribution") and p.get("version")
        }
    except Exception:
        log.warning("plugin-host catalog unavailable — storefront omits running versions")
        return {}


def _desired_versions(db: Session) -> dict[str, str]:
    from gdx_dispatch.plugin_host.reconcile import desired_versions

    ensure_registry_table(db)
    ensure_artifact_table(db)
    return desired_versions(db)


@router.get("/storefront")
def browse_storefront(
    _: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
) -> dict:
    """The curated plugin catalog, annotated with what this instance has.

    Returns an error string rather than an empty list when the catalog can't be
    read: an empty store would read as "there are no plugins", which is a
    different and wrong statement.
    """
    from gdx_dispatch.core import plugin_storefront as store

    try:
        entries = store.fetch_catalog()
    except store.StorefrontError as exc:
        return {"plugins": [], "error": exc.owner_message, "catalog_url": store.catalog_url()}

    return {
        "plugins": store.merge_install_state(entries, _desired_versions(db), _running_versions()),
        "error": None,
        "catalog_url": store.catalog_url(),
    }


@router.post("/storefront/install", status_code=201)
def install_from_storefront(
    body: StorefrontInstall,
    request: Request,
    user: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
) -> dict:
    """Install a catalog plugin: fetch its wheel here, verify it, record it.

    The app does the downloading because plugin-host has no egress. Only the
    plugin KEY and VERSION come from the caller — the URL is resolved from the
    catalog, so this cannot be aimed at an arbitrary host. From the
    plugin_artifact row onward this is the ordinary upload path, which is why
    the response says "restart to apply" rather than claiming it is running.
    """
    from gdx_dispatch.core import plugin_storefront as store

    try:
        entry = store.find_entry(body.key, body.version)
        filename, content = store.download_wheel(entry)
    except store.StorefrontError as exc:
        raise HTTPException(status_code=502, detail=exc.owner_message) from None

    name = safe_artifact_name(filename)
    if name is None:
        raise HTTPException(502, f"catalog wheel has an unusable filename: {filename!r}")

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
        {"f": name, "h": digest, "c": content,
         "by": f"storefront:{_actor(user)}"},
    )
    # Deliberately NOT a plugin_registry row: that would make plugin-host try to
    # pip-install the package from an index it cannot reach, failing every boot.
    _audit(
        db,
        request,
        user,
        "plugin.storefront_installed",
        entity_type="plugin",
        entity_id=entry["key"],
        details={"key": entry["key"], "distribution": entry["distribution"],
                 "version": entry["version"], "filename": name, "sha256": digest,
                 "permissions": entry["permissions"]},
    )
    db.commit()
    return {
        "key": entry["key"],
        "version": entry["version"],
        "filename": name,
        "sha256": digest,
        "status": "pending_restart",
        "note": "recorded — restart plugin-host to load it",
    }


@router.get("/{key}/permissions")
def plugin_permissions(
    key: str,
    _: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
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
    request: Request,
    user: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
) -> dict:
    """Owner grants consent for the plugin's currently-declared permissions.
    Records exactly what was declared now, so a later-added permission isn't
    silently covered by old consent."""
    declared = fetch_permissions(key)
    if not declared:
        raise HTTPException(status_code=400, detail="plugin declares no permissions")
    # Stage the grant first (commit=False), audit second, commit once — so the
    # consent row and its audit row land in the same transaction. Auditing first
    # would not work: record_consent's ensure_consent_table commits its DDL
    # before the INSERT, which would harden the audit row on its own and leave a
    # record of a grant that never happened if the INSERT then failed.
    record_consent(db, key, declared, _actor(user), commit=False)
    _audit(
        db,
        request,
        user,
        "plugin.consent_granted",
        entity_type="plugin",
        entity_id=key,
        details={"key": key, "permissions": list(declared)},
    )
    db.commit()
    return {"key": key, "consented": declared}


@router.post("/restart", status_code=202)
def restart_plugin_host(
    request: Request,
    user: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
) -> dict:
    """Trigger a plugin-host restart so pending installs/removals take effect.
    Safe from inside the app: plugin-host is a separate container, so the core
    app keeps serving while it cycles (unlike app self-update). Best-effort —
    plugin-host may already be cycling or not deployed; the UI polls
    /api/plugins to confirm it comes back."""
    # Audited before the trigger fires: a restart is what makes pending plugin
    # code go live, and an unrecordable restart must not happen at all.
    _audit(db, request, user, "plugin_host.restart_requested", entity_type="plugin_host")
    db.commit()
    url = os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")
    try:
        from gdx_dispatch.core.plugin_consent import internal_auth_headers

        httpx.post(f"{url}/internal/restart", timeout=5.0, headers=internal_auth_headers())
    except Exception:
        log.warning("plugin-host restart trigger failed (may be cycling)")
    # The permission catalog caches the installed-plugin list; a restart is
    # exactly when that list changes, so drop it rather than making the owner
    # wait out the TTL to see the new plugin's permission checkboxes.
    from gdx_dispatch.core.plugin_permissions import reset_catalog_cache

    reset_catalog_cache()
    return {"status": "restart requested"}


@router.delete("/{package}")
def remove_plugin(
    package: str,
    request: Request,
    user: dict = Depends(_require_owner),
    db: Session = Depends(audit_ready_db),
) -> dict:
    ensure_registry_table(db)
    # Report what actually happened. This used to DELETE unconditionally and
    # return {"status": "unregistered"} whether or not a row matched — a
    # success response for work never done, and worse, an audit row asserting
    # an unregistration that never occurred. The audit trail is append-only, so
    # a false entry there cannot be walked back.
    result = db.execute(
        text("DELETE FROM plugin_registry WHERE package = :p"), {"p": package}
    )
    if (result.rowcount or 0) == 0:
        db.rollback()
        raise HTTPException(
            404,
            f"No registry entry for '{package}'. A plugin installed from an "
            "uploaded wheel is removed under 'Installed from an uploaded file' "
            "on the Plugins page, not here.",
        )
    _audit(
        db,
        request,
        user,
        "plugin.unregistered",
        entity_type="plugin",
        entity_id=package,
        details={"package": package},
    )
    db.commit()
    return {"package": package, "status": "unregistered"}
