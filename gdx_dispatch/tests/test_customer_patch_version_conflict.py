"""A stale customer edit dialog is refused with 409, not silently applied (GDXA-448).

`PATCH /api/customers/{id}` set every key it was sent and committed, so two
people with the edit dialog open both "won": the second save reverted the
first person's fields without either of them knowing. Migration 114 gave the
row a `version`; the PATCH now takes the version the dialog loaded
(`expected_version` in the body, or `If-Match`) and refuses a mismatch with
409. A PATCH without a token is still accepted.

Writes that only touch bookkeeping columns (the QuickBooks push clearing
qb_dirty, the rolling-volume cache) do not move the token, or every dialog
open across one would get a false conflict.
"""
from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.customers import (
    CustomerUpdateIn,
    _expected_version,
    _version_claim_stmt,
    get_customer,
    update_customer,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db, **fields) -> str:
    cid = uuid.uuid4()
    db.add(Customer(id=cid, company_id="tenant-test", **{"name": "Ada Lane", **fields}))
    db.commit()
    return str(cid)


def _version(db, cid: str) -> int:
    db.expire_all()
    return db.execute(select(Customer.version).where(Customer.id == uuid.UUID(cid))).scalar_one()


async def _patch(db, cid: str, request=None, **fields):
    return await update_customer(
        customer_id=cid, payload=CustomerUpdateIn(**fields), request=request, _={}, db=db,
    )


async def test_the_second_of_two_stale_dialogs_gets_409_and_the_first_edit_stands(db):
    cid = _seed(db, phone="555-0100")
    loaded = (await get_customer(customer_id=cid, _={}, db=db))["version"]
    assert loaded == 1

    first = await _patch(db, cid, phone="555-0111", expected_version=loaded)
    assert first.phone == "555-0111"
    assert first.version == 2

    with pytest.raises(HTTPException) as exc:
        await _patch(db, cid, phone="555-0222", expected_version=loaded)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "version_conflict"
    assert exc.value.detail["expected_version"] == 1
    assert exc.value.detail["current_version"] == 2
    assert "Reload" in exc.value.detail["message"]

    db.expire_all()
    row = db.get(Customer, uuid.UUID(cid))
    assert row.phone == "555-0111"  # the colleague's edit was not reverted
    assert row.version == 2


async def test_a_fresh_token_saves_and_bumps_once(db):
    cid = _seed(db)
    out = await _patch(db, cid, notes="one", expected_version=1)
    assert out.version == 2
    out = await _patch(db, cid, notes="two", expected_version=2)
    assert out.version == 3
    assert _version(db, cid) == 3


async def test_a_patch_without_a_token_is_still_accepted(db):
    cid = _seed(db)
    await _patch(db, cid, notes="first")
    out = await _patch(db, cid, notes="no token at all")
    assert out.notes == "no token at all"
    assert out.version == 3


async def test_a_refused_patch_writes_nothing_and_logs_no_audit_row(db):
    from gdx_dispatch.core.audit import AuditLog

    cid = _seed(db, notes="kept")
    await _patch(db, cid, notes="colleague", expected_version=1)
    before = db.execute(select(AuditLog)).scalars().all()
    with pytest.raises(HTTPException):
        await _patch(db, cid, notes="stale", expected_version=1)
    db.expire_all()
    assert db.get(Customer, uuid.UUID(cid)).notes == "colleague"
    assert len(db.execute(select(AuditLog)).scalars().all()) == len(before)


async def test_expected_version_is_never_written_to_the_row(db):
    """Popped before the setattr loop: a token-only PATCH changes nothing."""
    cid = _seed(db)
    out = await _patch(db, cid, expected_version=1)
    assert out.version == 1
    assert _version(db, cid) == 1


async def test_if_match_header_carries_the_token_end_to_end(db):
    cid = _seed(db)
    req = SimpleNamespace(
        headers={"if-match": '"1"'}, state=SimpleNamespace(tenant={"id": "tenant-test"}),
        client=None,
    )
    out = await _patch(db, cid, req, notes="via header")
    assert out.version == 2
    with pytest.raises(HTTPException) as exc:
        await _patch(db, cid, req, notes="stale header")
    assert exc.value.status_code == 409


