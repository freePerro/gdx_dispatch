"""gdx_dispatch/core/sla_monitor.py — SLA monitoring and uptime tracking.

Tracks uptime for API, DB, Redis, Celery, QuickBooks, and Stripe.
Provides a public status page endpoint and admin SLA metrics.

SLA Targets:
  API:   99.9%  monthly uptime (43.8 min downtime budget)
  DB:    99.95% monthly uptime
  Jobs:  99.5%  monthly uptime
  p95 response time: < 500ms
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from redis import Redis
from redis import from_url as redis_from_url
from sqlalchemy import DateTime, Float, Index, Integer, String, text
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.control.models import Base, utcnow
from gdx_dispatch.core.database import SessionLocal, get_db
from gdx_dispatch.core.modules import require_role

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_HEALTH_CHECKS_KEY = "sla:health_checks"

# ---------------------------------------------------------------------------
# SLA targets (Redis-based metrics)
# ---------------------------------------------------------------------------

SLA_TARGETS: dict[str, float] = {
    "p99_ms": 500.0,
    "error_rate_pct": 0.1,
    "uptime_pct": 99.9,
}


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
# Redis-based latency / error tracking
# ---------------------------------------------------------------------------


def track_request_latency(
    endpoint: str,
    duration_ms: float,
    status_code: int,
) -> None:
    """Record a single request observation for *endpoint*.

    Stores the measurement in a Redis sorted set (score = epoch timestamp)
    and appends a health-check tick to the ``sla:health_checks`` list so
    :func:`get_uptime_percentage` can use it.
    """
    try:
        r = _get_redis()
        ts = time.time()
        value = f"{duration_ms}:{status_code}:{ts}"
        key = f"sla:latency:{endpoint}"

        pipe = r.pipeline()
        pipe.zadd(key, {value: ts})
        pipe.zremrangebyrank(key, 0, -10001)  # keep newest 10 000

        hc_entry = json.dumps({"ts": ts, "status_code": status_code})
        pipe.lpush(_HEALTH_CHECKS_KEY, hc_entry)
        pipe.ltrim(_HEALTH_CHECKS_KEY, 0, 9999)

        pipe.execute()
    except Exception:
        logger.warning("track_request_latency error for %s", endpoint, exc_info=True)


def _parse_latency_entries(
    endpoint: str,
    window_minutes: int,
) -> list[tuple[float, int]]:
    """Return ``(duration_ms, status_code)`` pairs within the time window."""
    try:
        r = _get_redis()
        cutoff = time.time() - window_minutes * 60
        raw = r.zrangebyscore(f"sla:latency:{endpoint}", cutoff, "+inf")
        result: list[tuple[float, int]] = []
        for entry in raw:
            parts = entry.split(":", 2)
            if len(parts) >= 2:
                result.append((float(parts[0]), int(parts[1])))
        return result
    except Exception:  # Return empty list if data retrieval or parsing fails.
        logger.warning("_parse_latency_entries error for %s", endpoint, exc_info=True)
        return []


def get_p99_latency(endpoint: str, window_minutes: int = 60) -> float:
    """Return the 99th-percentile request latency in ms over the given window."""
    entries = _parse_latency_entries(endpoint, window_minutes)
    if not entries:
        return 0.0
    latencies = sorted(e[0] for e in entries)
    idx = math.ceil(len(latencies) * 0.99) - 1
    return latencies[max(idx, 0)]


def get_error_rate(endpoint: str, window_minutes: int = 60) -> float:
    """Return the percentage of requests with status_code >= 400 in the window."""
    entries = _parse_latency_entries(endpoint, window_minutes)
    if not entries:
        return 0.0
    errors = sum(1 for _, sc in entries if sc >= 400)
    return round(errors / len(entries) * 100.0, 4)


def get_uptime_percentage(window_days: int = 30) -> float:
    """Return platform uptime % over the last *window_days* days.

    Uses the ``sla:health_checks`` Redis list.  Entries with
    ``status_code < 500`` count as *up*.  Returns ``100.0`` when no data.
    """
    try:
        r = _get_redis()
        cutoff = time.time() - window_days * 86400
        raw = r.lrange(_HEALTH_CHECKS_KEY, 0, -1)
        if not raw:
            return 100.0

        total = 0
        up = 0
        for item in raw:
            try:
                entry = json.loads(item)
                if entry.get("ts", 0) < cutoff:
                    continue
                total += 1
                if entry.get("status_code", 500) < 500:
                    up += 1
            except (json.JSONDecodeError, TypeError):
                logging.getLogger(__name__).exception("get_uptime_percentage caught exception")
                continue

        if total == 0:
            return 100.0
        return round(up / total * 100.0, 4)
    except Exception:
        logger.warning("get_uptime_percentage error", exc_info=True)
        return 100.0


def check_sla_violations(endpoint: str) -> list[dict]:
    """Return a list of SLA metric dicts with violation flags for *endpoint*."""
    p99 = get_p99_latency(endpoint)
    error_rate = get_error_rate(endpoint)
    uptime = get_uptime_percentage()

    return [
        {
            "metric": "p99_latency",
            "value": p99,
            "target": SLA_TARGETS["p99_ms"],
            "violated": p99 > SLA_TARGETS["p99_ms"],
        },
        {
            "metric": "error_rate",
            "value": error_rate,
            "target": SLA_TARGETS["error_rate_pct"],
            "violated": error_rate > SLA_TARGETS["error_rate_pct"],
        },
        {
            "metric": "uptime",
            "value": uptime,
            "target": SLA_TARGETS["uptime_pct"],
            "violated": uptime < SLA_TARGETS["uptime_pct"],
        },
    ]


def alert_if_sla_violated(endpoint: str) -> None:
    """Log and record an alert for any SLA violations on *endpoint*."""
    violations = [v for v in check_sla_violations(endpoint) if v["violated"]]
    if not violations:
        return

    logger.warning(
        "SLA violated for endpoint=%s violations=%s",
        endpoint,
        violations,
    )

    try:
        r = _get_redis()
        alert = {
            "endpoint": endpoint,
            "violations": violations,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        existing_raw = r.get("sla:alerts")
        existing: list[dict] = json.loads(existing_raw) if existing_raw else []
        existing.insert(0, alert)
        r.set("sla:alerts", json.dumps(existing[:100]))
    except Exception:
        logger.warning("alert_if_sla_violated storage error", exc_info=True)

# ---------------------------------------------------------------------------
# SLA target constants
# ---------------------------------------------------------------------------

API_SLA_PCT: float = 99.9
DB_SLA_PCT: float = 99.95
JOBS_SLA_PCT: float = 99.5
RESPONSE_P95_MS: float = 500.0

# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class SLACheck(Base):
    """Latest SLA state per named check — one row per check_name."""

    __tablename__ = "sla_checks"
    __table_args__ = (Index("ix_sla_checks_check_name", "check_name", unique=True),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    check_type: Mapped[str] = mapped_column(String(50), nullable=False)
    check_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    last_response_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    uptime_24h_pct: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    uptime_7d_pct: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    incident_count_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class UptimeRecord(Base):
    """Individual check result — retained for 90 days."""

    __tablename__ = "uptime_records"
    __table_args__ = (
        Index("ix_uptime_records_check_name", "check_name"),
        Index("ix_uptime_records_checked_at", "checked_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    check_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    response_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_uptime_pct(check_name: str, hours: int, db: Session) -> float:
    """Return % of 'ok' UptimeRecord rows for check_name over the last N hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        db.query(UptimeRecord)
        .filter(UptimeRecord.check_name == check_name, UptimeRecord.checked_at >= since)
        .all()
    )
    if not rows:
        return 100.0
    ok_count = sum(1 for r in rows if r.status == "ok")
    return round(ok_count / len(rows) * 100.0, 3)


