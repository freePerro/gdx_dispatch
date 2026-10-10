"""Core → plugin-host schedule driver (GDXA-439).

A plugin may declare ``schedules=((name, cron, callable), ...)`` and the owner
consents to "runs automatically on a fixed schedule". Until this module nothing
read the cron or called the callable: the consent was given and the work was
silently never done (GDXA-438). This is the driver, the sibling of
``core/plugin_events.py``:

- Once a minute (a beat entry platform-core owns, calling
  ``dispatch_plugin_schedules_task``) it reads the live plugin catalog, keeps the
  plugins whose owner consented ``schedules`` and whose declared surface has not
  drifted since (``plugin_consent.schedule_runners`` — consent is re-read every
  tick), and matches each cron against the current UTC minute.
- Each due run is POSTed to plugin-host's token-gated ``/internal/schedules``,
  which runs the callable inline and answers what happened.
- Every run that executed — ``ok`` or ``failed`` — writes one audit row
  (``plugin.schedule_run``) naming the plugin, the schedule, the minute, the
  outcome and who consented, keyed by a run hash in ``request_id``. A
  ``duplicate`` answer is audited with the outcome plugin-host remembers when
  no row for that run exists yet (an earlier POST timed out while the
  callable ran inline), and logged otherwise; ``unknown`` ran nothing and is
  logged.
- Runs are sent one after another on the priority:high worker, each waiting up
  to 30 s, so many slow schedules delay event dispatch on that queue. Keep
  callables short; long work belongs in the plugin's own background thread.

Delivery guarantee, stated honestly: **at-least-once per due minute, while
beat and the worker are up.** ``run_id`` is deterministic
(``<key>:<name>:<minute>``); plugin-host drops a repeated run_id it has seen,
and a retried tick keeps its original minute, so a retry is not a second run —
unless plugin-host restarted in between, which is why callables dedupe on
run_id. There is no catch-up: a minute beat did not tick (or a tick a worker
started more than a minute late) is a run that does not happen. A schedule
that must not miss belongs on a durable path, as with plugin events.

Consent DRIFT fail-closes the plugin's schedules and raises the same throttled
owner signal as event dispatch.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import httpx

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.plugin_consent import (
    _plugin_host_url,
    internal_auth_headers,
    schedule_runners,
)
from gdx_dispatch.plugin_api.schedules import cron_matches, schedule_run_id

log = logging.getLogger(__name__)

#: Outcomes plugin-host reports for a run that actually executed the callable.
EXECUTED = frozenset({"ok", "failed"})


class PluginHostUnavailable(Exception):
    """plugin-host could not be reached, or answered 5xx — worth a retry."""


def _minute(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC).replace(second=0, microsecond=0)


def due_runs(runners: list[dict], minute: datetime) -> list[dict]:
    """The (plugin, schedule) pairs whose cron falls on ``minute``."""
    stamp = minute.strftime("%Y-%m-%dT%H:%MZ")
    out = []
    for entry in runners:
        for spec in entry.get("schedule_specs") or ():
            name, cron = str(spec.get("name") or ""), str(spec.get("cron") or "")
            # Only names the consent fingerprint pinned: the catalog's names
            # list is the preimage; a spec outside it is never run.
            if not name or name not in (entry.get("schedules") or ()):
                continue
            if cron_matches(cron, minute):
                out.append({
                    "key": entry["key"], "name": name, "cron": cron,
                    "scheduled_for": stamp,
                    "run_id": schedule_run_id(entry["key"], name, stamp),
                    "consented_by": entry.get("consented_by"),
                })
    return out


def _fetch_catalog() -> list[dict]:
    """The live catalog, raising when plugin-host cannot answer. Deliberately
    not ``plugin_consent.fetch_catalog``, which returns [] on any error: here
    an empty catalog would read as "nothing is due" and the tick's runs would
    vanish without a word — the same silent no-op this module exists to end."""
    try:
        r = httpx.get(f"{_plugin_host_url()}/api/plugins", timeout=5.0)
    except Exception as exc:
        raise PluginHostUnavailable(str(exc)) from exc
    if r.status_code != 200:
        raise PluginHostUnavailable(f"plugin-host catalog {r.status_code}")
    try:
        return list(r.json())
    except Exception as exc:
        raise PluginHostUnavailable(f"plugin-host catalog unreadable: {exc}") from exc


def _post_run(run: dict) -> dict:
    body = {k: run[k] for k in ("key", "name", "scheduled_for", "run_id")}
    url, headers = f"{_plugin_host_url()}/internal/schedules", internal_auth_headers()
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=30.0)
    except Exception as exc:
        raise PluginHostUnavailable(str(exc)) from exc
    if r.status_code >= 500:
        raise PluginHostUnavailable(f"plugin-host {r.status_code}")
    if r.status_code in (401, 403):
        # Same misconfiguration plugin_events names: retrying cannot help.
        log.error(
            "plugin_schedule_dispatch_unauthorized key=%s schedule=%s status=%s — "
            "GDX_INTERNAL_TOKEN is missing or wrong in THIS container; every "
            "plugin schedule is being dropped.",
            run["key"], run["name"], r.status_code,
        )
        return {"status": "unauthorized"}
    if r.status_code >= 300:
        log.warning("plugin_schedule_dispatch_http_%s key=%s schedule=%s",
                    r.status_code, run["key"], run["name"])
        return {"status": f"http_{r.status_code}"}
    try:
        return dict(r.json())
    except Exception:
        # The callable may have run; without a readable answer core cannot say
        # whether, so it neither audits a guess nor stays quiet about it.
        log.warning("plugin_schedule_reply_unreadable key=%s schedule=%s status=%s",
                    run["key"], run["name"], r.status_code, exc_info=True)
        return {"status": "unreadable"}


def _run_ref(run_id: str) -> str:
    """The audit row's request_id for one run: fixed-length (run_id carries the
    plugin key and schedule name, which can overflow the column) and
    deterministic, so a later send can ask whether the run is already audited."""
    return "sched-" + hashlib.sha256(run_id.encode()).hexdigest()[:40]


def _already_audited(db, run_id: str) -> bool:
    from gdx_dispatch.core.audit import AuditLog, ensure_audit_table

    ensure_audit_table(db)
    return db.query(AuditLog.id).filter(
        AuditLog.action == "plugin.schedule_run",
        AuditLog.request_id == _run_ref(run_id),
    ).first() is not None


def _audit_run(db, run: dict, result: dict) -> None:
    from gdx_dispatch.core.audit import SYSTEM_ACTOR, log_audit_event_sync

    details = {k: run.get(k) for k in ("cron", "scheduled_for", "run_id", "consented_by")}
    details.update(plugin=run["key"], schedule=run["name"],
                   outcome=result.get("status"), error=result.get("error"))
    if result.get("confirmed_via"):
        details["confirmed_via"] = result["confirmed_via"]
    try:
        log_audit_event_sync(
            db, action="plugin.schedule_run", entity_type="plugin",
            entity_id=run["key"], user_id=SYSTEM_ACTOR,
            actor_role="plugin_schedule", details=details,
            request_id=_run_ref(run["run_id"]),
        )
        db.commit()
    except Exception:
        # The run already happened on plugin-host; nothing here can undo it,
        # so say so loudly rather than pretend the trail exists.
        db.rollback()
        log.exception("plugin_schedule_audit_failed run_id=%s", run["run_id"])


def _settle_duplicate(db, run: dict, result: dict, last_attempt: bool,
                      undelivered: list[str]) -> None:
    """plugin-host has seen this run_id. An earlier send may have timed out
    while the callable ran inline, on any retry path (a whole-tick retry too),
    so whether the run is audited is read from the trail, not inferred from
    how we got here. A run still ``running`` is checked back on by the next
    retry, for its final outcome; on the last attempt it is audited as
    ``running`` (plugin-host logs its eventual failure)."""
    outcome = str(result.get("outcome") or "")
    if outcome == "running" and not last_attempt:
        undelivered.append(run["run_id"])
        return
    if outcome in EXECUTED | {"running"} and not _already_audited(db, run["run_id"]):
        _audit_run(db, run, {
            "status": outcome, "error": result.get("error"),
            "confirmed_via": ("still running at the last retry; final outcome unknown"
                              if outcome == "running" else "a later send"),
        })
        return
    log.info("plugin_schedule_duplicate run_id=%s outcome=%s — already run and audited",
             run["run_id"], outcome or "?")


def dispatch_due_schedules(db, now: datetime | None = None,
                           only: list[str] | None = None,
                           last_attempt: bool = True) -> dict:
    """Run every consented plugin schedule due at ``now``'s UTC minute.

    ``only`` — run_ids to restrict to (a retry re-sends just the runs that could
    not be delivered, or are still running). ``last_attempt`` — False while the
    task can still retry. Returns ``{"minute", "due", "results", "undelivered",
    "drifted"}``; ``undelivered`` is the run_ids plugin-host could not be
    reached for, which the Celery task retries. When the catalog itself cannot
    be read, ``undelivered`` is ``None``: the whole tick is retried."""
    minute = _minute(now)
    try:
        catalog = _fetch_catalog()
    except PluginHostUnavailable as exc:
        log.warning("plugin_schedule_catalog_unreachable minute=%s err=%s",
                    minute.isoformat(), exc)
        return {"minute": minute.isoformat(), "due": 0, "results": {},
                "undelivered": None, "drifted": []}
    runners, drifted = schedule_runners(db, catalog)
    if drifted:
        from gdx_dispatch.core.plugin_events import _signal_consent_drift

        # One container-log line per run drift blocks, outside the signal's
        # throttle (which goes quiet while any drift row is pending). Only
        # runs actually due this minute, so a nightly cron logs once a night.
        blocked = due_runs([e for e in catalog if e.get("key") in drifted], minute)
        if blocked:
            log.warning("plugin_schedule_blocked_by_consent_drift run_ids=%s — declared "
                        "surface changed since consent; re-consent to resume",
                        [r["run_id"] for r in blocked])
        _signal_consent_drift(db, drifted, f"schedule@{minute:%Y-%m-%dT%H:%MZ}")
    runs = due_runs(runners, minute)
    if only is not None:
        runs = [r for r in runs if r["run_id"] in set(only)]
    results: dict[str, str] = {}
    undelivered: list[str] = []
    for run in runs:
        try:
            result = _post_run(run)
        except PluginHostUnavailable as exc:
            log.warning("plugin_schedule_dispatch_unreachable run_id=%s err=%s",
                        run["run_id"], exc)
            undelivered.append(run["run_id"])
            continue
        status = str(result.get("status") or "")
        results[run["run_id"]] = status
        if status in EXECUTED:
            _audit_run(db, run, result)
        elif status == "duplicate":
            _settle_duplicate(db, run, result, last_attempt, undelivered)
        elif status == "unknown":
            log.warning("plugin_schedule_unknown run_id=%s — plugin-host does not "
                        "declare it", run["run_id"])
    return {"minute": minute.isoformat(), "due": len(runs), "results": results,
            "undelivered": undelivered, "drifted": drifted}


# Named under core.plugin_events so the existing route sends it to the
# priority:high worker, the one compose gives the internal token alongside
# the event dispatch it mirrors. Registered because plugin_events imports this
# module, and the worker includes plugin_events.
@celery_app.task(bind=True, name="gdx_dispatch.core.plugin_events.dispatch_plugin_schedules",
                 max_retries=2, default_retry_delay=15)
def dispatch_plugin_schedules_task(self, minute_iso: str | None = None,
                                   only: list[str] | None = None) -> int:
    """Beat entry point: one tick. Returns how many runs plugin-host accepted.

    With no ``minute_iso`` the minute is the worker's clock when the task
    starts, not when beat queued it: a tick delayed past its minute (queue lag,
    or an acks_late redelivery after a killed worker) evaluates the later
    minute, and the original minute's runs do not happen.

    A retry carries the tick's ORIGINAL minute and only the undelivered
    run_ids, so the run_ids repeat and plugin-host can drop a duplicate."""
    now = datetime.fromisoformat(minute_iso) if minute_iso else None
    with SessionLocal() as db:
        out = dispatch_due_schedules(db, now=now, only=only,
                                     last_attempt=self.request.retries >= self.max_retries)
    undelivered = out["undelivered"]
    if undelivered is None or undelivered:
        if self.request.retries >= self.max_retries:
            log.error(
                "plugin_schedule_runs_dropped minute=%s run_ids=%s — plugin-host "
                "unreachable or timing out after retries; these runs did not "
                "happen, or ran with no confirmation and no audit row",
                out["minute"], "ALL (catalog unreadable)" if undelivered is None else undelivered,
                extra={"ops_alert": True, "ops_fingerprint": "plugin-schedules"},
            )
        else:
            # None → retry the whole tick (only=None), same minute.
            raise self.retry(args=[out["minute"], undelivered])
    return len(out["results"])
