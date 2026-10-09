"""Undo arrival — multi-day jobs plan §5.2a, *Undo arrival*.

A wrong arrival (a mis-tap, an office entry made by mistake, an old row
marked "arrived" with no time) is undone by asking, with a reason. Two ways
in:

- **on a visit** (the Appointments page row): clears the visit's arrival
  and, when a tap wrote it, that tap — putting back the move A2 made, unless
  a skip rule applies;
- **on a tap no visit holds** (the Job Detail crew row): undoes the tap
  records the office names.

Either way the job side — ``JobAssignment.arrived_at``, ``Job.arrived_at``
and an ``on_site`` dispatch status — is recomputed from the arrivals that
survive, never restored from a recorded "before" value. A revert that is
skipped is reported in ``not_reverted``, in the response and in the
``arrival_undone`` audit row; nothing is skipped silently.

A *tap record* is the audit row ``mobile_job_arrived`` writes on every
"I'm here" (action ``arrived``, entity the job). The time entry a tap
auto-opened is never touched: hours are attested on the timesheet path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from gdx_dispatch.services.visit_sync import (
    FINISHED_STAGES,
    ON_SITE,
    is_live,
    recompute_job_schedule,
    shop_day,
    shop_tz,
    to_minute,
    visit_rows,
    visit_state,
)


class UndoRefused(Exception):
    """A request the undo cannot act on; nothing has been written."""

    def __init__(self, status: int, message: str, code: str, **detail: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.detail = detail

    def body(self) -> dict:
        return {"detail": self.message, "code": self.code, **self.detail}


@dataclass(frozen=True)
class Tap:
    """One ``arrived`` record: who tapped, when, and what it stamped/moved."""

    id: str
    tech_id: str | None
    at: datetime
    visit_id: str | None  # PR 1b records name the stamped visit; None = stamped nothing
    pr1b: bool  # carries ``visit_id`` (stamped or not); old records do not
    moved: dict | None  # ``visit_moved``

    def as_dict(self, tz_name: str) -> dict:
        return {
            "id": self.id,
            "tech_id": self.tech_id,
            "arrived_at": self.at.isoformat(),
            "day": shop_day(self.at, tz_name).isoformat(),
        }


@dataclass
class _Ledger:
    """Everything an undo reads about one job."""

    job: Any
    tz: str
    visits: list[Any]
    taps: list[Tap]  # every tap record, undone ones included
    undone_taps: set[str]
    undone_visit_arrivals: list[datetime]
    corrections: dict[str, list[dict]]  # visit id -> its PATCH arrival corrections, oldest first
    matched: dict[str, Tap] = field(default_factory=dict)  # visit id -> its tap

    def surviving(self, tech: Any = ...) -> list[Tap]:
        return [
            t for t in self.taps
            if t.id not in self.undone_taps and (tech is ... or t.tech_id == tech)
        ]

    def unmatched(self, tech: Any = ...) -> list[Tap]:
        held = {t.id for t in self.matched.values()}
        return [t for t in self.surviving(tech) if t.id not in held]


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _technician_of(db: Any, user_id: str | None) -> str | None:
    """An old record's tech: the actor's technician, the way the tap resolved it."""
    if not user_id:
        return None
    from gdx_dispatch.models.tenant_models import Technician  # noqa: PLC0415

    row = (
        db.query(Technician.id)
        .filter(Technician.user_id == user_id, Technician.active.isnot(False))
        .order_by(Technician.created_at.desc())
        .first()
    )
    return str(row[0]) if row else None


def _ledger(db: Any, job: Any) -> _Ledger:
    from sqlalchemy import select  # noqa: PLC0415

    from gdx_dispatch.core.audit import AuditLog  # noqa: PLC0415

    db.flush()
    tz_name = shop_tz(db)
    visits = visit_rows(db, job.id)
    rows = db.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "job",
            AuditLog.entity_id == str(job.id),
            AuditLog.action.in_(("arrived", "arrival_undone")),
        ).order_by(AuditLog.created_at, AuditLog.id)
    ).scalars().all()
    taps: list[Tap] = []
    undone_taps: set[str] = set()
    undone_visit_arrivals: list[datetime] = []
    techs: dict[str, str | None] = {}
    for row in rows:
        details = row.details or {}
        if row.action == "arrival_undone":
            undone_taps.update(details.get("tap_record_ids") or [])
            for va in details.get("visit_arrivals") or []:
                at = _aware(va.get("arrived_at"))
                if at is not None:
                    undone_visit_arrivals.append(at)
            continue
        at = _aware(details.get("arrived_at"))
        if at is None:
            continue
        tech = details.get("tech_id")
        if tech is None:
            if row.user_id not in techs:
                techs[row.user_id] = _technician_of(db, row.user_id)
            tech = techs[row.user_id]
        taps.append(Tap(
            id=str(row.id),
            tech_id=str(tech) if tech else None,
            at=at,
            visit_id=details.get("visit_id"),
            pr1b="visit_id" in details,
            moved=details.get("visit_moved") or None,
        ))
    corrections: dict[str, list[dict]] = {}
    visit_ids = [str(v.id) for v in visits]
    if visit_ids:
        for row in db.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "appointment",
                AuditLog.entity_id.in_(visit_ids),
                AuditLog.action == "appointment_updated",
            ).order_by(AuditLog.created_at, AuditLog.id)
        ).scalars().all():
            change = (row.details or {}).get("arrived_at")
            if isinstance(change, dict):
                corrections.setdefault(row.entity_id, []).append(change)
    ledger = _Ledger(
        job=job, tz=tz_name, visits=visits, taps=taps, undone_taps=undone_taps,
        undone_visit_arrivals=undone_visit_arrivals, corrections=corrections,
    )
    for v in visits:
        tap = _match(ledger, v)
        if tap is not None:
            ledger.matched[str(v.id)] = tap
    return ledger


def _original_arrival(ledger: _Ledger, v: Any) -> datetime | None:
    """The value the tap stamped: the ``old`` of the first PATCH correction
    that had one, else the visit's arrival now."""
    for change in ledger.corrections.get(str(v.id), []):
        if change.get("old"):
            return _aware(change["old"])
    return _aware(v.arrived_at)


