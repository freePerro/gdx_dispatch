"""The Customer page's location dialog keeps what it collects (#683).

``CustomerDetailView.saveLocation`` sends label, address, city, state, zip,
access notes and is_primary, then toasts "Saved". Until #683 the request
models declared no city/state/zip and the page sent ``notes`` where the API
reads ``access_notes`` — all four were dropped on create AND edit, so the
gate codes a tech needs never reached the job page.

Endpoint-level through the real router, with the page's exact JSON, and each
test reads the ROW back — the response alone could echo a value the INSERT
never wrote.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Customer, CustomerLocation
from gdx_dispatch.routers import customers as customers_router
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
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER, "tenant_id": TENANT, "role": "admin",
    }
    app.dependency_overrides[require_module("customers")] = lambda: True

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


def _row(db, location_id: str) -> dict:
    db.expire_all()
    return dict(
        db.execute(
            text(
                "SELECT address, city, state, zip, access_notes, lat, lng "
                "FROM customer_locations WHERE id = :i"
            ),
            {"i": location_id},
        ).mappings().one()
    )


# What CustomerDetailView.saveLocation sends, key for key.
def _dialog_payload(**overrides) -> dict:
    body = {
        "label": "Lake house",
        "address": "12 Shore Ln",
        "city": "Brainerd",
        "state": "MN",
        "zip": "56401",
        "access_notes": "Gate 4471, dog in yard",
        "is_primary": False,
    }
    body.update(overrides)
    return body


def test_create_keeps_city_state_zip_and_access_notes(client_and_db):
    client, db = client_and_db
    c = _customer(db)

    r = client.post(f"/api/customers/{c.id}/locations", json=_dialog_payload())

    assert r.status_code == 201, r.text
    out = r.json()
    assert (out["city"], out["state"], out["zip"]) == ("Brainerd", "MN", "56401")
    assert out["access_notes"] == "Gate 4471, dog in yard"
    row = _row(db, out["id"])
    assert (row["city"], row["state"], row["zip"]) == ("Brainerd", "MN", "56401")
    assert row["access_notes"] == "Gate 4471, dog in yard"


def test_list_returns_the_saved_fields_the_dialog_reopens_with(client_and_db):
    client, db = client_and_db
    c = _customer(db)
    client.post(f"/api/customers/{c.id}/locations", json=_dialog_payload())

    r = client.get(f"/api/customers/{c.id}/locations")

    assert r.status_code == 200, r.text
    (loc,) = r.json()
    assert (loc["city"], loc["state"], loc["zip"], loc["access_notes"]) == (
        "Brainerd", "MN", "56401", "Gate 4471, dog in yard",
    )


def test_edit_keeps_city_state_zip_and_access_notes(client_and_db):
    client, db = client_and_db
    c = _customer(db)
    loc_id = client.post(
        f"/api/customers/{c.id}/locations",
        json=_dialog_payload(city="", state="", zip="", access_notes=""),
    ).json()["id"]

    # The dialog re-sends the whole object, same address.
    r = client.patch(f"/api/customers/{c.id}/locations/{loc_id}", json=_dialog_payload(zip="56468"))

    assert r.status_code == 200, r.text
    row = _row(db, loc_id)
    assert (row["city"], row["state"], row["zip"]) == ("Brainerd", "MN", "56468")
    assert row["access_notes"] == "Gate 4471, dog in yard"


def test_blank_fields_store_null_not_empty_string(client_and_db):
    client, db = client_and_db
    c = _customer(db)

    r = client.post(
        f"/api/customers/{c.id}/locations",
        json=_dialog_payload(city="", state="", zip="", access_notes=""),
    )

    assert r.status_code == 201, r.text
    row = _row(db, r.json()["id"])
    assert (row["city"], row["state"], row["zip"], row["access_notes"]) == (None, None, None, None)


def _duluth_site(db, c) -> CustomerLocation:
    loc = CustomerLocation(
        id=str(uuid4()), customer_id=str(c.id), label="Old", address="1 Old Rd",
        city="Duluth", state="MN", zip="55802", company_id=TENANT, lat=46.78, lng=-92.1,
    )
    db.add(loc)
    db.commit()
    return loc


def test_move_with_an_edited_split_keeps_all_of_it_and_drops_the_pin(client_and_db):
    """The user moved the site and retyped city and zip. The unchanged "MN"
    belongs to the new address too, so all three are kept; the geocode goes.

    Each column must also be assigned once in the UPDATE. Postgres rejects a
    second assignment outright (verified against postgres:16 for #683); SQLite
    accepts it, so here the guard's removal shows up as the wrong value."""
    client, db = client_and_db
    c = _customer(db)
    loc = _duluth_site(db, c)

    r = client.patch(
        f"/api/customers/{c.id}/locations/{loc.id}",
        json=_dialog_payload(address="12 Shore Ln", city="Brainerd", state="MN", zip="56401"),
    )

    assert r.status_code == 200, r.text
    row = _row(db, loc.id)
    assert row["address"] == "12 Shore Ln"
    assert (row["city"], row["state"], row["zip"]) == ("Brainerd", "MN", "56401")
    assert row["lat"] is None and row["lng"] is None


def test_move_retyping_only_the_street_clears_the_prefilled_old_split(client_and_db):
    """The dialog re-sends city/state/zip pre-filled from the row. Retyping
    only the Address box must not leave the old town under the new street."""
    client, db = client_and_db
    c = _customer(db)
    loc = _duluth_site(db, c)

    r = client.patch(
        f"/api/customers/{c.id}/locations/{loc.id}",
        json=_dialog_payload(address="12 Shore Ln", city="Duluth", state="MN", zip="55802"),
    )

    assert r.status_code == 200, r.text
    row = _row(db, loc.id)
    assert row["address"] == "12 Shore Ln"
    assert (row["city"], row["state"], row["zip"], row["lat"]) == (None, None, None, None)


def test_address_only_change_still_clears_the_stale_split(client_and_db):
    """The pre-#683 contract for callers that send just an address."""
    client, db = client_and_db
    c = _customer(db)
    loc = _duluth_site(db, c)

    r = client.patch(f"/api/customers/{c.id}/locations/{loc.id}", json={"address": "12 Shore Ln"})

    assert r.status_code == 200, r.text
    row = _row(db, loc.id)
    assert (row["city"], row["state"], row["zip"], row["lat"]) == (None, None, None, None)
