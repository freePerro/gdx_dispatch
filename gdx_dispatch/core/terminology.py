from __future__ import annotations

import json
import os
from functools import lru_cache

from redis import Redis, from_url
from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from gdx_dispatch.control.models import Base

DEFAULT_TERMINOLOGY = {
    "job": "Job", "estimate": "Estimate", "invoice": "Invoice", "customer": "Customer",
    "technician": "Technician", "dispatcher": "Dispatcher", "work_order": "Work Order", "service_call": "Service Call",
}
INDUSTRY_PRESETS = {
    "garage_door": {"job": "Service Call", "estimate": "Quote", "technician": "Installer"},
    "hvac": {"job": "Work Order", "estimate": "Proposal", "technician": "HVAC Tech"},
    "plumbing": {"job": "Service Call", "estimate": "Quote", "technician": "Plumber"},
    "general_field_service": {},
}


class TerminologyOverride(Base):
    __tablename__ = "terminology_overrides"

    tenant_id: Mapped[str] = mapped_column(String(50), ForeignKey("tenants.id"), primary_key=True)
    overrides: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    industry_preset: Mapped[str | None] = mapped_column(String(50))


@lru_cache(maxsize=1)
def _redis() -> Redis:
    return from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


def get_terminology(tenant_id: str, db: Session) -> dict[str, str]:
    cache_key = f"terminology:{tenant_id}"
    cached = _redis().get(cache_key)
    if cached:
        return json.loads(cached)

    merged = dict(DEFAULT_TERMINOLOGY)
    override = db.query(TerminologyOverride).filter_by(tenant_id=tenant_id).first()
    if override and override.industry_preset:
        merged.update(INDUSTRY_PRESETS.get(override.industry_preset, {}))
    if override and override.overrides:
        merged.update(override.overrides)

    _redis().setex(cache_key, 300, json.dumps(merged))
    return merged


def get_label(term: str, tenant_id: str, db: Session) -> str:
    return get_terminology(tenant_id, db).get(term, DEFAULT_TERMINOLOGY.get(term, term))
