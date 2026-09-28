"""Planner "Today" tab — the one path the planner router and the AI tools share.

A task is on Today while ``planner_tasks.today_date`` equals today's
business-local date, so the list empties itself each morning with nothing
scheduled to run. Unfinished pins from the last ``CARRY_DAYS`` days and
anything due come back as suggestions (the Microsoft To Do "My Day" model;
the 2026-09-28 planner Today plan).

The notes box is one ``PlannerDayNote`` per user per day. A save carries the
``updated_at`` the writer last saw and is refused with ``NoteConflict`` when
the stored note has moved on. The write itself is conditional on that version
(``UPDATE … WHERE updated_at = <seen>``, rowcount checked), so two writers that
read the same version cannot both win — one of them gets the 409, even when
the other commits between this one's check and its write.

Every mutation writes its audit row in the same commit as the change, with
``via`` saying whether a person or the AI made it.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import ensure_audit_table, log_audit_event_sync
from gdx_dispatch.models.tenant_models import PlannerDayNote, PlannerTask

log = logging.getLogger(__name__)

# Unfinished pins this many days back come back as suggestions — seven, so
# Friday's list is still offered on Monday. Older pins stay in My Tasks.
CARRY_DAYS = 7
# The "due / overdue" suggestions stop here; the rest is a count and a link.
DUE_CAP = 15
NOTE_MAX = 5000
VIA = ("user", "ai")
# PlannerTask.source for a task the AI created. HTTP callers may not send it.
SOURCE_AI = "ai"


# ── Calendar-date convention (moved from routers/planner.py 2026-09-28) ─────
# due_date / today_date are CALENDAR dates. Storage convention: date D =
# D@00:00:00 UTC. "Today" for a garage-door business is the shop's local day,
# not UTC's — after ~7pm CDT those differ, which put evening captures a day late.
try:
    from zoneinfo import ZoneInfo

    _BUSINESS_TZ = ZoneInfo(os.getenv("GDX_BUSINESS_TZ", "America/Chicago"))
except Exception:  # tzdata missing — degrade to UTC rather than crash the router
    _BUSINESS_TZ = timezone.utc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def calendar_today_utc() -> datetime:
    """Today's business-local calendar date, stored per the D@00:00 UTC
    convention. Shared with other planner-task writers (outlook capture)."""
    today = datetime.now(_BUSINESS_TZ).date()
    return datetime(today.year, today.month, today.day, tzinfo=timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands DateTime(timezone=True) back naive; Postgres aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _date_out(dt: datetime | None) -> str | None:
    """due_date OUT is a bare calendar date ('YYYY-MM-DD'). str(datetime)
    ('2026-08-03 00:00:00+00:00') made browsers parse UTC midnight and
    render the previous local day — the 2026-08-03 off-by-one fix.

    Two row shapes coexist: UTC-midnight rows (the calendar convention —
    return that date verbatim) and real-timestamp rows written by older
    server defaults (quick-capture/outlook `now()`) — those are instants,
    so their calendar day is the BUSINESS-local one. A genuine event at
    exactly 00:00:00 UTC degrades to the convention read — acceptable,
    same trade the useFormatters stamp helpers make."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc_dt = dt.astimezone(timezone.utc)
    if (utc_dt.hour, utc_dt.minute, utc_dt.second) == (0, 0, 0):
        return utc_dt.date().isoformat()
    return dt.astimezone(_BUSINESS_TZ).date().isoformat()


def _ts_out(dt: datetime | None) -> str | None:
    """Real timestamps go out as proper ISO-8601 ('T' separator + offset),
    not str(datetime)'s space-separated form."""
    return dt.isoformat() if dt else None


# ── Tasks ────────────────────────────────────────────────────────────────────

class NotYours(Exception):
    """The task is not in the caller's own set (assigned to them, or
    unassigned and created by them). A pin there would show on nobody's Today."""


class NoteConflict(Exception):
    """The stored note is not the one the writer last saw."""

    def __init__(self, reason: str, current: dict) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current = current


