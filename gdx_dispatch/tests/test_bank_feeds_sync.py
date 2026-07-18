"""Bank feeds transaction sync — backfill windows/resume, pagination
overlap, updatedSince incremental semantics, tombstones, amount parsing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from gdx_dispatch.modules.bank_feeds import oauth, service
from gdx_dispatch.modules.bank_feeds.client import BannoClient
from gdx_dispatch.modules.bank_feeds.models import (
    BankFeedAccount,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)

FI_HOST = "digital.garden-fi.com"
SUB = "sub-1"
ACCT = "acct-ext-1"
TXN_URL = f"https://{FI_HOST}/a/consumer/api/v0/users/{SUB}/accounts/{ACCT}/transactions"


@pytest.fixture
def setup(tenant_db):
    inst = BannoInstitution(fi_host=FI_HOST, display_label="Garden", client_id="cid",
                            client_secret_enc=oauth._encrypt("s"))
    tenant_db.add(inst)
    tenant_db.commit()
    conn = BannoConnection(
        institution_id=inst.id, fi_host=FI_HOST, banno_user_id=SUB,
        access_token_enc=oauth._encrypt("tok"), refresh_token_enc=oauth._encrypt("rt"),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    tenant_db.add(conn)
    tenant_db.commit()
    acct = BankFeedAccount(connection_id=conn.id, external_account_id=ACCT, name="Checking")
    tenant_db.add(acct)
    tenant_db.commit()
    for obj in (inst, conn, acct):
        tenant_db.refresh(obj)
    return inst, conn, acct


def _client():
    return BannoClient(FI_HOST, lambda stale_token=None: "tok")


def _txn(i: int, *, amount: str = "-10.00", pending: bool = False,
         posted: str | None = None, last_updated: str | None = None) -> dict:
    posted = posted or "2026-07-01T12:00:00Z"
    return {
        "id": f"txn-{i}",
        "amount": amount,
        "pendingStatus": "Pending" if pending else "Reconciled",
        "datePosted": None if pending else posted,
        "displayName": f"Payee {i}",
        "memo": f"memo {i}",
        "lastUpdated": last_updated or "2026-07-02T00:00:00Z",
    }


# ── amount parsing ─────────────────────────────────────────────────────


def test_parse_amount_cents():
    assert service.parse_amount_cents("-45.67") == -4567
    assert service.parse_amount_cents("120") == 12000
    assert service.parse_amount_cents("1.005") == 100  # ROUND_HALF_EVEN, warned
    assert service.parse_amount_cents("1.015") == 102
    assert service.parse_amount_cents(None) is None
    assert service.parse_amount_cents("") is None
    assert service.parse_amount_cents("not-a-number") is None
    assert service.parse_amount_cents("NaN") is None


# ── backfill ───────────────────────────────────────────────────────────


@respx.mock
def test_backfill_windows_and_cursor_anchor(respx_mock, tenant_db, setup):
    _, conn, acct = setup
    seen_windows: list[tuple[str | None, str | None]] = []

    def responder(request):
        params = request.url.params
        seen_windows.append((params.get("since"), params.get("until")))
        assert params.get("updatedSince") is None
        return Response(200, json={"transactions": [_txn(len(seen_windows))],
                                   "inactivatedTransactionIds": []})

    respx_mock.get(TXN_URL).mock(side_effect=responder)

    before = datetime.now(timezone.utc)
    stats = service.sync_account_transactions(
        tenant_db, _client(), conn, acct, backfill_days=200
    )
    assert stats["mode"] == "backfill"
    # 200 days at 90-day windows → 3 windows.
    assert len(seen_windows) == 3
    for since, until in seen_windows:
        assert since and until  # every backfill page is windowed

    tenant_db.refresh(acct)
    assert acct.initial_backfill_done is True
    assert acct.backfill_synced_through is not None
    # Cursor anchored at backfill start − 5 min (covers mid-backfill mutations).
    cursor = acct.updated_since_cursor
    if cursor.tzinfo is None:  # SQLite returns naive datetimes
        cursor = cursor.replace(tzinfo=timezone.utc)
    assert cursor <= before
    assert cursor >= before - timedelta(minutes=10)


@respx.mock
def test_backfill_resumes_from_committed_window(respx_mock, tenant_db, setup):
    """A crashed backfill resumes at backfill_synced_through, not day zero."""
    _, conn, acct = setup
    acct.backfill_started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    acct.backfill_synced_through = (datetime.now(timezone.utc) - timedelta(days=30)).date()
    tenant_db.commit()

    seen_since: list[str] = []

    def responder(request):
        seen_since.append(request.url.params.get("since"))
        return Response(200, json={"transactions": [], "inactivatedTransactionIds": []})

    respx_mock.get(TXN_URL).mock(side_effect=responder)
    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)

    # First window starts near the resume point (−1 day overlap), NOT 365 days back.
    first_since = datetime.fromisoformat(seen_since[0].replace("Z", "+00:00"))
    assert first_since > datetime.now(timezone.utc) - timedelta(days=33)


@respx.mock
def test_pagination_overlap_offset(respx_mock, tenant_db, setup):
    """Full page → next offset advances by limit − overlap (500 − 50)."""
    _, conn, acct = setup
    offsets: list[int] = []

    def responder(request):
        offset = int(request.url.params.get("offset") or 0)
        offsets.append(offset)
        if offset == 0:
            txns = [_txn(i) for i in range(500)]
        else:
            txns = [_txn(600)]
        return Response(200, json={"transactions": txns, "inactivatedTransactionIds": []})

    respx_mock.get(TXN_URL).mock(side_effect=responder)
    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=30)
    assert offsets[0] == 0
    assert offsets[1] == 450  # 500 − 50 overlap


# ── incremental ────────────────────────────────────────────────────────


def _finish_backfill(tenant_db, acct, cursor: datetime):
    acct.initial_backfill_done = True
    acct.backfill_started_at = cursor
    acct.updated_since_cursor = cursor
    tenant_db.commit()


@respx.mock
def test_incremental_updated_since_and_cursor_advance(respx_mock, tenant_db, setup):
    _, conn, acct = setup
    cursor = datetime.now(timezone.utc) - timedelta(days=2)
    _finish_backfill(tenant_db, acct, cursor)

    def responder(request):
        params = request.url.params
        assert params.get("updatedSince") is not None
        assert params.get("since") is None
        return Response(200, json={
            "transactions": [
                _txn(1, last_updated="2026-07-17T10:00:00Z"),
                _txn(2, last_updated="2026-07-17T11:00:00Z"),
            ],
            "inactivatedTransactionIds": [],
        })

    respx_mock.get(TXN_URL).mock(side_effect=responder)
    stats = service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    assert stats["mode"] == "incremental"
    assert stats["upserted"] == 2

    tenant_db.refresh(acct)
    expected = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc) - timedelta(minutes=5)
    got = acct.updated_since_cursor
    if got.tzinfo is None:
        got = got.replace(tzinfo=timezone.utc)
    assert got == expected  # THEIR timebase − overlap


@respx.mock
def test_incremental_empty_run_freezes_cursor(respx_mock, tenant_db, setup):
    _, conn, acct = setup
    cursor = datetime.now(timezone.utc) - timedelta(days=2)
    _finish_backfill(tenant_db, acct, cursor)
    respx_mock.get(TXN_URL).mock(
        return_value=Response(200, json={"transactions": [], "inactivatedTransactionIds": []})
    )
    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    tenant_db.refresh(acct)
    got = acct.updated_since_cursor
    if got.tzinfo is None:
        got = got.replace(tzinfo=timezone.utc)
    assert got == cursor  # NEVER advances on an empty run (clock-skew safety)


@respx.mock
def test_upsert_in_place_and_pending_to_posted_flip(respx_mock, tenant_db, setup):
    _, conn, acct = setup
    cursor = datetime.now(timezone.utc) - timedelta(days=2)
    _finish_backfill(tenant_db, acct, cursor)

    payloads = [
        {"transactions": [_txn(1, pending=True, amount="-20.00")], "inactivatedTransactionIds": []},
        {"transactions": [_txn(1, pending=False, amount="-20.00",
                               last_updated="2026-07-17T12:00:00Z")],
         "inactivatedTransactionIds": []},
    ]
    calls = {"n": 0}

    def responder(request):
        body = payloads[min(calls["n"], 1)]
        calls["n"] += 1
        return Response(200, json=body)

    respx_mock.get(TXN_URL).mock(side_effect=responder)

    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    row = tenant_db.execute(select(BankFeedTransaction)).scalar_one()
    assert row.pending is True
    assert row.line_hash is None  # hash undefined while pending
    assert row.posted_date is None

    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    rows = tenant_db.execute(select(BankFeedTransaction)).scalars().all()
    assert len(rows) == 1  # in place — unique key held
    assert rows[0].pending is False
    assert rows[0].line_hash is not None
    assert rows[0].posted_date is not None
    assert rows[0].amount_cents == -2000


@respx.mock
def test_tombstone_reappear_and_never_synced_noop(respx_mock, tenant_db, setup):
    _, conn, acct = setup
    cursor = datetime.now(timezone.utc) - timedelta(days=2)
    _finish_backfill(tenant_db, acct, cursor)

    payloads = [
        {"transactions": [_txn(1)], "inactivatedTransactionIds": ["never-synced-id"]},
        {"transactions": [], "inactivatedTransactionIds": ["txn-1"]},
        {"transactions": [_txn(1, last_updated="2026-07-17T14:00:00Z")],
         "inactivatedTransactionIds": []},
    ]
    calls = {"n": 0}

    def responder(request):
        body = payloads[min(calls["n"], len(payloads) - 1)]
        calls["n"] += 1
        return Response(200, json=body)

    respx_mock.get(TXN_URL).mock(side_effect=responder)

    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    rows = tenant_db.execute(select(BankFeedTransaction)).scalars().all()
    assert len(rows) == 1  # never-synced inactivation created NO phantom row

    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    tenant_db.refresh(rows[0])
    assert rows[0].deleted_at is not None  # tombstoned

    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    tenant_db.refresh(rows[0])
    assert rows[0].deleted_at is None  # reappearance clears the tombstone


@respx.mock
def test_missing_amount_stored_null(respx_mock, tenant_db, setup):
    _, conn, acct = setup
    cursor = datetime.now(timezone.utc) - timedelta(days=2)
    _finish_backfill(tenant_db, acct, cursor)
    txn = _txn(9)
    txn["amount"] = None
    respx_mock.get(TXN_URL).mock(
        return_value=Response(200, json={"transactions": [txn], "inactivatedTransactionIds": []})
    )
    service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=365)
    row = tenant_db.execute(select(BankFeedTransaction)).scalar_one()
    assert row.amount_cents is None  # stored, filtered out by consumers


@respx.mock
def test_full_resync_flag_rewinds_to_backfill(respx_mock, tenant_db, setup):
    _, conn, acct = setup
    _finish_backfill(tenant_db, acct, datetime.now(timezone.utc))
    acct.full_resync_required = True
    tenant_db.commit()

    seen = {"windowed": False}

    def responder(request):
        if request.url.params.get("since"):
            seen["windowed"] = True
        return Response(200, json={"transactions": [], "inactivatedTransactionIds": []})

    respx_mock.get(TXN_URL).mock(side_effect=responder)
    stats = service.sync_account_transactions(tenant_db, _client(), conn, acct, backfill_days=30)
    assert stats["mode"] == "backfill"
    assert seen["windowed"] is True
    tenant_db.refresh(acct)
    assert acct.full_resync_required is False
