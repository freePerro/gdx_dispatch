"""Phone.com Celery beat tasks.

One task wires `run_full_resync` into the periodic schedule:

- ``sync_all_phone_com_tenants``: beat task — every 15 min. Snapshots all
  tenants from the control DB, dispatches one ``run_phone_com_sync`` job
  per tenant whose ``tenant_settings.phone_com_token_enc`` is non-NULL.

- ``run_phone_com_sync(tenant_id)``: per-tenant worker — opens its own
  control-db session, calls :func:`gdx_dispatch.modules.phone_com.sync.run_full_resync`,
  stamps ``app_settings.phone_com_last_synced_at``, returns the count
  summary. Idempotent (the underlying upserts are keyed on Phone.com IDs).

The fan-out shape mirrors ``outlook.renew_all_outlook_subscriptions`` —
take a snapshot, close the iteration session, then dispatch per-tenant
worker tasks so one slow tenant doesn't block the others.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any
from uuid import UUID

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal, SessionLocal
from gdx_dispatch.control.models import Tenant, TenantSettings
from gdx_dispatch.modules.phone_com.stats import roll_up_recent
from gdx_dispatch.modules.phone_com.sync import _open_tenant_session, run_full_resync


log = logging.getLogger("gdx_dispatch.modules.phone_com.tasks")


@celery_app.task(name="phone_com.run_phone_com_sync", queue="priority:low", bind=True)
def run_phone_com_sync(self, tenant_id: str) -> dict[str, Any]:
    """Per-tenant sync worker. Catches its own exceptions so one bad tenant
    doesn't poison the queue. ``run_full_resync`` itself stamps
    ``app_settings.phone_com_last_synced_at`` on success (Wave B / S17)."""
    _ = self
    tid = UUID(tenant_id) if not isinstance(tenant_id, UUID) else tenant_id
    with contextlib.closing(SessionLocal()) as cdb:
        tenant = cdb.get(Tenant, tid)
        if tenant is None:
            return {"ok": False, "error": "unknown tenant", "tenant_id": str(tid)}
        try:
            result = run_full_resync(cdb, tid)
        except Exception as exc:  # noqa: BLE001
            log.exception("phone_com.run_phone_com_sync failed tenant=%s", tid)
            return {"ok": False, "error": str(exc), "tenant_id": str(tid)}

    return result | {"tenant_id": str(tid)}


@celery_app.task(name="phone_com.sync_all_phone_com_tenants", queue="priority:low", bind=True)
def sync_all_phone_com_tenants(self) -> dict[str, int]:
    """Beat task: dispatch run_phone_com_sync for every tenant with a token set.

    Snapshots tenant ids inside one short control-db session, then closes
    it before fanning out — long-running per-tenant work runs in its own
    task so one slow tenant doesn't block the rest.
    """
    _ = self
    with contextlib.closing(SessionLocal()) as cdb:
        rows = (
            cdb.query(TenantSettings.tenant_id)
            .filter(TenantSettings.phone_com_token_enc.isnot(None))
            .all()
        )
        tenant_ids = [str(r[0]) for r in rows]

    for tid in tenant_ids:
        run_phone_com_sync.delay(tid)

    log.info("phone_com.sync_all_phone_com_tenants dispatched=%d", len(tenant_ids))
    return {"dispatched": len(tenant_ids)}


@celery_app.task(name="phone_com.roll_up_phone_com_stats", queue="priority:low", bind=True)
def roll_up_phone_com_stats(self, tenant_id: str) -> dict[str, Any]:
    """Per-tenant nightly stats backstop. The 15-min sync also rolls up the
    last 7 days inline (sync.py:197); this task catches the case where sync
    erred mid-run and the inline roll-up was skipped — stats still advance.
    Idempotent: roll_up_recent upserts by stat_date.
    """
    _ = self
    tid = UUID(tenant_id) if not isinstance(tenant_id, UUID) else tenant_id
    with contextlib.closing(SessionLocal()) as tenant_db:
        try:
            result = roll_up_recent(tenant_db, days=7)
        except Exception as exc:  # noqa: BLE001
            log.exception("phone_com.roll_up_phone_com_stats failed tenant=%s", tid)
            return {"ok": False, "error": str(exc), "tenant_id": str(tid)}
    return result | {"ok": True, "tenant_id": str(tid)}


@celery_app.task(name="phone_com.reconcile_call_reports", queue="priority:low", bind=True)
def reconcile_call_reports(self, tenant_id: str) -> dict[str, Any]:
    """P3.11 — compare Phone.com /call-reports against phone_com_stats_daily."""
    _ = self
    from gdx_dispatch.modules.phone_com import key_storage
    from gdx_dispatch.modules.phone_com.client import PhoneComClient
    from gdx_dispatch.modules.phone_com.reconcile import reconcile_recent
    from gdx_dispatch.models.tenant_models import AppSettings

    tid = UUID(tenant_id) if not isinstance(tenant_id, UUID) else tenant_id
    with contextlib.closing(SessionLocal()) as cdb:
        token = key_storage.get_token(cdb, tid)
        if token is None:
            return {"ok": False, "skipped": "no token", "tenant_id": str(tid)}
        tenant_db = _open_tenant_session(cdb, tid)
        if tenant_db is None:
            return {"ok": False, "error": "no tenant db", "tenant_id": str(tid)}
        try:
            app = tenant_db.query(AppSettings).first()
            voip_id = (
                int(app.phone_com_voip_id)
                if app is not None and app.phone_com_voip_id
                else None
            )
            if voip_id is None:
                return {"ok": False, "error": "no voip_id", "tenant_id": str(tid)}
            try:
                with PhoneComClient(token=token, voip_id=voip_id) as c:
                    result = reconcile_recent(tenant_db, c, days=7)
            except Exception as exc:  # noqa: BLE001
                log.exception("phone_com.reconcile_call_reports failed tenant=%s", tid)
                return {"ok": False, "error": str(exc), "tenant_id": str(tid)}
        finally:
            tenant_db.close()
    return result | {"tenant_id": str(tid)}


@celery_app.task(name="phone_com.reconcile_all_call_reports", queue="priority:low", bind=True)
def reconcile_all_call_reports(self) -> dict[str, int]:
    _ = self
    with contextlib.closing(SessionLocal()) as cdb:
        rows = (
            cdb.query(TenantSettings.tenant_id)
            .filter(TenantSettings.phone_com_token_enc.isnot(None))
            .all()
        )
        tenant_ids = [str(r[0]) for r in rows]
    for tid in tenant_ids:
        reconcile_call_reports.delay(tid)
    log.info("phone_com.reconcile_all_call_reports dispatched=%d", len(tenant_ids))
    return {"dispatched": len(tenant_ids)}


@celery_app.task(name="phone_com.push_contacts", queue="priority:low", bind=True)
def push_contacts(self, tenant_id: str) -> dict[str, Any]:
    """P2.8 — push GDX customers as Phone.com contacts for one tenant.
    See push_contacts.push_contacts_for_tenant for the inner loop."""
    _ = self
    from gdx_dispatch.modules.phone_com import key_storage
    from gdx_dispatch.modules.phone_com.client import PhoneComClient
    from gdx_dispatch.modules.phone_com.push_contacts import push_contacts_for_tenant
    from gdx_dispatch.models.tenant_models import AppSettings

    tid = UUID(tenant_id) if not isinstance(tenant_id, UUID) else tenant_id
    with contextlib.closing(SessionLocal()) as cdb:
        token = key_storage.get_token(cdb, tid)
        if token is None:
            return {"ok": False, "skipped": "no token", "tenant_id": str(tid)}
        tenant_db = _open_tenant_session(cdb, tid)
        if tenant_db is None:
            return {"ok": False, "error": "no tenant db", "tenant_id": str(tid)}
        try:
            app = tenant_db.query(AppSettings).first()
            voip_id = (
                int(app.phone_com_voip_id)
                if app is not None and app.phone_com_voip_id
                else None
            )
            if voip_id is None:
                return {"ok": False, "error": "no voip_id", "tenant_id": str(tid)}
            try:
                with PhoneComClient(token=token, voip_id=voip_id) as c:
                    result = push_contacts_for_tenant(tenant_db, c)
            except Exception as exc:  # noqa: BLE001
                log.exception("phone_com.push_contacts failed tenant=%s", tid)
                return {"ok": False, "error": str(exc), "tenant_id": str(tid)}
        finally:
            tenant_db.close()
    return result | {"tenant_id": str(tid)}


@celery_app.task(name="phone_com.push_all_contacts", queue="priority:low", bind=True)
def push_all_contacts(self) -> dict[str, int]:
    """Beat task — fan out push_contacts to every configured tenant."""
    _ = self
    with contextlib.closing(SessionLocal()) as cdb:
        rows = (
            cdb.query(TenantSettings.tenant_id)
            .filter(TenantSettings.phone_com_token_enc.isnot(None))
            .all()
        )
        tenant_ids = [str(r[0]) for r in rows]
    for tid in tenant_ids:
        push_contacts.delay(tid)
    log.info("phone_com.push_all_contacts dispatched=%d", len(tenant_ids))
    return {"dispatched": len(tenant_ids)}


@celery_app.task(name="phone_com.rotate_webhook_secret", queue="priority:low", bind=True)
def rotate_webhook_secret(self, tenant_id: str) -> dict[str, Any]:
    """P1.4 — rotate one tenant's webhook URL secret.

    Steps (each must complete before the next):
      1. Generate a new secret in TenantSettings, copy old into ``_prev``
         with a 1-hour grace window.
      2. Build the new public URL with the new secret.
      3. PATCH the Phone.com callback to the new URL.
      4. On PATCH success: leave the rotation in place (grace window
         lets in-flight retries still 200 against the old URL).
      5. On PATCH failure: revert the rotation so the old URL is the
         active one again. Logs the failure for ops.
    """
    _ = self
    import os
    from gdx_dispatch.modules.phone_com import key_storage
    from gdx_dispatch.modules.phone_com.client import PhoneComClient

    tid = UUID(tenant_id) if not isinstance(tenant_id, UUID) else tenant_id
    base = os.environ.get("TENANT_BASE_DOMAIN", "example.com").strip("/")
    with contextlib.closing(SessionLocal()) as cdb:
        settings = cdb.get(TenantSettings, tid)
        tenant = cdb.get(Tenant, tid)
        if (
            settings is None
            or not settings.phone_com_token_enc
            or not settings.phone_com_webhook_secret
            or not settings.phone_com_webhook_callback_id
            or tenant is None
            or not tenant.slug
        ):
            return {"ok": False, "skipped": "not configured", "tenant_id": str(tid)}
        callback_id = settings.phone_com_webhook_callback_id
        token = key_storage.get_token(cdb, tid)
        if token is None:
            return {"ok": False, "skipped": "no token", "tenant_id": str(tid)}
        # Resolve voip_id from tenant DB
        tenant_db = _open_tenant_session(cdb, tid)
        if tenant_db is None:
            return {"ok": False, "error": "no tenant db", "tenant_id": str(tid)}
        try:
            from gdx_dispatch.models.tenant_models import AppSettings
            app = tenant_db.query(AppSettings).first()
            voip_id = (
                int(app.phone_com_voip_id)
                if app is not None and app.phone_com_voip_id
                else None
            )
        finally:
            tenant_db.close()
        if voip_id is None:
            return {"ok": False, "error": "no voip_id", "tenant_id": str(tid)}

        # Stage the rotation
        _old, new_secret = key_storage.rotate_webhook_secret(cdb, tid)
        new_url = (
            f"https://{tenant.slug}.{base}/api/webhooks/phone-com/"
            f"{tenant.slug}/{new_secret}"
        )
        # Push the new URL to Phone.com
        try:
            with PhoneComClient(token=token, voip_id=voip_id) as c:
                c.patch_callback(callback_id=callback_id, url=new_url)
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "phone_com.rotate_webhook_secret patch failed tenant=%s", tid,
            )
            key_storage.revert_webhook_secret(cdb, tid)
            return {
                "ok": False, "error": f"patch failed: {exc}",
                "tenant_id": str(tid), "reverted": True,
            }
    log.info("phone_com.rotate_webhook_secret ok tenant=%s callback_id=%s", tid, callback_id)
    return {"ok": True, "tenant_id": str(tid), "callback_id": callback_id}


@celery_app.task(name="phone_com.rotate_all_webhook_secrets", queue="priority:low", bind=True)
def rotate_all_webhook_secrets(self) -> dict[str, int]:
    """Beat task — fan out webhook secret rotation to every configured tenant."""
    _ = self
    with contextlib.closing(SessionLocal()) as cdb:
        rows = (
            cdb.query(TenantSettings.tenant_id)
            .filter(
                TenantSettings.phone_com_token_enc.isnot(None),
                TenantSettings.phone_com_webhook_callback_id.isnot(None),
            )
            .all()
        )
        tenant_ids = [str(r[0]) for r in rows]
    for tid in tenant_ids:
        rotate_webhook_secret.delay(tid)
    log.info("phone_com.rotate_all_webhook_secrets dispatched=%d", len(tenant_ids))
    return {"dispatched": len(tenant_ids)}


@celery_app.task(name="phone_com.roll_up_all_phone_com_stats", queue="priority:low", bind=True)
def roll_up_all_phone_com_stats(self) -> dict[str, int]:
    """Beat task: dispatch roll_up_phone_com_stats for every tenant with a
    Phone.com token. Same fan-out shape as sync_all_phone_com_tenants.
    """
    _ = self
    with contextlib.closing(SessionLocal()) as cdb:
        rows = (
            cdb.query(TenantSettings.tenant_id)
            .filter(TenantSettings.phone_com_token_enc.isnot(None))
            .all()
        )
        tenant_ids = [str(r[0]) for r in rows]

    for tid in tenant_ids:
        roll_up_phone_com_stats.delay(tid)

    log.info("phone_com.roll_up_all_phone_com_stats dispatched=%d", len(tenant_ids))
    return {"dispatched": len(tenant_ids)}
