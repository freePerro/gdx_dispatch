"""
test_16_contractors.py — Contractor/Subcontractor module tests.
Tests CRUD, job assignment, assignment completion, insurance expiry alert,
and tenant isolation.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.contractors.models import Contractor, ContractorAssignment


@pytest.fixture
def contractor_db():
    """Isolated in-memory SQLite DB with contractor tables."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Contractor.__table__.create(bind=engine, checkfirst=True)
    ContractorAssignment.__table__.create(bind=engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield db
    db.close()
    engine.dispose()


def _make_contractor(db, **kwargs) -> Contractor:
    defaults = dict(
        name="John Smith",
        company_name="Smith Doors LLC",
        phone="555-0100",
        email="john@smithdoors.com",
        specialty=["spring_repair", "installation"],
        license_number="LIC-12345",
        insurance_expiry=date.today() + timedelta(days=90),
        hourly_rate=75.00,
        is_active=True,
        tenant_id="tenant-A",
    )
    defaults.update(kwargs)
    c = Contractor(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# --- Test 1: Create contractor ---

def test_create_contractor(contractor_db):
    c = _make_contractor(contractor_db)
    assert c.id is not None
    assert c.name == "John Smith"
    assert c.hourly_rate == 75.00
    assert c.is_active is True
    assert c.deleted_at is None


# --- Test 2: Read contractor ---

def test_read_contractor(contractor_db):
    c = _make_contractor(contractor_db, name="Jane Doe")
    from sqlalchemy import select
    fetched = contractor_db.execute(
        select(Contractor).where(Contractor.id == c.id)
    ).scalar_one_or_none()
    assert fetched is not None
    assert fetched.name == "Jane Doe"


# --- Test 3: Update contractor ---

def test_update_contractor(contractor_db):
    c = _make_contractor(contractor_db)
    c.phone = "555-9999"
    c.hourly_rate = 85.00
    contractor_db.commit()
    contractor_db.refresh(c)
    assert c.phone == "555-9999"
    assert c.hourly_rate == 85.00


# --- Test 4: Soft delete contractor ---

def test_soft_delete_contractor(contractor_db):
    from datetime import datetime, timezone

    from sqlalchemy import select
    c = _make_contractor(contractor_db)
    c.deleted_at = datetime.now(timezone.utc)
    contractor_db.commit()

    # Should not appear in active query
    result = contractor_db.execute(
        select(Contractor).where(
            Contractor.id == c.id,
            Contractor.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    assert result is None

    # But still exists in DB
    raw = contractor_db.execute(
        select(Contractor).where(Contractor.id == c.id)
    ).scalar_one_or_none()
    assert raw is not None
    assert raw.deleted_at is not None


# --- Test 5: Assign contractor to a job ---

def test_assign_contractor_to_job(contractor_db):
    c = _make_contractor(contractor_db)
    job_id = uuid4()
    assignment = ContractorAssignment(
        contractor_id=c.id,
        job_id=job_id,
        scheduled_date=date.today() + timedelta(days=3),
        tenant_id="tenant-A",
    )
    contractor_db.add(assignment)
    contractor_db.commit()
    contractor_db.refresh(assignment)
    assert assignment.id is not None
    assert assignment.status == "scheduled"
    assert assignment.contractor_id == c.id
    assert assignment.job_id == job_id


# --- Test 6: Complete assignment with cost calculation ---

def test_complete_assignment_cost_calculation(contractor_db):
    c = _make_contractor(contractor_db, hourly_rate=80.00)
    assignment = ContractorAssignment(
        contractor_id=c.id,
        scheduled_date=date.today(),
        tenant_id="tenant-A",
    )
    contractor_db.add(assignment)
    contractor_db.commit()
    contractor_db.refresh(assignment)

    # Simulate complete
    hours = 3.5
    assignment.hours_worked = hours
    assignment.status = "completed"
    assignment.total_cost = float(c.hourly_rate) * hours
    contractor_db.commit()
    contractor_db.refresh(assignment)

    assert assignment.status == "completed"
    assert float(assignment.hours_worked) == 3.5
    assert abs(float(assignment.total_cost) - 280.0) < 0.01


# --- Test 7: Insurance expiry alert (expiring within 60 days) ---

def test_insurance_expiry_alert(contractor_db):
    from sqlalchemy import select
    # Expiring soon — should appear in alert
    c_soon = _make_contractor(
        contractor_db,
        name="Soon Expiry",
        insurance_expiry=date.today() + timedelta(days=30),
        tenant_id="tenant-A",
    )
    # Not expiring soon — 120 days out
    c_ok = _make_contractor(
        contractor_db,
        name="OK Expiry",
        insurance_expiry=date.today() + timedelta(days=120),
        tenant_id="tenant-A",
    )
    # Already expired — should NOT appear (before today)
    c_expired = _make_contractor(
        contractor_db,
        name="Already Expired",
        insurance_expiry=date.today() - timedelta(days=5),
        tenant_id="tenant-A",
    )

    today = date.today()
    cutoff = today + timedelta(days=60)
    results = contractor_db.execute(
        select(Contractor).where(
            Contractor.deleted_at.is_(None),
            Contractor.insurance_expiry.isnot(None),
            Contractor.insurance_expiry >= today,
            Contractor.insurance_expiry <= cutoff,
        )
    ).scalars().all()

    ids = {r.id for r in results}
    assert c_soon.id in ids
    assert c_ok.id not in ids
    assert c_expired.id not in ids


# --- Test 8: List only active non-deleted contractors ---

def test_list_active_contractors(contractor_db):
    from datetime import datetime, timezone

    from sqlalchemy import select
    c1 = _make_contractor(contractor_db, name="Active One", tenant_id="tenant-A")
    c2 = _make_contractor(contractor_db, name="Inactive", is_active=False, tenant_id="tenant-A")
    c3 = _make_contractor(contractor_db, name="Active Deleted", tenant_id="tenant-A")
    c3.deleted_at = datetime.now(timezone.utc)
    contractor_db.commit()

    results = contractor_db.execute(
        select(Contractor).where(
            Contractor.deleted_at.is_(None),
            Contractor.is_active.is_(True),
            Contractor.tenant_id == "tenant-A",
        )
    ).scalars().all()

    ids = {r.id for r in results}
    assert c1.id in ids
    assert c2.id not in ids
    assert c3.id not in ids


# --- Test 9: Tenant isolation ---

def test_tenant_isolation(contractor_db):
    from sqlalchemy import select
    c_a = _make_contractor(contractor_db, name="Tenant A Contractor", tenant_id="tenant-A")
    c_b = _make_contractor(contractor_db, name="Tenant B Contractor", tenant_id="tenant-B")

    results_a = contractor_db.execute(
        select(Contractor).where(
            Contractor.tenant_id == "tenant-A",
            Contractor.deleted_at.is_(None),
        )
    ).scalars().all()
    ids_a = {r.id for r in results_a}

    assert c_a.id in ids_a
    assert c_b.id not in ids_a


# --- Test 10: Multiple assignments per contractor ---

def test_multiple_assignments(contractor_db):
    from sqlalchemy import select
    c = _make_contractor(contractor_db)
    for i in range(3):
        a = ContractorAssignment(
            contractor_id=c.id,
            scheduled_date=date.today() + timedelta(days=i),
            tenant_id="tenant-A",
        )
        contractor_db.add(a)
    contractor_db.commit()

    assignments = contractor_db.execute(
        select(ContractorAssignment).where(
            ContractorAssignment.contractor_id == c.id
        )
    ).scalars().all()
    assert len(assignments) == 3
    assert all(a.status == "scheduled" for a in assignments)
