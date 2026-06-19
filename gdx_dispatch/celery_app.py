"""
gdx_dispatch/celery_app.py — Celery application for GDX background tasks.

Two priority queues per Sprint 1 architecture:
  priority.high — SMS, email, webhook delivery (time-sensitive)
  priority.low  — bulk sync, reports, exports (deferrable)

Per ADR: acks_late=True, reject_on_worker_lost=True on all tasks.
"""
from __future__ import annotations

import os

from celery import Celery

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
CELERY_TIMEZONE = os.getenv("CELERY_TIMEZONE", "UTC")

celery_app = Celery(
    "gdx_dispatch",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=[
        "gdx_dispatch.tasks.notifications",
        "gdx_dispatch.tasks.provisioning",
        "gdx_dispatch.tasks.timeclock_sweep",
    ],
)

celery_app.conf.update(
    # Reliability settings — all tasks
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Priority queues
    task_queues={
        "priority.high": {"exchange": "priority.high", "routing_key": "priority.high"},
        "priority.low":  {"exchange": "priority.low",  "routing_key": "priority.low"},
    },
    task_default_queue="priority.low",
    task_routes={
        "gdx_dispatch.tasks.notifications.*": {"queue": "priority.high"},
        "gdx_dispatch.tasks.provisioning.*":  {"queue": "priority.high"},
    },

    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Beat scheduler (timezone-aware — uses tenant.timezone for per-tenant jobs)
    timezone=CELERY_TIMEZONE,
    enable_utc=True,
    beat_schedule={
        # MH-7b — auto-close shifts open longer than MAX_SHIFT_HOURS.
        # Pre-fix: an indefinite open shift carried forever; one tenant
        # had a session at 781h. The router clock-in flow also enforces
        # the same threshold so a manual re-clock doesn't have to wait
        # for the next sweep.
        "timeclock-sweep-stuck-shifts": {
            "task": "gdx_dispatch.tasks.timeclock_sweep.sweep_stuck_shifts_for_all_tenants",
            "schedule": 1800.0,  # 30 minutes
            "options": {"queue": "priority.low"},
        },
    },
)
