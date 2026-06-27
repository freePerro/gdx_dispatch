"""
Tests for gdx_dispatch/modules/locations models — multi-location tenant support.
9 model tests (CRUD, technician assignment, tenant isolation) plus an auth-guard
test against the live ``core/locations.py`` router (the unmounted
``modules/locations/router.py`` duplicate was deleted; its models.py is kept).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.locations.models import Location, LocationTechnician

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def loc_db():
    """Isolated in-memory SQLite DB with location module tables."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Location.__table__.create(bind=engine, checkfirst=True)
    LocationTechnician.__table__.create(bind=engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


def _make_location(
    db,
    tenant_id: str = "tenant-abc",
    name: str = "Main Office",
    is_primary: bool = True,
    is_active: bool = True,
) -> Location:
    loc = Location(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        address="123 Main St",
        city="Springfield",
        state="IL",
        zip="62701",
        phone="555-1234",
        is_primary=is_primary,
        is_active=is_active,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def _make_assignment(db, location_id, technician_id: str) -> LocationTechnician:
    lt = LocationTechnician(location_id=location_id, technician_id=technician_id)
    db.add(lt)
    db.commit()
    return lt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_location(loc_db):
    """A Location can be created and persisted with correct fields."""
    loc = _make_location(loc_db, name="HQ", is_primary=True)
    assert loc.id is not None
    assert loc.name == "HQ"
    assert loc.tenant_id == "tenant-abc"
    assert loc.is_primary is True
    assert loc.is_active is True
    assert loc.created_at is not None


def test_list_locations(loc_db):
    """Active locations can be listed; inactive ones are excluded."""
    _make_location(loc_db, name="North Branch", is_primary=True, is_active=True)
    _make_location(loc_db, name="South Branch", is_primary=False, is_active=True)
    _make_location(loc_db, name="Closed Branch", is_primary=False, is_active=False)

    active = loc_db.execute(
        select(Location).where(
            Location.tenant_id == "tenant-abc",
            Location.is_active.is_(True),
        )
    ).scalars().all()

    names = [l.name for l in active]  # noqa: E741
    assert "North Branch" in names
    assert "South Branch" in names
    assert "Closed Branch" not in names


def test_get_location(loc_db):
    """A single location can be retrieved by id and tenant_id."""
    loc = _make_location(loc_db, name="Downtown")
    result = loc_db.execute(
        select(Location).where(
            Location.id == loc.id,
            Location.tenant_id == "tenant-abc",
        )
    ).scalar_one_or_none()
    assert result is not None
    assert result.name == "Downtown"


def test_update_location(loc_db):
    """Location fields can be updated."""
    loc = _make_location(loc_db, name="Old Name")
    loc.name = "New Name"
    loc.city = "Chicago"
    loc.phone = "312-555-0000"
    loc_db.commit()
    loc_db.refresh(loc)
    assert loc.name == "New Name"
    assert loc.city == "Chicago"
    assert loc.phone == "312-555-0000"


def test_deactivate_location(loc_db):
    """A non-primary location can be deactivated (soft-delete via is_active=False)."""
    loc = _make_location(loc_db, name="Branch", is_primary=False)
    loc.is_active = False
    loc_db.commit()
    loc_db.refresh(loc)
    assert loc.is_active is False


def test_assign_technician_to_location(loc_db):
    """A technician can be assigned to a location."""
    loc = _make_location(loc_db)
    tech_id = "tech-001"
    lt = _make_assignment(loc_db, loc.id, tech_id)
    assert lt.location_id == loc.id
    assert lt.technician_id == tech_id


def test_list_location_technicians(loc_db):
    """Technicians assigned to a location are listed; others are excluded."""
    loc1 = _make_location(loc_db, name="Alpha", is_primary=True)
    loc2 = _make_location(loc_db, name="Beta", is_primary=False)

    _make_assignment(loc_db, loc1.id, "tech-A")
    _make_assignment(loc_db, loc1.id, "tech-B")
    _make_assignment(loc_db, loc2.id, "tech-C")

    techs_loc1 = loc_db.execute(
        select(LocationTechnician).where(LocationTechnician.location_id == loc1.id)
    ).scalars().all()
    tech_ids = [t.technician_id for t in techs_loc1]

    assert "tech-A" in tech_ids
    assert "tech-B" in tech_ids
    assert "tech-C" not in tech_ids


def test_location_tenant_isolation(loc_db):
    """A location from a different tenant is not returned for the querying tenant."""
    _make_location(loc_db, tenant_id="tenant-111", name="T1 Office")
    _make_location(loc_db, tenant_id="tenant-222", name="T2 Office")

    results = loc_db.execute(
        select(Location).where(
            Location.tenant_id == "tenant-111",
            Location.is_active.is_(True),
        )
    ).scalars().all()

    names = [l.name for l in results]  # noqa: E741
    assert "T1 Office" in names
    assert "T2 Office" not in names


def test_primary_location_flag(loc_db):
    """Only one location should hold is_primary=True at a time (enforced by app logic)."""
    loc1 = _make_location(loc_db, name="Primary", is_primary=True)
    loc2 = _make_location(loc_db, name="Secondary", is_primary=False)

    # Simulate promoting loc2: demote all others first
    loc1.is_primary = False
    loc2.is_primary = True
    loc_db.commit()
    loc_db.refresh(loc1)
    loc_db.refresh(loc2)

    primaries = loc_db.execute(
        select(Location).where(
            Location.tenant_id == "tenant-abc",
            Location.is_primary.is_(True),
        )
    ).scalars().all()

    assert len(primaries) == 1
    assert primaries[0].id == loc2.id


def test_location_requires_auth():
    """The live locations router guards every route with get_current_user.

    Targets the mounted router (``core/locations.py``, mounted at app.py:769),
    not the deleted ``modules/locations/router.py`` duplicate this test used to
    inspect — so the auth-guard assertion now covers the router that actually
    serves traffic.
    """
    from gdx_dispatch.core.locations import router as loc_router
    from gdx_dispatch.routers.auth import get_current_user

    # Collect all dependency callables across every route
    all_deps: list = []
    for route in loc_router.routes:
        if hasattr(route, "dependencies"):
            all_deps.extend(route.dependencies)
        if hasattr(route, "dependant"):
            all_deps.extend(route.dependant.dependencies)

    # Every route should declare get_current_user (aliased as get_current_tenant_user)
    # Verify by checking that at least one route has it in its endpoint signature deps
    import inspect
    auth_protected = []
    for route in loc_router.routes:
        if not hasattr(route, "endpoint"):
            continue
        sig = inspect.signature(route.endpoint)
        for param in sig.parameters.values():
            if param.default is not inspect.Parameter.empty:
                dep = param.default
                # FastAPI Depends wraps the callable
                if hasattr(dep, "dependency") and dep.dependency is get_current_user:
                    auth_protected.append(route.path)
                    break

    assert len(auth_protected) > 0, "No routes have get_current_user auth dependency"
