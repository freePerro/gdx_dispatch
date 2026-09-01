"""#452 — invariant #1 on the equipment surface.

`modules/equipment/router.py` had seven state-changing endpoints and zero
`log_audit_event()` calls. Since #451 repointed the frontend at PUT
/api/equipment/{id}, edits land for real again — with nothing recording who
changed a door's serial number, when, or from what.

These tests call the handlers directly (the same way the closeout tests do)
with a fake request and a user dict, and read the audit table back through
the ORM: one row per mutation, carrying the actor, the entity id and the
change. Counterfactual: remove any `_audit(...)` call and its test fails on
the row count.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from sqlalchemy import select
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.modules.equipment.models import CustomerEquipment, EquipmentServiceHistory
from gdx_dispatch.modules.equipment.router import (
    EquipmentCreate,
    EquipmentIn,
    EquipmentPatch,
    EquipmentUpdate,
    ServiceEventIn,
    ServiceLogIn,
    create_equipment,
    create_equipment_for_customer,
    delete_equipment,
    log_equipment_service,
    log_service_event,
    update_equipment,
    update_equipment_for_customer,
)

USER_ID = "user-equipment-auditor"
USER = {"sub": USER_ID, "role": "admin"}


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _audit_rows(db, action: str) -> list[AuditLog]:
    return list(db.execute(select(AuditLog).where(AuditLog.action == action)).scalars())


def _seed(db, **overrides) -> CustomerEquipment:
    row = CustomerEquipment(
        customer_id=overrides.pop("customer_id", uuid4()),
        equipment_type="garage_door",
        manufacturer="Acme",
        model="8500",
        serial_number="SN-BEFORE",
        **overrides,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_create_for_customer_writes_an_audit_row(tenant_db):
    cid = uuid4()
    row = create_equipment_for_customer(
        cid,
        EquipmentIn(equipment_type="opener", manufacturer="Acme", model="X1", serial_number="SN-1"),
        _request(),
        user=USER,
        db=tenant_db,
    )
    rows = _audit_rows(tenant_db, "equipment_created")
    assert len(rows) == 1
    assert rows[0].user_id == USER_ID
    assert rows[0].entity_type == "equipment"
    assert rows[0].entity_id == str(row.id)
    assert rows[0].details["customer_id"] == str(cid)
    assert rows[0].details["serial_number"] == "SN-1"


def test_tenant_wide_create_writes_an_audit_row(tenant_db):
    row = create_equipment(
        EquipmentCreate(customer_id=uuid4(), type="opener", make="Acme", serial_number="SN-2"),
        _request(),
        user=USER,
        db=tenant_db,
    )
    rows = _audit_rows(tenant_db, "equipment_created")
    assert len(rows) == 1
    assert rows[0].entity_id == str(row.id)
    assert rows[0].user_id == USER_ID


def test_update_records_before_and_after(tenant_db):
    row = _seed(tenant_db)
    update_equipment(
        row.id,
        EquipmentUpdate(serial_number="SN-AFTER", notes="relabelled"),
        _request(),
        user=USER,
        db=tenant_db,
    )
    rows = _audit_rows(tenant_db, "equipment_updated")
    assert len(rows) == 1
    assert rows[0].entity_id == str(row.id)
    assert rows[0].user_id == USER_ID
    assert rows[0].details["changes"] == {"serial_number": "SN-AFTER", "notes": "relabelled"}
    assert rows[0].details["before"]["serial_number"] == "SN-BEFORE"


def test_customer_scoped_update_records_before_and_after(tenant_db):
    row = _seed(tenant_db)
    update_equipment_for_customer(
        row.customer_id,
        row.id,
        EquipmentPatch(model="9000", installation_date=date(2030, 1, 1)),
        _request(),
        user=USER,
        db=tenant_db,
    )
    rows = _audit_rows(tenant_db, "equipment_updated")
    assert len(rows) == 1
    assert rows[0].details["changes"]["model"] == "9000"
    assert rows[0].details["changes"]["installation_date"] == "2030-01-01"
    assert rows[0].details["before"]["model"] == "8500"


def test_delete_is_audited_with_what_was_deleted(tenant_db):
    row = _seed(tenant_db)
    delete_equipment(row.id, _request(), user=USER, db=tenant_db)
    tenant_db.refresh(row)
    assert row.deleted_at is not None, "still a soft delete"
    rows = _audit_rows(tenant_db, "equipment_deleted")
    assert len(rows) == 1
    assert rows[0].entity_id == str(row.id)
    assert rows[0].user_id == USER_ID
    assert rows[0].details["serial_number"] == "SN-BEFORE"


def test_both_service_log_routes_are_audited(tenant_db):
    row = _seed(tenant_db)
    job_id = uuid4()
    svc1 = log_service_event(
        job_id, row.id, ServiceEventIn(service_type="repair", technician_id="tech-1", notes="spring"),
        _request(), user=USER, db=tenant_db,
    )
    svc2 = log_equipment_service(
        row.id, ServiceLogIn(service_type="tune-up", technician_notes="lubed"),
        _request(), user=USER, db=tenant_db,
    )
    assert isinstance(svc1, EquipmentServiceHistory) and isinstance(svc2, EquipmentServiceHistory)
    rows = _audit_rows(tenant_db, "equipment_service_logged")
    assert {r.entity_id for r in rows} == {str(svc1.id), str(svc2.id)}
    assert all(r.user_id == USER_ID and r.entity_type == "equipment_service" for r in rows)
    assert all(r.details["equipment_id"] == str(row.id) for r in rows)


def test_a_missing_row_audits_nothing(tenant_db):
    """A 404 must not leave a phantom audit row behind."""
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        delete_equipment(uuid4(), _request(), user=USER, db=tenant_db)
    assert _audit_rows(tenant_db, "equipment_deleted") == []