def get_overall_status(checks: list[SLACheck]) -> str:
    """Derive overall system status from individual check states."""
    if not checks:
        return "operational"
    statuses = {c.last_status for c in checks}
    if "down" in statuses:
        return "outage"
    if "degraded" in statuses:
        return "degraded"
    return "operational"


def _upsert_sla_check(
    db: Session,
    check_name: str,
    check_type: str,
    status: str,
    response_ms: float | None,
) -> None:
    """Create or update the SLACheck row for check_name."""
    now = datetime.now(timezone.utc)
    row = db.query(SLACheck).filter(SLACheck.check_name == check_name).first()
    prev_status = row.last_status if row else "ok"

    if row is None:
        row = SLACheck(
            check_type=check_type,
            check_name=check_name,
            last_status=status,
            last_checked_at=now,
            last_response_ms=response_ms,
            updated_at=now,
        )
        db.add(row)
    else:
        if prev_status == "ok" and status != "ok":
            logger.warning("SLA degradation: %s changed from ok → %s", check_name, status)
        row.last_status = status
        row.last_checked_at = now
        row.last_response_ms = response_ms
        row.updated_at = now

    db.flush()

    row.uptime_24h_pct = compute_uptime_pct(check_name, 24, db)
    row.uptime_7d_pct = compute_uptime_pct(check_name, 168, db)

    if status != "ok":
        row.incident_count_30d = (row.incident_count_30d or 0) + 1


