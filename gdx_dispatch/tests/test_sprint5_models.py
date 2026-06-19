"""Sprint 5 / S92 — smoke tests for new tenant-plane models.

Covers JobDiagnosis, JobHazard, JobReceipt, TechLocation, VehicleInspection,
and the install_date / warranty_expires_on additions to CustomerEquipment.
Each test exercises insert + read path so a model-side typo or missing
column is caught at CI time rather than on lab walk.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from gdx_dispatch.models.tenant_models import (
    JobDiagnosis,
    JobHazard,
    JobReceipt,
    TechLocation,
    VehicleInspection,
)
from gdx_dispatch.modules.equipment.models import CustomerEquipment


def test_customer_equipment_warranty_field(tenant_db):
    cid = uuid4()
    eq = CustomerEquipment(
        customer_id=cid,
        equipment_type="opener",
        manufacturer="LiftMaster",
        model="8500W",
        serial_number="SN-1",
        installation_date=date(2024, 1, 15),
        warranty_expires_on=date(2026, 1, 15),
    )
    tenant_db.add(eq)
    tenant_db.commit()
    tenant_db.refresh(eq)
    assert eq.warranty_expires_on == date(2026, 1, 15)
    assert eq.installation_date == date(2024, 1, 15)


def test_job_diagnosis_insert(tenant_db):
    jid = uuid4()
    diag = JobDiagnosis(
        id=uuid4(),
        job_id=jid,
        service_type="broken_spring",
        data={"spring_type": "torsion", "wire_gauge": "0.250"},
        notes="snapped at the cone",
    )
    tenant_db.add(diag)
    tenant_db.commit()
    got = tenant_db.execute(
        select(JobDiagnosis).where(JobDiagnosis.job_id == jid)
    ).scalar_one()
    assert got.service_type == "broken_spring"
    assert got.data["spring_type"] == "torsion"


def test_job_hazard_sticky(tenant_db):
    jid = uuid4()
    cid = uuid4()
    h = JobHazard(
        id=uuid4(),
        job_id=jid,
        customer_id=cid,
        description="Aggressive dog in backyard",
        severity="high",
        applies_to_customer=True,
    )
    tenant_db.add(h)
    tenant_db.commit()
    got = tenant_db.execute(
        select(JobHazard).where(JobHazard.customer_id == cid)
    ).scalar_one()
    assert got.applies_to_customer is True
    assert got.severity == "high"


def test_job_receipt_amount_precision(tenant_db):
    jid = uuid4()
    r = JobReceipt(
        id=uuid4(),
        job_id=jid,
        vendor="Home Depot",
        amount=Decimal("47.99"),
    )
    tenant_db.add(r)
    tenant_db.commit()
    got = tenant_db.execute(select(JobReceipt).where(JobReceipt.job_id == jid)).scalar_one()
    assert got.amount == Decimal("47.99")
    assert got.vendor == "Home Depot"


def test_tech_location_insert(tenant_db):
    loc = TechLocation(
        id=uuid4(),
        user_id="user-1",
        technician_id="tech-1",
        lat=Decimal("40.7128000"),
        lng=Decimal("-74.0060000"),
        accuracy_m=Decimal("12.5"),
        recorded_at=datetime.now(UTC),
    )
    tenant_db.add(loc)
    tenant_db.commit()
    got = tenant_db.execute(
        select(TechLocation).where(TechLocation.user_id == "user-1")
    ).scalar_one()
    assert float(got.lat) == 40.7128
    assert float(got.lng) == -74.006


def test_vehicle_inspection_insert(tenant_db):
    insp = VehicleInspection(
        id=uuid4(),
        vehicle_label="Truck 7",
        technician_id="tech-1",
        inspection_type="pre_trip",
        odometer=84_321,
        fuel_cost=Decimal("63.45"),
        fuel_gallons=Decimal("18.500"),
        issues_found="left brake squeaking",
        inspection_at=datetime.now(UTC),
    )
    tenant_db.add(insp)
    tenant_db.commit()
    got = tenant_db.execute(
        select(VehicleInspection).where(VehicleInspection.technician_id == "tech-1")
    ).scalar_one()
    assert got.odometer == 84_321
    assert got.inspection_type == "pre_trip"
    assert got.fuel_cost == Decimal("63.45")
