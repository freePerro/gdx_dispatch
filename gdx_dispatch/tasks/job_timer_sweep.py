"""Stop per-job timers left running past the tech's shift end (D14).

Beat-scheduled every 15 minutes. A per-job timer (``time_entries``,
``entry_type='job'``) is costing and attribution, not pay, and a forgotten
one used to run until a closeout or a "No" found it. This sweep stops it the
way the phone's Stop does: ``clock_out`` set, ``duration_minutes = 0``, and
the Stop marker as the FIRST words of the note.

When a timer is due, for the shop day its ``clock_in`` falls on:

* started before that day's effective shift end (``users.shift_end``, else
  ``AppSettings.default_shift_end``): due at that shift end;
* started at or after it (late work): due at the next shop midnight, so the
  sweep never fights a tech who restarted a timer after hours.

Every day, workday or not: the workdays mask only decides whether the phone
shows the warning. ``clock_out`` is the moment the rule fired (the shift end
or the midnight), not the moment the beat happened to run.

What it never does is invent hours. The row banks 0 minutes, as Stop's does;
the day's hours still come from that tech's "No" or the closeout, which both
find the row by the Stop marker (``services/day_close.py`` ``is_candidate``,
``routers/jobs.py`` ``_stopped_job_timer_for``, ``day_close_job``'s people).
A different note would make the tech's "No" a 409 ``person_not_open``.

One ``job_timer_auto_stop`` audit row per timer, actor ``system``, carrying
the elapsed span as evidence (never onto the row).
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.pay_periods import shop_day_of, shop_tz_name_from_settings
from gdx_dispatch.models.tenant_models import AppSettings, TimeEntry
from gdx_dispatch.services.visit_sync import shop_instant

if TYPE_CHECKING:
    from gdx_dispatch.core.time_off import PersonSchedule

log = logging.getLogger(__name__)

AUTO_STOP_ACTION = "job_timer_auto_stop"
REASON_SHIFT_END = "shift_end"
REASON_MIDNIGHT = "shop_midnight"


def auto_stop_note() -> str:
    # mobile.py owns the marker its own Stop writer stamps; every reader tests
    # it as a PREFIX, so it must lead.
    from gdx_dispatch.routers.mobile import MOBILE_AUTO_STOP_LABOR_NOTE  # noqa: PLC0415

    return MOBILE_AUTO_STOP_LABOR_NOTE


def _aware(value: datetime) -> datetime:
    """SQLite hands timestamps back naive; every writer stores UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def stop_due_at(
    clock_in: datetime, schedule: PersonSchedule, tz_name: str,
) -> tuple[datetime, str]:
    """When an open timer started at ``clock_in`` is due to stop, and why."""
    # Local: celery_app imports this module, and core.time_off reaches
    # celery_app through core.webhooks — a module-level import here makes a
    # process that imports core.time_off first see it half-loaded.
    from gdx_dispatch.core.time_off import shift_end_at  # noqa: PLC0415

    started = _aware(clock_in)
    day = shop_day_of(started, tz_name)
    end = shift_end_at(day, schedule, tz_name)
    if started < end:
        return end, REASON_SHIFT_END
    return shop_instant(day + timedelta(days=1), time(0, 0), tz_name), REASON_MIDNIGHT


def stop_timers_past_shift_end(
    db: Session, tenant_id: str, now: datetime | None = None,
) -> dict[str, int]:
    """Stop every open job timer that is due by ``now``; commit each alone.

    No tenant filter, as in ``tasks/timeclock_sweep.py``: one tenant per
    database, and isolation is the connection.
    """
    from gdx_dispatch.core.time_off import person_schedule  # noqa: PLC0415

    now = _aware(now or datetime.now(UTC))
    tz_name = shop_tz_name_from_settings(db)
    settings = db.execute(select(AppSettings).limit(1)).scalars().first()
    rows = db.execute(
        select(TimeEntry.id, TimeEntry.job_id, TimeEntry.user_id, TimeEntry.clock_in)
        .where(
            TimeEntry.entry_type == "job",
            TimeEntry.clock_out.is_(None),
            TimeEntry.deleted_at.is_(None),
            # A user-less open row is an office-entered labor row
            # (routers/labor.py leaves user_id NULL): there is no person, so
            # no shift end to apply. Left alone, and counted.
            TimeEntry.user_id.is_not(None),
        )
        .order_by(TimeEntry.clock_in, TimeEntry.id)
    ).all()
    schedules: dict[str, PersonSchedule] = {}
    stopped = failures = 0
    note = auto_stop_note()
    for row in rows:
        try:
            uid = str(row.user_id)
            if uid not in schedules:
                schedules[uid] = person_schedule(db, settings, uid)
            due, reason = stop_due_at(row.clock_in, schedules[uid], tz_name)
            if now < due:
                continue
            started = _aware(row.clock_in)
            elapsed = int(max((due - started).total_seconds(), 0) // 60)
            result = db.execute(
                update(TimeEntry)
                .where(TimeEntry.id == row.id, TimeEntry.clock_out.is_(None))
                .values(
                    clock_out=due,
                    duration_minutes=0,
                    notes=case(
                        (or_(TimeEntry.notes.is_(None), TimeEntry.notes == ""), note),
                        else_=note + " -- " + TimeEntry.notes,
                    ),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                # The tech's own Stop (or a closeout) got there first.
                db.rollback()
                continue
            details: dict[str, Any] = {
                "entry_id": str(row.id),
                "user_id": uid,
                "entry_type": "job",
                "reason": reason,
                "clock_in": started.isoformat(),
                "stopped_at": due.isoformat(),
                "shift_end": schedules[uid].shift_end.isoformat(timespec="minutes"),
                "shop_timezone": tz_name,
                # What the clock read vs what was banked: evidence only.
                "elapsed_minutes": elapsed,
                "recorded_minutes": 0,
            }
            log_audit_event_sync(
                db,
                tenant_id=tenant_id,
                user_id="system",
                action=AUTO_STOP_ACTION,
                entity_type="job",
                entity_id=str(row.job_id) if row.job_id is not None else str(row.id),
                details=details,
            )
            db.commit()
            stopped += 1
        except Exception:
            db.rollback()
            log.exception("job_timer_sweep_stop_failed entry=%s", row.id)
            failures += 1
    return {"stopped": stopped, "failures": failures}


@celery_app.task(
    name="gdx_dispatch.tasks.job_timer_sweep.sweep_job_timers_past_shift_end",
    queue="priority:low",
)
def sweep_job_timers_past_shift_end() -> dict[str, int]:
    """Stop every job timer left running past its tech's shift end."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    db = SessionLocal()
    try:
        result = stop_timers_past_shift_end(db, tenant_id)
    except Exception:
        log.exception("job_timer_sweep_tenant_failed tenant=%s", tenant_id)
        result = {"stopped": 0, "failures": 1}
    finally:
        db.close()
    if result["stopped"]:
        log.info("job_timer_sweep stopped=%d", result["stopped"])
    return {**result, "tenants": 1}