def _record_check(
    db: Session,
    check_name: str,
    check_type: str,
    status: str,
    response_ms: float | None,
) -> None:
    """Persist one UptimeRecord and update the SLACheck summary."""
    record = UptimeRecord(
        check_name=check_name,
        status=status,
        response_ms=response_ms,
        checked_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.flush()
    _upsert_sla_check(db, check_name, check_type, status, response_ms)


def _prune_old_records(db: Session) -> None:
    """Delete UptimeRecord rows older than 90 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    db.query(UptimeRecord).filter(UptimeRecord.checked_at < cutoff).delete(
        synchronize_session=False
    )


# ---------------------------------------------------------------------------
# Individual health probes
# ---------------------------------------------------------------------------


def _check_api() -> tuple[str, float | None]:
    """Probe /health endpoint; return (status, response_ms)."""
    base_url = os.environ.get("GDX_BASE_URL", "http://localhost:8000")
    url = base_url.rstrip("/") + "/health"
    start = time.monotonic()
    try:
        try:
            import httpx

            resp = httpx.get(url, timeout=3.0)
            elapsed = (time.monotonic() - start) * 1000
            if resp.status_code == 200:
                return ("degraded" if elapsed > RESPONSE_P95_MS else "ok", round(elapsed, 2))
            return ("down", round(elapsed, 2))
        except ImportError:
            logging.getLogger(__name__).exception("_check_api caught exception")
            import requests as req_lib

            resp2 = req_lib.get(url, timeout=3)
            elapsed = (time.monotonic() - start) * 1000
            if resp2.status_code == 200:
                return ("degraded" if elapsed > RESPONSE_P95_MS else "ok", round(elapsed, 2))
            return ("down", round(elapsed, 2))
    except Exception as exc:
        logger.warning("API health probe failed: %s", exc)
        return ("down", None)


def _check_db() -> tuple[str, float | None]:
    """Probe control DB with SELECT 1; return (status, response_ms)."""
    start = time.monotonic()
    try:
        from gdx_dispatch.core.database import control_engine

        with control_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        elapsed = round((time.monotonic() - start) * 1000, 2)
        return ("ok", elapsed)
    except Exception as exc:
        logger.warning("DB health probe failed: %s", exc)
        return ("down", None)


def _check_redis() -> tuple[str, float | None]:
    """Probe Redis via REDIS_URL; return (status, response_ms)."""
    start = time.monotonic()
    try:
        import redis

        r = redis.from_url(
            os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL),
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        r.ping()
        elapsed = round((time.monotonic() - start) * 1000, 2)
        return ("ok", elapsed)
    except Exception as exc:
        logger.warning("Redis health probe failed: %s", exc)
        return ("down", None)


def _check_celery() -> tuple[str, float | None]:
    """Probe Celery broker reachability; return (status, response_ms)."""
    start = time.monotonic()
    try:
        broker_url = os.environ.get("CELERY_BROKER_URL", _DEFAULT_REDIS_URL)
        if broker_url.startswith("redis"):
            import redis

            r = redis.from_url(broker_url, socket_connect_timeout=2, socket_timeout=2)
            r.ping()
        else:
            import kombu

            conn = kombu.Connection(broker_url)
            conn.ensure_connection(max_retries=1, timeout=2)
            conn.close()
        elapsed = round((time.monotonic() - start) * 1000, 2)
        return ("ok", elapsed)
    except Exception as exc:
        logger.warning("Celery broker health probe failed: %s", exc)
        return ("down", None)


# ---------------------------------------------------------------------------
# Celery beat task
# ---------------------------------------------------------------------------

_CHECKS = [
    ("api_health", "api", _check_api),
    ("db_health", "db", _check_db),
    ("redis_health", "redis", _check_redis),
    ("celery_broker", "celery", _check_celery),
]


def run_sla_checks_sync() -> list[dict]:
    """Run all SLA checks and persist results. Returns list of result dicts."""
    results: list[dict] = []
    db = SessionLocal()
    try:
        for check_name, check_type, probe_fn in _CHECKS:
            try:
                status, response_ms = probe_fn()
            except Exception as exc:
                logger.warning("SLA probe %s raised: %s", check_name, exc)
                status, response_ms = "down", None
            _record_check(db, check_name, check_type, status, response_ms)
            results.append(
                {"check_name": check_name, "status": status, "response_ms": response_ms}
            )
        _prune_old_records(db)
        db.commit()
        logger.info("SLA checks complete: %s", results)
    except Exception as exc:
        logger.error("SLA check batch failed: %s", exc)
        with contextlib.suppress(Exception):
            db.rollback()
    finally:
        db.close()
    return results


try:
    from gdx_dispatch.core.celery_app import celery_app

    @celery_app.task(name="gdx_dispatch.core.sla_monitor.run_sla_checks", bind=False)
    def run_sla_checks() -> list[dict]:
        """Celery beat task: run all SLA checks every 5 minutes."""
        return run_sla_checks_sync()

except Exception:
    # Celery not available in test/dev — provide no-op
    logging.getLogger(__name__).exception("<module> caught exception")
    def run_sla_checks() -> list[dict]:  # type: ignore[misc]
        return run_sla_checks_sync()


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["sla"])

_admin_dep = Depends(require_role("admin", "owner"))

_STATUS_PAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "status.html")


def _sla_check_to_dict(c: SLACheck) -> dict:
    return {
        "name": c.check_name,
        "check_type": c.check_type,
        "status": c.last_status,
        "uptime_24h_pct": c.uptime_24h_pct,
        "uptime_7d_pct": c.uptime_7d_pct,
        "last_response_ms": c.last_response_ms,
        "last_checked_at": c.last_checked_at.isoformat() if c.last_checked_at else None,
        "incident_count_30d": c.incident_count_30d,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _compute_uptime_30d(check_name: str, db: Session) -> float:
    return compute_uptime_pct(check_name, 24 * 30, db)


@router.get("/api/status")
def public_status(db: Session = Depends(get_db)) -> dict:
    """Public status page data — no authentication required."""
    checks = db.query(SLACheck).all()
    overall = get_overall_status(checks)

    components = []
    for c in checks:
        components.append(
            {
                "name": c.check_name,
                "check_type": c.check_type,
                "status": c.last_status,
                "uptime_30d_pct": _compute_uptime_30d(c.check_name, db),
                "last_response_ms": c.last_response_ms,
            }
        )

    # Uptime 30d average across all checks
    uptime_30d = (
        round(sum(c["uptime_30d_pct"] for c in components) / len(components), 3)
        if components
        else 100.0
    )

    # Last 5 incidents: most recent UptimeRecord rows with status != 'ok'
    incident_rows = (
        db.query(UptimeRecord)
        .filter(UptimeRecord.status != "ok")
        .order_by(UptimeRecord.checked_at.desc())
        .limit(5)
        .all()
    )
    incidents = [
        {
            "check_name": r.check_name,
            "status": r.status,
            "response_ms": r.response_ms,
            "occurred_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in incident_rows
    ]

    return {
        "overall": overall,
        "components": components,
        "uptime_30d": uptime_30d,
        "incidents": incidents,
    }


@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
def status_page() -> HTMLResponse:
    """Serve the public status HTML page."""
    path = os.path.abspath(_STATUS_PAGE_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        return HTMLResponse(content=content, status_code=200)
    except FileNotFoundError:
        logging.getLogger(__name__).exception("status_page caught exception")
        return HTMLResponse(
            content="<h1>Status page not found</h1>", status_code=500
        )


@router.get("/api/admin/sla/metrics")
def admin_sla_metrics(
    db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> list[dict]:
    """Detailed SLA metrics for all checks (admin only)."""
    try:
        checks = db.query(SLACheck).order_by(SLACheck.check_name).all()
        return [_sla_check_to_dict(c) for c in checks]
    except Exception:
        logger.exception("admin_sla_metrics: sla_checks table may not exist yet")
        with contextlib.suppress(Exception):
            db.rollback()
        return []


@router.get("/api/admin/sla/history")
def admin_sla_history(
    check: str = Query(..., description="check_name to retrieve history for"),
    days: int = Query(7, ge=1, le=90, description="Number of days of history"),
    db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> list[dict]:
    """Historical uptime records for a specific check (admin only)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(UptimeRecord)
        .filter(UptimeRecord.check_name == check, UptimeRecord.checked_at >= since)
        .order_by(UptimeRecord.checked_at.desc())
        .all()
    )
    return [
        {
            "id": str(r.id),
            "check_name": r.check_name,
            "status": r.status,
            "response_ms": r.response_ms,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        }
        for r in rows
    ]
