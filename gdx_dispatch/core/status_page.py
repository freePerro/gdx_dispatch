"""gdx_dispatch/core/status_page.py — Public status page data layer and router.

Stores service component statuses and incident records in Redis.
All data is public-readable (no auth required on the read paths).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Query
from redis import Redis
from redis import from_url as redis_from_url

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_INCIDENTS_KEY = "status:incidents"
_SERVICES_KEY = "status:services"

SERVICE_COMPONENTS: list[str] = [
    "API",
    "Database",
    "Email",
    "SMS",
    "QuickBooks Sync",
    "Payments",
    "GPS Tracking",
]


class StatusLevel(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    OUTAGE = "outage"


# ---------------------------------------------------------------------------
# Redis singleton
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_redis() -> Redis:
    return redis_from_url(
        os.getenv("REDIS_URL", _DEFAULT_REDIS_URL),
        decode_responses=True,
    )


# ---------------------------------------------------------------------------
# Service status
# ---------------------------------------------------------------------------


def update_service_status(service_name: str, status: str) -> None:
    """Set the current status for a named service component.

    Raises ``ValueError`` for unknown service names or invalid status values.
    """
    valid_statuses = {s.value for s in StatusLevel}
    if service_name not in SERVICE_COMPONENTS:
        raise ValueError(f"Unknown service: {service_name!r}. Must be one of {SERVICE_COMPONENTS}")
    if status not in valid_statuses:
        raise ValueError(f"Invalid status: {status!r}. Must be one of {sorted(valid_statuses)}")

    r = _get_redis()
    raw = r.get(_SERVICES_KEY)
    services: dict[str, str] = json.loads(raw) if raw else {}
    services[service_name] = status
    r.set(_SERVICES_KEY, json.dumps(services))


def _load_service_statuses() -> dict[str, str]:
    """Return the current status dict from Redis, defaulting all to operational."""
    try:
        r = _get_redis()
        raw = r.get(_SERVICES_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        logger.warning("_load_service_statuses error", exc_info=True)
    return {}


def _derive_overall(services: list[dict]) -> str:
    statuses = {s["status"] for s in services}
    if StatusLevel.OUTAGE.value in statuses:
        return StatusLevel.OUTAGE.value
    if StatusLevel.DEGRADED.value in statuses:
        return StatusLevel.DEGRADED.value
    return StatusLevel.OPERATIONAL.value


def get_current_status() -> dict:
    """Return the current platform status suitable for the public status page."""
    stored = _load_service_statuses()
    services = [
        {
            "name": name,
            "status": stored.get(name, StatusLevel.OPERATIONAL.value),
            "latency_ms": None,
        }
        for name in SERVICE_COMPONENTS
    ]
    return {
        "overall": _derive_overall(services),
        "services": services,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


def record_incident(title: str, description: str, severity: str) -> str:
    """Create and persist a new incident.  Returns the new incident id."""
    valid_severities = {"critical", "major", "minor"}
    if severity not in valid_severities:
        raise ValueError(f"Invalid severity: {severity!r}. Must be one of {sorted(valid_severities)}")

    incident_id = uuid4().hex
    incident = {
        "id": incident_id,
        "title": title,
        "description": description,
        "severity": severity,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": None,
        "resolution": None,
    }

    try:
        r = _get_redis()
        raw = r.get(_INCIDENTS_KEY)
        incidents: list[dict] = json.loads(raw) if raw else []
        incidents.insert(0, incident)
        r.set(_INCIDENTS_KEY, json.dumps(incidents[:200]))
    except Exception:
        logger.error("record_incident storage error", exc_info=True)

    return incident_id


def resolve_incident(incident_id: str, resolution: str) -> bool:
    """Mark an incident as resolved.  Returns ``True`` if found, ``False`` otherwise."""
    try:
        r = _get_redis()
        raw = r.get(_INCIDENTS_KEY)
        if not raw:
            return False
        incidents: list[dict] = json.loads(raw)
        found = False
        for inc in incidents:
            if inc.get("id") == incident_id:
                inc["status"] = "resolved"
                inc["resolved_at"] = datetime.now(timezone.utc).isoformat()
                inc["resolution"] = resolution
                found = True
                break
        if found:
            r.set(_INCIDENTS_KEY, json.dumps(incidents))
        return found
    except Exception:
        logger.error("resolve_incident error", exc_info=True)
        return False


def get_incident_history(days: int = 30) -> list[dict]:
    """Return incidents created within the last *days* days, newest first."""
    try:
        r = _get_redis()
        raw = r.get(_INCIDENTS_KEY)
        if not raw:
            return []
        incidents: list[dict] = json.loads(raw)
        cutoff_ts = time.time() - days * 86400
        result = []
        for inc in incidents:
            try:
                created = datetime.fromisoformat(inc["created_at"])
                if created.timestamp() >= cutoff_ts:
                    result.append(inc)
            except (KeyError, ValueError):
                logging.getLogger(__name__).exception("get_incident_history caught exception")
                continue
        return result
    except Exception:
        logger.warning("get_incident_history error", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Uptime history (for 90-day graph)
# ---------------------------------------------------------------------------


def get_uptime_history(days: int = 90) -> list[dict]:
    """Return daily uptime percentages for the last *days* days, oldest first.

    Reads from the ``sla:health_checks`` Redis list written by
    :func:`gdx_dispatch.core.sla_monitor.track_request_latency`.
    """
    from collections import defaultdict

    try:
        r = _get_redis()
        raw = r.lrange("sla:health_checks", 0, -1)
        cutoff = time.time() - days * 86400

        day_total: dict[str, int] = defaultdict(int)
        day_up: dict[str, int] = defaultdict(int)

        for item in raw:
            try:
                entry = json.loads(item)
                ts = entry.get("ts", 0)
                if ts < cutoff:
                    continue
                day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                day_total[day] += 1
                if entry.get("status_code", 500) < 500:
                    day_up[day] += 1
            except (json.JSONDecodeError, TypeError, ValueError, OSError):
                logging.getLogger(__name__).exception("get_uptime_history caught exception")
                continue

        result = []
        for day, total in day_total.items():
            pct = round(day_up[day] / total * 100.0, 3) if total else 100.0
            result.append({"date": day, "uptime_pct": pct})

        return sorted(result, key=lambda x: x["date"])
    except Exception:
        logger.warning("get_uptime_history error", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# FastAPI router — public JSON endpoints consumed by status_page.html
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("/incidents")
def list_incidents(
    days: Annotated[int, Query(ge=1, le=365, description="Number of days of history to return")] = 30,
) -> list[dict]:
    """Return incident history for the last *days* days (public, no auth)."""
    return get_incident_history(days=days)


@router.get("/uptime-history")
def uptime_history(
    days: Annotated[int, Query(ge=1, le=365, description="Number of days of uptime history")] = 90,
) -> list[dict]:
    """Return daily uptime percentages (public, no auth)."""
    return get_uptime_history(days=days)


@router.get("/services")
def list_services() -> list[dict]:
    """Return current status for all service components (public, no auth)."""
    return get_service_status()


# ---------------------------------------------------------------------------
# Additional data-layer helpers
# ---------------------------------------------------------------------------


def get_service_status() -> list[dict]:
    """Return a list of all tracked services with their current status.

    Each entry: ``{"name": str, "status": str}``.
    Defaults to ``operational`` for any service not explicitly set.
    """
    stored = _load_service_statuses()
    return [
        {
            "name": name,
            "status": stored.get(name, StatusLevel.OPERATIONAL.value),
        }
        for name in SERVICE_COMPONENTS
    ]


def get_incidents(limit: int = 20) -> list[dict]:
    """Return the *limit* most recent incidents (across the last 90 days)."""
    all_incidents = get_incident_history(days=90)
    return all_incidents[:max(1, limit)]


def create_incident(
    title: str,
    severity: str,
    affected_services: list[str],
) -> str:
    """Create a new incident record.

    Validates *severity* against ``{critical, major, minor}`` and each entry of
    *affected_services* against :data:`SERVICE_COMPONENTS`.  Returns the new
    incident id.
    """
    valid_severities = {"critical", "major", "minor"}
    if severity not in valid_severities:
        raise ValueError(
            f"Invalid severity: {severity!r}. Must be one of {sorted(valid_severities)}"
        )
    for svc in affected_services:
        if svc not in SERVICE_COMPONENTS:
            raise ValueError(
                f"Unknown service: {svc!r}. Must be one of {SERVICE_COMPONENTS}"
            )

    # Build a description from the affected services list
    description = (
        f"Affected services: {', '.join(affected_services)}"
        if affected_services
        else "No specific services listed."
    )
    incident_id = record_incident(title=title, description=description, severity=severity)

    # Attach affected_services and an empty updates timeline to the stored record
    try:
        r = _get_redis()
        raw = r.get(_INCIDENTS_KEY)
        incidents: list[dict] = json.loads(raw) if raw else []
        for inc in incidents:
            if inc.get("id") == incident_id:
                inc["affected_services"] = affected_services
                inc["updates"] = []
                break
        r.set(_INCIDENTS_KEY, json.dumps(incidents[:200]))
    except Exception:
        logger.error("create_incident patch error", exc_info=True)

    return incident_id


def update_incident(incident_id: str, status: str, message: str) -> bool:
    """Append a timeline update to an existing incident.

    *status* should be one of ``investigating``, ``identified``, ``monitoring``,
    or ``resolved`` (informational — does **not** close the incident; use
    :func:`resolve_incident` to close it).

    Returns ``True`` if the incident was found and updated, ``False`` otherwise.
    """
    try:
        r = _get_redis()
        raw = r.get(_INCIDENTS_KEY)
        if not raw:
            return False
        incidents: list[dict] = json.loads(raw)
        found = False
        for inc in incidents:
            if inc.get("id") == incident_id:
                if "updates" not in inc or not isinstance(inc["updates"], list):
                    inc["updates"] = []
                inc["updates"].append(
                    {
                        "status": status,
                        "message": message,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                found = True
                break
        if found:
            r.set(_INCIDENTS_KEY, json.dumps(incidents))
        return found
    except Exception:
        logger.error("update_incident error", exc_info=True)
        return False


def get_uptime_stats(service: str, days: int = 90) -> dict:
    """Return uptime statistics for a single *service* over *days* days.

    Tries ``status:uptime:{service}`` Redis key first (written by external
    monitors).  Falls back to the aggregate from :func:`get_uptime_history`
    when the per-service key is absent.

    Returns ``{"service": str, "uptime_pct": float, "days": int}``.
    """
    if service not in SERVICE_COMPONENTS:
        raise ValueError(
            f"Unknown service: {service!r}. Must be one of {SERVICE_COMPONENTS}"
        )

    # Try dedicated per-service key first
    try:
        r = _get_redis()
        key = f"status:uptime:{service}"
        raw = r.get(key)
        if raw:
            data = json.loads(raw)
            uptime_pct = float(data.get("uptime_pct", 100.0))
            return {"service": service, "uptime_pct": uptime_pct, "days": days}
    except Exception:
        logger.warning("get_uptime_stats per-service key error", exc_info=True)

    # Fallback: derive from aggregate uptime history
    history = get_uptime_history(days=days)
    if not history:
        return {"service": service, "uptime_pct": 100.0, "days": days}
    avg = sum(d["uptime_pct"] for d in history) / len(history)
    return {"service": service, "uptime_pct": round(avg, 3), "days": days}


# ---------------------------------------------------------------------------
# Pydantic request models for admin endpoints
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field  # noqa: E402 — import after module-level setup


class CreateIncidentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    severity: str = Field(..., pattern="^(critical|major|minor)$")
    affected_services: list[str] = Field(default_factory=list)


class UpdateIncidentRequest(BaseModel):
    status: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=2000)


class ResolveIncidentRequest(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Admin router — write endpoints (auth required)
# ---------------------------------------------------------------------------

admin_router = APIRouter(prefix="/api/admin/incidents", tags=["status-admin"])


def _get_require_role():
    """Lazy import to avoid circular imports at module load time."""
    from gdx_dispatch.core.modules import require_role  # noqa: PLC0415
    return require_role


@admin_router.post(
    "",
    status_code=201,
    responses={422: {"description": "Invalid severity or unknown service name"}},
)
def admin_create_incident(body: CreateIncidentRequest) -> dict:
    """Create a new incident (admin/owner only)."""
    from fastapi import HTTPException as _HTTPException  # noqa: PLC0415
    try:
        incident_id = create_incident(
            title=body.title,
            severity=body.severity,
            affected_services=body.affected_services,
        )
    except ValueError as exc:
        raise _HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": incident_id, "created": True}


@admin_router.patch(
    "/{incident_id}",
    responses={404: {"description": "Incident not found"}},
)
def admin_update_incident(incident_id: str, body: UpdateIncidentRequest) -> dict:
    """Append a timeline update to an existing incident (admin/owner only)."""
    from fastapi import HTTPException as _HTTPException  # noqa: PLC0415
    ok = update_incident(
        incident_id=incident_id,
        status=body.status,
        message=body.message,
    )
    if not ok:
        raise _HTTPException(status_code=404, detail="Incident not found")
    return {"id": incident_id, "updated": True}


@admin_router.post(
    "/{incident_id}/resolve",
    responses={404: {"description": "Incident not found"}},
)
def admin_resolve_incident(incident_id: str, body: ResolveIncidentRequest) -> dict:
    """Resolve an incident (admin/owner only)."""
    from fastapi import HTTPException as _HTTPException  # noqa: PLC0415
    ok = resolve_incident(incident_id=incident_id, resolution=body.resolution)
    if not ok:
        raise _HTTPException(status_code=404, detail="Incident not found")
    return {"id": incident_id, "resolved": True}
