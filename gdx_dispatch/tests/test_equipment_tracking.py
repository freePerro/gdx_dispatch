"""gdx_dispatch/tests/test_equipment_tracking.py — Equipment tracking module tests.

8 tests covering: create, get, service log, service history, update, soft delete,
list with customer filter, and tenant isolation.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.modules.equipment.models import CustomerEquipment, EquipmentServiceHistory
from gdx_dispatch.tests.conftest import make_fresh_db

# ---------------------------------------------------------------------------
# Tests 1-7 use the shared tenant_db fixture from conftest.py
# ---------------------------------------------------------------------------

def test_create_equipment(tenant_db):
    """Create a CustomerEquipment record and verify all fields are stored."""
    cid = uuid4()
    eq = CustomerEquipment(
        customer_id=cid,
        equipment_type="garage_door",
        manufacturer="LiftMaster",
        model="8500W",
        serial_number="SN-001",
    )
    tenant_db.add(eq)
    tenant_db.commit()
    tenant_db.refresh(eq)

    assert eq.id is not None
    assert eq.customer_id == cid
    assert eq.equipment_type == "garage_door"
    assert eq.manufacturer == "LiftMaster"
    assert eq.model == "8500W"
    assert eq.serial_number == "SN-001"
    assert eq.deleted_at is None


def test_get_equipment(tenant_db):
    """Query back a created equipment record by id, filtered for non-deleted."""
    cid = uuid4()
    eq = CustomerEquipment(customer_id=cid, equipment_type="opener")
    tenant_db.add(eq)
    tenant_db.commit()
    tenant_db.refresh(eq)

    result = tenant_db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.id == eq.id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    assert result is not None
    assert result.id == eq.id
    assert result.equipment_type == "opener"
    assert result.deleted_at is None


def test_service_log(tenant_db):
    """Log a service history record linked to equipment, verify FK and fields."""
    cid = uuid4()
    eq = CustomerEquipment(customer_id=cid, equipment_type="gate")
    tenant_db.add(eq)
    tenant_db.commit()
    tenant_db.refresh(eq)

    svc = EquipmentServiceHistory(
        equipment_id=eq.id,
        service_type="maintenance",
        technician_id="tech-1",
        notes="Lubricated springs and tracks",
        parts_used=[{"sku": "SPRING-01", "qty": 2}],
    )
    tenant_db.add(svc)
    tenant_db.commit()
    tenant_db.refresh(svc)

    assert svc.id is not None
    assert svc.equipment_id == eq.id
    assert svc.service_type == "maintenance"
    assert svc.technician_id == "tech-1"
    assert svc.notes == "Lubricated springs and tracks"
    assert svc.parts_used == [{"sku": "SPRING-01", "qty": 2}]


def test_service_history(tenant_db):
    """Create equipment with 2 service records, assert both are returned."""
    cid = uuid4()
    eq = CustomerEquipment(customer_id=cid, equipment_type="opener")
    tenant_db.add(eq)
    tenant_db.commit()
    tenant_db.refresh(eq)

    for i in range(2):
        tenant_db.add(
            EquipmentServiceHistory(
                equipment_id=eq.id,
                service_type="inspection",
                technician_id=f"tech-{i}",
            )
        )
    tenant_db.commit()

    rows = tenant_db.execute(
        select(EquipmentServiceHistory).where(
            EquipmentServiceHistory.equipment_id == eq.id
        )
    ).scalars().all()

    assert len(rows) == 2
    assert all(r.equipment_id == eq.id for r in rows)


def test_update_equipment(tenant_db):
    """Update manufacturer field and verify the change persists."""
    cid = uuid4()
    eq = CustomerEquipment(
        customer_id=cid,
        equipment_type="garage_door",
        manufacturer="Old Brand",
    )
    tenant_db.add(eq)
    tenant_db.commit()
    tenant_db.refresh(eq)

    eq.manufacturer = "New Brand"
    tenant_db.commit()
    tenant_db.refresh(eq)

    updated = tenant_db.execute(
        select(CustomerEquipment).where(CustomerEquipment.id == eq.id)
    ).scalar_one()

    assert updated.manufacturer == "New Brand"


def test_delete_equipment(tenant_db):
    """Soft-delete equipment by setting deleted_at; filtered query returns nothing."""
    cid = uuid4()
    eq = CustomerEquipment(customer_id=cid, equipment_type="other")
    tenant_db.add(eq)
    tenant_db.commit()
    tenant_db.refresh(eq)

    eq.deleted_at = utcnow()
    tenant_db.commit()

    result = tenant_db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.id == eq.id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    assert result is None

    # But the record still exists without the filter
    raw = tenant_db.execute(
        select(CustomerEquipment).where(CustomerEquipment.id == eq.id)
    ).scalar_one_or_none()
    assert raw is not None
    assert raw.deleted_at is not None


def test_list_with_customer_filter(tenant_db):
    """Create equipment for two customers; filter by customer_id returns only that customer's."""
    cid_a = uuid4()
    cid_b = uuid4()

    for _ in range(2):
        tenant_db.add(CustomerEquipment(customer_id=cid_a, equipment_type="garage_door"))
    tenant_db.add(CustomerEquipment(customer_id=cid_b, equipment_type="opener"))
    tenant_db.commit()

    rows_a = tenant_db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.customer_id == cid_a,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalars().all()

    rows_b = tenant_db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.customer_id == cid_b,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalars().all()

    assert len(rows_a) == 2
    assert len(rows_b) == 1
    assert all(r.customer_id == cid_a for r in rows_a)
    assert rows_b[0].customer_id == cid_b


def test_tenant_isolation():
    """Equipment created in one tenant DB is not visible in a separate tenant DB."""
    engine_a = make_fresh_db()
    engine_b = make_fresh_db()
    db_a = sessionmaker(bind=engine_a, autoflush=False, autocommit=False)()
    db_b = sessionmaker(bind=engine_b, autoflush=False, autocommit=False)()

    try:
        cid = uuid4()
        eq_a = CustomerEquipment(customer_id=cid, equipment_type="garage_door")
        db_a.add(eq_a)
        db_a.commit()

        # Tenant B's DB should have no equipment
        rows_b = db_b.execute(select(CustomerEquipment)).scalars().all()
        assert len(rows_b) == 0

        # Tenant A's DB should have exactly one
        rows_a = db_a.execute(select(CustomerEquipment)).scalars().all()
        assert len(rows_a) == 1
    finally:
        db_a.close()
        db_b.close()
        engine_a.dispose()
        engine_b.dispose()