def _match(ledger: _Ledger, v: Any) -> Tap | None:
    """A visit's tap: the not-yet-undone record that names it, else (an old
    record only) the one whose time equals the visit's original arrival.
    A record never matches another tech's visit."""
    if v.arrived_at is None:
        return None
    vid = str(v.id)

    def same_tech(t: Tap) -> bool:
        return v.tech_id is None or t.tech_id == str(v.tech_id)

    taps = [t for t in ledger.surviving() if same_tech(t)]
    for t in taps:
        if t.visit_id == vid or (not t.pr1b and (t.moved or {}).get("visit_id") == vid):
            return t
    original = _original_arrival(ledger, v)
    for t in taps:
        if not t.pr1b and t.at == original:
            return t
    return None


def _later_taps(ledger: _Ledger, tap: Tap) -> list[Tap]:
    """The same tech's later unmatched taps on the job, on the tap's shop day."""
    day = shop_day(tap.at, ledger.tz)
    return [
        t for t in ledger.unmatched(tap.tech_id)
        if t.id != tap.id and t.at > tap.at and shop_day(t.at, ledger.tz) == day
    ]


def _source(ledger: _Ledger, v: Any) -> str:
    if v.arrived_at is None:
        return "status_only"
    return "tap" if str(v.id) in ledger.matched else "manual"


# ---------------------------------------------------------------- previews


def preview_visit(db: Any, job: Any | None, v: Any) -> dict:
    """What the visit's undo dialog shows."""
    out = {
        "visit_id": str(v.id),
        "tech_id": v.tech_id,
        "arrived_at": _iso(_aware(v.arrived_at)),
        "status": v.status,
        "source": "manual" if v.arrived_at is not None else "status_only",
        "tap": None,
        "unmatched_taps": [],
        "later_taps": {},
    }
    if job is None:
        return out
    ledger = _ledger(db, job)
    out["source"] = _source(ledger, v)
    tap = ledger.matched.get(str(v.id))
    if tap is not None:
        out["tap"] = tap.as_dict(ledger.tz)
        out["later_taps"] = {tap.id: [t.as_dict(ledger.tz) for t in _later_taps(ledger, tap)]}
    else:
        candidates = ledger.unmatched(str(v.tech_id) if v.tech_id else ...)
        out["unmatched_taps"] = [t.as_dict(ledger.tz) for t in candidates]
        out["later_taps"] = {
            t.id: [x.as_dict(ledger.tz) for x in _later_taps(ledger, t)] for t in candidates
        }
    return out


