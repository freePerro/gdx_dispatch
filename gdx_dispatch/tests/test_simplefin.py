"""SimpleFIN provider — claim/credential discipline, posted-only ingest,
window/watermark semantics, fetch ledger + hours window, provider dispatch,
router endpoints (direct-call house style), feed↔statement tie-out."""
from __future__ import annotations

import base64
import contextlib
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time

import pytest
import respx
from fastapi import HTTPException
from httpx import Response
from sqlalchemy import select
from starlette.requests import Request as StarletteRequest

from gdx_dispatch.modules.bank_feeds import (
    oauth,
    service,
)
from gdx_dispatch.modules.bank_feeds import (
    simplefin_client as sc,
)
from gdx_dispatch.modules.bank_feeds import (
    simplefin_router as sr,
)
from gdx_dispatch.modules.bank_feeds import (
    simplefin_service as ss,
)
from gdx_dispatch.modules.bank_feeds import tasks as bf_tasks
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    AUTH_HEALTHY,
    AUTH_NEEDS_RECONNECT,
    DAILY_FETCH_CAP_MAX,
    PROVIDER_SIMPLEFIN,
    BankFeedAccount,
    BankFeedBalanceSnapshot,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.modules.bank_feeds.statement_models import (
    BankAccount,
    BankStatementImport,
    BankStatementLine,
)

BRIDGE_HOST = "bridge.sfin-test.example"
BASE = f"https://{BRIDGE_HOST}/simplefin"
ACCOUNTS_URL = f"{BASE}/accounts"
CLAIM_URL = f"{BASE}/claim/TOK123"
SETUP_TOKEN = base64.b64encode(CLAIM_URL.encode()).decode()
ACCESS_URL = f"https://demo:demopass@{BRIDGE_HOST}/simplefin"

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"sub": "tester", "tenant_id": COMPANY, "role": "admin"}


def _request():
    scope = {
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 80), "state": {},
    }
    req = StarletteRequest(scope)
    req.state.tenant = {"id": COMPANY}
    return req


def _epoch(d: date, hour: int = 12) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp())


def _today_local(db) -> date:
    """The module's notion of "today": tenant timezone (America/New_York
    when AppSettings is empty), exactly as quota_state/record_fetches and
    the watermark stamps compute it. ``date.today()`` is the CONTAINER's
    day — from 00:00 UTC until Eastern midnight the two differ, and every
    date assertion here went off by one (CI shard 7, 2026-08-14 02:08 UTC:
    six failures, all vs the module's still-yesterday local day)."""
    return datetime.now(timezone.utc).astimezone(service.tenant_zoneinfo(db)).date()


def _txn(i, posted_epoch, amount="-10.00", description="Fishing bait", **extra):
    return {"id": f"sftxn-{i}", "posted": posted_epoch, "amount": amount,
            "description": description, **extra}


def _acct(acct_id="A1", name="Business Checking 2204", balance="100.00",
          txns=None, balance_date=None):
    return {
        "id": acct_id, "name": name, "currency": "USD",
        "balance": balance, "available-balance": balance,
        "balance-date": balance_date or _epoch(date.today()),
        "transactions": txns or [],
    }


def _payload(accounts, errlist=None):
    return {"errlist": errlist or [], "connections": [], "accounts": accounts}


@pytest.fixture
def sfin(tenant_db):
    inst = BannoInstitution(
        provider=PROVIDER_SIMPLEFIN, fi_host=BRIDGE_HOST, display_label="SimpleFIN Bridge"
    )
    tenant_db.add(inst)
    tenant_db.commit()
    conn = BannoConnection(
        institution_id=inst.id, fi_host=BRIDGE_HOST, banno_user_id=sr.SIMPLEFIN_USER_ID,
        provider_base_url=BASE, access_token_enc=oauth._encrypt("demo:demopass"),
        auth_state=AUTH_HEALTHY,
    )
    tenant_db.add(conn)
    tenant_db.commit()
    tenant_db.refresh(inst)
    tenant_db.refresh(conn)
    return inst, conn


# ══ client: token decode / claim / credential discipline ═══════════════


def test_decode_setup_token_rejects_garbage():
    with pytest.raises(ValueError):
        sc.decode_setup_token("!!!not-base64!!!")


def test_decode_setup_token_rejects_http():
    tok = base64.b64encode(b"http://bridge.sfin-test.example/claim/x").decode()
    with pytest.raises(ValueError, match="https"):
        sc.decode_setup_token(tok)


def test_decode_setup_token_rejects_embedded_creds():
    tok = base64.b64encode(f"https://u:p@{BRIDGE_HOST}/claim/x".encode()).decode()
    with pytest.raises(ValueError, match="credentials"):
        sc.decode_setup_token(tok)


def test_split_access_url_strips_credentials():
    base, user, password = sc.split_access_url(ACCESS_URL)
    assert base == BASE
    assert (user, password) == ("demo", "demopass")
    assert "demo" not in base and "demopass" not in base


def test_split_access_url_requires_creds_and_https():
    with pytest.raises(ValueError):
        sc.split_access_url(f"https://{BRIDGE_HOST}/simplefin")
    with pytest.raises(ValueError):
        sc.split_access_url(f"http://u:p@{BRIDGE_HOST}/simplefin")


