"""PATCH /api/customers/{id} emits customer.updated (GDXA-968).

`customer.updated` sat in WEBHOOK_EVENTS with nothing emitting it, so a
subscriber who picked it from the catalogue never got a delivery. These drive
the real handler and the real emitter (no mock of emit_domain_event) and read
the staged `webhook_deliveries` rows back.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.webhooks.models import WebhookDelivery
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.customers import CustomerUpdateIn, update_customer
from gdx_dispatch.routers.webhooks import WebhookSubscription

pytestmark = pytest.mark.anyio

TENANT = "tenant-test"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )

    # SQLAlchemy's SQLite SAVEPOINT recipe, as in test_webhooks_emit: emit
    # stages each delivery inside begin_nested().
    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _real_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN")

    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(WebhookSubscription(
        company_id=TENANT, name="hook", url="https://example.test/hook",
        secret="s3cr3t", events=json.dumps(["customer.updated"]), active=True,
    ))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db) -> str:
    cid = uuid.uuid4()
    db.add(Customer(
        id=cid, company_id=TENANT, name="Ada Lane",
        email="ada@example.test", phone="555-0100",
    ))
    db.commit()
    return str(cid)


async def _patch(db, cid: str, **fields):
    return await update_customer(
        customer_id=cid, payload=CustomerUpdateIn(**fields), request=None, _={}, db=db,
    )


def _deliveries(db) -> list[WebhookDelivery]:
    db.expire_all()
    return list(db.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_type == "customer.updated")
    ).scalars().all())


async def test_an_edit_stages_one_customer_updated_delivery_without_pii_values(db):
    cid = _seed(db)
    await _patch(db, cid, email="new@example.test", phone="555-0199", notes="gate code")

    rows = _deliveries(db)
    assert len(rows) == 1
    data = rows[0].payload["data"]
    assert rows[0].payload["event"] == "customer.updated"
    assert data["customer_id"] == cid
    assert data["name"] == "Ada Lane"
    assert data["changed_fields"] == ["email", "notes", "phone"]
    assert data["version"] == 2
    # Names of what changed cross to the receiver; these values never do.
    wire = json.dumps(rows[0].payload)
    for value in ("new@example.test", "ada@example.test", "555-0199", "555-0100", "gate code"):
        assert value not in wire


async def test_a_rename_ships_the_current_name_and_never_the_old_one(db):
    # The name is the one value on the wire, as on customer.created.
    cid = _seed(db)
    await _patch(db, cid, name="Ada Smith")
    payload = _deliveries(db)[0].payload
    assert payload["data"]["name"] == "Ada Smith"
    assert payload["data"]["changed_fields"] == ["name"]
    assert "Ada Lane" not in json.dumps(payload)


async def test_two_edits_are_two_deliveries_not_a_deduped_reemit(db):
    cid = _seed(db)
    await _patch(db, cid, notes="one")
    await _patch(db, cid, notes="two")

    rows = _deliveries(db)
    assert len(rows) == 2
    assert {r.payload["data"]["version"] for r in rows} == {2, 3}
    assert len({r.idempotency_key for r in rows}) == 2


async def test_a_save_that_changes_nothing_emits_nothing(db):
    # The edit dialog resends every field on save; an unchanged save is not an
    # update and does not move the version.
    cid = _seed(db)
    await _patch(db, cid, name="Ada Lane", email="ada@example.test", phone="555-0100")
    assert _deliveries(db) == []


async def test_referral_source_is_reported_under_its_operator_facing_name(db):
    cid = _seed(db)
    await _patch(db, cid, referral_source="Yard sign")
    assert _deliveries(db)[0].payload["data"]["changed_fields"] == ["referral_source"]
