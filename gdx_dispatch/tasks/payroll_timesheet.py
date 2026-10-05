"""Mail the pay period's timesheet once it closes — or hold and say so.

Doug 2026-08-26 chose automatic sending, and "hold and alert me" when the
hours are not clean. Both halves live here.

WHEN it fires is the load-bearing decision. The send runs the morning AFTER
a period closes, not on payday morning:

    period ends Sun ──► send Mon 7am ──► ... ──► paid Fri
                             │
                             └─ a hold discovered here leaves four days to
                                fix it. A hold discovered on payday morning
                                leaves the hours late.

Beat fires this hourly, and the task decides. Three reasons it is not a
once-a-day cron at the configured hour:

  1. Catch-up. A container down at 7am on Monday would otherwise skip that
     fortnight entirely and nobody would know until the bookkeeper asked.
     This retries every hour until it succeeds or the pay date passes.
  2. It clears its own hold. The office corrects the open shift at 9:15;
     the 10:00 run sends. No second button to remember.
  3. Idempotence has to exist anyway (an hourly beat can run twice in an
     hour after a restart), and once it exists, catch-up is free.

"Already sent" is read from the audit log rather than a new column: the send
already writes `timesheet_sent` keyed by the period, that row is the record
of what actually left, and a separate flag could disagree with it.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from gdx_dispatch.core.audit import AuditLog, log_audit_event_sync
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal, contained_read
from gdx_dispatch.core.office_notifications import notify_office
from gdx_dispatch.core.pay_periods import (
    PayPeriod,
    PayPeriodUnconfigured,
    pay_date,
    period_containing,
    previous_period,
    resolve_zone,
    settings_config,
)
from gdx_dispatch.core.timesheet_delivery import (
    BLOCKED_FLAGGED,
    BLOCKED_NO_SENDER,
    BLOCKED_UNREADABLE,
    SendOutcome,
    send_period_timesheet,
)
from gdx_dispatch.core.timesheet_hours import build_timesheet

log = logging.getLogger(__name__)

#: The acting identity on an unattended send. Money mutations may never run
#: as a system default — this is not one (nothing is written to a payment,
#: an invoice or an hour), but the audit row still has to say plainly that
#: nobody pressed anything.
SYSTEM_ACTOR = "system:payroll-timesheet"


def _entity_id(period: PayPeriod) -> str:
    return f"{period.start.isoformat()}..{period.end.isoformat()}"


def _audit_says(db, tenant_id: str, action: str, period: PayPeriod) -> bool:
    """Has `action` already been recorded for this period?"""
    try:
        # SAVEPOINT: `db` is the task's own long-lived SessionLocal. Be precise
        # about what this buys. In `send_closed_period` the only call is the
        # `timesheet_sent` probe, whose `True` default returns
        # `{"skipped": "already_sent"}` on the very next line — there,
        # containment changes no outcome. It is kept so the helper leaves its
        # session usable for whatever a caller reads next (the hold probe that
        # used to share it is `_held_for` now, contained on its own).
        # See core.database.contained_read.
        with contained_read(db):
            row = db.execute(
                text(
                    "SELECT 1 FROM audit_logs "
                    "WHERE action = :action AND entity_id = :eid "
                    "LIMIT 1"
                ),
                {"action": action, "eid": _entity_id(period)},
            ).first()
    except Exception:
        # Unreadable audit log means we cannot prove it has NOT been sent.
        # Claim it has: a missed timesheet is a phone call, a duplicate one
        # mailed every hour is a mess nobody can unsend.
        log.exception("payroll_timesheet_audit_read_failed", extra={"tenant_id": tenant_id})
        return True
    return row is not None


def _held_for(db, tenant_id: str, reason: str, period: PayPeriod) -> bool:
    """Is the LATEST hold on record for this period a hold for `reason`?

    The dedupe that keeps the office alert to one per hold. It keys on the
    reason because each alert is a promise about what happens next — the
    flagged one ends "correct them and it sends itself", the unreadable one
    "it tries again every hour", the no-sender one names a setting. Keyed on
    "held for any reason", whichever hold came first silenced every later
    one, and the office kept acting on a promise that had stopped being true
    (GDXA-197 audit, both directions). Latest rather than any-ever, so a
    period that goes flagged → unreadable → flagged re-alerts on the way
    back: the last thing the office was told must match the current hold.

    The manual Send route writes the same action, so a hold the office just
    saw on its own screen counts as heard.

    Called BEFORE this tick's hold row is written. Read through the ORM so
    `details` comes back parsed on SQLite and Postgres alike. An unreadable
    audit log answers False: here the cost of a wrong answer is a duplicate
    alert, not a duplicate payroll file.
    """
    try:
        with contained_read(db):
            latest = db.execute(
                select(AuditLog.details)
                .where(
                    AuditLog.action == "timesheet_send_blocked",
                    AuditLog.entity_id == _entity_id(period),
                )
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            ).scalar()
    except Exception:
        log.exception("payroll_timesheet_hold_read_failed", extra={"tenant_id": tenant_id})
        return False
    return isinstance(latest, dict) and latest.get("blocked") == reason


def _closed_period(today, cfg) -> PayPeriod:
    """The most recently finished period.

    `today` always sits inside the current period, so the one before it has
    ended — true on the morning after close and every day after, which is
    what makes catching up possible.
    """
    current = period_containing(
        today, cadence=cfg["cadence"], anchor_start=cfg["anchor_start"]
    )
    return previous_period(
        current, cadence=cfg["cadence"], anchor_start=cfg["anchor_start"]
    )


@celery_app.task(name="payroll_timesheet.send_closed_period")
def send_closed_period() -> dict:
    """One tick. Returns a dict naming what it did — never a bare None.

    Every early return says WHY it did nothing. A scheduled task that logs
    "succeeded" with no detail is how this repo ended up with an hourly beat
    that did nothing for months while looking healthy.
    """
    db = SessionLocal()
    try:
        from gdx_dispatch.models.tenant_models import AppSettings

        # Counted by the GDXA-164 sweep, deliberately NOT contained: this is the
        # FIRST statement on a session this task just opened, so there is no
        # caller and no pending work behind it, and a failure here jumps to the
        # outer handler without running any later read. `_audit_says` and
        # `_names` below DO get savepoints — they sit mid-run, with work after
        # them. The difference is what is downstream, not the shape.
        settings = db.query(AppSettings).first()
        if settings is None:
            return {"skipped": "no_settings"}
        if not bool(getattr(settings, "payroll_autosend_enabled", False)):
            return {"skipped": "autosend_off"}

        tenant_id = _tenant_id()
        if not tenant_id:
            return {"skipped": "no_tenant"}

        tz_name = getattr(settings, "timezone", None) or "America/New_York"
        now_shop = datetime.now(UTC).astimezone(resolve_zone(tz_name))
        today = now_shop.date()
        cfg = settings_config(settings)

        try:
            period = _closed_period(today, cfg)
        except PayPeriodUnconfigured as exc:
            # Biweekly with no anchor. Settings refuses to save that, so
            # reaching here means a hand-edited row; say so rather than
            # guessing a fortnight.
            log.warning("payroll_timesheet_unconfigured: %s", exc)
            return {"skipped": "unconfigured", "detail": str(exc)}

        send_hour = int(getattr(settings, "payroll_autosend_hour", 7) or 0)
        if now_shop.hour < send_hour:
            return {"skipped": "too_early", "period": _entity_id(period)}

        # Stop retrying once the money has been paid — at that point a late
        # file is a conversation, not an automation.
        deadline = max(
            pay_date(period, cfg["lag_days"]), period.end + timedelta(days=1)
        )
        if today > deadline:
            return {"skipped": "past_pay_date", "period": _entity_id(period)}

        if _audit_says(db, tenant_id, "timesheet_sent", period):
            return {"skipped": "already_sent", "period": _entity_id(period)}

        names = _names(db, tenant_id)
        pto_overtime = bool(getattr(settings, "time_off_counts_toward_overtime", False))
        try:
            sheet = build_timesheet(
                db,
                tenant_id=tenant_id,
                period=period,
                tz_name=tz_name,
                names=names,
                time_off_counts_toward_overtime=pto_overtime,
            )
        except SQLAlchemyError:
            # The hours (or their breaks) could not be read, so there is
            # nothing honest to mail — gross hours were what went out before
            # the breaks read refused (GDXA-197). A hold like the others: an
            # audit row per tick, one office alert per period for THIS reason,
            # and every tick retries.
            # Rolled back first: the breaks read contains itself, but the
            # entries read inside build_timesheet does not, and on Postgres its
            # failure leaves this session aborted with the hold still to write.
            log.exception("payroll_timesheet_unreadable: %s", _entity_id(period))
            db.rollback()
            already_held = _held_for(db, tenant_id, BLOCKED_UNREADABLE, period)
            outcome = SendOutcome(
                sent=False,
                blocked=BLOCKED_UNREADABLE,
                detail=(
                    "the hours could not be read. Nothing was mailed; "
                    "it tries again every hour."
                ),
            )
            _audit(db, tenant_id, outcome, period)
            if not already_held:
                _alert_office(db, tenant_id, outcome, period)
            return {
                "held": BLOCKED_UNREADABLE,
                "period": _entity_id(period),
                "notified": not already_held,
            }

        # A blocked period is evaluated on every tick — deliberately. The
        # gate runs before any PDF is rendered, so an hourly no-op is cheap,
        # and the moment the office corrects the shift the next tick sends.
        # Whether the office has already heard about THIS hold is asked once
        # the outcome names its reason, before `_audit` records this tick.

        # WHOSE mailbox this leaves from. Outlook Graph authenticates as a
        # specific person and an unattended run has no calling user, so the
        # tenant nominates one (the same setting automated workflow email
        # already uses). Passing SYSTEM_ACTOR here instead — which the first
        # version did — makes every send undeliverable while looking fine.
        sender = str(getattr(settings, "automation_sender_user_id", "") or "")

        outcome = send_period_timesheet(
            db,
            tenant_id=tenant_id,
            settings=settings,
            sheet=sheet,
            actor_user_id=SYSTEM_ACTOR,
            initiator_kind="system",
            sender_user_id=sender,
        )

        already_held = bool(outcome.blocked) and _held_for(
            db, tenant_id, outcome.blocked, period
        )
        _audit(db, tenant_id, outcome, period)

        if outcome.blocked:
            # A missing sender is a configuration problem, not a data one —
            # it never clears itself by correcting a shift, so it must be
            # said out loud rather than retried quietly every hour.
            if outcome.blocked == BLOCKED_NO_SENDER and already_held:
                log.warning("payroll_timesheet_no_sender: %s", _entity_id(period))
            if not already_held:
                _alert_office(db, tenant_id, outcome, period)
            log.info(
                "payroll_timesheet_held: %s (%s)", _entity_id(period), outcome.blocked
            )
            return {
                "held": outcome.blocked,
                "period": _entity_id(period),
                "flagged": len(outcome.flagged),
                "notified": not already_held,
            }

        if not outcome.sent:
            # Mail refused. Tell the office — an automation that fails
            # silently is indistinguishable from one that never ran.
            _alert_office(db, tenant_id, outcome, period)
            return {"failed": outcome.detail, "period": _entity_id(period)}

        notify_office(
            db,
            tenant_id,
            title="Timesheet sent to payroll",
            message=(
                f"{outcome.hours:.2f} hours for {outcome.people} "
                f"{'person' if outcome.people == 1 else 'people'}, "
                f"{period.label()} — sent to {', '.join(outcome.delivered_to)}."
            ),
            category="timesheet",
        )
        log.info("payroll_timesheet_sent: %s", _entity_id(period))
        return {
            "sent": True,
            "period": _entity_id(period),
            "hours": outcome.hours,
            "recipients": outcome.delivered_to,
        }
    except Exception:
        log.exception("payroll_timesheet_tick_failed")
        return {"failed": "exception"}
    finally:
        db.close()


def _tenant_id() -> str:
    """The env-derived tenant id, matching every other beat task here
    (timeclock_sweep, billing_followup).

    Verified against prod 2026-08-26: all 53 timeclock rows carry
    GDX_TENANT_ID's value, so the hours this task reads are the hours the
    app writes. Using anything else — a `tenants` table lookup, say — risks
    an id nothing else matches, and the failure mode is silent: the query
    finds no rows and the period reports as empty.

    Unlike the sibling tasks this does NOT fall back to a literal "gdx".
    A wrong tenant id here does not error, it returns zero hours, and a
    timesheet that says nobody worked is the worst possible wrong answer.
    Better to skip and say why.
    """
    import os

    return (
        os.getenv("GDX_TENANT_ID")
        or os.getenv("GDX_DEFAULT_TENANT_ID")
        or ""
    ).strip()


def _names(db, tenant_id: str) -> dict[str, str]:
    """Display names, best effort — an unnamed row is still a real person's
    hours and must not be dropped for want of a label."""
    try:
        # SAVEPOINT for the same reason as `_audit_says`: a missing label must
        # not cost the send that follows on this same session.
        with contained_read(db):
            rows = db.execute(
                text(
                    "SELECT id::text AS id, "
                    "COALESCE(NULLIF(full_name,''), NULLIF(name,''), "
                    "NULLIF(username,''), email) AS label "
                    "FROM users WHERE company_id = :tid"
                ),
                {"tid": tenant_id},
            ).mappings().all()
        return {str(r["id"]): r["label"] for r in rows if r["label"]}
    except Exception:
        log.debug("payroll_timesheet_names_failed", exc_info=True)
        return {}


def _audit(db, tenant_id: str, outcome, period: PayPeriod) -> None:
    try:
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=SYSTEM_ACTOR,
            action="timesheet_sent" if outcome.sent else "timesheet_send_blocked",
            entity_type="timesheet",
            entity_id=_entity_id(period),
            details=outcome.as_dict(),
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("payroll_timesheet_audit_failed")


def _alert_office(db, tenant_id: str, outcome, period: PayPeriod) -> None:
    """The hold, as something a person can act on.

    Names up to three of the offending shifts. A bare count ("3 shifts need
    a look") sends the operator hunting through a fortnight; a list tells
    them whose day to open.
    """
    if outcome.blocked == BLOCKED_FLAGGED and outcome.flagged:
        preview = "; ".join(
            f"{f['name']} {f['date']} ({f['reason']})" for f in outcome.flagged[:3]
        )
        more = len(outcome.flagged) - 3
        if more > 0:
            preview += f"; and {more} more"
        message = (
            f"{period.label()} was not sent — {preview}. "
            "Correct them on Timesheets and it sends itself."
        )
    else:
        message = f"{period.label()} was not sent — {outcome.detail}"

    notify_office(
        db,
        tenant_id,
        title="Timesheet held — payroll not sent",
        message=message,
        category="timesheet",
    )