@respx.mock
def test_claim_success_and_one_time_403():
    respx.post(CLAIM_URL).mock(return_value=Response(200, text=ACCESS_URL))
    base, user, password = sc.claim_access_url(SETUP_TOKEN)
    assert (base, user, password) == (BASE, "demo", "demopass")

    respx.post(CLAIM_URL).mock(return_value=Response(403, text="denied"))
    with pytest.raises(sc.SimpleFINClaimError):
        sc.claim_access_url(SETUP_TOKEN)


@respx.mock
def test_client_url_never_carries_userinfo():
    route = respx.get(ACCOUNTS_URL).mock(
        return_value=Response(200, json=_payload([_acct()]))
    )
    with sc.SimpleFINClient(BASE, "demo", "demopass") as client:
        client.get_accounts()
    sent = route.calls.last.request
    assert "demopass" not in str(sent.url) and "demo:" not in str(sent.url)
    assert sent.headers.get("authorization", "").startswith("Basic ")
    assert client.requests_made == 1


@respx.mock
def test_client_refuses_redirects():
    respx.get(ACCOUNTS_URL).mock(return_value=Response(302, headers={"location": "https://evil.example/"}))
    with sc.SimpleFINClient(BASE, "demo", "demopass") as client, pytest.raises(sc.SimpleFINError, match="redirect"):
        client.get_accounts()


@respx.mock
def test_client_403_and_402_are_auth_errors():
    respx.get(ACCOUNTS_URL).mock(return_value=Response(403))
    with sc.SimpleFINClient(BASE, "demo", "demopass") as client, pytest.raises(sc.SimpleFINAuthError):
        client.get_accounts()
    respx.get(ACCOUNTS_URL).mock(return_value=Response(402))
    with sc.SimpleFINClient(BASE, "demo", "demopass") as client, pytest.raises(sc.SimpleFINAuthError):
        client.get_accounts()


@respx.mock
def test_client_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    route = respx.get(ACCOUNTS_URL)
    route.side_effect = [Response(502), Response(200, json=_payload([_acct()]))]
    with sc.SimpleFINClient(BASE, "demo", "demopass") as client:
        data = client.get_accounts()
    assert data["accounts"]


def test_client_rejects_credentialed_base_url():
    with pytest.raises(ValueError):
        sc.SimpleFINClient(ACCESS_URL.rsplit("/simplefin", 1)[0] + "/simplefin", "u", "p")


# ══ fetch-hours window (tenant-local) ══════════════════════════════════


class _Sched:
    def __init__(self, start=None, end=None):
        self.fetch_window_start = start
        self.fetch_window_end = end


@pytest.mark.parametrize("start,end,now,allowed", [
    (None, None, dt_time(3, 0), True),            # no window
    ("08:00", "18:00", dt_time(9, 30), True),     # inside
    ("08:00", "18:00", dt_time(7, 59), False),    # before
    ("08:00", "18:00", dt_time(18, 0), False),    # end exclusive
    ("08:00", "08:00", dt_time(3, 0), True),      # degenerate = no window
    ("22:00", "06:00", dt_time(23, 30), True),    # midnight wrap, late
    ("22:00", "06:00", dt_time(5, 59), True),     # midnight wrap, early
    ("22:00", "06:00", dt_time(12, 0), False),    # wrap, midday
    ("bogus", "18:00", dt_time(12, 0), True),     # invalid = no window
])
def test_within_fetch_window(start, end, now, allowed):
    assert ss.within_fetch_window(_Sched(start, end), now) is allowed


# ══ fetch ledger ═══════════════════════════════════════════════════════


def test_ledger_rolls_on_new_local_day(tenant_db):
    sched = service.get_or_create_schedule(tenant_db)
    tz = service.tenant_zoneinfo(tenant_db)
    sched.fetch_count_date = _today_local(tenant_db) - timedelta(days=1)
    sched.fetch_count_today = 17
    tenant_db.commit()
    quota = ss.quota_state(tenant_db, sched, tz)
    assert quota["used"] == 0 and quota["remaining"] == quota["cap"]
    ss.record_fetches(tenant_db, sched, 3, tz)
    assert ss.quota_state(tenant_db, sched, tz)["used"] == 3


def test_effective_cap_clamps_to_hard_max(tenant_db):
    sched = service.get_or_create_schedule(tenant_db)
    sched.daily_fetch_cap = 999
    assert ss.effective_cap(sched) == DAILY_FETCH_CAP_MAX


# ══ sync: backfill, posted-only, snapshots, watermarks ═════════════════


def _run(db, inst):
    return ss.sync_institution(db, inst)


