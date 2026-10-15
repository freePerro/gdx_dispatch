"""Planner reminders — a task's ``remind_at`` arrives, its setter gets a bell.

Every minute (``core/scheduler.py``) this turns each due reminder into one
``Notification`` row addressed to the person who set it (``remind_user_id``,
falling back to the task's creator), category ``planner_reminder``. That row
shows in the web bell drawer, and the GDX Planner app's phone-alerts service
(``routers/device_alerts.py`` feed, ``scope=planner``) turns it into an
Android notification. No Firebase: the native foreground service polls.

Sent once: the claim is a guarded UPDATE (``reminder_sent_at IS NULL`` and
``remind_at`` still due), and the bell row and the audit row commit in the
same transaction as the claim. A second, overlapping beat updates nothing and
moves on; a failed write rolls the claim back and the next minute retries.
A done task is not reminded while it is done; reopened, a reminder whose
time has passed fires at the next beat (it was never sent). Moving
``remind_at`` (routers/planner.py) clears ``reminder_sent_at`` so the reminder
fires again at the new time; re-saving the same instant does not.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import closing, suppress
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)

CATEGORY = "planner_reminder"
BATCH_SIZE = 200


def _due_line(task: Any) -> str:
    due = task.due_date
    if due is None:
        return "Planner reminder"
    return f"Due {due.strftime('%Y-%m-%d')}"


def _due(now: datetime) -> tuple:
    """The "due, not yet sent, not done" filter, shared by the select and the
    claim so the two can never disagree."""
    from gdx_dispatch.models.tenant_models import PlannerTask  # noqa: PLC0415

    return (
        PlannerTask.reminder_sent_at.is_(None),
        PlannerTask.remind_at.isnot(None),
        PlannerTask.remind_at <= now,
        PlannerTask.status != "done",
    )


def _send_one(db: Session, task_id: str, now: datetime) -> bool:
    """Claim one reminder and write its bell + audit rows, all in one
    transaction. True only when this call sent it."""
    from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: PLC0415
    from gdx_dispatch.models.tenant_models import Notification, PlannerTask  # noqa: PLC0415

    won = db.execute(
        update(PlannerTask)
        .where(PlannerTask.id == task_id, *_due(now))
        .values(reminder_sent_at=now)
    ).rowcount
    if won != 1:
        db.rollback()
        return False
    task = db.get(PlannerTask, task_id)
    db.refresh(task)
    recipient = task.remind_user_id or task.created_by
    title = f"Reminder: {task.title}"[:255]
    db.add(Notification(
        id=uuid.uuid4().hex,
        tenant_id=task.company_id,
        user_id=recipient,
        title=title,
        message=_due_line(task),
        category=CATEGORY,
        is_read=0,
        # The exact format notify_office writes. The phone feed compares
        # created_at as TEXT against an isoformat() cursor; a DB-side now()
        # ('2026-10-15 04:25:55+00') sorts below it and never reaches a phone.
        created_at=datetime.now(timezone.utc).isoformat(),
    ))
    log_audit_event_sync(
        db=db, tenant_id=task.company_id or None, user_id=None,
        action="planner_reminder_sent", entity_type="planner_task",
        entity_id=str(task.id),
        details={"title": task.title, "recipient": recipient,
                 "remind_at": task.remind_at.isoformat() if task.remind_at else None,
                 "via": "system"},
    )
    db.commit()
    return True


def send_due(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Send every due reminder once. One task's failure never blocks the batch."""
    from gdx_dispatch.core.audit import ensure_audit_table  # noqa: PLC0415
    from gdx_dispatch.models.tenant_models import PlannerTask  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    ensure_audit_table(db)  # before staging: its first run on an engine commits
    due = db.execute(
        select(PlannerTask.id)
        .where(*_due(now))
        .order_by(PlannerTask.remind_at)
        .limit(BATCH_SIZE)
    ).scalars().all()
    counts = {"due": len(due), "sent": 0, "failed": 0}
    for task_id in due:
        try:
            if _send_one(db, task_id, now):
                counts["sent"] += 1
        except Exception:  # noqa: BLE001 — one row must not stop the batch
            log.exception("planner_reminder_failed task=%s", task_id)
            with suppress(Exception):
                db.rollback()
            counts["failed"] += 1
    return counts


@celery_app.task(name="gdx_dispatch.tasks.planner_reminders.send_due_planner_reminders")
def send_due_planner_reminders() -> dict[str, int]:
    """Beat task — every minute. No-ops instantly when nothing is due."""
    with closing(SessionLocal()) as db:
        result = send_due(db)
    if result["due"]:
        log.info("planner_reminders %s", result)
    return result