@pytest.mark.parametrize(
    ("body", "header", "expected"),
    [
        (None, None, None),
        (3, None, 3),
        (None, '"3"', 3),
        (None, 'W/"3"', 3),
        (None, "3", 3),
        (None, "*", None),
        (3, '"3"', 3),
    ],
)
def test_expected_version_reads_body_and_if_match(body, header, expected):
    req = SimpleNamespace(headers={} if header is None else {"if-match": header})
    assert _expected_version(body, req) == expected


@pytest.mark.parametrize(("body", "header"), [(None, '"abc"'), (2, '"3"')])
def test_unreadable_or_disagreeing_tokens_are_422(body, header):
    with pytest.raises(HTTPException) as exc:
        _expected_version(body, SimpleNamespace(headers={"if-match": header}))
    assert exc.value.status_code == 422


def test_expected_version_must_be_positive():
    with pytest.raises(ValueError):
        CustomerUpdateIn(expected_version=0)


# ── bookkeeping writes do not hand open dialogs a false conflict ────────────


async def test_a_quickbooks_push_clearing_qb_dirty_keeps_the_token(db):
    cid = _seed(db)
    await _patch(db, cid, notes="edited", expected_version=1)
    row = db.get(Customer, uuid.UUID(cid))
    assert row.qb_dirty is True
    # What push_customer does after a successful push (quickbooks/sync.py).
    row.qb_dirty = False
    row.qb_synced_at = datetime.now(UTC)
    db.commit()
    assert _version(db, cid) == 2
    # And the qb_dirty listener did not re-flip it on our version SET.
    assert db.get(Customer, uuid.UUID(cid)).qb_dirty is False
    out = await _patch(db, cid, notes="still fresh", expected_version=2)
    assert out.version == 3


async def test_a_rolling_volume_refresh_keeps_the_token(db):
    from gdx_dispatch.services.customer_rolling_volume import refresh_cached_volume

    cid = _seed(db)
    refresh_cached_volume(uuid.UUID(cid), db)
    db.commit()
    row = db.get(Customer, uuid.UUID(cid))
    assert row.cached_rolling_volume_at is not None
    assert _version(db, cid) == 1


async def test_an_edit_alongside_a_bookkeeping_column_still_bumps(db):
    cid = _seed(db)
    row = db.get(Customer, uuid.UUID(cid))
    row.cached_rolling_volume_paid_12mo = Decimal("10.00")
    row.notes = "a real edit"
    db.commit()
    assert _version(db, cid) == 2


# ── Postgres: two racing stale saves ────────────────────────────────────────


def test_postgres_a_racing_stale_claim_waits_then_misses(pg_test_db):
    """The claim is a compare-and-set holding the row lock, not a read then a
    compare. B claims the same stale version while A's edit is uncommitted: B
    blocks on the row, then re-evaluates its WHERE against A's committed row
    and matches nothing, which the router turns into 409. A read-then-compare
    would have let both through."""
    eng = create_engine(pg_test_db, future=True)
    cid = uuid.uuid4()
    try:
        with eng.begin() as c:
            # Raw insert: the fixture's customers predates some ORM columns.
            c.execute(
                text("INSERT INTO customers (id, name, company_id) VALUES (:id, 'Ada', 't1')"),
                {"id": cid},
            )
        a, b = eng.connect(), eng.connect()
        try:
            b.exec_driver_sql("SET lock_timeout = '15s'")
            b.commit()
            ta = a.begin()
            assert a.execute(_version_claim_stmt(cid, 1)).rowcount == 1
            # SET version = version: the claim alone does not move the token.
            assert a.execute(text("SELECT version FROM customers WHERE id = :id"), {"id": cid}).scalar_one() == 1

            result: dict = {}

            def racer() -> None:
                try:
                    with b.begin():
                        result["matched"] = b.execute(_version_claim_stmt(cid, 1)).rowcount
                except Exception as exc:  # surfaced by the assert below
                    result["error"] = exc

            t = threading.Thread(target=racer)
            t.start()
            t.join(timeout=1.0)
            assert t.is_alive(), f"B did not wait for A's row lock: {result}"
            a.execute(update(Customer).where(Customer.id == cid).values(notes="first"))
            ta.commit()
            t.join(timeout=20)
            assert not t.is_alive()
            assert result == {"matched": 0}
        finally:
            a.close()
            b.close()
        with eng.connect() as c:
            row = c.execute(
                text("SELECT version, notes FROM customers WHERE id = :id"), {"id": cid},
            ).one()
        assert tuple(row) == (2, "first")
    finally:
        eng.dispose()