@respx.mock
def test_first_sync_backfills_creates_accounts_txns_snapshots(tenant_db, sfin):
    inst, conn = sfin
    sched = service.get_or_create_schedule(tenant_db)
    sched.backfill_days = 100  # two 89-day windows
    tenant_db.commit()
    today = _today_local(tenant_db)
    txns = [
        _txn(1, _epoch(today - timedelta(days=3)), "-65.50"),
        _txn(2, _epoch(today - timedelta(days=2)), "1200.00", "Customer payment"),
        _txn(3, 0, "-1.00", "still pending"),                      # posted=0 → dropped
        _txn(4, _epoch(today - timedelta(days=1)), "-9.99", "p", pending=True),  # dropped
    ]
    route = respx.get(ACCOUNTS_URL).mock(
        return_value=Response(200, json=_payload([_acct(txns=txns)]))
    )
    result = _run(tenant_db, inst)

    assert result["mode"] == "backfill"
    assert route.call_count == 2
    assert result["fetches"] == 2
    # Every window's epoch span must stay strictly under the bridge's
    # 90-day range limit, whichever way the bridge reads "90 days".
    for call in route.calls:
        params = dict(call.request.url.params)
        span = int(params["end-date"]) - int(params["start-date"])
        assert span < 90 * 86400
    acct = tenant_db.execute(select(BankFeedAccount)).scalars().one()
    assert acct.provider == PROVIDER_SIMPLEFIN
    assert acct.initial_backfill_done is True
    assert acct.backfill_synced_through == today
    assert acct.account_number_masked == "•2204"
    rows = tenant_db.execute(select(BankFeedTransaction)).scalars().all()
    assert len(rows) == 2  # posted-only
    by_ext = {r.external_transaction_id: r for r in rows}
    assert by_ext["sftxn-1"].amount_cents == -6550
    assert by_ext["sftxn-2"].amount_cents == 120000
    assert all(r.line_hash for r in rows)
    assert all(r.pending is False for r in rows)
    snaps = tenant_db.execute(select(BankFeedBalanceSnapshot)).scalars().all()
    assert len(snaps) == 1 and snaps[0].balance_cents == 10000
    sched = service.get_or_create_schedule(tenant_db)
    assert sched.fetch_count_today == 2


@respx.mock
def test_second_sync_is_incremental_and_dedupes(tenant_db, sfin):
    inst, conn = sfin
    today = _today_local(tenant_db)
    txns = [_txn(1, _epoch(today - timedelta(days=2)))]
    route = respx.get(ACCOUNTS_URL).mock(
        return_value=Response(200, json=_payload([_acct(txns=txns)]))
    )
    _run(tenant_db, inst)
    first_count = len(tenant_db.execute(select(BankFeedTransaction)).scalars().all())

    result = _run(tenant_db, inst)
    assert result["mode"] == "incremental"
    assert len(tenant_db.execute(select(BankFeedTransaction)).scalars().all()) == first_count
    # Incremental start overlaps the watermark by FETCH_OVERLAP_DAYS.
    params = dict(route.calls.last.request.url.params)
    expected_start = ss._epoch_of(today - timedelta(days=ss.FETCH_OVERLAP_DAYS))
    assert int(params["start-date"]) == expected_start


@respx.mock
def test_act_failed_freezes_that_accounts_watermark(tenant_db, sfin):
    inst, conn = sfin
    today = _today_local(tenant_db)
    payload = _payload(
        [_acct("A1", "Checking 1111", txns=[_txn(1, _epoch(today))]),
         _acct("A2", "Savings 2222", txns=[])],
        errlist=[{"code": "act.failed", "msg": "try later", "account_id": "A2"}],
    )
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=payload))
    result = _run(tenant_db, inst)
    rows = {a.external_account_id: a for a in tenant_db.execute(select(BankFeedAccount)).scalars()}
    assert rows["A1"].backfill_synced_through == today
    assert rows["A2"].backfill_synced_through is None
    assert rows["A2"].initial_backfill_done is False
    assert any(e.get("account") == "A2" for e in result["errors"])
    assert result["messages"]


@respx.mock
def test_con_auth_marks_needs_reconnect(tenant_db, sfin):
    inst, conn = sfin
    payload = _payload([], errlist=[{"code": "con.auth", "msg": "Re-auth at the bridge",
                                     "conn_id": "C1"}])
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=payload))
    result = _run(tenant_db, inst)
    tenant_db.refresh(conn)
    assert conn.auth_state == AUTH_NEEDS_RECONNECT
    assert any(e.get("auth") == "needs_reconnect" for e in result["errors"])


@respx.mock
def test_http_403_marks_needs_reconnect(tenant_db, sfin):
    inst, conn = sfin
    respx.get(ACCOUNTS_URL).mock(return_value=Response(403))
    result = _run(tenant_db, inst)
    tenant_db.refresh(conn)
    assert conn.auth_state == AUTH_NEEDS_RECONNECT
    assert result["fetches"] >= 1  # the failed fetch still burned quota


@respx.mock
def test_quota_exhausted_skips_without_fetching(tenant_db, sfin):
    inst, conn = sfin
    sched = service.get_or_create_schedule(tenant_db)
    tz = service.tenant_zoneinfo(tenant_db)
    sched.fetch_count_date = datetime.now(timezone.utc).astimezone(tz).date()
    sched.fetch_count_today = ss.effective_cap(sched)
    tenant_db.commit()
    route = respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_payload([])))
    result = _run(tenant_db, inst)
    assert result.get("skipped_quota") is True
    assert route.call_count == 0


