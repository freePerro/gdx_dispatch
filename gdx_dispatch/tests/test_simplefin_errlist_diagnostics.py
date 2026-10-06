"""SimpleFIN errlist diagnostics survive the run (GDXA-293).

Class: a failing external call whose diagnostics are discarded. The errlist
CODE was never logged or stored, and the next quota-skipped run recorded
"ok" with no error, erasing the failing run's messages within the hour.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import date, datetime, timezone

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from gdx_dispatch.modules.bank_feeds import oauth, service
from gdx_dispatch.modules.bank_feeds import simplefin_service as ss
from gdx_dispatch.modules.bank_feeds import tasks as bf_tasks
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_HEALTHY,
    PROVIDER_SIMPLEFIN,
    BankFeedSyncSchedule,
    BannoConnection,
    BannoInstitution,
)

BRIDGE_HOST = "bridge.sfin-diag.example"
BASE = f"https://{BRIDGE_HOST}/simplefin"
ACCOUNTS_URL = f"{BASE}/accounts"
COMPANY = "11111111-1111-1111-1111-111111111111"


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp())


def _acct(acct_id: str) -> dict:
    return {"id": acct_id, "name": f"Checking {acct_id}", "currency": "USD",
            "balance": "1.00", "available-balance": "1.00",
            "balance-date": _epoch(date.today()), "transactions": []}


ERRLIST = [{"code": "act.failed", "msg": "Institution timed out", "account_id": "A2"}]


def _failing_payload() -> dict:
    return {"errlist": ERRLIST, "connections": [], "accounts": [_acct("A1"), _acct("A2")]}


@pytest.fixture
def sfin(tenant_db):
    inst = BannoInstitution(provider=PROVIDER_SIMPLEFIN, fi_host=BRIDGE_HOST,
                            display_label="SimpleFIN Bridge")
    tenant_db.add(inst)
    tenant_db.commit()
    conn = BannoConnection(
        institution_id=inst.id, fi_host=BRIDGE_HOST, banno_user_id="simplefin",
        provider_base_url=BASE, access_token_enc=oauth._encrypt("demo:demopass"),
        auth_state=AUTH_HEALTHY,
    )
    tenant_db.add(conn)
    tenant_db.commit()
    tenant_db.refresh(inst)
    return inst


@pytest.fixture
def task_db(tenant_db, monkeypatch):
    monkeypatch.setattr(bf_tasks, "_tenant_session", lambda tid: contextlib.nullcontext(tenant_db))
    monkeypatch.setattr(bf_tasks, "_breaker_open", lambda inst_id: False)
    monkeypatch.setattr(bf_tasks, "_breaker_record", lambda inst_id, success: None)
    return tenant_db


def _spend_quota(db) -> None:
    sched = service.get_or_create_schedule(db)
    sched.fetch_count_date = datetime.now(timezone.utc).astimezone(service.tenant_zoneinfo(db)).date()
    sched.fetch_count_today = ss.effective_cap(sched)
    db.commit()


@respx.mock
def test_errlist_code_is_logged_and_returned(tenant_db, sfin, caplog):
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_failing_payload()))
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        result = ss.sync_institution(tenant_db, sfin)
    assert result["errlist_codes"] == ["act.failed"]
    lines = [r.getMessage() for r in caplog.records if r.name == ss.__name__]
    assert any("code=act.failed" in m and "account=A2" in m and "Institution timed out" in m
               for m in lines), lines


@respx.mock
def test_quota_skip_keeps_the_failing_runs_error(task_db, sfin):
    route = respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_failing_payload()))
    first = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert first["status"] == "error"
    sfin_result = first["results"][str(sfin.id)]
    assert sfin_result["messages"] == ["Institution timed out"]  # one per entry, not per window
    sched = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    failing_error = sched.last_run_error
    assert failing_error and "act.failed" in failing_error and "Institution timed out" in failing_error
    assert "'account': 'A2'" in failing_error  # which account is stuck survives the cap

    fetched = route.call_count  # backfill walks several windows
    _spend_quota(task_db)
    second = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert route.call_count == fetched  # the skipped run fetched nothing
    assert second["status"] == "skipped"
    task_db.refresh(sched)
    assert sched.last_run_status == "skipped"
    assert sched.last_run_error == failing_error


@respx.mock
def test_ok_sibling_does_not_clear_a_quota_skipped_institutions_error(task_db, sfin, monkeypatch):
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_failing_payload()))
    bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    sched = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    failing_error = sched.last_run_error
    assert failing_error and "act.failed" in failing_error

    task_db.add(BannoInstitution(fi_host="digital.ok-bank.example", display_label="Ok",
                                 client_id="cid", client_secret_enc=oauth._encrypt("s")))
    task_db.commit()
    real_sync = bf_tasks._sync_one_institution

    def sync(db, institution, *, force_fetch):
        if institution.provider == PROVIDER_SIMPLEFIN:
            return real_sync(db, institution, force_fetch=force_fetch)
        return {"institution_id": str(institution.id), "accounts": {}, "errors": []}

    monkeypatch.setattr(bf_tasks, "_sync_one_institution", sync)
    _spend_quota(task_db)
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert out["status"] == "ok"
    task_db.refresh(sched)
    assert sched.last_run_error == failing_error


@respx.mock
def test_erroring_sibling_does_not_overwrite_a_quota_skipped_institutions_error(
    task_db, sfin, monkeypatch,
):
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_failing_payload()))
    bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    sched = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    assert "act.failed" in sched.last_run_error

    task_db.add(BannoInstitution(fi_host="digital.bad-bank.example", display_label="Bad",
                                 client_id="cid", client_secret_enc=oauth._encrypt("s")))
    task_db.commit()
    real_sync = bf_tasks._sync_one_institution

    def sync(db, institution, *, force_fetch):
        if institution.provider == PROVIDER_SIMPLEFIN:
            return real_sync(db, institution, force_fetch=force_fetch)
        return {"institution_id": str(institution.id), "accounts": {},
                "errors": [{"connection": "c", "skipped_unhealthy": True}]}

    monkeypatch.setattr(bf_tasks, "_sync_one_institution", sync)
    _spend_quota(task_db)
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert out["status"] == "error"
    task_db.refresh(sched)
    assert "act.failed" in sched.last_run_error
    assert "skipped_unhealthy" in sched.last_run_error


@respx.mock
def test_unhealthy_skip_keeps_the_auth_failures_code_and_message(task_db, sfin):
    route = respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json={
        "errlist": [{"code": "gen.auth", "msg": "Token revoked by bank"}],
        "connections": [], "accounts": []}))
    bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    sched = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    assert "gen.auth" in sched.last_run_error

    fetched = route.call_count
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert route.call_count == fetched  # the connection is skipped, not fetched
    assert out["status"] == "error"
    task_db.refresh(sched)
    assert "gen.auth" in sched.last_run_error
    assert "Token revoked by bank" in sched.last_run_error
    assert "skipped_unhealthy" in sched.last_run_error  # and the current state


@respx.mock
def test_filtered_sync_of_a_sibling_keeps_the_unsynced_institutions_error(
    task_db, sfin, monkeypatch,
):
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_failing_payload()))
    bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    sched = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    failing_error = sched.last_run_error
    assert "act.failed" in failing_error

    other = BannoInstitution(fi_host="digital.ok-bank.example", display_label="Ok",
                             client_id="cid", client_secret_enc=oauth._encrypt("s"))
    task_db.add(other)
    task_db.commit()
    monkeypatch.setattr(bf_tasks, "_sync_one_institution", lambda db, institution, *, force_fetch: {
        "institution_id": str(institution.id), "accounts": {}, "errors": []})
    # A manual / connect-callback sync of the sibling alone never ran SimpleFIN.
    out = bf_tasks.bank_feeds_sync_task.apply(
        args=(COMPANY,), kwargs={"institution_id": other.id}).get()
    assert out["status"] == "ok" and list(out["results"]) == [str(other.id)]
    task_db.refresh(sched)
    assert sched.last_run_error == failing_error


@respx.mock
def test_recovery_clears_the_error_beside_a_permanently_skipped_sibling(task_db, sfin):
    task_db.add(BannoInstitution(fi_host="digital.idle-bank.example", display_label="Idle",
                                 client_id="cid", client_secret_enc=oauth._encrypt("s")))
    task_db.commit()
    route = respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_failing_payload()))
    bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    sched = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    assert sched.last_run_error

    route.mock(return_value=Response(200, json={"errlist": [], "connections": [],
                                                "accounts": [_acct("A1"), _acct("A2")]}))
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert out["status"] == "ok"
    task_db.refresh(sched)
    assert sched.last_run_error is None


def test_run_error_summary_codes_survive_wordy_siblings():
    results = {
        "a": {"errors": [{"connection": "x" * 300, "skipped_unhealthy": True}]},
        "b": {"errors": [{"connection": "y" * 300, "skipped_unhealthy": True}]},
        "sfin": {"errors": [{"account": "A2", "incomplete": True}], "errlist_codes": ["act.failed"]},
    }
    assert "act.failed" in bf_tasks._run_error_summary(results)


def test_run_error_summary_keeps_messages_on_a_green_run_and_leads_with_codes():
    results = {
        "i1": {"errors": [], "messages": ["Bank says hi"], "errlist_codes": ["x.notice"],
               "stats": {"upserted": 10**200}},
        "i2": {"errors": [], "stats": {"upserted": 1}},
        "i3": {"skipped_quota": True, "errors": []},
    }
    summary = bf_tasks._run_error_summary(results)
    assert summary is not None and "i2" not in summary and "i3" not in summary
    assert summary.index("x.notice") < summary.index("Bank says hi")
    assert "upserted" not in summary  # stats never crowd out the diagnosis
    assert bf_tasks._run_error_summary({"i2": results["i2"]}) is None


def test_record_scheduled_run_stores_more_than_the_old_500_cap(tenant_db):
    service.record_scheduled_run(tenant_db, "error", "x" * (service.LAST_RUN_ERROR_MAX + 1000))
    assert service.LAST_RUN_ERROR_MAX > 500
    s = service.get_or_create_schedule(tenant_db)
    assert len(s.last_run_error) == service.LAST_RUN_ERROR_MAX
    service.record_scheduled_run(tenant_db, "ok", None)
    assert s.last_run_error is None


def test_carry_over_keeps_only_the_unrun_institutions_entry():
    # S failed earlier and is quota-skipped now; B's OLD error must not ride
    # along, and B's NEW error must not displace S's — wherever S sat in prior.
    prior = str({"B": {"error_class": "Old"}, "S": {"errlist_codes": ["act.failed"]}})
    out = bf_tasks._run_error_summary({"B": {"error_class": "New"}}, prior, ["S"])
    assert "act.failed" in out and "New" in out and "Old" not in out
    assert out.index("act.failed") < out.index("New")  # codes lead
    # B recovers: only S's entry remains.
    out = bf_tasks._run_error_summary({"B": {"errors": []}}, out, ["S"])
    assert out == str({"S": {"errlist_codes": ["act.failed"]}})
    # S runs clean: nothing left.
    assert bf_tasks._run_error_summary({"S": {"errors": []}, "B": {"errors": []}}, out, []) is None


def test_carry_over_does_not_compound_over_repeated_runs():
    out = str({"S": {"errlist_codes": ["act.failed"]}})
    for err in ["E1"] * 6 + ["E2"]:
        out = bf_tasks._run_error_summary({"B": {"error_class": err}}, out, ["S"])
    assert out.count("act.failed") == 1 and "E2" in out and "E1" not in out


def test_unparseable_prior_is_kept_only_when_the_run_has_nothing_new():
    legacy = "{'S': {'errors': [{'account': 'A2', 'incomplete': True}]}, 'B': {'err"  # cut at 500
    assert bf_tasks._run_error_summary({}, legacy, ["S"]) == legacy
    assert bf_tasks._run_error_summary({"B": {"error_class": "New"}}, legacy, ["S"]) == str(
        {"B": {"error_class": "New"}})
    assert bf_tasks._run_error_summary({}, legacy, ["Z"]) is None


def test_a_maxed_single_institution_summary_fits_and_parses_back():
    import ast
    worst = {"S": {"errlist_codes": ["act.failed", "gen.x"], "messages": [str(i) * 300 for i in range(5)],
                   "errors": [{"account": "a" * 36, "incomplete": True}] * 10, "error_class": "SimpleFINError"}}
    out = bf_tasks._run_error_summary(worst)
    assert len(out) * 2 < service.LAST_RUN_ERROR_MAX  # two maxed institutions still fit
    assert ast.literal_eval(out)["S"]["errlist_codes"] == ["act.failed", "gen.x"]


@respx.mock
def test_rate_limited_sibling_beside_a_quota_skip_records_partial(task_db, sfin, monkeypatch):
    from gdx_dispatch.modules.bank_feeds.client import BannoRateLimitError

    task_db.add(BannoInstitution(fi_host="digital.busy-bank.example", display_label="Busy",
                                 client_id="cid", client_secret_enc=oauth._encrypt("s")))
    task_db.commit()

    def sync(db, institution, *, force_fetch):
        if institution.provider == PROVIDER_SIMPLEFIN:
            return {"skipped_quota": True, "errors": []}
        raise BannoRateLimitError("slow down")

    monkeypatch.setattr(bf_tasks, "_sync_one_institution", sync)
    out = bf_tasks.bank_feeds_sync_task.apply(args=(COMPANY,)).get()
    assert out["status"] == "partial"
    sched = task_db.execute(select(BankFeedSyncSchedule)).scalar_one()
    assert sched.last_run_status == "partial"
