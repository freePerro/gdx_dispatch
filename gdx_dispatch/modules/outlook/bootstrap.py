"""Sprint Outlook Integration — startup bootstrap.

Doug already has an Entra app registered (the ``POWER_APPS_*`` /
``GDX_MICROSOFT_*`` env vars); this module seeds those values into the
GDX tenant's ``TenantSettings.outlook_*`` columns at app startup, so
employees can connect their mailboxes without Doug manually pasting
credentials into the admin UI.

Idempotent: only writes when the destination column is NULL. Safe to
call on every startup. Other tenants are unaffected — they configure
their own creds via ``/api/admin/outlook/credentials`` (slice S39).

ENV VARS (all 3 must be present to seed):
- ``POWER_APPS_TENANT_ID`` → ``TenantSettings.outlook_microsoft_tenant_id``
- ``POWER_APPS_CLIENT_ID`` → ``TenantSettings.outlook_client_id``
- ``GDX_MICROSOFT_SECRET_KEY`` → Fernet-encrypted into
  ``TenantSettings.outlook_client_secret_enc`` (via key_storage)

Set ``OUTLOOK_BOOTSTRAP_TENANT_SLUG`` in env to override the default
``"gdx"`` if Doug needs to seed a different tenant.
"""
from __future__ import annotations

import logging
import os
from contextlib import closing

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant, TenantSettings
from gdx_dispatch.core.database import tenant_context
from gdx_dispatch.modules.outlook import key_storage


log = logging.getLogger("gdx_dispatch.modules.outlook.bootstrap")


DEFAULT_TENANT_SLUG = "gdx"


def seed_outlook_credentials_from_env(
    control_db: Session,
    *,
    tenant_slug: str | None = None,
) -> dict:
    """Idempotent: copy POWER_APPS_* + GDX_MICROSOFT_SECRET_KEY into the
    target tenant's TenantSettings.outlook_* columns when missing.

    Returns a small dict describing what happened so the caller can log it.
    Raises nothing — bootstrap failures must never block app startup. The
    caller wraps the call in try/except for total safety.
    """
    slug = tenant_slug or os.environ.get("OUTLOOK_BOOTSTRAP_TENANT_SLUG") or DEFAULT_TENANT_SLUG

    ms_tenant = os.environ.get("POWER_APPS_TENANT_ID")
    ms_client_id = os.environ.get("POWER_APPS_CLIENT_ID")
    ms_secret = os.environ.get("GDX_MICROSOFT_SECRET_KEY")
    if not (ms_tenant and ms_client_id and ms_secret):
        return {"seeded": False, "reason": "env vars missing"}

    tenant = control_db.query(Tenant).filter(Tenant.slug == slug).one_or_none()
    if tenant is None:
        return {"seeded": False, "reason": f"no tenant with slug={slug}"}

    # Close the read txn so the next one is opened with the tenant GUC set.
    # Without this, any INSERT/UPDATE on tenant_settings is rejected by
    # RLS WITH CHECK because app.tenant_id is empty (the engine `begin`
    # listener at gdx_dispatch/core/database.py:50 reads from the contextvar set by
    # tenant_context, and the previous READ txn began before we knew the
    # tenant id). 2026-04-28 incident: bootstrap silently failed in prod
    # with "InsufficientPrivilege: new row violates row-level security
    # policy for table tenant_settings".
    control_db.commit()

    with tenant_context(str(tenant.id)):
        # Belt-and-suspenders: also issue SET LOCAL on the session itself.
        # The engine begin listener at gdx_dispatch/core/database.py:50 reads the
        # tenant_context contextvar at txn-begin time, but in the lifespan
        # path we observed in 2026-04-28 prod logs the listener didn't
        # propagate the GUC for the INSERT (likely an asyncio contextvar
        # boundary on app startup). An explicit SET LOCAL here is
        # transaction-scoped and safe in all paths.
        control_db.execute(text("SET LOCAL app.tenant_id = :v"), {"v": str(tenant.id)})

        settings = control_db.get(TenantSettings, tenant.id)
        if settings is None:
            settings = TenantSettings()
            settings.tenant_id = tenant.id
            control_db.add(settings)
            control_db.flush()

        # All-or-nothing seed: don't write outlook_microsoft_tenant_id /
        # outlook_client_id without also encrypting the secret. Otherwise a
        # missing GDX_FERNET_KEY at startup leaves the tenant in a half-state
        # where OAuth start succeeds but callback fails with
        # `client_secret_missing` AND the next bootstrap won't re-attempt the
        # secret because the other columns are now populated.

        needs_tenant = not settings.outlook_microsoft_tenant_id
        needs_client = not settings.outlook_client_id
        needs_secret = not settings.outlook_client_secret_enc

        if not (needs_tenant or needs_client or needs_secret):
            return {"seeded": False, "reason": "all columns already populated", "tenant_slug": slug}

        if needs_secret:
            # Try to encrypt FIRST. Skip the whole seed if Fernet isn't ready —
            # avoids the half-state bug.
            try:
                key_storage.set_client_secret(control_db, tenant.id, ms_secret)
            except key_storage.OutlookKeyStorageError as exc:
                log.warning(
                    "outlook bootstrap: GDX_FERNET_KEY not ready, deferring seed for %s: %s",
                    slug, exc,
                )
                control_db.rollback()
                return {"seeded": False, "reason": f"fernet_not_ready: {exc}", "tenant_slug": slug}

        changed: list[str] = []
        if needs_tenant:
            settings.outlook_microsoft_tenant_id = ms_tenant
            changed.append("outlook_microsoft_tenant_id")
        if needs_client:
            settings.outlook_client_id = ms_client_id
            changed.append("outlook_client_id")
        if needs_secret:
            changed.append("outlook_client_secret_enc")

        control_db.commit()
        log.info("outlook bootstrap: seeded %s on tenant %s: %s", slug, tenant.id, changed)
        return {"seeded": True, "tenant_slug": slug, "fields": changed}


def run_outlook_bootstrap_safely() -> dict:
    """App-startup wrapper: opens a control-plane session, calls the seeder,
    catches+logs every exception (with full traceback) so a single bootstrap
    failure cannot block app startup. Returns a status dict for the startup
    logs."""
    try:
        from gdx_dispatch.core.database import SessionLocal
    except Exception as exc:  # noqa: BLE001
        log.exception("outlook bootstrap: control-plane import failed at startup")
        return {"seeded": False, "error": f"control-plane import failed: {exc}"}
    try:
        with closing(SessionLocal()) as cdb:
            return seed_outlook_credentials_from_env(cdb)
    except Exception as exc:  # noqa: BLE001
        log.exception("outlook bootstrap: unexpected failure at startup")
        return {"seeded": False, "error": str(exc)[:200]}