@respx.mock
def test_quota_midrun_stop_resumes_next_day(tenant_db, sfin):
    inst, conn = sfin
    sched = service.get_or_create_schedule(tenant_db)
    sched.backfill_days = 200   # needs 3 windows
    sched.daily_fetch_cap = 2
    tenant_db.commit()
    today = _today_local(tenant_db)
    route = respx.get(ACCOUNTS_URL).mock(
        return_value=Response(200, json=_payload([_acct(txns=[])]))
    )
    result = _run(tenant_db, inst)
    assert result.get("stopped_quota") is True
    assert route.call_count == 2
    acct = tenant_db.execute(select(BankFeedAccount)).scalars().one()
    assert acct.initial_backfill_done is False
    resumed_from = acct.backfill_synced_through
    assert resumed_from is not None and resumed_from < today

    # Next local day: ledger rolls, backfill resumes from the watermark.
    sched = service.get_or_create_schedule(tenant_db)
    sched.fetch_count_date = today - timedelta(days=1)
    tenant_db.commit()
    result2 = _run(tenant_db, inst)
    assert result2["mode"] == "backfill"
    tenant_db.refresh(acct)
    assert acct.initial_backfill_done is True
    assert acct.backfill_synced_through == today
    # Resume asked the bridge for a window starting at the committed
    # watermark (minus the 1-day overlap), not the whole horizon again.
    first_resume_call = route.calls[2].request
    start = int(dict(first_resume_call.url.params)["start-date"])
    assert start == ss._epoch_of(resumed_from - timedelta(days=1))


@respx.mock
def test_balance_snapshot_upserts_one_row_per_day(tenant_db, sfin):
    inst, conn = sfin
    respx.get(ACCOUNTS_URL).mock(
        return_value=Response(200, json=_payload([_acct(balance="100.00")]))
    )
    _run(tenant_db, inst)
    respx.get(ACCOUNTS_URL).mock(
        return_value=Response(200, json=_payload([_acct(balance="250.50")]))
    )
    _run(tenant_db, inst)
    snaps = tenant_db.execute(select(BankFeedBalanceSnapshot)).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].balance_cents == 25050  # last observation of the day wins


@respx.mock
def test_disconnected_connection_is_skipped(tenant_db, sfin):
    inst, conn = sfin
    conn.auth_state = AUTH_DISCONNECTED
    tenant_db.commit()
    route = respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_payload([])))
    result = _run(tenant_db, inst)
    assert result.get("skipped_no_connection") is True
    assert route.call_count == 0


@respx.mock
def test_watermark_only_advances_for_payload_members(tenant_db, sfin):
    """An account MX omitted from this response must NOT be stamped synced."""
    inst, conn = sfin
    today = _today_local(tenant_db)
    old_mark = today - timedelta(days=10)
    for ext in ("A1", "A2"):
        tenant_db.add(BankFeedAccount(
            connection_id=conn.id, external_account_id=ext, provider=PROVIDER_SIMPLEFIN,
            name=ext, initial_backfill_done=True, backfill_synced_through=old_mark,
        ))
    tenant_db.commit()
    respx.get(ACCOUNTS_URL).mock(
        return_value=Response(200, json=_payload([_acct("A1", txns=[])]))  # A2 missing
    )
    _run(tenant_db, inst)
    rows = {a.external_account_id: a for a in tenant_db.execute(select(BankFeedAccount)).scalars()}
    assert rows["A1"].backfill_synced_through == today
    assert rows["A2"].backfill_synced_through == old_mark  # frozen, not lied about


@respx.mock
def test_new_account_in_incremental_gets_full_backfill_next_run(tenant_db, sfin):
    """A brand-new bank account appearing mid-incremental must not be marked
    done off a few days of overlap — the next run backfills its history."""
    inst, conn = sfin
    today = _today_local(tenant_db)
    tenant_db.add(BankFeedAccount(
        connection_id=conn.id, external_account_id="A1", provider=PROVIDER_SIMPLEFIN,
        name="A1", initial_backfill_done=True, backfill_synced_through=today - timedelta(days=2),
    ))
    tenant_db.commit()
    sched = service.get_or_create_schedule(tenant_db)
    sched.backfill_days = 60
    tenant_db.commit()
    payload = _payload([
        _acct("A1", txns=[]),
        _acct("A2", "Brand New Savings", txns=[_txn(9, _epoch(today - timedelta(days=1)))]),
    ])
    route = respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=payload))
    result = _run(tenant_db, inst)
    assert result["mode"] == "incremental"
    rows = {a.external_account_id: a for a in tenant_db.execute(select(BankFeedAccount)).scalars()}
    assert rows["A2"].initial_backfill_done is False
    assert rows["A2"].backfill_synced_through is None

    result2 = _run(tenant_db, inst)
    assert result2["mode"] == "backfill"
    tenant_db.refresh(rows["A2"])
    assert rows["A2"].initial_backfill_done is True
    # The backfill actually asked for the full horizon, not the overlap.
    start = int(dict(route.calls.last.request.url.params)["start-date"])
    assert start <= ss._epoch_of(today - timedelta(days=59))