def preview_job(db: Any, job: Any, tech_id: str) -> dict:
    """What the crew-row undo dialog shows: the tech's unmatched taps."""
    ledger = _ledger(db, job)
    return {
        "job_id": str(job.id),
        "tech_id": tech_id,
        "unmatched_taps": [t.as_dict(ledger.tz) for t in ledger.unmatched(tech_id)],
    }


def taps_if_tech(db: Any, job: Any, v: Any, *techs: str | None) -> list[Tap | None]:
    """The tap ``v`` would match carrying each of ``techs``, in order: what
    an edit of its tech keeps or strands (GDXA-383). A tap ``v`` does not
    match now and another visit does is not ``v``'s to take: an old record
    matches by time alone, so several visits can match one tap, and the
    answer must not depend on which of them sorts last."""
    ledger = _ledger(db, job)
    vid = str(v.id)
    mine = ledger.matched.get(vid)
    others = {t.id for k, t in ledger.matched.items() if k != vid}
    out: list[Tap | None] = []
    for tech in techs:
        tap = _match(ledger, SimpleNamespace(id=v.id, arrived_at=v.arrived_at, tech_id=tech))
        taken = tap is not None and tap.id in others and (mine is None or mine.id != tap.id)
        out.append(None if taken else tap)
    return out


# ---------------------------------------------------------------- the undo


def _not_found(ids: list[str], allowed: list[Tap], what: str) -> None:
    allowed_ids = {t.id for t in allowed}
    stray = [i for i in ids if i not in allowed_ids]
    if stray:
        raise UndoRefused(422, f"{what} is not one of the taps listed.", "tap_not_listed", tap_record_ids=stray)


