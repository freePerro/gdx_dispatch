#!/usr/bin/env python3
"""Close per-job labor timers that were left running and never closed out.

Mobile's ``/jobs/{id}/arrived`` (and ``/start``) opens a ``time_entries`` row
with ``entry_type='job'`` and ``clock_out NULL``. Until ``73bf873``
(2026-07-16) nothing ever closed it: closeout looked for ``tech_id ==
user_id``, which mobile never writes, so it missed the timer entirely. That
fix landed and the leak stopped — every job timer opened from 2026-07 onward
is closed. What it did not do is clean up the rows already stranded.

On prod that is four rows, opened 2026-04-29, 05-04, 05-21 and 06-01, still
running 85-118 days later on jobs that were never closed out.

What this writes, and why it is the only honest option:

* ``clock_out = now()`` — the row stops claiming to be a live timer.
* ``duration_minutes = 0`` — NOT the elapsed span. That column IS payroll
  hours (payroll.py:248 sums ``COALESCE(duration_minutes, 0)`` with no rate
  filter), and banking three months of wall clock would pay out roughly 2,800
  hours per row. Elapsed measures how long a timer went unattended, not work.
  This is the same call jobs.py::_close_labor_entry makes for an unattested
  timer and mobile.py::_close_open_time_entry makes for a manual stop.
* ``hourly_rate`` untouched (NULL) — with zero minutes, job_costing's fallback
  rate multiplies zero, so these rows cost nothing rather than being priced at
  the tenant default.
* ``notes`` — stamped with the original span so the office can still see what
  happened and enter attested hours through the closeout or labor.py if any of
  this work was real.

Soft, not destructive: no row is deleted, ``clock_in`` is never moved (payroll
windows on ``DATE(clock_in)``), and every row gets an audit event naming the
elapsed span it refused to bank. Re-runnable — a closed row is not selected
twice.

Usage (inside the app container)::

    python tools/stale_job_timer_repair.py                      # dry-run
    python tools/stale_job_timer_repair.py --apply --operator doug
    python tools/stale_job_timer_repair.py --apply --operator doug --older-than 30

Rollback: every affected id is printed and audited. To undo a row::

    UPDATE time_entries SET clock_out = NULL, duration_minutes = NULL,
           notes = NULL WHERE id = '<id>';
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: E402
from gdx_dispatch.core.database import SessionLocal  # noqa: E402
from gdx_dispatch.core.tenant import company_id  # noqa: E402

REPAIR_NOTE = "Timer never closed out; closed by repair"

#: Default age floor. A timer open for less than a day is very likely a tech
#: still on the job — closing that one would be the bug, not the fix.
DEFAULT_MIN_AGE_DAYS = 7


def _as_utc(value) -> datetime | None:
    """Raw-SQL timestamps come back as datetime on Postgres and str on SQLite.

    Both planes have to work (this tool's tests run on SQLite), and a naive
    datetime subtracted from an aware ``now()`` raises rather than returning a
    wrong number — so normalise here instead of at three call sites.
    """
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip().replace(" ", "T", 1)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class StaleTimer:
    entry_id: str
    job_id: str | None
    user_id: str | None
    clock_in: datetime
    elapsed_minutes: int

    @property
    def days(self) -> float:
        return round(self.elapsed_minutes / 1440, 1)


def fetch_stale(db, *, min_age_days: int) -> list[StaleTimer]:
    """Open per-job timers older than the floor.

    Scoped to ``entry_type='job'`` on purpose: the day/shift clock lives in
    ``timeclock_entries_router`` and has its own sweep
    (tasks/timeclock_sweep.py), which already closes with ``minutes = NULL``.
    """
    # CAST(... AS TEXT) rather than ``::text``, and no ``CAST(:id AS uuid)``
    # on the write side: this repo runs on SQLite as well as Postgres, and
    # both of those are Postgres-only spellings that fail outright on SQLite —
    # including in this tool's own tests, which is how it was caught.
    rows = db.execute(text(
        "SELECT CAST(id AS TEXT) AS id, CAST(job_id AS TEXT) AS job_id, "
        "       user_id, clock_in "
        "FROM time_entries "
        "WHERE entry_type = 'job' "
        "  AND clock_out IS NULL "
        "  AND deleted_at IS NULL "
        "ORDER BY clock_in"
    )).mappings().all()

    now = datetime.now(UTC)
    out: list[StaleTimer] = []
    for r in rows:
        clock_in = _as_utc(r["clock_in"])
        if clock_in is None:
            continue
        elapsed = int(max((now - clock_in).total_seconds(), 0) // 60)
        if elapsed < min_age_days * 1440:
            continue
        out.append(StaleTimer(
            entry_id=r["id"], job_id=r["job_id"], user_id=r["user_id"],
            clock_in=clock_in, elapsed_minutes=elapsed,
        ))
    return out


def _print_plan(stale: list[StaleTimer], *, min_age_days: int) -> None:
    if not stale:
        print(f"No per-job timers open longer than {min_age_days} day(s). Nothing to do.")
        return
    print(f"\n{len(stale)} stale per-job timer(s) open longer than {min_age_days} day(s):\n")
    print(f"  {'entry_id':38} {'opened':22} {'open for':>10}   job")
    for s in stale:
        print(f"  {s.entry_id:38} {s.clock_in.isoformat(timespec='minutes'):22} "
              f"{s.days:>8} d   {s.job_id or '-'}")
    total = sum(s.elapsed_minutes for s in stale)
    print(f"\n  Elapsed across all rows: {total:,} minutes ({total / 60:,.0f} h).")
    print("  NONE of it will be banked as worked time — every row closes at 0 minutes.")
    print("  The span goes into notes so the office can attest real hours if any of")
    print("  this work happened.")


def apply_plan(db, stale: list[StaleTimer], *, operator: str) -> int:
    tenant = company_id()
    actor = f"cli:{operator}"
    now = datetime.now(UTC)

    for s in stale:
        note = f"{REPAIR_NOTE}; ran {s.elapsed_minutes} min ({s.days} d), not attested"
        db.execute(text(
            "UPDATE time_entries "
            "SET clock_out = :now, "
            "    duration_minutes = 0, "
            "    notes = CASE WHEN notes IS NULL OR notes = '' THEN :note "
            "                 ELSE notes || ' -- ' || :note END "
            "WHERE CAST(id AS TEXT) = :id AND clock_out IS NULL"
        ), {"now": now, "note": note, "id": s.entry_id})
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action="stale_job_timer_closed",
            entity_type="time_entry", entity_id=s.entry_id,
            details={
                "job_id": s.job_id,
                "timer_user_id": s.user_id,
                "clock_in": s.clock_in.isoformat(),
                # The number we refused to bank, kept forever.
                "elapsed_minutes": s.elapsed_minutes,
                "recorded_minutes": 0,
                "reason": "open per-job timer, job never closed out",
            },
        )
    db.commit()
    return len(stale)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the plan (default: dry-run report)")
    ap.add_argument("--operator", default="",
                    help="who is running this (required with --apply; audited)")
    ap.add_argument("--older-than", type=int, default=DEFAULT_MIN_AGE_DAYS,
                    metavar="DAYS",
                    help=f"only close timers open longer than this (default {DEFAULT_MIN_AGE_DAYS}); "
                         "the floor exists so a tech still on the job never gets clocked out")
    args = ap.parse_args()

    if args.apply and not args.operator.strip():
        ap.error("--apply requires --operator")
    if args.older_than < 1:
        ap.error("--older-than must be at least 1 day")

    db = SessionLocal()
    try:
        stale = fetch_stale(db, min_age_days=args.older_than)
        _print_plan(stale, min_age_days=args.older_than)

        if not stale:
            return 0
        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply --operator <you>.")
            return 0

        n = apply_plan(db, stale, operator=args.operator.strip())
        print(f"\nApplied: {n} timer(s) closed at 0 payable minutes. "
              "Audit rows written (stale_job_timer_closed).")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