@respx.mock
def test_disabled_account_ingests_no_txns_or_snapshots(tenant_db, sfin):
    """One GET returns every account regardless — the sync_enabled toggle
    must stop ingestion for the disabled one while its sibling syncs."""
    inst, conn = sfin
    tenant_db.add(BankFeedAccount(
        connection_id=conn.id, external_account_id="A1", provider=PROVIDER_SIMPLEFIN,
        name="A1", sync_enabled=True,
    ))
    tenant_db.add(BankFeedAccount(
        connection_id=conn.id, external_account_id="A2", provider=PROVIDER_SIMPLEFIN,
        name="A2", sync_enabled=False,
    ))
    tenant_db.commit()
    d = date.today() - timedelta(days=1)
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_payload([
        _acct("A1", txns=[_txn(1, _epoch(d))]),
        _acct("A2", txns=[_txn(2, _epoch(d))]),
    ])))
    _run(tenant_db, inst)
    txns = tenant_db.execute(select(BankFeedTransaction)).scalars().all()
    accounts = {a.id: a.external_account_id for a in tenant_db.execute(select(BankFeedAccount)).scalars()}
    assert [accounts[t.account_id] for t in txns] == ["A1"]
    snaps = tenant_db.execute(select(BankFeedBalanceSnapshot)).scalars().all()
    assert [accounts[s.account_id] for s in snaps] == ["A1"]


@respx.mock
def test_all_accounts_disabled_skips_without_fetching(tenant_db, sfin):
    """With nothing enabled, no watermark could ever advance — fetching
    would re-walk the horizon EVERY run, burning ~5 of 20 daily fetches
    on data nothing may ingest."""
    inst, conn = sfin
    tenant_db.add(BankFeedAccount(
        connection_id=conn.id, external_account_id="A1", provider=PROVIDER_SIMPLEFIN,
        name="A1", sync_enabled=False,
    ))
    tenant_db.commit()
    route = respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_payload([])))
    result = _run(tenant_db, inst)
    assert result.get("skipped_all_disabled") is True
    assert route.call_count == 0
    assert service.get_or_create_schedule(tenant_db).fetch_count_today == 0


@respx.mock
def test_failed_run_records_true_request_count(tenant_db, sfin, monkeypatch):
    """Retries inside a failed call are real requests against the bridge's
    quota — the ledger must count all of them, not a flat 1."""
    import httpx as _httpx

    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    inst, conn = sfin
    respx.get(ACCOUNTS_URL).mock(side_effect=_httpx.ConnectError("down"))
    result = _run(tenant_db, inst)
    assert any(e.get("error_class") == "SimpleFINError" for e in result["errors"])
    assert result["fetches"] == sc.RETRY_MAX_ATTEMPTS + 1  # every attempt counted
    sched = service.get_or_create_schedule(tenant_db)
    assert sched.fetch_count_today == sc.RETRY_MAX_ATTEMPTS + 1


def test_simplefin_routes_included_exactly_once():
    """Regression: the sub-router was once include_router'd TWICE (a botched
    shell append). This FastAPI defers inclusion via _IncludedRouter markers,
    so count the markers pointing at sfin_router."""
    from gdx_dispatch.modules.bank_feeds.router import router as bf_router

    markers = [
        r for r in bf_router.routes
        if getattr(r, "original_router", None) is sr.sfin_router
    ]
    assert len(markers) == 1


# ══ task dispatch + dispatcher window gate ═════════════════════════════


def test_sync_one_institution_dispatches_by_provider(tenant_db, sfin, monkeypatch):
    inst, conn = sfin
    sentinel = {"institution_id": str(inst.id), "provider": "simplefin", "errors": []}
    monkeypatch.setattr(
        "gdx_dispatch.modules.bank_feeds.simplefin_service.sync_institution",
        lambda db, institution: sentinel,
    )
    out = bf_tasks._sync_one_institution(tenant_db, inst, force_fetch=False)
    assert out is sentinel


def test_dispatcher_respects_fetch_window(tenant_db, monkeypatch):
    sched = service.update_schedule(tenant_db, "hourly")
    sched.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    tz = service.tenant_zoneinfo(tenant_db)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    # A 2-minute window that ended an hour ago — now is outside it.
    closed_start = (now_local - timedelta(hours=2)).time().strftime("%H:%M")
    closed_end = (now_local - timedelta(hours=1)).time().strftime("%H:%M")
    sched.fetch_window_start = closed_start
    sched.fetch_window_end = closed_end
    tenant_db.commit()
    frozen_next = sched.next_run_at

    monkeypatch.setattr(
        bf_tasks, "_tenant_session", lambda tid: contextlib.nullcontext(tenant_db)
    )
    monkeypatch.setattr(
        "gdx_dispatch.core.tenant.single_tenant", lambda: {"id": COMPANY}
    )
    queued = []
    monkeypatch.setattr(
        bf_tasks.bank_feeds_sync_task, "delay", lambda *a, **k: queued.append(a)
    )

    out = bf_tasks.bank_feeds_schedule_dispatcher()
    assert queued == [] and out["skipped"] == 1
    tenant_db.refresh(sched)
    # next_run_at was NOT advanced — the first tick inside the window fires.
    assert sched.next_run_at == frozen_next

    # Open the window → the overdue run dispatches.
    sched.fetch_window_start = None
    sched.fetch_window_end = None
    tenant_db.commit()
    bf_tasks.bank_feeds_schedule_dispatcher()
    assert len(queued) == 1


# ══ router endpoints (direct-call) ═════════════════════════════════════