def mine_clause(uid: str):
    """The planner's "mine": assigned to me, or unassigned and created by me."""
    return (PlannerTask.assigned_to == uid) | (
        PlannerTask.assigned_to.is_(None) & (PlannerTask.created_by == uid)
    )


def owner_of(task: PlannerTask) -> str:
    """Whose Today a pin on this task shows on — the assignee, or the creator
    while unassigned. The same rule as mine_clause, so the two cannot drift."""
    return task.created_by if task.assigned_to is None else task.assigned_to


def is_mine(task: PlannerTask, uid: str) -> bool:
    return owner_of(task) == uid


def task_out(t: PlannerTask, today: datetime | None = None) -> dict:
    today = today or calendar_today_utc()
    return {
        "id": str(t.id), "title": t.title, "description": t.description,
        "status": t.status, "priority": t.priority,
        "due_date": _date_out(t.due_date),
        "assigned_to": t.assigned_to, "created_by": t.created_by,
        "job_id": t.job_id, "customer_id": t.customer_id,
        "contact_phone": t.contact_phone, "phone_com_call_id": t.phone_com_call_id,
        "source": t.source,
        "created_at": _ts_out(t.created_at),
        "completed_at": _ts_out(t.completed_at),
        "today_date": _date_out(t.today_date),
        "on_today": _as_utc(t.today_date) == today,
    }


def today_view(db: Session, uid: str, *, today: datetime | None = None) -> dict:
    """Today's pinned tasks, the suggestions, and today's note for ``uid``."""
    today = today or calendar_today_utc()
    mine = mine_clause(uid)
    done_last = case((PlannerTask.status == "done", 1), else_=0)

    pinned = db.execute(
        select(PlannerTask)
        .where(mine, PlannerTask.today_date == today)
        .order_by(done_last, PlannerTask.created_at.asc().nullslast())
    ).scalars().all()

    carried = db.execute(
        select(PlannerTask)
        .where(
            mine,
            PlannerTask.status != "done",
            PlannerTask.today_date >= today - timedelta(days=CARRY_DAYS),
            PlannerTask.today_date < today,
        )
        .order_by(PlannerTask.today_date.desc(), PlannerTask.created_at.asc().nullslast())
    ).scalars().all()
    carried_ids = {t.id for t in carried}

    due_rows = db.execute(
        select(PlannerTask)
        .where(
            mine,
            PlannerTask.status != "done",
            PlannerTask.due_date.isnot(None),
            PlannerTask.due_date < today + timedelta(days=1),
            or_(PlannerTask.today_date.is_(None), PlannerTask.today_date != today),
        )
        .order_by(PlannerTask.due_date.asc(), PlannerTask.created_at.asc().nullslast())
    ).scalars().all()
    due = [t for t in due_rows if t.id not in carried_ids]

    return {
        "date": today.date().isoformat(),
        "tasks": [task_out(t, today) for t in pinned],
        "carried": [task_out(t, today) for t in carried],
        "due": [task_out(t, today) for t in due[:DUE_CAP]],
        "due_more": max(0, len(due) - DUE_CAP),
        "note": note_out(_note_row(db, uid, today.date()), today.date()),
    }


def set_today(
    db: Session, *, tid: str, uid: str, task_id: str, on: bool, via: str = "user",
    today: datetime | None = None, request=None,
) -> dict:
    """Pin ``task_id`` to Today (``on``) or take it off. Raises LookupError
    for no such task and NotYours when it is not in the caller's set."""
    today = today or calendar_today_utc()
    task = db.execute(select(PlannerTask).where(PlannerTask.id == task_id)).scalar_one_or_none()
    if task is None:
        raise LookupError(task_id)
    if not is_mine(task, uid):
        raise NotYours(task_id)
    target = today if on else None
    if _as_utc(task.today_date) == target:
        return task_out(task, today)  # already so — no write, no audit row

    ensure_audit_table(db)  # before staging: its first run on an engine commits
    task.today_date = target
    log_audit_event_sync(
        db=db, tenant_id=tid or None, user_id=uid,
        action="planner_today_add" if on else "planner_today_remove",
        entity_type="planner_task", entity_id=str(task.id),
        details={"title": task.title, "via": via, "today": today.date().isoformat()},
        request=request,
    )
    db.commit()
    return task_out(task, today)


