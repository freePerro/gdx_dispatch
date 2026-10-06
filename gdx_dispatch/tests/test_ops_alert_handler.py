"""GDXA-271: the ops-alert channel for the marked alarm checks.

Done-when, from the issue: a marked ERROR from a celery task writes a
``server_errors`` row and queues exactly one email per fingerprint per hour;
an unmarked ERROR sends nothing; an unset ``OPS_ALERT_EMAIL`` sends nothing;
an SMTP failure does not raise.

``server_errors`` has no ORM model — it is created by the squashed baseline
SQL (migration 001). The fixture builds it from the column list parsed out of
that SQL, so a column this code writes that the real table lacks fails here.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.modules.error_sink import ops_alert
from gdx_dispatch.modules.error_sink.ops_alert import OpsAlertHandler, install_ops_alert_handler

_BASELINE = Path(__file__).resolve().parents[1] / "migrations" / "baseline_squashed.sql"
FP = "audit-chain-verify-nightly"


def _server_errors_columns() -> list[str]:
    sql = _BASELINE.read_text()
    block = re.search(r"CREATE TABLE public\.server_errors \((.*?)\n\);", sql, re.S)
    assert block, "server_errors DDL not found in the baseline"
    return [line.strip().split()[0] for line in block.group(1).strip().splitlines()]


@pytest.fixture(autouse=True)
def _real_looking_smtp_user(monkeypatch):
    """The image's default PLATFORM_SMTP_USER is the compose placeholder,
    which reads as unconfigured; tests that need it say so explicitly."""
    from gdx_dispatch.routers.auth import core as auth_core

    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "alerts@mailbox.test")


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ops.db'}", connect_args={"check_same_thread": False}
    )
    cols = ", ".join(f"{c} TEXT" for c in _server_errors_columns())
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE server_errors ({cols})"))
        conn.execute(text(
            "CREATE TABLE audit_logs (id TEXT PRIMARY KEY, row_hash TEXT)"
        ))
    factory = sessionmaker(bind=engine)
    # ops_alert imports SessionLocal from core.database at call time.
    monkeypatch.setattr("gdx_dispatch.core.database.SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ops_alert, "_smtp_configured", lambda: True)

    def _fake_send(to_email, subject, html_body):
        calls.append({
            "to": to_email, "subject": subject, "body": html_body,
            "thread": threading.current_thread().name,
        })

    monkeypatch.setattr(ops_alert, "_send_email", _fake_send)
    return calls


@pytest.fixture
def root_handler(monkeypatch, db):
    """Install through the real entry point on the root logger; remove after."""
    monkeypatch.setenv("OPS_ALERT_EMAIL", "ops@example.test")
    root = logging.getLogger()
    before = list(root.handlers)
    handler = install_ops_alert_handler()
    assert isinstance(handler, OpsAlertHandler)
    yield handler
    handler.flush_queue()
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)


def _rows(factory) -> list[tuple]:
    with factory() as s:
        return s.execute(text(
            "SELECT group_fingerprint, exception_class, path, exception_message "
            "FROM server_errors ORDER BY occurred_at"
        )).all()


def _run_audit_task_with_broken_chain(monkeypatch, factory):
    from gdx_dispatch.tasks import audit_chain_verify

    monkeypatch.setattr(audit_chain_verify, "SessionLocal", factory)
    monkeypatch.setattr(audit_chain_verify, "verify_audit_chain", lambda _db: False)
    result = audit_chain_verify.verify_chain_nightly.apply().get()
    assert result["ok"] is False and result["unchained"] == 0
    return result


def test_marked_error_from_celery_task_records_row_and_emails_once_per_hour(
    monkeypatch, db, sent, root_handler
):
    _run_audit_task_with_broken_chain(monkeypatch, db)
    _run_audit_task_with_broken_chain(monkeypatch, db)
    assert root_handler.flush_queue()

    rows = _rows(db)
    assert len(rows) == 2, rows
    assert {r[0] for r in rows} == {FP}
    assert {r[1] for r in rows} == {"OpsAlert"}
    assert rows[0][2] == "gdx_dispatch.tasks.audit_chain_verify"
    assert "AUDIT_CHAIN_BROKEN" in rows[0][3]
    assert _methods(db) == ["EMAIL", "DEDUP"]

    assert len(sent) == 1, sent
    assert sent[0]["to"] == "ops@example.test"
    assert FP in sent[0]["subject"]
    # Never on the logging caller's thread.
    assert sent[0]["thread"] == "ops-alert"
    assert sent[0]["thread"] != threading.current_thread().name


def test_each_fingerprint_gets_its_own_email(db, sent, root_handler):
    lg = logging.getLogger("gdx_dispatch.test_ops_alert")
    for fp in ("a-check", "b-check", "a-check"):
        lg.error("boom %s", fp, extra={"ops_alert": True, "ops_fingerprint": fp})
    assert root_handler.flush_queue()
    assert sorted(c["subject"] for c in sent) == [
        "[GDX ops alert] a-check", "[GDX ops alert] b-check",
    ]
    assert len(_rows(db)) == 3


def test_alert_row_lists_on_the_server_errors_page(monkeypatch, db, sent, root_handler):
    """Read back the way an operator does: the real list endpoint, with the
    request tenant the middleware sets. A row without the tenant id is in the
    table and on no page."""
    import types

    from gdx_dispatch.core.tenant import single_tenant
    from gdx_dispatch.modules.error_sink import router as es_router

    monkeypatch.setenv("GDX_TENANT_ID", "11111111-2222-3333-4444-555555555555")
    _run_audit_task_with_broken_chain(monkeypatch, db)
    assert root_handler.flush_queue()
    request = types.SimpleNamespace(state=types.SimpleNamespace(tenant=single_tenant()))
    with db() as s:
        out = es_router.list_errors(
            request=request, user={"role": "admin"}, status="open", path=None,
            exception_class="OpsAlert",
            fingerprint=FP, page=1, page_size=50, db=s,
        )
    assert out["total"] == 1, out
    item = out["items"][0]
    item = item if isinstance(item, dict) else item.model_dump()
    assert item["group_fingerprint"] == FP
    assert "AUDIT_CHAIN_BROKEN" in item["exception_message"]


def test_unset_smtp_password_is_not_counted_as_sent(monkeypatch, db, caplog):
    """_send_platform_email returns quietly with no password. That must not
    spend the hour window or the daily cap, or the channel looks on and is
    silent for an hour after the password is fixed."""
    from gdx_dispatch.routers.auth import core as auth_core

    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "")
    sent_calls: list = []
    monkeypatch.setattr(ops_alert, "_send_email", lambda *a: sent_calls.append(a))
    h = OpsAlertHandler("ops@example.test", daily_cap=1)
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    with caplog.at_level(logging.WARNING, logger=ops_alert.log.name):
        assert h.process("fp-a", rec) is False
    assert sent_calls == []
    assert h._sent_today == 0 and "fp-a" not in h._last_sent
    assert any("PLATFORM_SMTP_PASS" in r.getMessage() for r in caplog.records)
    assert _methods(db) == ["NO_SMTP"]  # still recorded, and says why

    # Password fixed within the hour: the SAME fingerprint now sends — the
    # unsent row must not hold the window, in this process or any other.
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "pw")
    assert h.process("fp-a", rec) is True
    assert OpsAlertHandler("ops@example.test").process("fp-a", rec) is False
    assert len(sent_calls) == 1
    assert _methods(db) == ["NO_SMTP", "EMAIL", "DEDUP"]


def _methods(factory) -> list[str]:
    with factory() as s:
        return [r[0] for r in s.execute(text(
            "SELECT method FROM server_errors ORDER BY occurred_at"
        )).all()]


def _seed(factory, fp: str, minutes_ago: int, method: str) -> None:
    with factory() as s:
        s.execute(text(
            "INSERT INTO server_errors (id, exception_class, method, group_fingerprint, occurred_at) "
            "VALUES (:id, 'OpsAlert', :m, :fp, :ts)"
        ), {
            "id": str(uuid.uuid4()), "fp": fp, "m": method,
            "ts": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        })
        s.commit()


def test_hour_window_is_read_from_emailed_rows_across_processes(db, sent):
    """An email another process sent 10 minutes ago suppresses this one; one
    from two hours ago does not; and an un-emailed row 10 minutes ago (a
    failed, capped or suppressed occurrence) does not hold the window."""
    _seed(db, "recent", 10, "EMAIL")
    _seed(db, "stale", 120, "EMAIL")
    _seed(db, "unsent", 10, "FAILED")
    h = OpsAlertHandler("ops@example.test")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    assert h.process("recent", rec) is False
    assert h.process("stale", rec) is True
    assert h.process("unsent", rec) is True
    assert [c["subject"] for c in sent] == [
        "[GDX ops alert] stale", "[GDX ops alert] unsent",
    ]


def test_a_condition_firing_more_than_hourly_alerts_again_each_hour(db, sent):
    """Emailed 110 min ago, suppressed repeat 50 min ago: the window slides
    over emails, not occurrences, so this one emails."""
    _seed(db, FP, 110, "EMAIL")
    _seed(db, FP, 50, "DEDUP")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    assert OpsAlertHandler("ops@example.test").process(FP, rec) is True
    assert len(sent) == 1


def test_unmarked_error_sends_nothing_and_writes_nothing(db, sent, root_handler):
    lg = logging.getLogger("gdx_dispatch.test_ops_alert")
    lg.error("plain error, no marker")
    lg.error("explicitly unmarked", extra={"ops_alert": False, "ops_fingerprint": "x"})
    lg.error("truthy but not True", extra={"ops_alert": "yes", "ops_fingerprint": "x"})
    assert root_handler.flush_queue()
    assert sent == []
    assert _rows(db) == []


def test_unset_env_installs_nothing_and_sends_nothing(monkeypatch, db, sent):
    monkeypatch.delenv("OPS_ALERT_EMAIL", raising=False)
    root = logging.getLogger()
    before = list(root.handlers)
    assert install_ops_alert_handler() is None
    monkeypatch.setenv("OPS_ALERT_EMAIL", "   ")
    assert install_ops_alert_handler() is None
    assert root.handlers == before
    _run_audit_task_with_broken_chain(monkeypatch, db)
    assert sent == []
    assert _rows(db) == []


def test_smtp_failure_is_a_warning_and_never_raises(monkeypatch, db, caplog):
    """Real invocation of the platform sender with the SMTP connection failing,
    not a mocked send — the failure has to cross the real code path."""
    import smtplib

    from gdx_dispatch.routers.auth import core as auth_core

    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "pw")

    def _refuse(*_a, **_k):
        raise smtplib.SMTPConnectError(421, b"no")

    monkeypatch.setattr(smtplib, "SMTP_SSL", _refuse)
    h = OpsAlertHandler("ops@example.test")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    with caplog.at_level(logging.WARNING, logger=ops_alert.log.name):
        assert h.process(FP, rec) is False
    assert any("ops_alert_email_failed" in r.getMessage() for r in caplog.records)
    assert all(not getattr(r, "ops_alert", False) for r in caplog.records)
    assert _methods(db) == ["FAILED"]

    # SMTP back within the hour: the same fingerprint emails, from this very
    # handler (its memory must not hold the failed send) ...
    sent_calls: list = []
    monkeypatch.setattr(ops_alert, "_send_email", lambda *a: sent_calls.append(a))
    assert h.process(FP, rec) is True
    assert len(sent_calls) == 1
    assert _methods(db) == ["FAILED", "EMAIL"]


def test_smtp_failure_in_one_process_does_not_silence_another(monkeypatch, db):
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    monkeypatch.setattr(ops_alert, "_smtp_configured", lambda: True)

    def _boom(*_a, **_k):
        raise OSError("smtp down")

    monkeypatch.setattr(ops_alert, "_send_email", _boom)
    assert OpsAlertHandler("ops@example.test").process(FP, rec) is False
    sent_calls: list = []
    monkeypatch.setattr(ops_alert, "_send_email", lambda *a: sent_calls.append(a))
    assert OpsAlertHandler("ops@example.test").process(FP, rec) is True
    assert len(sent_calls) == 1


def test_smtp_failure_on_the_worker_thread_does_not_reach_the_caller(
    monkeypatch, db, root_handler
):
    def _boom(*_a, **_k):
        raise OSError("smtp down")

    monkeypatch.setattr(ops_alert, "_send_email", _boom)
    _run_audit_task_with_broken_chain(monkeypatch, db)  # must not raise
    assert root_handler.flush_queue()
    assert len(_rows(db)) == 1


def test_placeholder_smtp_user_is_not_configured(monkeypatch, db, caplog):
    """The compose default user is a placeholder: with a real password the
    login has to fail, so it must read as unconfigured, at install and per
    alert, not as a channel that is on."""
    from gdx_dispatch.routers.auth import core as auth_core

    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_PASS", "pw")
    monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "info@example.com")
    calls: list = []
    monkeypatch.setattr(ops_alert, "_send_email", lambda *a: calls.append(a))
    monkeypatch.setenv("OPS_ALERT_EMAIL", "ops@example.test")
    lg = logging.getLogger("gdx_dispatch.test_ops_alert_placeholder")
    with caplog.at_level(logging.WARNING, logger=ops_alert.log.name):
        h = install_ops_alert_handler(lg)
    try:
        assert any("ops_alert_email_unconfigured" in r.getMessage() for r in caplog.records)
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
        assert h.process(FP, rec) is False
        assert calls == [] and _methods(db) == ["NO_SMTP"]
        monkeypatch.setattr(auth_core, "PLATFORM_SMTP_USER", "alerts@mailbox.test")
        assert h.process(FP, rec) is True
    finally:
        lg.removeHandler(h)


def test_failed_sends_count_toward_the_daily_cap(monkeypatch, db):
    """A mailbox refusing every login is tried at most daily_cap times."""
    monkeypatch.setattr(ops_alert, "_smtp_configured", lambda: True)
    attempts: list = []

    def _refuse(*a):
        attempts.append(a)
        raise OSError("535 authentication failed")

    monkeypatch.setattr(ops_alert, "_send_email", _refuse)
    h = OpsAlertHandler("ops@example.test", daily_cap=3)
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    assert [h.process(FP, rec) for _ in range(10)] == [False] * 10
    assert len(attempts) == 3


def test_db_failure_still_emails_once_from_memory(monkeypatch, sent):
    def _broken():
        raise RuntimeError("db down")

    monkeypatch.setattr("gdx_dispatch.core.database.SessionLocal", _broken)
    h = OpsAlertHandler("ops@example.test")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    assert h.process(FP, rec) is True
    assert h.process(FP, rec) is False
    assert len(sent) == 1


def test_daily_cap(db, sent):
    h = OpsAlertHandler("ops@example.test", daily_cap=2)
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    results = [h.process(f"fp-{i}", rec) for i in range(4)]
    assert results == [True, True, False, False]
    assert len(sent) == 2
    # Capped occurrences are still recorded, as not emailed.
    assert sorted(_methods(db)) == ["CAPPED", "CAPPED", "EMAIL", "EMAIL"]


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setenv("OPS_ALERT_EMAIL", "ops@example.test")
    lg = logging.getLogger("gdx_dispatch.test_ops_alert_idem")
    first = install_ops_alert_handler(lg)
    try:
        assert install_ops_alert_handler(lg) is first
        assert sum(isinstance(h, OpsAlertHandler) for h in lg.handlers) == 1
    finally:
        lg.removeHandler(first)


def test_celery_worker_process_init_installs_it(monkeypatch):
    from celery.signals import worker_process_init

    import gdx_dispatch.core.celery_app  # noqa: F401 — connects the receiver

    monkeypatch.setenv("OPS_ALERT_EMAIL", "ops@example.test")
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        worker_process_init.send(sender=None)
        added = [h for h in root.handlers if h not in before]
        assert [type(h) for h in added] == [OpsAlertHandler]
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_fastapi_lifespan_installs_it(monkeypatch):
    import gdx_dispatch.app as app_mod
    from gdx_dispatch.core import mcp_mount
    from gdx_dispatch.modules.outlook import bootstrap

    @asynccontextmanager
    async def _no_mcp(_app):
        yield

    monkeypatch.setattr(mcp_mount, "mcp_subapp_lifespan", _no_mcp)
    monkeypatch.setattr(app_mod.observability, "init_otel", lambda **_k: None)
    monkeypatch.setattr(bootstrap, "run_outlook_bootstrap_safely", lambda: {})
    monkeypatch.setenv("OPS_ALERT_EMAIL", "ops@example.test")
    root = logging.getLogger()
    before = list(root.handlers)

    async def _enter():
        async with app_mod.lifespan(FastAPI()):
            return [h for h in root.handlers if h not in before]

    try:
        added = asyncio.run(_enter())
        assert [type(h) for h in added] == [OpsAlertHandler]
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