@respx.mock
def test_connect_persists_claim_before_preview(tenant_db):
    respx.post(CLAIM_URL).mock(return_value=Response(200, text=ACCESS_URL))
    # Preview fetch hard-fails — the claim must survive anyway.
    respx.get(ACCOUNTS_URL).mock(return_value=Response(403))
    out = sr.simplefin_connect(
        sr.ConnectIn(setup_token=SETUP_TOKEN), _request(),
        current_user=USER, _perm=None, db=tenant_db,
    )
    assert out["connected"] is True
    assert out["preview"] is None and out["warning"]
    conn = tenant_db.execute(select(BannoConnection)).scalars().one()
    assert conn.provider_base_url == BASE
    assert oauth._decrypt(conn.access_token_enc) == "demo:demopass"
    assert conn.auth_state == AUTH_HEALTHY
    inst = tenant_db.execute(select(BannoInstitution)).scalars().one()
    assert inst.provider == PROVIDER_SIMPLEFIN
    # The failed preview still made a real bridge request — ledgered.
    assert service.get_or_create_schedule(tenant_db).fetch_count_today == 1


@respx.mock
def test_connect_preview_proposes_relink(tenant_db, sfin):
    inst, conn = sfin
    orphan = BankFeedAccount(
        connection_id=conn.id, external_account_id="OLD-1",
        provider=PROVIDER_SIMPLEFIN, name="Business Checking 2204",
    )
    tenant_db.add(orphan)
    tenant_db.commit()
    respx.post(CLAIM_URL).mock(return_value=Response(200, text=ACCESS_URL))
    respx.get(ACCOUNTS_URL).mock(return_value=Response(200, json=_payload(
        [_acct("NEW-9", "Business Checking 2204", balance="10.00")]
    )))
    out = sr.simplefin_connect(
        sr.ConnectIn(setup_token=SETUP_TOKEN), _request(),
        current_user=USER, _perm=None, db=tenant_db,
    )
    preview = out["preview"]
    assert [a["id"] for a in preview["incoming"]] == ["NEW-9"]
    assert preview["orphaned"][0]["external_account_id"] == "OLD-1"
    assert preview["proposals"][0]["new_external_id"] == "NEW-9"


@respx.mock
def test_connect_used_token_conflicts(tenant_db):
    respx.post(CLAIM_URL).mock(return_value=Response(403))
    with pytest.raises(HTTPException) as exc:
        sr.simplefin_connect(
            sr.ConnectIn(setup_token=SETUP_TOKEN), _request(),
            current_user=USER, _perm=None, db=tenant_db,
        )
    assert exc.value.status_code == 409


def test_connect_bad_token_422(tenant_db):
    with pytest.raises(HTTPException) as exc:
        sr.simplefin_connect(
            sr.ConnectIn(setup_token="aaaaaaaaaa"), _request(),
            current_user=USER, _perm=None, db=tenant_db,
        )
    assert exc.value.status_code == 422


def test_relink_applies_and_refuses_older_collisions(tenant_db, sfin):
    inst, conn = sfin
    base_time = datetime.now(timezone.utc)
    # KEEP-2 is OLDER than OLD-1 → colliding onto it is genuine ambiguity.
    a2 = BankFeedAccount(connection_id=conn.id, external_account_id="KEEP-2",
                         provider=PROVIDER_SIMPLEFIN, name="Savings",
                         created_at=base_time - timedelta(days=2))
    a1 = BankFeedAccount(connection_id=conn.id, external_account_id="OLD-1",
                         provider=PROVIDER_SIMPLEFIN, name="Checking",
                         created_at=base_time - timedelta(days=1))
    tenant_db.add_all([a2, a1])
    tenant_db.commit()
    with pytest.raises(HTTPException) as exc:
        sr.simplefin_relink(
            sr.RelinkIn(mappings=[sr.RelinkMapping(account_id=str(a1.id), new_external_id="KEEP-2")]),
            _request(), current_user=USER, _perm=None, db=tenant_db,
        )
    assert exc.value.status_code == 409
    out = sr.simplefin_relink(
        sr.RelinkIn(mappings=[sr.RelinkMapping(account_id=str(a1.id), new_external_id="NEW-9")]),
        _request(), current_user=USER, _perm=None, db=tenant_db,
    )
    assert out["updated"] == [str(a1.id)]
    tenant_db.refresh(a1)
    assert a1.external_account_id == "NEW-9"


