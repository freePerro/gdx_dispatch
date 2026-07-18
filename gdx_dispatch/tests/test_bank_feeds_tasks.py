"""Bank feeds Celery tasks — dispatcher advance-before-delay, per-
institution failure isolation, unhealthy/circuit skips. Celery eager mode;
sessions redirected at the module seam (``_tenant_session``)."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.modules.bank_feeds import oauth, service
from gdx_dispatch.modules.bank_feeds import tasks as bf_tasks
from gdx_dispatch.modules.bank_feeds.client import BannoRateLimitError
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_NEEDS_RECONNECT,
    BankFeedSyncSchedule,
    BannoConnection,
    BannoInstitution,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
FI_HOST = "digital.garden-fi.com"


@pytest.fixture(autouse=True)
def _celery_eager():
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


@pytest.fixture(autouse=True)
def _no_redis_breakers(monkeypatch):
    monkeypatch.setattr(bf_tasks, "_breaker_open", lambda inst_id: False)
    monkeypatch.setattr(bf_tasks, "_breaker_record", lambda inst_id, success: None)


@pytest.fixture
def task_db(tenant_db, monkeypatch):
    monkeypatch.setattr(
        bf_tasks, "_tenant_session", lambda tid: contextlib.nullcontext(tenant_db)
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.tenant.single_tenant",
        lambda: {"id": COMPANY, "slug": "test"},
    )
    return tenant_db


def _institution(db, fi_host=FI_HOST):
    inst = BannoInstitution(fi_host=fi_host, display_label=fi_host, client_id="cid",
                            client_secret_enc=oauth._encrypt("s"))
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def test_sync_task_no_institutions(task_db):
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert out == {"skipped_no_institutions": True}


def test_sync_task_skips_unhealthy_connection(task_db):
    inst = _institution(task_db)
    conn = BannoConnection(
        institution_id=inst.id, fi_host=FI_HOST, banno_user_id="s",
        refresh_token_enc=oauth._encrypt("rt"), auth_state=AUTH_NEEDS_RECONNECT,
    )
    task_db.add(conn)
    task_db.commit()

    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    inst_result = out["results"][str(inst.id)]
    assert inst_result["errors"][0]["skipped_unhealthy"] is True
    # A skipped-unhealthy run recorded on the schedule, not silently green.
    schedule = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    assert schedule.last_run_status in ("error", "partial")


def test_rate_limited_institution_does_not_block_healthy_one(task_db, monkeypatch):
    inst_a = _institution(task_db, fi_host="digital.bank-a.example.com")
    inst_b = _institution(task_db, fi_host="digital.bank-b.example.com")

    calls = []

    def fake_sync(db, institution, *, force_fetch):
        calls.append(institution.fi_host)
        if institution.id == inst_a.id:
            raise BannoRateLimitError("429")
        return {"institution_id": str(institution.id), "accounts": {}, "errors": []}

    monkeypatch.setattr(bf_tasks, "_sync_one_institution", fake_sync)
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()

    assert len(calls) == 2  # bank B still ran after bank A rate-limited
    assert out["results"][str(inst_a.id)] == {"rate_limited": True}
    assert out["results"][str(inst_b.id)]["errors"] == []
    assert out["status"] == "partial"


def test_all_circuit_open_records_skipped(task_db, monkeypatch):
    inst = _institution(task_db)
    monkeypatch.setattr(bf_tasks, "_breaker_open", lambda inst_id: True)
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert out["results"][str(inst.id)] == {"skipped_circuit_open": True}
    assert out["status"] == "skipped"


def test_dispatcher_advances_next_run_at_before_delay(task_db, monkeypatch):
    schedule = service.get_or_create_schedule(task_db)
    schedule.frequency = "hourly"
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    task_db.commit()
    old_next = schedule.next_run_at

    observed = {}

    def fake_delay(tid):
        # Capture next_run_at AT DELAY TIME — must already be advanced.
        row = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
        nra = row.next_run_at
        if nra is not None and nra.tzinfo is None:
            nra = nra.replace(tzinfo=timezone.utc)
        observed["next_run_at"] = nra
        observed["tenant"] = tid

    monkeypatch.setattr(bf_tasks.bank_feeds_sync_task, "delay", fake_delay)
    out = bf_tasks.bank_feeds_schedule_dispatcher.apply().get()

    assert out["queued"] == [COMPANY]
    assert observed["next_run_at"] > datetime.now(timezone.utc)
    assert observed["next_run_at"] != old_next


def test_dispatcher_skips_manual_and_not_due(task_db, monkeypatch):
    schedule = service.get_or_create_schedule(task_db)

    called = {"n": 0}
    monkeypatch.setattr(
        bf_tasks.bank_feeds_sync_task, "delay", lambda tid: called.__setitem__("n", called["n"] + 1)
    )

    # manual → skipped
    out = bf_tasks.bank_feeds_schedule_dispatcher.apply().get()
    assert out["queued"] == []
    assert called["n"] == 0

    # scheduled but not due → skipped
    schedule.frequency = "daily"
    schedule.next_run_at = datetime.now(timezone.utc) + timedelta(hours=3)
    task_db.commit()
    out2 = bf_tasks.bank_feeds_schedule_dispatcher.apply().get()
    assert out2["queued"] == []
    assert called["n"] == 0
