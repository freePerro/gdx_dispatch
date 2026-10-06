"""A location's audit row says WHICH site and WHAT changed (GDXA-237).

Until GDXA-237 create/update/delete of a customer location wrote
``entity_id=<customer id>`` and ``details={}``: with two sites on one customer
the trail could not say which one moved, nor from where to where. The PATCH
and DELETE handlers took no ``request`` either, so their rows carried no
tenant and no client IP.

Through the real router, and each test reads ``audit_logs`` back — a mock of
``log_audit_event_sync`` would prove the arguments, not the stored row.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers import customers as customers_router
from gdx_dispatch.routers import loyalty as loyalty_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"


@pytest.fixture
def client_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    app = FastAPI()
    app.include_router(customers_router.router)
    app.include_router(loyalty_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER, "tenant_id": TENANT, "role": "admin",
    }
    for module in ("customers", "loyalty"):
        app.dependency_overrides[require_module(module)] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.user = {"user_id": USER, "tenant_id": TENANT}
        return await call_next(request)

    yield TestClient(app), db
    db.close()
    engine.dispose()


def _customer(db) -> Customer:
    c = Customer(id=uuid4(), name="Acme", phone="555-1111", address="100 Billing Rd", company_id=TENANT)
    db.add(c)
    db.commit()
    return c


def _audit(db, action: str) -> AuditLog:
    db.expire_all()
    (row,) = db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
    return row


def _create(client, customer_id, **overrides) -> str:
    body = {
        "label": "Lake house", "address": "12 Shore Ln", "city": "Brainerd",
        "state": "MN", "zip": "56401", "access_notes": "Gate 4471", "is_primary": False,
    }
    body.update(overrides)
    r = client.post(f"/api/customers/{customer_id}/locations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_create_is_audited_against_the_location_with_its_fields(client_and_db):
    client, db = client_and_db
    c = _customer(db)

    loc_id = _create(client, c.id)

    row = _audit(db, "create_customer_location")
    assert row.entity_type == "customer_location"
    assert row.entity_id == loc_id
    assert row.entity_id != str(c.id)
    assert row.tenant_id == TENANT
    assert row.user_id == USER
    assert row.details["customer_id"] == str(c.id)
    assert row.details["address"] == "12 Shore Ln"
    assert row.details["label"] == "Lake house"
    assert (row.details["city"], row.details["state"], row.details["zip"]) == ("Brainerd", "MN", "56401")
    assert row.details["has_access_notes"] is True
    # Gate codes never land in the append-only log.
    assert "Gate 4471" not in str(row.details)


def test_update_records_old_and_new_for_what_changed_only(client_and_db):
    client, db = client_and_db
    c = _customer(db)
    loc_id = _create(client, c.id)

    # The dialog re-sends every field; only label and notes really change.
    r = client.patch(
        f"/api/customers/{c.id}/locations/{loc_id}",
        json={
            "label": "Cabin", "address": "12 Shore Ln", "city": "Brainerd",
            "state": "MN", "zip": "56401", "access_notes": "Gate 9999", "is_primary": False,
        },
    )
    assert r.status_code == 200, r.text

    row = _audit(db, "update_customer_location")
    assert row.entity_id == loc_id
    # The handler now takes the request: tenant and actor come from it.
    assert row.tenant_id == TENANT
    assert row.user_id == USER
    assert row.details["customer_id"] == str(c.id)
    assert row.details["changes"] == {"label": {"old": "Lake house", "new": "Cabin"}}
    assert row.details["access_notes_changed"] is True
    assert row.details["coords_cleared"] is False
    assert "Gate" not in str(row.details)


def test_update_records_an_address_move_and_the_cleared_pin(client_and_db):
    client, db = client_and_db
    c = _customer(db)
    loc_id = _create(client, c.id)

    r = client.patch(
        f"/api/customers/{c.id}/locations/{loc_id}",
        json={"address": "800 Pine Ave", "is_primary": True},
    )
    assert r.status_code == 200, r.text

    changes = _audit(db, "update_customer_location").details["changes"]
    assert changes["address"] == {"old": "12 Shore Ln", "new": "800 Pine Ave"}
    assert changes["is_primary"] == {"old": False, "new": True}
    # city/state/zip the request did not send are cleared on a real move.
    assert changes["city"] == {"old": "Brainerd", "new": None}
    assert _audit(db, "update_customer_location").details["coords_cleared"] is True


def test_delete_is_audited_against_the_location(client_and_db):
    client, db = client_and_db
    c = _customer(db)
    keep = _create(client, c.id, label="Home")
    gone = _create(client, c.id, label="Shop", address="5 Mill Rd")

    r = client.delete(f"/api/customers/{c.id}/locations/{gone}")
    assert r.status_code == 200, r.text

    row = _audit(db, "delete_customer_location")
    assert row.entity_id == gone
    assert row.entity_id != keep
    assert row.tenant_id == TENANT
    assert row.user_id == USER
    assert row.details["customer_id"] == str(c.id)
    assert row.details["soft_delete"] is True


def test_award_points_is_audited_against_the_points_entry(client_and_db):
    client, db = client_and_db

    r = client.post("/api/loyalty/customers/cust-7/points", json={"amount": 50, "reason": "Referral bonus"})
    assert r.status_code == 201, r.text

    row = _audit(db, "award_points")
    assert row.entity_type == "award_point"
    assert row.entity_id == r.json()["id"]
    assert row.tenant_id == TENANT
    assert row.details == {"customer_id": "cust-7", "amount": 50, "reason": "Referral bonus"}


def test_gdpr_export_of_a_customer_with_no_locations_or_points(client_and_db):
    """The common case: no child ids. An empty expanding IN once rendered an
    INTEGER empty set that Postgres rejects, emptying the export there."""
    from types import SimpleNamespace

    from gdx_dispatch.core.audit import log_audit_event_sync
    from gdx_dispatch.routers.gdpr import export_customer

    _client, db = client_and_db
    customer = _customer(db)
    log_audit_event_sync(
        db=db, tenant_id=TENANT, user_id=USER, action="customer_updated",
        entity_type="customer", entity_id=str(customer.id), details={},
    )
    db.commit()

    out = export_customer(
        customer_id=customer.id,
        request=SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT}), headers={}, client=None),
        user={"sub": USER, "user_id": USER, "tenant_id": TENANT, "role": "admin"},
        db=db,
    )
    assert [e["action"] for e in out["data"]["audit_events"]] == ["customer_updated"]


def test_gdpr_export_still_carries_the_rekeyed_audit_rows(client_and_db):
    """The customer export matched audit rows on ``entity_id = <customer id>``.

    Keying location and points rows on their own id would have dropped them
    from the export; it now gathers the customer's location and points-entry
    ids, deleted locations included.
    """
    from types import SimpleNamespace

    from gdx_dispatch.routers.gdpr import export_customer

    client, db = client_and_db
    customer = _customer(db)
    cid = str(customer.id)
    loc_id = _create(client, cid)
    assert client.patch(f"/api/customers/{cid}/locations/{loc_id}", json={"label": "Cabin"}).status_code == 200
    assert client.delete(f"/api/customers/{cid}/locations/{loc_id}").status_code == 200
    r = client.post(f"/api/loyalty/customers/{cid}/points", json={"amount": 5, "reason": "Visit"})
    assert r.status_code == 201, r.text
    points_id = r.json()["id"]
    # Shaped like the estimate-conversion jobsite row (estimates.py), which
    # stores no tenant at all.
    from gdx_dispatch.core.audit import log_audit_event_sync

    log_audit_event_sync(
        db=db, tenant_id=None, user_id=USER, action="create_customer_location",
        entity_type="customer_location", entity_id=loc_id,
        details={"source": "estimate_conversion", "customer_id": cid},
    )
    db.commit()

    out = export_customer(
        customer_id=customer.id,
        request=SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT}), headers={}, client=None),
        user={"sub": USER, "user_id": USER, "tenant_id": TENANT, "role": "admin"},
        db=db,
    )
    events = out["data"]["audit_events"]
    assert sum(e["action"] == "create_customer_location" for e in events) == 2
    exported = {(e["action"], e["entity_id"]) for e in events}
    assert {
        ("create_customer_location", loc_id),
        ("update_customer_location", loc_id),
        ("delete_customer_location", loc_id),
        ("award_points", points_id),
    } <= exported


@pytest.fixture
def pg_session(pg_template_db):
    """A throwaway Postgres database built from the ORM, which — like prod —
    has no ``communications`` table. SQLite cannot stand in: it neither
    aborts a transaction on a failed statement nor type-checks an empty IN.
    Depends on ``pg_template_db`` for the shared skip-when-unreachable gate."""
    from sqlalchemy import create_engine

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models import tenant_models  # noqa: F401  (registers every table)
    from gdx_dispatch.tests.fixtures.pg import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, _create_db, _drop_db

    name = f"gdpr_export_{uuid4().hex[:12]}"
    _create_db(name)
    engine = create_engine(f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{name}", future=True)
    TenantBase.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        _drop_db(name)


def test_gdpr_export_on_postgres_carries_the_trail_with_and_without_children(pg_session):
    """On Postgres the export's failed ``communications`` query aborted the
    transaction, so every later query — the audit trail included — came back
    empty; and a customer with no locations bound an empty IN that Postgres
    rejects. Both customers' trails must arrive, and the export be audited."""
    from types import SimpleNamespace

    from sqlalchemy import text

    from gdx_dispatch.core.audit import log_audit_event_sync
    from gdx_dispatch.routers.gdpr import export_customer

    db = pg_session
    request = SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT}), headers={}, client=None)
    user = {"sub": USER, "user_id": USER, "tenant_id": TENANT, "role": "admin"}

    for with_location in (False, True):
        customer = _customer(db)
        cid = str(customer.id)
        log_audit_event_sync(
            db=db, tenant_id=TENANT, user_id=USER, action="customer_updated",
            entity_type="customer", entity_id=cid, details={},
        )
        expected = ["customer_updated"]
        if with_location:
            loc_id = str(uuid4())
            db.execute(
                text("INSERT INTO customer_locations (id, customer_id, company_id, address) VALUES (:i, :c, :t, '1 A St')"),
                {"i": loc_id, "c": cid, "t": TENANT},
            )
            log_audit_event_sync(
                db=db, tenant_id=None, user_id=USER, action="create_customer_location",
                entity_type="customer_location", entity_id=loc_id, details={"customer_id": cid},
            )
            expected = ["create_customer_location", "customer_updated"]
        db.commit()

        out = export_customer(customer_id=customer.id, request=request, user=user, db=db)

        assert sorted(e["action"] for e in out["data"]["audit_events"]) == expected
        exported = db.execute(
            select(AuditLog).where(AuditLog.action == "gdpr_customer_exported", AuditLog.entity_id == cid)
        ).scalars().all()
        assert len(exported) == 1