def test_relink_absorbs_race_minted_duplicate(tenant_db, sfin):
    """The reconnect race: a sync ran between /connect and /relink and
    minted a fresh row under the new external id. Re-linking absorbs the
    younger duplicate — history unified, nothing double-counted."""
    inst, conn = sfin
    base_time = datetime.now(timezone.utc)
    orphan = BankFeedAccount(connection_id=conn.id, external_account_id="OLD-1",
                             provider=PROVIDER_SIMPLEFIN, name="Checking",
                             created_at=base_time - timedelta(days=30))
    dup = BankFeedAccount(connection_id=conn.id, external_account_id="NEW-9",
                          provider=PROVIDER_SIMPLEFIN, name="Checking",
                          created_at=base_time)
    tenant_db.add_all([orphan, dup])
    tenant_db.commit()
    d_shared = date(2026, 8, 1)
    def _feed_txn(acct, ext, d, cents):
        tenant_db.add(BankFeedTransaction(
            account_id=acct.id, external_transaction_id=ext, amount_cents=cents,
            pending=False, posted_at=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
            posted_date=d,
        ))
    _feed_txn(orphan, "t-shared", d_shared, -1000)
    _feed_txn(orphan, "t-old", date(2026, 7, 1), -2000)
    _feed_txn(dup, "t-shared", d_shared, -1000)   # same real txn, re-fetched
    _feed_txn(dup, "t-new", date(2026, 8, 10), -3000)
    tenant_db.add(BankFeedBalanceSnapshot(account_id=orphan.id, snapshot_date=d_shared, balance_cents=100))
    tenant_db.add(BankFeedBalanceSnapshot(account_id=dup.id, snapshot_date=d_shared, balance_cents=999))
    tenant_db.add(BankFeedBalanceSnapshot(account_id=dup.id, snapshot_date=date(2026, 8, 10), balance_cents=555))
    tenant_db.commit()

    out = sr.simplefin_relink(
        sr.RelinkIn(mappings=[sr.RelinkMapping(account_id=str(orphan.id), new_external_id="NEW-9")]),
        _request(), current_user=USER, _perm=None, db=tenant_db,
    )
    assert out["updated"] == [str(orphan.id)]
    assert out["absorbed"] == [str(dup.id)]
    accounts = tenant_db.execute(select(BankFeedAccount)).scalars().all()
    assert len(accounts) == 1 and accounts[0].external_account_id == "NEW-9"
    txns = {t.external_transaction_id: t for t in tenant_db.execute(select(BankFeedTransaction)).scalars()}
    assert set(txns) == {"t-shared", "t-old", "t-new"}
    assert all(t.account_id == orphan.id for t in txns.values())
    snaps = tenant_db.execute(select(BankFeedBalanceSnapshot)).scalars().all()
    assert {(s.snapshot_date, s.balance_cents) for s in snaps} == {
        (d_shared, 100),                 # keeper's copy won
        (date(2026, 8, 10), 555),        # dup-only snapshot moved
    }
    assert all(s.account_id == orphan.id for s in snaps)


def test_settings_validation_and_persistence(tenant_db):
    with pytest.raises(HTTPException):
        sr.simplefin_settings(
            sr.SimplefinSettingsIn(fetch_window_start="25:00", fetch_window_end="18:00"),
            _request(), current_user=USER, _perm=None, db=tenant_db,
        )
    with pytest.raises(HTTPException):
        sr.simplefin_settings(
            sr.SimplefinSettingsIn(fetch_window_start="08:00", fetch_window_end="08:00"),
            _request(), current_user=USER, _perm=None, db=tenant_db,
        )
    out = sr.simplefin_settings(
        sr.SimplefinSettingsIn(
            frequency="every_4h", fetch_window_start="07:00",
            fetch_window_end="19:00", daily_fetch_cap=12,
        ),
        _request(), current_user=USER, _perm=None, db=tenant_db,
    )
    assert out["frequency"] == "every_4h"
    assert out["fetch_window_start"] == "07:00" and out["fetch_window_end"] == "19:00"
    assert out["daily_fetch_cap"] == 12
    out = sr.simplefin_settings(
        sr.SimplefinSettingsIn(clear_fetch_window=True),
        _request(), current_user=USER, _perm=None, db=tenant_db,
    )
    assert out["fetch_window_start"] is None and out["fetch_window_end"] is None


def test_settings_cap_cannot_exceed_hard_max():
    with pytest.raises(Exception):
        sr.SimplefinSettingsIn(daily_fetch_cap=DAILY_FETCH_CAP_MAX + 1)


def test_sync_now_blocks_at_cap(tenant_db, sfin, monkeypatch):
    inst, conn = sfin
    sched = service.get_or_create_schedule(tenant_db)
    tz = service.tenant_zoneinfo(tenant_db)
    sched.fetch_count_date = datetime.now(timezone.utc).astimezone(tz).date()
    sched.fetch_count_today = ss.effective_cap(sched)
    tenant_db.commit()
    with pytest.raises(HTTPException) as exc:
        sr.simplefin_sync_now(_request(), current_user=USER, _perm=None, db=tenant_db)
    assert exc.value.status_code == 429

    sched.fetch_count_today = 0
    tenant_db.commit()
    queued = []
    monkeypatch.setattr(
        "gdx_dispatch.modules.bank_feeds.tasks.bank_feeds_sync_task.delay",
        lambda *a, **k: queued.append(a),
    )
    out = sr.simplefin_sync_now(_request(), current_user=USER, _perm=None, db=tenant_db)
    assert out["queued"] is True and len(queued) == 1


