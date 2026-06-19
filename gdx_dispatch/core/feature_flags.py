from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from gdx_dispatch.control.models import PlatformFeatureFlag as FeatureFlag

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _invalidate_flag_cache(flag_key: str, redis_client, tenant_ids: list[str] | None = None) -> None:
    """Delete cached entries for *flag_key*.

    If *tenant_ids* is provided only those scoped keys are removed; otherwise
    a best-effort SCAN is attempted so all tenant cache entries are evicted.
    """
    if redis_client is None:
        return
    if tenant_ids:
        for tid in tenant_ids:
            redis_client.delete(f"flag:{flag_key}:{tid}")
    else:
        try:
            cursor = 0
            pattern = f"flag:{flag_key}:*"
            while True:
                cursor, keys = redis_client.scan(cursor, match=pattern, count=200)
                for k in keys:
                    redis_client.delete(k)
                if cursor == 0:
                    break
        except Exception:
            logging.getLogger(__name__).exception("_invalidate_flag_cache caught exception")
            pass  # Client may not support SCAN — skip silently.


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def is_flag_enabled(flag_key: str, tenant_id: str, db: Session, redis_client=None) -> bool:
    """Return True when *flag_key* is active for *tenant_id*."""
    cache_key = f"flag:{flag_key}:{tenant_id}"
    if redis_client is not None:
        cached = redis_client.get(cache_key)
        if cached is not None:
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8")
            return str(cached).lower() in {"1", "true"}

    flag = db.query(FeatureFlag).filter_by(flag_key=flag_key).first()
    if not flag:
        if redis_client is not None:
            redis_client.setex(cache_key, 60, "0")
        return False

    overrides = flag.tenant_overrides or {}
    if tenant_id in overrides:
        enabled = bool(overrides[tenant_id])
    else:
        digest = hashlib.md5(f"{flag_key}{tenant_id}".encode(), usedforsecurity=False).hexdigest()
        enabled = int(digest, 16) % 100 < flag.rollout_pct

    if redis_client is not None:
        redis_client.setex(cache_key, 60, "1" if enabled else "0")
    return enabled


# ---------------------------------------------------------------------------
# Management API
# ---------------------------------------------------------------------------

def list_flags(db: Session) -> list[dict[str, Any]]:
    """Return all feature flags with description, rollout %, and tenant override count."""
    flags = db.query(FeatureFlag).order_by(FeatureFlag.flag_key).all()
    result = []
    for f in flags:
        overrides = f.tenant_overrides or {}
        result.append({
            "id": str(f.id),
            "flag_key": f.flag_key,
            "description": getattr(f, "description", ""),
            "default_value": getattr(f, "default_value", False),
            "rollout_pct": f.rollout_pct,
            "tenant_count": len(overrides),
            "tenant_overrides": overrides,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        })
    return result


def create_flag(
    key: str,
    description: str,
    default_value: bool,
    rollout_percent: int,
    db: Session,
) -> FeatureFlag:
    """Register a new feature flag.  Raises ValueError on duplicate key or bad rollout."""
    if not 0 <= rollout_percent <= 100:
        raise ValueError(f"rollout_percent must be 0-100, got {rollout_percent}")
    existing = db.query(FeatureFlag).filter_by(flag_key=key).first()
    if existing:
        raise ValueError(f"Feature flag '{key}' already exists")
    flag = FeatureFlag(
        flag_key=key,
        rollout_pct=rollout_percent,
        tenant_overrides={},
    )
    # Set optional extended columns if the model supports them.
    if hasattr(flag, "description"):
        flag.description = description
    if hasattr(flag, "default_value"):
        flag.default_value = default_value
    db.add(flag)
    db.commit()
    db.refresh(flag)
    return flag


def delete_flag(key: str, db: Session, redis_client=None) -> bool:
    """Remove a feature flag entirely.  Returns True if deleted, False if not found."""
    flag = db.query(FeatureFlag).filter_by(flag_key=key).first()
    if not flag:
        return False
    _invalidate_flag_cache(key, redis_client)
    db.delete(flag)
    db.commit()
    return True


def set_rollout_percentage(
    flag_key: str,
    percentage: int,
    db: Session,
    redis_client=None,
) -> None:
    """Update rollout % for a flag across all tenants without overrides."""
    if not 0 <= percentage <= 100:
        raise ValueError(f"percentage must be 0-100, got {percentage}")
    flag = db.query(FeatureFlag).filter_by(flag_key=flag_key).first()
    if not flag:
        flag = FeatureFlag(flag_key=flag_key, rollout_pct=percentage, tenant_overrides={})
        db.add(flag)
    else:
        flag.rollout_pct = percentage
    db.commit()
    _invalidate_flag_cache(flag_key, redis_client)


def set_tenant_override(
    flag_key: str,
    tenant_id: str,
    value: bool,
    db: Session,
    redis_client=None,
) -> None:
    """Force a specific flag value for one tenant (creates flag if absent)."""
    flag = db.query(FeatureFlag).filter_by(flag_key=flag_key).first()
    if not flag:
        flag = FeatureFlag(flag_key=flag_key, rollout_pct=0, tenant_overrides={tenant_id: value})
        db.add(flag)
    else:
        overrides = dict(flag.tenant_overrides or {})
        overrides[tenant_id] = value
        flag.tenant_overrides = overrides
    db.commit()
    if redis_client is not None:
        redis_client.delete(f"flag:{flag_key}:{tenant_id}")


def get_flags_for_tenant(tenant_id: str, db: Session, redis_client=None) -> dict[str, bool]:
    """Return a mapping of every flag key -> evaluated bool for *tenant_id*."""
    flags = db.query(FeatureFlag).order_by(FeatureFlag.flag_key).all()
    result: dict[str, bool] = {}
    for flag in flags:
        result[flag.flag_key] = is_flag_enabled(flag.flag_key, tenant_id, db, redis_client)
    return result


# ---------------------------------------------------------------------------
# Legacy helpers (kept for backwards compatibility)
# ---------------------------------------------------------------------------

def set_flag_rollout(flag_key: str, pct: int, db: Session) -> None:
    """Alias for set_rollout_percentage — retained for backwards compat."""
    set_rollout_percentage(flag_key, pct, db)


def delete_tenant_override(tenant_id: str, flag_key: str, db: Session) -> None:
    """Remove a tenant override so the tenant falls back to rollout percentage."""
    flag = db.query(FeatureFlag).filter_by(flag_key=flag_key).first()
    if not flag:
        return
    overrides = dict(flag.tenant_overrides or {})
    if tenant_id not in overrides:
        return
    del overrides[tenant_id]
    flag.tenant_overrides = overrides
    db.commit()


def get_flag_stats(flag_key: str, db: Session) -> dict[str, Any]:
    """Return aggregate stats for a flag across all tenant overrides."""
    flag = db.query(FeatureFlag).filter_by(flag_key=flag_key).first()
    if not flag:
        return {
            "flag_key": flag_key,
            "rollout_pct": 0,
            "override_count": 0,
            "enabled_overrides": 0,
            "disabled_overrides": 0,
        }
    overrides = flag.tenant_overrides or {}
    enabled_overrides = sum(1 for v in overrides.values() if v)
    disabled_overrides = sum(1 for v in overrides.values() if not v)
    return {
        "flag_key": flag_key,
        "rollout_pct": flag.rollout_pct,
        "override_count": len(overrides),
        "enabled_overrides": enabled_overrides,
        "disabled_overrides": disabled_overrides,
    }
