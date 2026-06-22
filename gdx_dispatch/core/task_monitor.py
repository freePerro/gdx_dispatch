"""
Celery task monitoring and queue metrics — control-plane router.

Routes (all admin-only via bearer token):
  GET  /api/admin/tasks/recent       — last 100 task executions
  GET  /api/admin/tasks/failed       — failed tasks needing attention
  GET  /api/admin/tasks/metrics      — success_rate_24h, avg_duration_ms, queue_depth, failed_count
  POST /api/admin/tasks/{task_id}/retry — re-queue a failed task
  GET  /api/admin/tasks/scheduled    — upcoming Celery beat tasks
  GET  /api/admin/tasks/dashboard    — HTML admin dashboard

  GET  /admin/tasks                  — task monitoring dashboard page (HTML)
  GET  /api/admin/tasks/active       — currently running tasks (JSON)
  GET  /api/admin/tasks/history      — recent completed/failed from Redis (JSON)
  GET  /api/admin/tasks/stats        — aggregate stats (JSON)
  POST /api/admin/tasks/{task_id}/revoke — cancel a pending/running task
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Column, DateTime, Integer, String, Text, func
from sqlalchemy.orm import declarative_base

# ---------------------------------------------------------------------------
# Control-plane ORM model
# ---------------------------------------------------------------------------

ControlBase = declarative_base()


class TaskExecution(ControlBase):
    """Persisted record of every Celery task execution observed via signals."""

    __tablename__ = "task_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_name = Column(String(255), nullable=False)
    task_id = Column(String(255), unique=True, nullable=True)
    tenant_id = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    retries = Column(Integer, nullable=False, default=0)
    args_summary = Column(String(500), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_name": self.task_name,
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "retries": self.retries,
            "args_summary": self.args_summary,
        }


# ---------------------------------------------------------------------------
# Celery signal handlers
# ---------------------------------------------------------------------------

def _on_task_prerun(task_id: str, task: Any, args: Any, **kwargs: Any) -> None:
    try:
        from gdx_dispatch.core.database import SessionLocal
        db = SessionLocal()
        try:
            record = TaskExecution(
                task_id=task_id,
                task_name=getattr(task, "name", str(task)),
                status="started",
                started_at=datetime.now(UTC),
                args_summary=str(args)[:500] if args else None,
            )
            db.add(record)
            db.commit()
        finally:
            db.close()
    except Exception:
        logging.getLogger(__name__).exception("_on_task_prerun caught exception")
        pass


def _on_task_success(sender: Any, result: Any, **kwargs: Any) -> None:
    try:
        task_id = getattr(sender.request, "id", None)
        if not task_id:
            return
        from gdx_dispatch.core.database import SessionLocal
        db = SessionLocal()
        try:
            record = db.query(TaskExecution).filter_by(task_id=task_id).first()
            if record:
                now = datetime.now(UTC)
                record.status = "success"
                record.completed_at = now
                if record.started_at:
                    delta = now - record.started_at
                    record.duration_ms = int(delta.total_seconds() * 1000)
                db.commit()
        finally:
            db.close()
    except Exception:
        logging.getLogger(__name__).exception("_on_task_success caught exception")
        pass


def _on_task_failure(task_id: str, exception: Any, **kwargs: Any) -> None:
    try:
        from gdx_dispatch.core.database import SessionLocal
        db = SessionLocal()
        try:
            record = db.query(TaskExecution).filter_by(task_id=task_id).first()
            if record:
                record.status = "failure"
                record.error_message = str(exception)[:500] if exception else None
                record.completed_at = datetime.now(UTC)
                db.commit()
        finally:
            db.close()
    except Exception:
        logging.getLogger(__name__).exception("_on_task_failure caught exception")
        pass


def _on_task_retry(request: Any, reason: Any, **kwargs: Any) -> None:
    try:
        task_id = getattr(request, "id", None)
        if not task_id:
            return
        from gdx_dispatch.core.database import SessionLocal
        db = SessionLocal()
        try:
            record = db.query(TaskExecution).filter_by(task_id=task_id).first()
            if record:
                record.retries = (record.retries or 0) + 1
                record.status = "retry"
                db.commit()
        finally:
            db.close()
    except Exception:
        logging.getLogger(__name__).exception("_on_task_retry caught exception")
        pass


# Connect signals at import time
try:
    from celery.signals import task_failure, task_prerun, task_retry, task_success

    task_prerun.connect(_on_task_prerun)
    task_success.connect(_on_task_success)
    task_failure.connect(_on_task_failure)
    task_retry.connect(_on_task_retry)
except ImportError:
    logging.getLogger(__name__).exception("<module> caught exception")
    pass


# ---------------------------------------------------------------------------
# Queue depth helper
# ---------------------------------------------------------------------------

def get_queue_depth(queue_name: str) -> int:
    """Return the number of messages pending in a Redis-backed Celery queue."""
    try:
        import redis as redis_lib

        broker_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
        client = redis_lib.Redis.from_url(broker_url)
        depth = client.llen(queue_name)
        return int(depth)
    except Exception:  # Return 0 if the queue depth cannot be retrieved due to connection errors.
        logging.getLogger(__name__).exception("get_queue_depth caught exception")
        return 0


# ---------------------------------------------------------------------------
# Admin auth (matches gdx_dispatch.core.admin_ops pattern)
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)
ADMIN_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")


def _require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )
    if credentials is None or credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied",
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/admin/tasks", tags=["task-monitor"])


@router.get("/recent", dependencies=[Depends(_require_admin)])
def get_recent_tasks() -> list[dict]:
    """Return the last 100 task executions."""
    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(TaskExecution)
            .order_by(TaskExecution.started_at.desc().nullslast())
            .limit(100)
            .all()
        )
        return [r.to_dict() for r in rows]
    finally:
        db.close()


@router.get("/failed", dependencies=[Depends(_require_admin)])
def get_failed_tasks() -> list[dict]:
    """Return the last 50 failed tasks."""
    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(TaskExecution)
            .filter(TaskExecution.status == "failure")
            .order_by(TaskExecution.started_at.desc().nullslast())
            .limit(50)
            .all()
        )
        return [r.to_dict() for r in rows]
    finally:
        db.close()


@router.get("/metrics", dependencies=[Depends(_require_admin)])
def get_task_metrics() -> dict:
    """Return queue health metrics for the last 24 hours."""
    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        since = datetime.now(UTC) - timedelta(hours=24)
        total_24h = (
            db.query(TaskExecution)
            .filter(TaskExecution.started_at >= since)
            .count()
        )
        success_24h = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.started_at >= since,
                TaskExecution.status == "success",
            )
            .count()
        )
        failed_24h = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.started_at >= since,
                TaskExecution.status == "failure",
            )
            .count()
        )
        avg_row = (
            db.query(func.avg(TaskExecution.duration_ms))
            .filter(
                TaskExecution.started_at >= since,
                TaskExecution.duration_ms.isnot(None),
            )
            .scalar()
        )
        success_rate = (success_24h / total_24h * 100.0) if total_24h > 0 else 0.0
        avg_duration = float(avg_row) if avg_row is not None else 0.0
        return {
            "success_rate_24h": round(success_rate, 2),
            "avg_duration_ms": round(avg_duration, 2),
            "failed_count": failed_24h,
            "queue_depth_high": get_queue_depth("high"),
            "queue_depth_low": get_queue_depth("low"),
        }
    finally:
        db.close()


@router.post("/{task_id}/retry", dependencies=[Depends(_require_admin)])
def retry_task(task_id: str) -> dict:
    """Re-queue a failed task by its Celery task ID."""
    from gdx_dispatch.core.celery_app import celery_app
    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        record = db.query(TaskExecution).filter_by(task_id=task_id).first()
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task {task_id!r} not found",
            )
        celery_app.send_task(record.task_name)
        record.status = "pending"
        db.commit()
        return {"queued": True, "task_id": task_id}
    finally:
        db.close()


@router.get("/scheduled", dependencies=[Depends(_require_admin)])
def get_scheduled_tasks() -> list[dict]:
    """Return the Celery beat schedule as a list of upcoming tasks."""
    from gdx_dispatch.core.celery_app import celery_app

    schedule = getattr(celery_app.conf, "beat_schedule", {}) or {}
    result = []
    for name, entry in schedule.items():
        result.append(
            {
                "name": name,
                "task": entry.get("task", ""),
                "schedule": str(entry.get("schedule", "")),
                "queue": str(entry.get("options", {}).get("queue", "default")),
            }
        )
    return result


_DASHBOARD_PATH = Path(__file__).parent.parent / "templates" / "task_dashboard.html"

_DASHBOARD_FALLBACK = """<!DOCTYPE html>
<html><head><title>Task Dashboard</title></head>
<body><h1>Task Monitor</h1>
<p>Template not found. Place task_dashboard.html in gdx_dispatch/templates/.</p>
</body></html>"""


@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(_require_admin)])
async def task_dashboard() -> HTMLResponse:
    """Serve the task monitoring admin dashboard."""
    if _DASHBOARD_PATH.exists():
        return HTMLResponse(content=_DASHBOARD_PATH.read_text())
    return HTMLResponse(content=_DASHBOARD_FALLBACK)


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------

def connect_signals() -> None:
    """Call from app startup to activate Celery signal handlers.

    Signals are connected at import time; this is a no-op hook provided
    for explicit startup wiring / documentation purposes.
    """
    pass


# ---------------------------------------------------------------------------
# Utility functions — inspect/Redis based (no DB required)
# ---------------------------------------------------------------------------

def _redis_backend_client():
    """Return a Redis client pointed at the Celery result backend."""
    import redis as _redis
    backend_url = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
    return _redis.Redis.from_url(backend_url, socket_connect_timeout=2, socket_timeout=2)


def _safe_inspect():
    """Return a Celery Inspector with a short timeout, or None on failure."""
    try:
        from gdx_dispatch.core.celery_app import celery_app as _app
        return _app.control.inspect(timeout=2.0)
    except Exception:  # intentional fallback for inspection timeout or connection failure
        logging.getLogger(__name__).exception("_safe_inspect caught exception")
        return None


def get_active_tasks() -> list[dict]:
    """Return currently running Celery tasks from all workers.

    Each item contains: task_id, name, tenant_id, started_at, duration_s,
    worker, args, kwargs.
    Returns [] when the Celery broker is unreachable.
    """
    try:
        insp = _safe_inspect()
        if insp is None:
            return []
        active = insp.active()
        if not active:
            return []

        now = _dt.datetime.now(_dt.timezone.utc)
        result: list[dict] = []
        for worker_name, tasks in active.items():
            for t in tasks:
                time_start = t.get("time_start")
                if time_start:
                    started_dt = _dt.datetime.fromtimestamp(time_start, tz=_dt.timezone.utc)
                    started_iso = started_dt.isoformat()
                    duration_s = round((now - started_dt).total_seconds(), 2)
                else:
                    started_iso = None
                    duration_s = None

                kwargs = t.get("kwargs") or {}
                tenant_id = kwargs.get("tenant_id") or kwargs.get("tenant") or None

                result.append(
                    {
                        "task_id": t.get("id"),
                        "name": t.get("name"),
                        "tenant_id": tenant_id,
                        "started_at": started_iso,
                        "duration_s": duration_s,
                        "worker": worker_name,
                        "args": t.get("args", []),
                        "kwargs": kwargs,
                    }
                )
        return result
    except Exception:  # returns empty list on broker failure per docstring
        logging.getLogger(__name__).exception("get_active_tasks caught exception")
        return []


def get_scheduled_tasks() -> list[dict]:  # noqa: F811
    """Return the beat schedule as a list with human-readable descriptions.

    Each item: name, task, schedule_human, next_run_approx, queue.
    """
    try:
        from celery.schedules import crontab

        from gdx_dispatch.core.celery_app import celery_app as _app

        schedule = getattr(_app.conf, "beat_schedule", {}) or {}
        result: list[dict] = []
        now = _dt.datetime.now(_dt.timezone.utc)

        for entry_name, entry in schedule.items():
            task_name = entry.get("task", "")
            sched = entry.get("schedule")
            queue = (entry.get("options") or {}).get("queue", getattr(_app.conf, "task_default_queue", "default"))

            if isinstance(sched, crontab):
                dom = sched.day_of_month
                dow = sched.day_of_week
                hour = sched.hour
                minute = sched.minute
                if str(dom) not in ("*", "*/1"):
                    schedule_human = f"Monthly on day {dom} at {hour}:{str(minute).zfill(2)}"
                elif str(dow) not in ("*", "*/1"):
                    schedule_human = f"Weekly on weekday {dow} at {hour}:{str(minute).zfill(2)}"
                else:
                    schedule_human = f"Cron {minute} {hour} * * {dow}"
                next_run_approx = "varies (crontab)"
            elif isinstance(sched, (int, float)):
                schedule_human = f"Every {sched}s"
                next_run_approx = (now + _dt.timedelta(seconds=sched)).isoformat()
            elif hasattr(sched, "total_seconds"):
                seconds = sched.total_seconds()
                schedule_human = f"Every {int(seconds)}s"
                next_run_approx = (now + sched).isoformat()
            else:
                schedule_human = str(sched)
                next_run_approx = "unknown"

            result.append(
                {
                    "name": entry_name,
                    "task": task_name,
                    "schedule_human": schedule_human,
                    "next_run_approx": next_run_approx,
                    "queue": queue,
                }
            )
        return result
    except Exception:
        logging.getLogger(__name__).exception("get_scheduled_tasks caught exception")
        return []


def get_task_history(limit: int = 50) -> list[dict]:
    """Return recent completed/failed tasks from the Redis result backend.

    Scans ``celery-task-meta-*`` keys and returns up to *limit* items sorted
    by date_done descending. Returns [] when Redis is unavailable.
    """
    try:
        r = _redis_backend_client()
        keys = list(r.scan_iter("celery-task-meta-*", count=500))
        if not keys:
            return []

        records: list[dict] = []
        raw_values = r.mget(keys)
        for raw in raw_values:
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logging.getLogger(__name__).exception("get_task_history caught exception")
                continue

            task_id = data.get("task_id") or data.get("id") or ""
            date_done_raw = data.get("date_done")
            date_done = date_done_raw if isinstance(date_done_raw, str) else None

            # runtime_s may be embedded in result for some backends
            runtime_s: float | None = None
            result_val = data.get("result")
            if isinstance(result_val, (int, float)):
                runtime_s = float(result_val)

            traceback = data.get("traceback")
            if traceback and len(traceback) > 500:
                traceback = traceback[:500] + "…"

            result_str = str(result_val)[:200] if result_val is not None else None

            records.append(
                {
                    "task_id": task_id,
                    "name": data.get("name") or data.get("task_name") or "",
                    "status": data.get("status", ""),
                    "result": result_str,
                    "date_done": date_done,
                    "traceback": traceback,
                    "runtime_s": runtime_s,
                }
            )

        records.sort(key=lambda x: x.get("date_done") or "", reverse=True)
        return records[:limit]
    except Exception:  # returns empty list if redis backend is unavailable
        logging.getLogger(__name__).exception("get_task_history caught exception")
        return []


def get_task_stats() -> dict:
    """Return aggregate task statistics.

    Returns::

        {
            "total_today": int,
            "failed_today": int,
            "avg_duration_ms": float,
            "queues": {
                "high": {"pending": int, "active": int},
                "low":  {"pending": int, "active": int},
            }
        }
    """
    today_str = _dt.datetime.now(_dt.timezone.utc).date().isoformat()

    history = get_task_history(limit=500)
    total_today = 0
    failed_today = 0
    durations: list[float] = []
    for task in history:
        date_done = task.get("date_done") or ""
        if date_done.startswith(today_str):
            total_today += 1
            if task.get("status") == "FAILURE":
                failed_today += 1
            if task.get("runtime_s") is not None:
                durations.append(task["runtime_s"])

    avg_duration_ms = round(sum(durations) / len(durations) * 1000, 2) if durations else 0.0

    queue_stats: dict[str, dict] = {
        "high": {"pending": 0, "active": 0},
        "low": {"pending": 0, "active": 0},
    }
    try:
        insp = _safe_inspect()
        if insp:
            active_map = insp.active() or {}
            reserved_map = insp.reserved() or {}

            for tasks in active_map.values():
                for t in tasks:
                    q = (t.get("delivery_info") or {}).get("routing_key", "low")
                    if q in queue_stats:
                        queue_stats[q]["active"] += 1

            for tasks in reserved_map.values():
                for t in tasks:
                    q = (t.get("delivery_info") or {}).get("routing_key", "low")
                    if q in queue_stats:
                        queue_stats[q]["pending"] += 1
    except Exception:
        logging.getLogger(__name__).exception("get_task_stats caught exception")
        pass

    return {
        "total_today": total_today,
        "failed_today": failed_today,
        "avg_duration_ms": avg_duration_ms,
        "queues": queue_stats,
    }


def revoke_task(task_id: str) -> dict:
    """Cancel a pending or running Celery task.

    Returns ``{success: bool, error: str|None}``.
    """
    try:
        from gdx_dispatch.core.celery_app import celery_app as _app
        _app.control.revoke(task_id, terminate=True)
        return {"success": True, "error": None}
    except Exception as exc:
        logging.getLogger(__name__).exception("revoke_task caught exception")
        # Generic error; full exception is logged above. (CodeQL stack-trace-exposure)
        return {"success": False, "error": "Failed to revoke task"}


# ---------------------------------------------------------------------------
# Additional routes — dashboard page + JSON endpoints for new UI
# ---------------------------------------------------------------------------

_TASK_MONITOR_TEMPLATE = Path(__file__).parent.parent / "templates" / "task_monitor.html"


@router.get("/active", dependencies=[Depends(_require_admin)])
def api_active_tasks() -> list[dict]:
    """Return currently running tasks (inspect-based, no DB)."""
    return get_active_tasks()


@router.get("/history", dependencies=[Depends(_require_admin)])
def api_task_history(limit: int = 50) -> list[dict]:
    """Return recent completed/failed tasks from Redis result backend."""
    return get_task_history(limit=limit)


@router.get("/stats", dependencies=[Depends(_require_admin)])
def api_task_stats() -> dict:
    """Return aggregate stats: total_today, failed_today, avg_duration_ms, queues."""
    return get_task_stats()


@router.post("/{task_id}/revoke", dependencies=[Depends(_require_admin)])
def api_revoke_task(task_id: str) -> dict:
    """Cancel (revoke + terminate) a pending or running Celery task."""
    return revoke_task(task_id)


_TASK_MONITOR_PAGE = Path(__file__).parent.parent / "templates" / "task_monitor.html"


@router.get(
    "/monitor",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_admin)],
)
async def task_monitor_page() -> HTMLResponse:
    """Serve the Celery task monitoring dashboard page."""
    if _TASK_MONITOR_PAGE.exists():
        return HTMLResponse(content=_TASK_MONITOR_PAGE.read_text())
    return HTMLResponse(content="<h1>Task Monitor template not found</h1>")
