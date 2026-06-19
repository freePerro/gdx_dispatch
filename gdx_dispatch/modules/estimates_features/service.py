"""Estimates-features resolver — read tenant flags, enforce on writes."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text

from gdx_dispatch.core.database import SessionLocal, tenant_context

log = logging.getLogger(__name__)


@dataclass
class EstimatesFeatures:
    allow_line_margin_override: bool = True
    default_terms: str = ""
    email_subject_template: str = ""
    email_body_template: str = ""
    deposit_pct: int = 50


def get_features(tenant_id: str) -> EstimatesFeatures:
    """Read per-tenant estimates flags. Best-effort — defaults on any read error."""
    try:
        with tenant_context(tenant_id), SessionLocal() as cdb:
            row = cdb.execute(
                text(
                    "SELECT estimates_allow_line_margin_override, "
                    "       COALESCE(estimates_default_terms, ''), "
                    "       COALESCE(estimate_email_subject_template, ''), "
                    "       COALESCE(estimate_email_body_template, ''), "
                    "       COALESCE(estimate_deposit_pct, 50) "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).first()
            if row is None:
                return EstimatesFeatures()
            return EstimatesFeatures(
                allow_line_margin_override=bool(row[0]),
                default_terms=str(row[1] or ""),
                email_subject_template=str(row[2] or ""),
                email_body_template=str(row[3] or ""),
                deposit_pct=int(row[4] or 0),
            )
    except Exception:
        log.exception("estimates_features_read_failed", extra={"tenant_id": tenant_id})
        return EstimatesFeatures()


def require_line_margin_override_allowed(tenant_id: str) -> None:
    """Raise 403 when the tenant has line margin override disabled."""
    if not get_features(tenant_id).allow_line_margin_override:
        raise HTTPException(
            status_code=403,
            detail="per-line margin override is disabled for this tenant",
        )