def undo_visit_arrival(
    db: Any, job: Any | None, v: Any, *, user_id: str, reason: str,
    tap_record_id: str | None = None, also_undo: list[str] | None = None,
) -> dict:
    """Undo the arrival on visit ``v``. Flushes, never commits. Raises
    ``UndoRefused`` before writing anything."""
    also_undo = list(dict.fromkeys(also_undo or []))
    if v.arrived_at is None and (v.status or "") != "arrived":
        raise UndoRefused(409, "This visit has no arrival to undo.", "no_arrival", visit_id=str(v.id))

    ledger = _ledger(db, job) if job is not None else None
    tap: Tap | None = None
    not_reverted: list[dict] = []
    if ledger is not None:
        tap = ledger.matched.get(str(v.id))
        if tap is not None:
            if tap_record_id is not None and tap_record_id != tap.id:
                raise UndoRefused(422, "That tap is not this visit's.", "tap_not_listed",
                                  tap_record_ids=[tap_record_id])
        else:
            candidates = ledger.unmatched(str(v.tech_id) if v.tech_id else ...)
            if tap_record_id is not None:
                _not_found([tap_record_id], candidates, "The tap named")
                tap = next(t for t in candidates if t.id == tap_record_id)
            elif v.arrived_at is not None:
                not_reverted.append({"field": "tap", "reason": "no_record"})
    elif tap_record_id is not None:
        raise UndoRefused(422, "This appointment is on no job, so it has no tap.", "tap_not_listed",
                          tap_record_ids=[tap_record_id])
    if tap is None and also_undo:
        raise UndoRefused(422, "Later taps can be undone only with a tap.", "tap_not_listed",
                          tap_record_ids=also_undo)
    later = _later_taps(ledger, tap) if tap is not None else []
    _not_found(also_undo, later, "A later tap named")

    undone = ([tap] if tap is not None else []) + [t for t in later if t.id in also_undo]
    changes: dict[str, dict] = {}
    visit_arrival = _aware(v.arrived_at)
    held_values = [visit_arrival] + [
        _aware(c.get(k)) for c in (ledger.corrections.get(str(v.id), []) if ledger else [])
        for k in ("old", "new")
    ]

    # The visit side.
    now = datetime.now(UTC)
    changes["visit.arrived_at"] = {"old": _iso(visit_arrival), "new": None}
    v.arrived_at = None
    if (v.status or "") == "arrived":
        changes["visit.status"] = {"old": v.status, "new": "scheduled"}
        v.status = "scheduled"
    v.updated_at = now

    if ledger is not None:
        for t in undone:
            ledger.undone_taps.add(t.id)
        if tap is not None and (tap.moved or {}).get("visit_id") == str(v.id):
            skip = _move_skip(db, ledger, v, tap)
            if skip is None:
                start_from = _aware(tap.moved.get("visit_start_from"))
                end_from = _aware(tap.moved.get("visit_end_from"))
                length = (_aware(v.end_at) - _aware(v.start_at)) if v.end_at else timedelta(hours=1)
                end_from = end_from or start_from + length
                changes["visit.start_at"] = {"old": _iso(_aware(v.start_at)), "new": _iso(start_from)}
                changes["visit.end_at"] = {"old": _iso(_aware(v.end_at)), "new": _iso(end_from)}
                v.start_at = start_from
                v.end_at = end_from
                v.duration_minutes = int((end_from - start_from).total_seconds() // 60)
            else:
                not_reverted.append({"field": "visit.start_at", "reason": skip})
        db.flush()
        _job_side(db, ledger, undone, held_values, changes, not_reverted)

    details = {
        "reason": reason,
        "source": "status_only" if visit_arrival is None else ("tap" if tap is not None else "manual"),
        "visit_id": str(v.id),
        "tap_record_ids": [t.id for t in undone],
        "visit_arrivals": [{"visit_id": str(v.id), "arrived_at": _iso(visit_arrival)}],
        "changes": changes,
        "not_reverted": not_reverted,
    }
    _audit(db, job, v, user_id, details)
    if job is not None:
        recompute_job_schedule(db, job, user_id, "arrival_undone")
    return {"not_reverted": not_reverted, "tap_record_ids": details["tap_record_ids"], "changes": changes}


def undo_job_taps(
    db: Any, job: Any, *, tech_id: str, tap_record_ids: list[str], user_id: str, reason: str,
) -> dict:
    """Undo taps no visit holds (the crew row). Flushes, never commits."""
    ledger = _ledger(db, job)
    listed = ledger.unmatched(tech_id)
    if not listed:
        raise UndoRefused(
            409,
            "No tap to undo here. Each of this tech's taps stamped a visit — "
            "undo the arrival on that visit on the Appointments page.",
            "no_tap", tech_id=tech_id,
        )
    ids = list(dict.fromkeys(tap_record_ids))
    if not ids:
        raise UndoRefused(422, "Name at least one tap to undo.", "tap_not_listed", tap_record_ids=[])
    _not_found(ids, listed, "A tap named")
    undone = [t for t in listed if t.id in ids]
    for t in undone:
        ledger.undone_taps.add(t.id)
    changes: dict[str, dict] = {}
    not_reverted: list[dict] = []
    _job_side(db, ledger, undone, [], changes, not_reverted)
    details = {
        "reason": reason,
        "source": "tap",
        "visit_id": None,
        "tech_id": tech_id,
        "tap_record_ids": [t.id for t in undone],
        "visit_arrivals": [],
        "changes": changes,
        "not_reverted": not_reverted,
    }
    _audit(db, job, None, user_id, details)
    recompute_job_schedule(db, job, user_id, "arrival_undone")
    return {"not_reverted": not_reverted, "tap_record_ids": details["tap_record_ids"], "changes": changes}


def _move_skip(db: Any, ledger: _Ledger, v: Any, tap: Tap) -> str | None:
    """Why the move A2 made is not put back, or None to put it back."""
    stage = (getattr(ledger.job, "lifecycle_stage", None) or "").lower()
    if (v.status or "") in ("completed", "cancelled") or stage in FINISHED_STAGES:
        return "closed"
    start = _aware(v.start_at)
    if start not in (tap.at, to_minute(tap.at)) or _edited_since(db, v, tap):
        return "edited_since"
    start_from = _aware(tap.moved.get("visit_start_from"))
    old_day = shop_day(start_from, ledger.tz)
    if v.tech_id is not None and any(
        str(o.id) != str(v.id) and is_live(o) and o.tech_id == v.tech_id
        and shop_day(o.start_at, ledger.tz) == old_day
        for o in ledger.visits
    ):
        return "double_book"
    tap_day = shop_day(tap.at, ledger.tz)
    if any(shop_day(t.at, ledger.tz) == tap_day for t in ledger.surviving(tap.tech_id)):
        return "crew_came"
    return None


def _edited_since(db: Any, v: Any, tap: Tap) -> bool:
    """Was the visit's day or time changed after the tap? (A PATCH that
    changed ``start_at`` or ``end_at``, or a planner move.) A PATCH row from
    before ``times_changed`` was recorded counts if it merely named them."""
    from sqlalchemy import select  # noqa: PLC0415

    from gdx_dispatch.core.audit import AuditLog  # noqa: PLC0415

    for row in db.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "appointment",
            AuditLog.entity_id == str(v.id),
            AuditLog.action == "appointment_updated",
        )
    ).scalars().all():
        details = row.details or {}
        touched = details["times_changed"] if "times_changed" in details else details.get("fields") or []
        if _aware(row.created_at) > tap.at and {"start_at", "end_at"} & set(touched):
            return True
    for row in db.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "job",
            AuditLog.entity_id == str(v.job_id),
            AuditLog.action == "visit_moved",
        )
    ).scalars().all():
        if _aware(row.created_at) > tap.at and (row.details or {}).get("visit_id") == str(v.id):
            return True
    return False


