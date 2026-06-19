"""
test_27_service_areas.py — Service Area and Coverage Zone management tests.

Tests: create, list, coverage match/no-match, update, deactivate,
tenant isolation, and technician assignment.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.service_areas.models import ServiceArea, ServiceAreaTechnician


@pytest.fixture
def sa_db():
    """Isolated in-memory SQLite DB with service area tables."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    ServiceArea.__table__.create(bind=engine, checkfirst=True)
    ServiceAreaTechnician.__table__.create(bind=engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


def _make_area(db, **kwargs) -> ServiceArea:
    defaults = dict(
        name="Downtown Zone",
        zip_codes=["90210", "90211", "90212"],
        radius_miles=15.0,
        center_lat=34.0522,
        center_lng=-118.2437,
        is_active=True,
        tenant_id="tenant-A",
    )
    defaults.update(kwargs)
    area = ServiceArea(**defaults)
    db.add(area)
    db.commit()
    db.refresh(area)
    return area


# --- Test 1: Create service area ---

def test_create_service_area(sa_db):
    area = _make_area(sa_db)
    assert area.id is not None
    assert area.name == "Downtown Zone"
    assert area.zip_codes == ["90210", "90211", "90212"]
    assert area.radius_miles == 15.0
    assert area.center_lat == 34.0522
    assert area.center_lng == -118.2437
    assert area.is_active is True
    assert area.created_at is not None


# --- Test 2: List service areas ---

def test_list_service_areas(sa_db):
    _make_area(sa_db, name="Zone A", tenant_id="tenant-A")
    _make_area(sa_db, name="Zone B", tenant_id="tenant-A")
    _make_area(sa_db, name="Zone C", is_active=False, tenant_id="tenant-A")

    active = list(
        sa_db.execute(
            select(ServiceArea).where(ServiceArea.is_active.is_(True))
        ).scalars().all()
    )
    assert len(active) == 2
    names = {a.name for a in active}
    assert "Zone A" in names
    assert "Zone B" in names
    assert "Zone C" not in names


# --- Test 3: Coverage check — match ---

def test_check_coverage_match(sa_db):
    area = _make_area(sa_db, zip_codes=["30301", "30302", "30303"])
    areas = list(sa_db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True))).scalars().all())

    zip_to_check = "30302"
    matched = None
    for a in areas:
        if zip_to_check in (a.zip_codes or []):
            matched = a
            break

    assert matched is not None
    assert matched.id == area.id
    assert matched.name == area.name


# --- Test 4: Coverage check — no match ---

def test_check_coverage_no_match(sa_db):
    _make_area(sa_db, zip_codes=["10001", "10002"])
    areas = list(sa_db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True))).scalars().all())

    zip_to_check = "99999"
    matched = None
    for a in areas:
        if zip_to_check in (a.zip_codes or []):
            matched = a
            break

    assert matched is None


# --- Test 5: Update service area ---

def test_update_service_area(sa_db):
    area = _make_area(sa_db)
    area.name = "Updated Zone"
    area.zip_codes = ["90210", "90213", "90214", "90215"]
    area.radius_miles = 20.0
    sa_db.commit()
    sa_db.refresh(area)

    assert area.name == "Updated Zone"
    assert len(area.zip_codes) == 4
    assert "90215" in area.zip_codes
    assert area.radius_miles == 20.0


# --- Test 6: Deactivate service area ---

def test_deactivate_service_area(sa_db):
    area = _make_area(sa_db)
    area.is_active = False
    sa_db.commit()

    # Should not appear in active query
    active = sa_db.execute(
        select(ServiceArea).where(
            ServiceArea.id == area.id,
            ServiceArea.is_active.is_(True),
        )
    ).scalar_one_or_none()
    assert active is None

    # Record still exists
    raw = sa_db.execute(
        select(ServiceArea).where(ServiceArea.id == area.id)
    ).scalar_one_or_none()
    assert raw is not None
    assert raw.is_active is False

    # Deactivated areas do not appear in coverage checks
    areas = list(sa_db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True))).scalars().all())
    ids = {a.id for a in areas}
    assert area.id not in ids


# --- Test 7: Tenant isolation ---

def test_service_area_tenant_isolation(sa_db):
    area_a = _make_area(sa_db, name="Zone for A", tenant_id="tenant-A")
    area_b = _make_area(sa_db, name="Zone for B", tenant_id="tenant-B")

    results_a = list(
        sa_db.execute(
            select(ServiceArea).where(
                ServiceArea.tenant_id == "tenant-A",
                ServiceArea.is_active.is_(True),
            )
        ).scalars().all()
    )
    ids_a = {a.id for a in results_a}

    assert area_a.id in ids_a
    assert area_b.id not in ids_a

    results_b = list(
        sa_db.execute(
            select(ServiceArea).where(
                ServiceArea.tenant_id == "tenant-B",
                ServiceArea.is_active.is_(True),
            )
        ).scalars().all()
    )
    ids_b = {a.id for a in results_b}
    assert area_b.id in ids_b
    assert area_a.id not in ids_b


# --- Test 8: Assign technician to area ---

def test_assign_technician_to_area(sa_db):
    area = _make_area(sa_db)
    tech_id = uuid4()

    assignment = ServiceAreaTechnician(
        service_area_id=area.id,
        technician_id=tech_id,
    )
    sa_db.add(assignment)
    sa_db.commit()
    sa_db.refresh(assignment)

    assert assignment.id is not None
    assert assignment.service_area_id == area.id
    assert assignment.technician_id == tech_id
    assert assignment.assigned_at is not None


# --- Test 9: Multiple technicians per area ---

def test_multiple_technicians_per_area(sa_db):
    area = _make_area(sa_db)
    tech_ids = [uuid4() for _ in range(3)]

    for tid in tech_ids:
        a = ServiceAreaTechnician(service_area_id=area.id, technician_id=tid)
        sa_db.add(a)
    sa_db.commit()

    assignments = list(
        sa_db.execute(
            select(ServiceAreaTechnician).where(
                ServiceAreaTechnician.service_area_id == area.id
            )
        ).scalars().all()
    )
    assigned_tech_ids = {a.technician_id for a in assignments}
    assert len(assignments) == 3
    for tid in tech_ids:
        assert tid in assigned_tech_ids


# --- Test 10: Coverage check skips inactive areas ---

def test_coverage_check_skips_inactive(sa_db):
    # Active area covers 77001
    _make_area(sa_db, name="Active Zone", zip_codes=["77001", "77002"], is_active=True)
    # Inactive area also claims 77001 — should be ignored
    _make_area(sa_db, name="Inactive Zone", zip_codes=["77001"], is_active=False)

    active_areas = list(
        sa_db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True))).scalars().all()
    )
    matched_names = [a.name for a in active_areas if "77001" in (a.zip_codes or [])]
    assert matched_names == ["Active Zone"]