# ── Notes ────────────────────────────────────────────────────────────────────

def _note_row(db: Session, uid: str, day: date) -> PlannerDayNote | None:
    # populate_existing: a row already in this session's identity map would
    # otherwise come back with the attribute values it was first loaded with.
    return db.execute(
        select(PlannerDayNote)
        .where(PlannerDayNote.user_id == uid, PlannerDayNote.note_date == day)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def note_out(row: PlannerDayNote | None, day: date) -> dict:
    return {
        "date": day.isoformat(),
        "body": row.body if row else "",
        "updated_at": _ts_out(row.updated_at) if row else None,
        "updated_via": row.updated_via if row else None,
    }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def save_note(
    db: Session, *, tid: str, uid: str, body: str, note_date: str | None,
    base_updated_at: str | None, via: str = "user", today: datetime | None = None,
    request=None,
) -> dict:
    """Replace today's note with ``body``.

    ``note_date`` is the day the writer's copy belongs to and
    ``base_updated_at`` the version it last saw (None: it saw no note). A
    mismatch on either raises NoteConflict with the stored note — a page left
    open past midnight must not write yesterday's text into today, and an AI
    append must not be overwritten by a stale autosave. The version check is
    repeated inside the UPDATE itself, so a save that commits between this
    one's check and its write still produces a conflict, not an overwrite.
    """
    today = today or calendar_today_utc()
    day = today.date()
    body = (body or "").replace("\x00", "")
    if len(body) > NOTE_MAX:
        raise ValueError(f"note longer than {NOTE_MAX} characters")
    row = _note_row(db, uid, day)
    if note_date is not None and note_date != day.isoformat():
        raise NoteConflict("day_changed", note_out(row, day))
    stored = _as_utc(row.updated_at) if row else None
    if stored != _parse_ts(base_updated_at):
        raise NoteConflict("stale", note_out(row, day))
    if row is not None and row.body == body:
        return note_out(row, day)  # nothing changed — no write, no audit row

    ensure_audit_table(db)  # before staging: its first run on an engine commits
    now = _now()
    if row is None:
        before_len = 0
        row = PlannerDayNote(
            id=str(uuid4()), company_id=tid, user_id=uid, note_date=day,
            body=body, created_at=now, updated_at=now, updated_by=uid, updated_via=via,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # Two first-saves of the day raced on (user_id, note_date) and the
            # other one won. Any other integrity failure is not a conflict.
            db.rollback()
            current = _note_row(db, uid, day)
            if current is None:
                raise
            raise NoteConflict("stale", note_out(current, day)) from None
        note_id = row.id
    else:
        before_len = len(row.body)
        note_id = row.id
        # Conditional on the version checked above: a writer that committed
        # in between moved updated_at, this matches nothing, and we refuse.
        result = db.execute(
            update(PlannerDayNote)
            .where(PlannerDayNote.id == note_id, PlannerDayNote.updated_at == row.updated_at)
            .values(body=body, updated_at=now, updated_by=uid, updated_via=via)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            raise NoteConflict("stale", note_out(_note_row(db, uid, day), day))
    # One copy of the text per save: save N's "before" is save N-1's "after",
    # so the full history rebuilds from `after` alone (plan, audit #2).
    log_audit_event_sync(
        db=db, tenant_id=tid or None, user_id=uid,
        action="planner_day_note_save",
        entity_type="planner_day_note", entity_id=str(note_id),
        details={"note_date": day.isoformat(), "via": via, "before_len": before_len, "after": body},
        request=request,
    )
    db.commit()
    return note_out(_note_row(db, uid, day), day)
