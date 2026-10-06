"""Ops-alert channel for the marked alarm checks (GDXA-269 / GDXA-271).

Sentry was retired 2026-04-29, and every nightly/hourly check that "logs an
ERROR (→ Sentry)" has reported to nobody since: celery-side errors never
reach ``server_errors`` and nothing emails anyone. This is the replacement
channel, scoped by ruling to the checks that opt in — it does NOT alarm on
every ERROR.

The contract a check opts in with::

    log.error(msg, extra={"ops_alert": True, "ops_fingerprint": "<stable-name>"})

For each such record the handler

1. when no process has emailed that fingerprint in the past hour, emails
   ``OPS_ALERT_EMAIL`` once through the platform SMTP mailbox
   (``_send_platform_email``; no new vendor or credential), capped at
   ``DAILY_CAP`` send attempts (failed ones included) per process per UTC
   day; and
2. writes one ``server_errors`` row (``group_fingerprint`` = the
   fingerprint, ``exception_class`` = ``OpsAlert``, ``method`` = what
   happened to its email: ``EMAIL``, ``DEDUP``, ``NO_SMTP``, ``CAPPED`` or
   ``FAILED``), so it shows on the
   Server Errors page beside the request-path errors.

The hour window is read from the ``EMAIL`` rows in ``server_errors``, so it
holds across gunicorn workers, celery children and restarts, and a send that
failed, was capped or found no SMTP password does not hold it. Two processes
alerting in the same instant can each send once. The daily cap is in memory
and per process — a ceiling on a runaway, not an exact count.
The window is strict (``> now - 1h``), so an hourly check whose next run
lands a moment inside the hour is deduped: a condition that never clears
emails every one to two hours.

Never on the caller's thread: ``emit`` only enqueues; one daemon thread per
process does the DB write and the SMTP send. A full queue drops the record
with a WARNING. A failed write or send logs a WARNING and never raises into
the check. Those WARNINGs carry no marker, so the handler cannot feed itself.

Off unless ``OPS_ALERT_EMAIL`` is set. Installed from the FastAPI lifespan
and from celery ``worker_process_init`` (core/celery_app.py).
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import traceback as tb_mod
import uuid
from datetime import datetime, timedelta, timezone
from html import escape

from sqlalchemy import text

log = logging.getLogger(__name__)

ENV_RECIPIENT = "OPS_ALERT_EMAIL"
DAILY_CAP = 20
DEDUP_WINDOW = timedelta(hours=1)
EXCEPTION_CLASS = "OpsAlert"
# server_errors.method (varchar(10), NULL on non-request rows otherwise) says
# what happened to this occurrence's email, so the Server Errors page tells a
# dead channel from routine dedup. The hour window counts EMAIL rows only.
METHOD_EMAILED = "EMAIL"
METHOD_DEDUP = "DEDUP"  # emailed within the hour already
METHOD_NO_SMTP = "NO_SMTP"  # mailbox password unset or user a placeholder
METHOD_CAPPED = "CAPPED"  # daily cap reached in this process
METHOD_FAILED = "FAILED"  # the send raised

_QUEUE_MAX = 200
_MSG_MAX = 2000
_TRACEBACK_MAX = 8192
_FP_MAX = 64  # server_errors.group_fingerprint is varchar(64)
# The compose default for PLATFORM_SMTP_USER; a login as it can only fail.
_PLACEHOLDER_DOMAIN = "@example.com"


def _smtp_configured() -> bool:
    # _send_platform_email returns quietly (a WARNING, no raise) when the
    # password is unset, so "it did not raise" is not "it sent". Ask first,
    # or an unsent email would spend the hour window and the daily cap. A
    # placeholder user with a real password is a login that has to fail.
    from gdx_dispatch.routers.auth import core as auth_core  # noqa: PLC0415

    user = (auth_core.PLATFORM_SMTP_USER or "").strip().lower()
    return bool(auth_core.PLATFORM_SMTP_PASS) and bool(user) and not user.endswith(
        _PLACEHOLDER_DOMAIN
    )


def _send_email(to_email: str, subject: str, html_body: str) -> None:
    # Imported at call time: the auth router module is heavy and this runs in
    # celery workers too. Tests patch this function.
    from gdx_dispatch.routers.auth.core import _send_platform_email  # noqa: PLC0415

    _send_platform_email(to_email, subject, html_body)


def _tenant_id() -> str | None:
    # The Server Errors endpoints filter on the request's tenant id, so a row
    # without it never lists. Same UUID-or-NULL rule as record_server_error.
    from gdx_dispatch.core.tenant import company_id  # noqa: PLC0415

    try:
        return str(uuid.UUID(str(company_id())))
    except ValueError:
        return None


def _emailed_within_window(fingerprint: str, now: datetime) -> bool | None:
    """Whether any process emailed this fingerprint in the dedup window (None
    when the DB cannot answer). Counts only rows marked ``METHOD_EMAILED``: a
    row whose send failed, was capped or was suppressed must not hold the
    window, or one SMTP hiccup silences the alarm for the hour and a condition
    that fires more often than hourly never alerts again."""
    from gdx_dispatch.core.database import SessionLocal  # noqa: PLC0415

    try:
        with SessionLocal() as db:
            return bool(db.execute(
                text(
                    "SELECT COUNT(*) FROM server_errors "
                    "WHERE group_fingerprint = :fp AND exception_class = :ec "
                    "AND method = :m AND occurred_at > :since"
                ),
                {
                    "fp": fingerprint, "ec": EXCEPTION_CLASS,
                    "m": METHOD_EMAILED, "since": now - DEDUP_WINDOW,
                },
            ).scalar())
    except Exception as exc:  # noqa: BLE001 — the alarm path must never raise
        log.warning("ops_alert_window_read_failed fingerprint=%s: %s", fingerprint, exc)
        return None


def _record_row(
    fingerprint: str, record: logging.LogRecord, now: datetime, outcome: str
) -> None:
    """Insert the ``server_errors`` row, its ``method`` saying whether this
    occurrence's email went (the Server Errors page shows it beside the path)."""
    from gdx_dispatch.core.database import SessionLocal  # noqa: PLC0415
    from gdx_dispatch.modules.error_sink.service import _GIT_SHA  # noqa: PLC0415

    tb_text = None
    if record.exc_info and record.exc_info[0] is not None:
        tb_text = "".join(tb_mod.format_exception(*record.exc_info))[-_TRACEBACK_MAX:]
    try:
        with SessionLocal() as db:
            db.execute(
                text(
                    "INSERT INTO server_errors ("
                    "  id, tenant_id, method, path, exception_class, exception_message, "
                    "  traceback, git_sha, group_fingerprint, occurred_at"
                    ") VALUES (:id, :tid, :m, :p, :ec, :em, :tb, :gs, :fp, :ts)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": _tenant_id(),
                    "m": outcome,
                    # No request: the logger name says where it came from.
                    "p": record.name,
                    "ec": EXCEPTION_CLASS,
                    "em": record.getMessage()[:_MSG_MAX],
                    "tb": tb_text,
                    "gs": _GIT_SHA,
                    "fp": fingerprint,
                    "ts": now,
                },
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 — the alarm path must never raise
        log.warning("ops_alert_sink_write_failed fingerprint=%s: %s", fingerprint, exc)


class OpsAlertHandler(logging.Handler):
    """Acts only on records carrying ``ops_alert=True``; ignores everything else."""

    def __init__(self, recipient: str, daily_cap: int = DAILY_CAP) -> None:
        super().__init__(level=logging.ERROR)
        self.recipient = recipient
        self.daily_cap = daily_cap
        self._queue: queue.Queue[tuple[str, logging.LogRecord]] = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        # In-process fallback when the DB cannot answer "first this hour?".
        self._last_sent: dict[str, datetime] = {}
        self._day: str = ""
        self._sent_today = 0

    # ── caller thread ────────────────────────────────────────────────────
    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "ops_alert", None) is not True:
            return
        fingerprint = str(getattr(record, "ops_fingerprint", "") or "unfingerprinted")[:_FP_MAX]
        try:
            # Format now: args may be mutated by the caller after we return.
            record.msg = record.getMessage()
            record.args = None
            self._ensure_worker()
            self._queue.put_nowait((fingerprint, record))
        except queue.Full:
            log.warning("ops_alert_queue_full: dropped fingerprint=%s", fingerprint)
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _ensure_worker(self) -> None:
        # Started lazily, in the process that logs: a thread started before a
        # prefork fork would not exist in the child.
        if self._worker is not None and self._worker.is_alive():
            return
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="ops-alert", daemon=True
                )
                self._worker.start()

    # ── worker thread ────────────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            fingerprint, record = self._queue.get()
            try:
                self.process(fingerprint, record)
            except Exception as exc:  # noqa: BLE001
                log.warning("ops_alert_process_failed fingerprint=%s: %s", fingerprint, exc)
            finally:
                self._queue.task_done()

    def process(self, fingerprint: str, record: logging.LogRecord) -> bool:
        """Send at most one email, then record the row with the outcome.
        Returns True if sent."""
        now = datetime.now(timezone.utc)
        outcome = self._maybe_send(fingerprint, record, now)
        _record_row(fingerprint, record, now, outcome)
        return outcome == METHOD_EMAILED

    def _maybe_send(self, fingerprint: str, record: logging.LogRecord, now: datetime) -> str:
        """Return the outcome, one of the ``METHOD_*`` values."""
        last = self._last_sent.get(fingerprint)
        if last is not None and now - last < DEDUP_WINDOW:
            return METHOD_DEDUP
        if _emailed_within_window(fingerprint, now):
            return METHOD_DEDUP
        day = now.strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._sent_today = day, 0
        if not _smtp_configured():
            log.warning(
                "ops_alert_email_not_sent fingerprint=%s: PLATFORM_SMTP_PASS is "
                "unset or PLATFORM_SMTP_USER is a placeholder; the alert is "
                "recorded on Server Errors only", fingerprint,
            )
            return METHOD_NO_SMTP
        if self._sent_today >= self.daily_cap:
            log.warning(
                "ops_alert_daily_cap_reached cap=%d: not emailing fingerprint=%s",
                self.daily_cap, fingerprint,
            )
            return METHOD_CAPPED
        subject = f"[GDX ops alert] {fingerprint}"
        body = (
            f"<p><b>{escape(fingerprint)}</b> raised an ops alert at "
            f"{now.isoformat(timespec='seconds')}.</p>"
            f"<p>Logger: <code>{escape(record.name)}</code></p>"
            f"<pre>{escape(record.getMessage()[:_MSG_MAX])}</pre>"
            "<p>Recorded on Server Errors. Further occurrences of this "
            "fingerprint within the hour are recorded there without email.</p>"
        )
        # The cap counts attempts: a mailbox that keeps refusing the login
        # must not be hammered (each try is an SMTP_SSL login, up to 15s).
        self._sent_today += 1
        try:
            _send_email(self.recipient, subject, body)
        except Exception as exc:  # noqa: BLE001 — never raise into the check
            log.warning("ops_alert_email_failed fingerprint=%s: %s", fingerprint, exc)
            return METHOD_FAILED
        # Only a send that went out holds the hour window.
        self._last_sent[fingerprint] = now
        return METHOD_EMAILED

    def flush_queue(self, timeout: float = 10.0) -> bool:
        """Block until queued records are processed (tests, shutdown)."""
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        while self._queue.unfinished_tasks:
            if datetime.now(timezone.utc) > deadline:
                return False
            threading.Event().wait(0.01)
        return True


def install_ops_alert_handler(logger: logging.Logger | None = None) -> OpsAlertHandler | None:
    """Attach the handler to the root logger when ``OPS_ALERT_EMAIL`` is set.

    Idempotent per process. Returns the installed handler, or None when off."""
    recipient = (os.environ.get(ENV_RECIPIENT) or "").strip()
    if not recipient:
        return None
    target = logger or logging.getLogger()
    for h in target.handlers:
        if isinstance(h, OpsAlertHandler):
            return h
    handler = OpsAlertHandler(recipient)
    target.addHandler(handler)
    log.info("ops_alert_handler_installed daily_cap=%d", handler.daily_cap)
    if not _smtp_configured():
        log.warning(
            "ops_alert_email_unconfigured: OPS_ALERT_EMAIL is set but "
            "PLATFORM_SMTP_PASS is unset or PLATFORM_SMTP_USER is a "
            "placeholder; alerts will be recorded, not emailed"
        )
    return handler
