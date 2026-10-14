"""A customer text that just arrived rings the office bell.

``upsert_message`` writes one broadcast ``Notification`` (category ``sms``)
only when it INSERTS an inbound message stamped within the last 30 minutes:
never on the update path (the poll and the webhook cannot both ring), never
for outbound texts, never for backlog the 48-hour poll or the nightly resync
lands, and a failed bell never fails the sync.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register Notification)
from gdx_dispatch.models.tenant_models import Customer, Notification
from gdx_dispatch.modules.phone_com import upserts
from gdx_dispatch.modules.phone_com.upserts import upsert_message
from gdx_dispatch.tests.conftest import production_sessionmaker


@pytest.fixture
def tenant_db():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(e)
    db = production_sessionmaker(e)()
    yield db
    db.close()
    e.dispose()


def _payload(*, id=1001, direction="in", age_seconds=10, text="Is my door ready?"):
    return {
        "id": id,
        "text": text,
        "from": "+15555550142",
        "to": [{"number": "+15555550100", "delivery_status": "delivered"}],
        "created_at": int(time.time()) - age_seconds,
        "direction": direction,
        "media": [],
    }


def _bells(db):
    return db.execute(select(Notification)).scalars().all()


def test_new_inbound_text_rings_once_with_last_four(tenant_db):
    upsert_message(tenant_db, _payload())
    bells = _bells(tenant_db)
    assert len(bells) == 1
    assert bells[0].category == "sms"
    assert bells[0].user_id is None  # broadcast to the office
    assert bells[0].title == "New text"
    assert bells[0].message == "…0142: Is my door ready?"


def test_update_of_existing_message_does_not_ring_again(tenant_db):
    upsert_message(tenant_db, _payload())
    upsert_message(tenant_db, _payload())  # the webhook or the next poll
    assert len(_bells(tenant_db)) == 1


def test_outbound_text_does_not_ring(tenant_db):
    upsert_message(tenant_db, _payload(direction="out"))
    assert _bells(tenant_db) == []


def test_backlog_text_does_not_ring(tenant_db):
    upsert_message(tenant_db, _payload(age_seconds=3 * 3600))
    assert _bells(tenant_db) == []


def test_known_customer_is_named(tenant_db, monkeypatch):
    cust = Customer(name="Pat Example", company_id="00000000-0000-0000-0000-000000000001")
    tenant_db.add(cust)
    tenant_db.commit()
    monkeypatch.setattr(upserts, "match_caller_id", lambda db, number: cust.id)
    upsert_message(tenant_db, _payload())
    assert _bells(tenant_db)[0].message == "Pat Example: Is my door ready?"


def test_long_body_is_cut_to_140(tenant_db):
    upsert_message(tenant_db, _payload(text="x" * 500))
    assert _bells(tenant_db)[0].message == "…0142: " + "x" * 140


def test_failed_bell_never_fails_the_upsert(tenant_db, monkeypatch):
    import gdx_dispatch.core.office_notifications as office

    def boom(*a, **kw):
        raise RuntimeError("bell down")

    monkeypatch.setattr(office, "notify_office", boom)
    row = upsert_message(tenant_db, _payload())
    assert row is not None and row.id is not None
