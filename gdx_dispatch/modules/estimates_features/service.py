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
    # Tenant-wide default for "total-only" estimates. Per-estimate
    # Estimate.hide_line_prices (NULL = inherit this) wins when set.
    hide_line_prices: bool = False
    # Days an estimate stays valid after it's sent (plan §15). On send,
    # valid_until = sent_at + this; the nightly task expires past-due ones.
    estimate_expiry_days: int = 60
    # Invoice + receipt email copy (issue #351, migration 086) — the same
    # blank-means-platform-default contract as the estimate pair above. The
    # defaults themselves live in routers/invoices.py next to the renderer.
    invoice_email_subject_template: str = ""
    invoice_email_body_template: str = ""
    receipt_email_subject_template: str = ""
    receipt_email_body_template: str = ""


def effective_hide_line_prices(override: bool | None, default: bool) -> bool:
    """Resolve the tri-state per-estimate override against the tenant default.

    NULL override → inherit the tenant default; otherwise the explicit
    True/False wins. Single source of truth for every customer-facing surface
    (estimate PDF, email, install sheet, estimate->invoice snapshot)."""
    return bool(default) if override is None else bool(override)


def get_features(tenant_id: str) -> EstimatesFeatures:
    """Read per-tenant estimates flags. Best-effort — defaults on any read error."""
    try:
        with tenant_context(), SessionLocal() as cdb:
            row = cdb.execute(
                text(
                    "SELECT estimates_allow_line_margin_override, "
                    "       COALESCE(estimates_default_terms, ''), "
                    "       COALESCE(estimate_email_subject_template, ''), "
                    "       COALESCE(estimate_email_body_template, ''), "
                    "       COALESCE(estimate_deposit_pct, 50), "
                    "       COALESCE(estimates_hide_line_prices, false), "
                    "       COALESCE(estimate_expiry_days, 60), "
                    "       COALESCE(invoice_email_subject_template, ''), "
                    "       COALESCE(invoice_email_body_template, ''), "
                    "       COALESCE(receipt_email_subject_template, ''), "
                    "       COALESCE(receipt_email_body_template, '') "
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
                hide_line_prices=bool(row[5]),
                estimate_expiry_days=int(row[6]) if row[6] else 60,
                invoice_email_subject_template=str(row[7] or ""),
                invoice_email_body_template=str(row[8] or ""),
                receipt_email_subject_template=str(row[9] or ""),
                receipt_email_body_template=str(row[10] or ""),
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
