"""Bank feeds router — direct-call house style (session + user dict,
``_perm=None`` bypasses the permission Depends; module dep not exercised)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request as StarletteRequest

from gdx_dispatch.modules.bank_feeds import oauth
from gdx_dispatch.modules.bank_feeds import router as r
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    AUTH_HEALTHY,
    AUTH_NEEDS_RECONNECT,
    BankFeedAccount,
    BankFeedDocument,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"sub": "tester", "tenant_id": COMPANY, "role": "admin"}
FI_HOST = "digital.garden-fi.com"


def _request():
    scope = {
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 80), "state": {},
    }
    req = StarletteRequest(scope)
    req.state.tenant = {"id": COMPANY}
    return req


def _institution(db, fi_host=FI_HOST, with_creds=True):
    inst = BannoInstitution(
        fi_host=fi_host, display_label="Garden",
        client_id="cid" if with_creds else None,
        client_secret_enc=oauth._encrypt("secret") if with_creds else None,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _connection(db, inst, auth_state=AUTH_HEALTHY):
    conn = BannoConnection(
        institution_id=inst.id, fi_host=inst.fi_host, banno_user_id="sub-1",
        access_token_enc=oauth._encrypt("at"), refresh_token_enc=oauth._encrypt("rt"),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        auth_state=auth_state,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


# ── institutions CRUD ──────────────────────────────────────────────────


def test_create_institution_and_secret_never_leaks(tenant_db):
    out = r.create_institution(
        r.InstitutionIn(fi_host="Digital.Garden-FI.com", display_label="Garden",
                        client_id="cid", client_secret="super-secret"),
        _request(), USER, None, tenant_db,
    )
    assert out["fi_host"] == "digital.garden-fi.com"
    assert out["secret_set"] is True
    assert "super-secret" not in str(out)

    listing = r.list_institutions(None, tenant_db)
    assert "super-secret" not in str(listing)
    assert listing["institutions"][0]["secret_set"] is True


@pytest.mark.parametrize("bad_host", [
    "https://h.example.com", "h.example.com/path", "h.example.com:8080",
    "10.0.0.1", "user@h.example.com",
])
def test_create_institution_rejects_bad_hosts(tenant_db, bad_host):
    with pytest.raises(HTTPException) as exc:
        r.create_institution(
            r.InstitutionIn(fi_host=bad_host), _request(), USER, None, tenant_db,
        )
    assert exc.value.status_code == 422


def test_create_institution_duplicate_host_409(tenant_db):
    _institution(tenant_db)
    with pytest.raises(HTTPException) as exc:
        r.create_institution(
            r.InstitutionIn(fi_host=FI_HOST), _request(), USER, None, tenant_db,
        )
    assert exc.value.status_code == 409


def test_delete_refuses_with_data_then_purges(tenant_db):
    inst = _institution(tenant_db)
    conn = _connection(tenant_db, inst)
    acct = BankFeedAccount(connection_id=conn.id, external_account_id="a1")
    tenant_db.add(acct)
    tenant_db.commit()
    tenant_db.add(BankFeedTransaction(account_id=acct.id, external_transaction_id="t1"))
    tenant_db.add(BankFeedDocument(connection_id=conn.id, external_document_id="d1"))
    tenant_db.commit()

    with pytest.raises(HTTPException) as exc:
        r.delete_institution(str(inst.id), _request(), None, USER, None, tenant_db)
    assert exc.value.status_code == 409

    out = r.delete_institution(
        str(inst.id), _request(), r.InstitutionDeleteIn(purge=True), USER, None, tenant_db,
    )
    assert out["deleted"] is True
    for model in (BannoInstitution, BannoConnection, BankFeedAccount,
                  BankFeedTransaction, BankFeedDocument):
        assert tenant_db.execute(select(model)).first() is None


def test_disconnect_is_soft(tenant_db):
    inst = _institution(tenant_db)
    conn = _connection(tenant_db, inst)
    acct = BankFeedAccount(connection_id=conn.id, external_account_id="a1")
    tenant_db.add(acct)
    tenant_db.commit()

    out = r.disconnect(r.InstitutionRefIn(institution_id=str(inst.id)),
                       _request(), USER, None, tenant_db)
    assert out["disconnected"] == 1
    row = tenant_db.execute(select(BannoConnection)).scalar_one()
    assert row.auth_state == AUTH_DISCONNECTED
    assert row.access_token_enc is None
    assert tenant_db.execute(select(BankFeedAccount)).first() is not None  # data kept


# ── status ─────────────────────────────────────────────────────────────


def test_status_states(tenant_db):
    unconfigured = _institution(tenant_db, fi_host="digital.bank2.example.com", with_creds=False)
    configured = _institution(tenant_db)
    connected_inst = _institution(tenant_db, fi_host="digital.bank3.example.com")
    _connection(tenant_db, connected_inst)
    broken_inst = _institution(tenant_db, fi_host="digital.bank4.example.com")
    _connection(tenant_db, broken_inst, auth_state=AUTH_NEEDS_RECONNECT)

    out = r.bank_feeds_status(_request(), USER, None, tenant_db)
    by_host = {i["fi_host"]: i for i in out["institutions"]}

    assert by_host[unconfigured.fi_host]["configured"] is False
    assert by_host[unconfigured.fi_host]["connected"] is False
    assert by_host[configured.fi_host]["configured"] is True
    assert by_host[configured.fi_host]["connected"] is False
    assert by_host[connected_inst.fi_host]["connected"] is True
    assert by_host[connected_inst.fi_host]["auth_state"] == AUTH_HEALTHY
    assert by_host[broken_inst.fi_host]["auth_state"] == AUTH_NEEDS_RECONNECT
    for entry in out["institutions"]:
        assert entry["breaker_state"] in ("CLOSED", "OPEN", "HALF_OPEN")
    assert out["schedule"]["frequency"] == "manual"


# ── transactions listing ───────────────────────────────────────────────


@pytest.fixture
def txn_setup(tenant_db):
    inst = _institution(tenant_db)
    conn = _connection(tenant_db, inst)
    acct = BankFeedAccount(connection_id=conn.id, external_account_id="a1", name="Checking")
    tenant_db.add(acct)
    tenant_db.commit()
    rows = [
        BankFeedTransaction(
            account_id=acct.id, external_transaction_id="old",
            amount_cents=-1000, pending=False, posted_date=date(2026, 6, 1),
            payee="Old Vendor",
        ),
        BankFeedTransaction(
            account_id=acct.id, external_transaction_id="new",
            amount_cents=-2000, pending=False, posted_date=date(2026, 7, 10),
            payee="Gas Co",
        ),
        BankFeedTransaction(
            account_id=acct.id, external_transaction_id="pend",
            amount_cents=-500, pending=True, posted_date=None, payee="Card Hold",
        ),
        BankFeedTransaction(
            account_id=acct.id, external_transaction_id="dead",
            amount_cents=-999, pending=False, posted_date=date(2026, 7, 11),
            payee="Ghost", deleted_at=datetime.now(timezone.utc),
        ),
    ]
    tenant_db.add_all(rows)
    tenant_db.commit()
    return inst, acct


def test_transactions_default_excludes_tombstones(tenant_db, txn_setup):
    out = r.list_transactions(_perm=None, db=tenant_db)
    ids = {i["payee"] for i in out["items"]}
    assert "Ghost" not in ids
    assert out["total"] == 3


def test_transactions_date_filter_keeps_pendings(tenant_db, txn_setup):
    out = r.list_transactions(date_from=date(2026, 7, 1), _perm=None, db=tenant_db)
    payees = {i["payee"] for i in out["items"]}
    assert payees == {"Gas Co", "Card Hold"}  # pending kept despite NULL date

    out2 = r.list_transactions(
        date_from=date(2026, 7, 1), include_pending=False, _perm=None, db=tenant_db
    )
    assert {i["payee"] for i in out2["items"]} == {"Gas Co"}


def test_transactions_q_and_account_filters(tenant_db, txn_setup):
    _, acct = txn_setup
    out = r.list_transactions(q="gas", _perm=None, db=tenant_db)
    assert [i["payee"] for i in out["items"]] == ["Gas Co"]
    out2 = r.list_transactions(account_id=str(acct.id), _perm=None, db=tenant_db)
    assert out2["total"] == 3


def test_transactions_pagination_totals(tenant_db, txn_setup):
    out = r.list_transactions(limit=2, offset=0, _perm=None, db=tenant_db)
    assert out["total"] == 3
    assert len(out["items"]) == 2


# ── accounts + schedule ────────────────────────────────────────────────


def test_account_toggle(tenant_db, txn_setup):
    _, acct = txn_setup
    out = r.patch_account(str(acct.id), r.AccountPatch(sync_enabled=False),
                          _request(), USER, None, tenant_db)
    assert out["sync_enabled"] is False


def test_schedule_put_validates_frequency(tenant_db):
    with pytest.raises(HTTPException) as exc:
        r.put_schedule(r.SchedulePut(frequency="fortnightly"), _request(), USER, None, tenant_db)
    assert exc.value.status_code == 422

    out = r.put_schedule(
        r.SchedulePut(frequency="daily", backfill_days=180), _request(), USER, None, tenant_db
    )
    assert out["frequency"] == "daily"
    assert out["backfill_days"] == 180
    assert out["next_run_at"] is not None
