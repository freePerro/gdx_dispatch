"""Dispatch-settings resolver — read tenant flags, enforce on writes."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text

from gdx_dispatch.core.database import SessionLocal, tenant_context

log = logging.getLogger(__name__)


@dataclass
class DispatchSettings:
    warn_save_no_tech: bool = False
    block_save_no_tech: bool = False
    show_unassigned_lane: bool = False


def get_settings(tenant_id: str) -> DispatchSettings:
    """Read per-tenant dispatch flags. Best-effort — defaults on any read error."""
    try:
        with tenant_context(), SessionLocal() as cdb:
            row = cdb.execute(
                text(
                    "SELECT dispatch_warn_save_no_tech, "
                    "       dispatch_block_save_no_tech, "
                    "       dispatch_show_unassigned_lane "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).first()
            if row is None:
                return DispatchSettings()
            return DispatchSettings(
                warn_save_no_tech=bool(row[0]),
                block_save_no_tech=bool(row[1]),
                show_unassigned_lane=bool(row[2]),
            )
    except Exception:
        log.exception("dispatch_settings_read_failed", extra={"tenant_id": tenant_id})
        return DispatchSettings()


def require_tech_for_scheduled_job(tenant_id: str, scheduled_at, assigned_to) -> None:
    """Raise 422 when the hard gate is on and a scheduled job has no tech."""
    if scheduled_at is None or assigned_to:
        return
    if get_settings(tenant_id).block_save_no_tech:
        raise HTTPException(
            status_code=422,
            detail="A technician is required for scheduled jobs (tenant policy).",
        )