def test_status_reports_quota_stale_and_backfill(tenant_db, sfin):
    inst, conn = sfin
    acct = BankFeedAccount(
        connection_id=conn.id, external_account_id="A1", provider=PROVIDER_SIMPLEFIN,
        name="Checking", initial_backfill_done=True,
        last_synced_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    tenant_db.add(acct)
    tenant_db.commit()
    out = sr.simplefin_status(_perm=None, db=tenant_db)
    assert out["connected"] is True
    assert out["stale"] is True          # 3 days > 48h
    assert out["quota"]["cap"] <= DAILY_FETCH_CAP_MAX
    assert out["schedule"]["daily_fetch_cap_max"] == DAILY_FETCH_CAP_MAX
    assert out["accounts"][0]["external_account_id"] == "A1"


def test_status_stale_tracks_worst_account_not_best(tenant_db, sfin):
    """One healthy sibling must not mask a frozen account (persistent
    act.failed) — staleness follows the OLDEST enabled account."""
    inst, conn = sfin
    tenant_db.add(BankFeedAccount(
        connection_id=conn.id, external_account_id="A1", provider=PROVIDER_SIMPLEFIN,
        name="Healthy", initial_backfill_done=True,
        last_synced_at=datetime.now(timezone.utc),
    ))
    tenant_db.add(BankFeedAccount(
        connection_id=conn.id, external_account_id="A2", provider=PROVIDER_SIMPLEFIN,
        name="Frozen", initial_backfill_done=True,
        last_synced_at=datetime.now(timezone.utc) - timedelta(days=3),
    ))
    tenant_db.commit()
    out = sr.simplefin_status(_perm=None, db=tenant_db)
    assert out["stale"] is True


def test_disconnect_soft_nulls_creds(tenant_db, sfin):
    inst, conn = sfin
    out = sr.simplefin_disconnect(_request(), current_user=USER, _perm=None, db=tenant_db)
    assert out["disconnected"] == 1
    tenant_db.refresh(conn)
    assert conn.access_token_enc is None
    assert conn.auth_state == AUTH_DISCONNECTED
    status = sr.simplefin_status(_perm=None, db=tenant_db)
    assert status["connected"] is False


# ══ balance history + tie-out ══════════════════════════════════════════


def test_balance_history_returns_snapshots(tenant_db, sfin):
    inst, conn = sfin
    acct = BankFeedAccount(connection_id=conn.id, external_account_id="A1",
                           provider=PROVIDER_SIMPLEFIN, name="Checking")
    tenant_db.add(acct)
    tenant_db.commit()
    for i, cents in enumerate([1000, 2000, 1500]):
        tenant_db.add(BankFeedBalanceSnapshot(
            account_id=acct.id, snapshot_date=date.today() - timedelta(days=2 - i),
            balance_cents=cents,
        ))
    tenant_db.commit()
    out = sr.simplefin_balance_history(str(acct.id), days=30, _perm=None, db=tenant_db)
    assert [s["balance_cents"] for s in out["snapshots"]] == [1000, 2000, 1500]


def _stmt_fixture(db, month_first: date):
    bank = BankAccount(name="Checking", institution="Primary Bank", last4="2204")
    db.add(bank)
    db.commit()
    imp = BankStatementImport(
        bank_account_id=bank.id, file_sha256="f" * 64,
        period_start=month_first, period_end=month_first + timedelta(days=27),
        beginning_balance_cents=0, ending_balance_cents=0, tie_out_status="passed",
    )
    db.add(imp)
    db.commit()

    def line(n, d, cents, desc):
        row = BankStatementLine(
            bank_account_id=bank.id, import_id=imp.id, txn_date=d,
            amount_cents=cents, description=desc, section="debits",
            occurrence_n=1, line_hash=f"{n:0>64}",
        )
        db.add(row)
        return row

    return bank, line


def test_tieout_matches_with_one_day_tolerance(tenant_db, sfin):
    inst, conn = sfin
    acct = BankFeedAccount(connection_id=conn.id, external_account_id="A1",
                           provider=PROVIDER_SIMPLEFIN, name="Checking")
    tenant_db.add(acct)
    tenant_db.commit()
    first = date(2026, 7, 1)
    bank, line = _stmt_fixture(tenant_db, first)
    line(1, date(2026, 7, 10), -6550, "BAIT SHOP")          # matches feed ±1 day
    line(2, date(2026, 7, 15), -9999, "MYSTERY DEBIT")      # statement-only
    tenant_db.commit()
    for ext, d, cents in [("t1", date(2026, 7, 11), -6550), ("t2", date(2026, 7, 20), 4200)]:
        tenant_db.add(BankFeedTransaction(
            account_id=acct.id, external_transaction_id=ext, amount_cents=cents,
            pending=False, posted_at=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
            posted_date=d, memo="feed txn",
        ))
    tenant_db.commit()

    out = sr.simplefin_tieout(
        feed_account_id=str(acct.id), bank_account_id=str(bank.id),
        month="2026-07", _perm=None, db=tenant_db,
    )
    assert out["matched"] == 1
    assert [row["amount_cents"] for row in out["statement_only"]] == [-9999]
    assert [f["amount_cents"] for f in out["feed_only"]] == [4200]
    assert out["statement_sum_cents"] == -6550 - 9999
    assert out["feed_sum_cents"] == -6550 + 4200


def test_tieout_rejects_bad_month(tenant_db, sfin):
    inst, conn = sfin
    with pytest.raises(HTTPException) as exc:
        sr.simplefin_tieout(
            feed_account_id="not-a-uuid", bank_account_id="also-not",
            month="2026-13", _perm=None, db=tenant_db,
        )
    assert exc.value.status_code == 422