def _job_side(
    db: Any, ledger: _Ledger, undone: list[Tap], held_values: list, changes: dict, not_reverted: list,
) -> None:
    """Recompute the job's arrival stamps from what survives this undo."""
    from sqlalchemy import select  # noqa: PLC0415

    from gdx_dispatch.models.tenant_models import JobAssignment  # noqa: PLC0415

    job = ledger.job
    # Each tech with a tap undone here: their assignment stamp.
    for tech in dict.fromkeys(t.tech_id for t in undone if t.tech_id):
        row = db.execute(
            select(JobAssignment).where(
                JobAssignment.job_id == str(job.id),
                JobAssignment.tech_id == tech,
                JobAssignment.deleted_at.is_(None),
            )
        ).scalars().first()
        if row is None or row.arrived_at is None:
            continue
        stamp = _aware(row.arrived_at)
        if any(t.at == stamp for t in ledger.taps if t.tech_id == tech):
            survivors = [t.at for t in ledger.surviving(tech)]
            new = min(survivors) if survivors else None
            if new != stamp:
                changes[f"assignment.{tech}.arrived_at"] = {"old": _iso(stamp), "new": _iso(new)}
                row.arrived_at = new
        else:
            not_reverted.append({"field": f"assignment.{tech}.arrived_at", "reason": "no_record"})

    # The job's own stamp.
    db.flush()
    surviving_arrivals = [t.at for t in ledger.surviving()] + [
        _aware(v.arrived_at) for v in visit_rows(db, job.id) if v.arrived_at is not None
    ]
    if job.arrived_at is not None:
        stamp = _aware(job.arrived_at)
        known = [t.at for t in ledger.taps] + [h for h in held_values if h is not None] + ledger.undone_visit_arrivals
        if stamp in known:
            new = min(surviving_arrivals) if surviving_arrivals else None
            if new != stamp:
                changes["job.arrived_at"] = {"old": _iso(stamp), "new": _iso(new)}
                job.arrived_at = new
        else:
            not_reverted.append({"field": "job.arrived_at", "reason": "no_record"})

    # dispatch_status: an undo never moves a job up a stage.
    stage = (getattr(job, "lifecycle_stage", None) or "").lower()
    if stage != "completed" and (job.dispatch_status or "") == "on_site":
        on_site = any(visit_state(v) == ON_SITE for v in visit_rows(db, job.id))
        if surviving_arrivals or on_site:
            not_reverted.append({"field": "dispatch_status", "reason": "other_arrival"})
        else:
            new = "assigned" if job.assigned_to else "unassigned"
            changes["job.dispatch_status"] = {"old": job.dispatch_status, "new": new}
            job.dispatch_status = new
    db.flush()


def _audit(db: Any, job: Any | None, v: Any | None, user_id: str, details: dict) -> None:
    from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: PLC0415

    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(job, "company_id", "") or getattr(v, "company_id", "") or "") or None,
        user_id=user_id or "system",
        action="arrival_undone",
        entity_type="job" if job is not None else "appointment",
        entity_id=str(job.id) if job is not None else str(v.id),
        details=details,
    )

