"""core/job_site.py — the effective-jobsite precedence rule.

Pins desktop parity (JobDetailView's pickedLocation/customerAddress chain):
bound location → primary-or-first-created location → customer.address only
when the customer has ZERO location rows. And the D2 rule: a bound location
with no address is MISSING, never silently the customer's HQ.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.job_site import resolve_job_site, resolve_job_sites
from gdx_dispatch.models.tenant_models import Customer, CustomerLocation
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"


@pytest.fixture
def db():
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


def _customer(db, address="100 Billing Rd") -> Customer:
    c = Customer(id=uuid4(), name="Acme", address=address, company_id=TENANT)
    db.add(c)
    db.commit()
    return c


def _location(
    db,
    customer,
    *,
    label=None,
    address=None,
    access_notes=None,
    is_primary=False,
    created_offset_s=0,
    deleted=False,
):
    loc = CustomerLocation(
        id=str(uuid4()),
        customer_id=str(customer.id),
        label=label,
        address=address,
        access_notes=access_notes,
        is_primary=is_primary,
        company_id=TENANT,
        created_at=datetime.now(UTC) + timedelta(seconds=created_offset_s),
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(loc)
    db.commit()
    return loc


def test_bound_location_wins(db):
    c = _customer(db)
    loc = _location(db, c, label="Warehouse 3", address="9 Dock St", access_notes="gate 4411")
    site = resolve_job_site(db, "j1", loc.id, c.id)
    assert site.source == "location"
    assert site.address == "9 Dock St"
    assert site.label == "Warehouse 3"
    assert site.access_notes == "gate 4411"
    assert site.address_missing is False


def test_bound_location_without_address_is_missing_never_hq(db):
    """The D2 rule: label-only bound row => missing, NOT the customer address."""
    c = _customer(db, address="100 Billing Rd")
    loc = _location(db, c, label="Warehouse 3", address=None)
    site = resolve_job_site(db, "j1", loc.id, c.id)
    assert site.source == "location"
    assert site.address is None
    assert site.address_missing is True
    assert site.label == "Warehouse 3"


def test_bound_but_deleted_location_falls_back(db):
    c = _customer(db, address="100 Billing Rd")
    dead = _location(db, c, address="9 Dock St", deleted=True)
    site = resolve_job_site(db, "j1", dead.id, c.id)
    # Desktop treats a vanished binding as unbound: chain continues.
    assert site.source == "customer"
    assert site.address == "100 Billing Rd"


def test_primary_location_beats_customer_address(db):
    c = _customer(db, address="100 Billing Rd")
    _location(db, c, address="1 First St", created_offset_s=0)
    _location(db, c, address="2 Primary Ave", is_primary=True, created_offset_s=1)
    site = resolve_job_site(db, "j1", None, c.id)
    assert site.source == "customer_location"
    assert site.address == "2 Primary Ave"


def test_no_primary_uses_first_created(db):
    """is_primary is nullable/default-false; 'none primary' is a normal state."""
    c = _customer(db, address="100 Billing Rd")
    _location(db, c, address="1 First St", created_offset_s=0)
    _location(db, c, address="2 Second St", created_offset_s=1)
    site = resolve_job_site(db, "j1", None, c.id)
    assert site.address == "1 First St"


def test_primary_without_address_falls_to_first_created(db):
    """Desktop's `find(primary)?.address || [0].address` chain, exactly."""
    c = _customer(db)
    _location(db, c, address="1 First St", access_notes=None, created_offset_s=0)
    _location(db, c, address=None, access_notes="ring twice", is_primary=True, created_offset_s=1)
    site = resolve_job_site(db, "j1", None, c.id)
    assert site.address == "1 First St"
    # Access-notes chain is independent: the primary still supplies them.
    assert site.access_notes == "ring twice"


def test_locations_exist_but_no_address_does_not_use_customer(db):
    """Desktop parity: customer.address is only reached at ZERO locations."""
    c = _customer(db, address="100 Billing Rd")
    _location(db, c, label="Site A", address=None)
    site = resolve_job_site(db, "j1", None, c.id)
    assert site.source == "customer_location"
    assert site.address is None
    assert site.address_missing is True


def test_zero_locations_uses_customer_address(db):
    c = _customer(db, address="100 Billing Rd")
    site = resolve_job_site(db, "j1", None, c.id)
    assert site.source == "customer"
    assert site.address == "100 Billing Rd"
    assert site.address_missing is False


def test_customer_with_no_address_is_not_missing_state(db):
    """Nothing on file is 'no data', not the site-specific missing flag."""
    c = _customer(db, address=None)
    site = resolve_job_site(db, "j1", None, c.id)
    assert site.source == "customer"
    assert site.address is None
    assert site.address_missing is False


def test_no_customer(db):
    site = resolve_job_site(db, "j1", None, None)
    assert site.source == "none"
    assert site.address is None


def test_batch_mixed_and_uuid_inputs(db):
    """One batch call resolves heterogeneous rows; UUID vs str never matters."""
    c1 = _customer(db, address="100 Billing Rd")
    loc = _location(db, c1, address="9 Dock St")
    c2 = _customer(db, address="200 Other Rd")
    sites = resolve_job_sites(
        db,
        [
            (uuid4(), loc.id, c1.id),   # bound
            ("j2", None, str(c2.id)),   # customer fallback, str ids
            ("j3", None, None),          # no customer
        ],
    )
    assert len(sites) == 3
    by_source = sorted(s.source for s in sites.values())
    assert by_source == ["customer", "location", "none"]


def test_batch_empty_input_returns_empty(db):
    assert resolve_job_sites(db, []) == {}


def test_undashed_hex_customer_id_still_resolves(db):
    """Raw-SQL rows on SQLite hand back 32-char undashed customer ids; the
    resolver must canonicalize or every fallback lookup silently misses
    (regression: test_mobile_api.py::test_get_job_detail_success)."""
    c = _customer(db, address="100 Billing Rd")
    site = resolve_job_site(db, "j1", None, c.id.hex)
    assert site.address == "100 Billing Rd"
    _location(db, c, address="9 Dock St")
    site2 = resolve_job_site(db, "j2", None, c.id.hex)
    assert site2.address == "9 Dock St"
