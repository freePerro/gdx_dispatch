from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import make_fresh_db
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers import booking, checklists, equipment_tracking, fleet, notifications, timeclock


class DummyRequest:
    def __init__(self, tenant_id: str = "tenant-remaining-routers") -> None:
        self.state = SimpleNamespace(tenant={"id": tenant_id}, request_id="req-1")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def app_ctx() -> tuple[Session, DummyRequest, dict[str, str], sessionmaker]:
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    request = DummyRequest()
    current_user = {"user_id": "admin-1", "role": "admin"}
    try:
        yield db, request, current_user, SessionLocal
    finally:
        db.close()
        engine.dispose()


def _audit_count(SessionLocal: sessionmaker) -> int:
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
        return int(row or 0)
    except Exception:
        return 0
    finally:
        db.close()


def test_routers_include_module_dependencies() -> None:
    assert require_module("communications") in [dep.dependency for dep in notifications.router.dependencies]
    assert require_module("equipment_tracking") in [dep.dependency for dep in equipment_tracking.router.dependencies]
    assert require_module("timeclock") in [dep.dependency for dep in timeclock.router.dependencies]
    assert require_module("jobs") in [dep.dependency for dep in checklists.router.dependencies]
    assert require_module("customer_portal") in [dep.dependency for dep in booking.router.dependencies]
    assert require_module("fleet") in [dep.dependency for dep in fleet.router.dependencies]


# Notifications (3)

def test_notifications_defaults_and_templates(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, _ = app_ctx
    settings = notifications.get_notification_settings(request=request, current_user=current_user, db=db)
    assert settings.email_enabled is True

    templates = notifications.list_notification_templates(request=request, current_user=current_user, db=db)
    keys = {row.key for row in templates}
    assert {
        "appointment_reminder_24h",
        "on_my_way",
        "job_completed",
        "review_request",
        "payment_received",
    }.issubset(keys)


def test_notifications_patch_settings_writes_audit(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    updated = notifications.patch_notification_settings(
        payload=notifications.NotificationSettingsPatch(sms_enabled=False, sender_name="Ops"),
        request=request,
        current_user=current_user,
        db=db,
    )
    assert updated.sms_enabled is False
    assert updated.sender_name == "Ops"
    assert _audit_count(SessionLocal) == before + 1


def test_notifications_send_and_history(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    notifications.list_notification_templates(request=request, current_user=current_user, db=db)
    before = _audit_count(SessionLocal)

    sent = notifications.send_notification(
        payload=notifications.NotificationSendRequest(
            customer_id="cust-1",
            template_key="on_my_way",
            channel="sms",
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    assert sent.status == "sent"

    history = notifications.list_notification_history(
        request=request,
        page=1,
        page_size=10,
        current_user=current_user,
        db=db,
    )
    assert history.total >= 1
    assert history.items[0].customer_id == "cust-1"
    assert _audit_count(SessionLocal) == before + 1


# Equipment Tracking (3)

def test_equipment_crud_and_audit(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    created = equipment_tracking.create_equipment(
        payload=equipment_tracking.EquipmentCreateRequest(
            customer_id="cust-eq-1",
            equipment_type="opener",
            manufacturer="LiftMaster",
            model="8500W",
        ),
        request=request,
        current_user=current_user,
        db=db,
    )

    patched = equipment_tracking.update_equipment(
        equipment_id=created.id,
        payload=equipment_tracking.EquipmentUpdateRequest(notes="updated notes"),
        request=request,
        current_user=current_user,
        db=db,
    )
    assert patched.notes == "updated notes"

    deleted = equipment_tracking.delete_equipment(
        equipment_id=created.id,
        request=request,
        current_user=current_user,
        db=db,
    )
    assert deleted == {"deleted": True}
    assert _audit_count(SessionLocal) == before + 3


def test_equipment_history_add_and_get(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    created = equipment_tracking.create_equipment(
        payload=equipment_tracking.EquipmentCreateRequest(customer_id="cust-eq-2", equipment_type="roller"),
        request=request,
        current_user=current_user,
        db=db,
    )

    item = equipment_tracking.add_equipment_history(
        equipment_id=created.id,
        payload=equipment_tracking.EquipmentHistoryCreateRequest(
            service_type="repair",
            technician_id="tech-1",
            notes="fixed roller",
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    assert item.service_type == "repair"

    history = equipment_tracking.get_equipment_history(
        equipment_id=created.id,
        request=request,
        current_user=current_user,
        db=db,
    )
    assert len(history) == 1
    assert history[0].service_type == "repair"
    assert _audit_count(SessionLocal) == before + 2


def test_equipment_expiring_warranties(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, _ = app_ctx
    soon = date.today() + timedelta(days=10)
    later = date.today() + timedelta(days=45)

    equipment_tracking.create_equipment(
        payload=equipment_tracking.EquipmentCreateRequest(
            customer_id="cust-eq-3",
            equipment_type="track",
            warranty_expires_on=soon,
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    equipment_tracking.create_equipment(
        payload=equipment_tracking.EquipmentCreateRequest(
            customer_id="cust-eq-4",
            equipment_type="track",
            warranty_expires_on=later,
        ),
        request=request,
        current_user=current_user,
        db=db,
    )

    rows = equipment_tracking.get_expiring_warranties(request=request, current_user=current_user, db=db)
    assert len(rows) == 1
    assert rows[0].customer_id == "cust-eq-3"


# Timeclock (3)

def test_timeclock_clock_in_out_and_status(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)

    timeclock.post_clock_in(
        payload=timeclock.ClockActionRequest(technician_id="tech-10"),
        request=request,
        current_user=current_user,
        db=db,
    )
    status = timeclock.get_timeclock_status(
        request=request,
        technician_id="tech-10",
        current_user=current_user,
        db=db,
    )
    assert status.clocked_in is True

    clocked_out = timeclock.post_clock_out(
        payload=timeclock.ClockActionRequest(technician_id="tech-10"),
        request=request,
        current_user=current_user,
        db=db,
    )
    assert clocked_out.minutes is not None
    assert _audit_count(SessionLocal) == before + 2


def test_timeclock_manual_entry_and_patch(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    start = datetime.now(UTC) - timedelta(hours=2)
    end = datetime.now(UTC) - timedelta(hours=1)

    created = timeclock.create_manual_entry(
        payload=timeclock.TimeEntryCreateRequest(
            technician_id="tech-11",
            clock_in_at=start,
            clock_out_at=end,
            notes="manual",
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    patched = timeclock.update_time_entry(
        entry_id=created.id,
        payload=timeclock.TimeEntryUpdateRequest(notes="adjusted"),
        request=request,
        current_user=current_user,
        db=db,
    )
    assert patched.notes == "adjusted"
    assert _audit_count(SessionLocal) == before + 2


def test_timeclock_payroll_summary(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    """Timezone boundary note: entries are stored in UTC but the
    query filter is a date range. We must derive the date bounds from
    the same clock the entry uses (UTC) — using `date.today()` (local)
    fails the assertion around local-midnight rollover when UTC has
    advanced to the next day but local hasn't.
    """
    db, request, current_user, _ = app_ctx
    now_utc = datetime.now(UTC)
    start = now_utc - timedelta(hours=3)
    end = now_utc - timedelta(hours=2)
    timeclock.create_manual_entry(
        payload=timeclock.TimeEntryCreateRequest(
            technician_id="tech-pay",
            clock_in_at=start,
            clock_out_at=end,
        ),
        request=request,
        current_user=current_user,
        db=db,
    )

    today_utc = now_utc.date()
    payroll = timeclock.payroll_summary(
        request=request,
        start=today_utc - timedelta(days=1),
        end=today_utc,
        current_user=current_user,
        db=db,
    )
    assert any(row.technician_id == "tech-pay" and row.total_minutes > 0 for row in payroll)


# Checklists (3)

def test_checklists_template_create_and_list(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    checklists.create_checklist_template(
        payload=checklists.ChecklistTemplateCreateRequest(
            name="Garage Door Install",
            items=["Measure opening", "Install track"],
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    rows = checklists.list_checklist_templates(request=request, current_user=current_user, db=db)
    assert len(rows) >= 1
    assert _audit_count(SessionLocal) == before + 1


def test_checklists_create_for_job_and_get(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    tpl = checklists.create_checklist_template(
        payload=checklists.ChecklistTemplateCreateRequest(name="Repair", items=["Inspect", "Tighten springs"]),
        request=request,
        current_user=current_user,
        db=db,
    )
    checklists.create_job_checklist(
        job_id="job-1",
        payload=checklists.JobChecklistCreateRequest(template_id=tpl.id),
        request=request,
        current_user=current_user,
        db=db,
    )
    loaded = checklists.get_job_checklist(job_id="job-1", request=request, current_user=current_user, db=db)
    assert loaded.job_id == "job-1"
    assert len(loaded.items) == 2
    assert _audit_count(SessionLocal) == before + 2


def test_checklists_mark_item_complete(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    tpl = checklists.create_checklist_template(
        payload=checklists.ChecklistTemplateCreateRequest(name="Final", items=["Clean area"]),
        request=request,
        current_user=current_user,
        db=db,
    )
    checklist = checklists.create_job_checklist(
        job_id="job-2",
        payload=checklists.JobChecklistCreateRequest(template_id=tpl.id),
        request=request,
        current_user=current_user,
        db=db,
    )

    updated = checklists.update_checklist_item(
        checklist_id=checklist.id,
        item_id=checklist.items[0].id,
        payload=checklists.ChecklistItemUpdateRequest(completed=True),
        request=request,
        current_user=current_user,
        db=db,
    )
    assert updated.completed is True
    assert _audit_count(SessionLocal) == before + 3


# Booking (3)

def test_booking_available_slots_excludes_pending(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, _ = app_ctx
    target_date = date.today() + timedelta(days=1)
    booking.create_booking_request(
        payload=booking.BookingRequestCreate(
            name="Pat",
            phone="5551112222",
            service="opener repair",
            preferred_date=target_date,
            preferred_slot="09:00",
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    available = booking.get_available_slots(date=target_date, request=request, current_user=current_user, db=db)
    assert "09:00" not in available.slots


def test_booking_request_create_and_list_pending(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    booking.create_booking_request(
        payload=booking.BookingRequestCreate(
            name="Jordan",
            phone="5553334444",
            service="spring replacement",
            preferred_date=date.today() + timedelta(days=2),
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    rows = booking.list_booking_requests(request=request, status="pending", current_user=current_user, db=db)
    assert any(row.name == "Jordan" for row in rows)
    assert _audit_count(SessionLocal) == before + 1


def test_booking_approve_and_decline(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    req1 = booking.create_booking_request(
        payload=booking.BookingRequestCreate(
            name="Alex",
            phone="5550001111",
            service="new opener",
            preferred_date=date.today() + timedelta(days=3),
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    req2 = booking.create_booking_request(
        payload=booking.BookingRequestCreate(
            name="Casey",
            phone="5550002222",
            service="door tune-up",
            preferred_date=date.today() + timedelta(days=4),
        ),
        request=request,
        current_user=current_user,
        db=db,
    )

    approved = booking.approve_booking_request(request_id=req1.id, request=request, current_user=current_user, db=db)
    declined = booking.decline_booking_request(
        request_id=req2.id,
        payload=booking.BookingDeclineRequest(reason="Outside service area"),
        request=request,
        current_user=current_user,
        db=db,
    )

    assert approved.status == "approved"
    assert declined.status == "declined"
    assert _audit_count(SessionLocal) == before + 4


# Fleet (3)

def test_fleet_vehicle_crud_and_audit(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    created = fleet.create_vehicle(
        payload=fleet.VehicleCreateRequest(make="Ford", model="Transit", year=2022, odometer=10000),
        request=request,
        current_user=current_user,
        db=db,
    )
    updated = fleet.update_vehicle(
        vehicle_id=created.id,
        payload=fleet.VehicleUpdateRequest(odometer=12000),
        request=request,
        current_user=current_user,
        db=db,
    )
    deleted = fleet.delete_vehicle(vehicle_id=created.id, request=request, current_user=current_user, db=db)

    assert updated.odometer == 12000
    assert deleted == {"deleted": True}
    assert _audit_count(SessionLocal) == before + 3


def test_fleet_service_log_create_and_list(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, SessionLocal = app_ctx
    before = _audit_count(SessionLocal)
    vehicle = fleet.create_vehicle(
        payload=fleet.VehicleCreateRequest(make="Chevy", model="Express", year=2021, odometer=20000),
        request=request,
        current_user=current_user,
        db=db,
    )

    created = fleet.create_vehicle_service_log(
        vehicle_id=vehicle.id,
        payload=fleet.VehicleServiceCreateRequest(service_type="oil_change", mileage_at_service=20500, notes="routine"),
        request=request,
        current_user=current_user,
        db=db,
    )
    rows = fleet.list_vehicle_service_log(
        vehicle_id=vehicle.id,
        request=request,
        current_user=current_user,
        db=db,
    )
    assert created.service_type == "oil_change"
    assert len(rows) == 1
    assert _audit_count(SessionLocal) == before + 2


def test_fleet_due_for_service(app_ctx: tuple[Session, DummyRequest, dict[str, str], sessionmaker]) -> None:
    db, request, current_user, _ = app_ctx
    fleet.create_vehicle(
        payload=fleet.VehicleCreateRequest(
            make="RAM",
            model="ProMaster",
            year=2020,
            odometer=50000,
            next_service_due_on=date.today() - timedelta(days=1),
        ),
        request=request,
        current_user=current_user,
        db=db,
    )
    fleet.create_vehicle(
        payload=fleet.VehicleCreateRequest(
            make="Nissan",
            model="NV200",
            year=2023,
            odometer=5000,
            next_service_due_on=date.today() + timedelta(days=40),
        ),
        request=request,
        current_user=current_user,
        db=db,
    )

    rows = fleet.list_due_for_service(request=request, current_user=current_user, db=db)
    assert len(rows) == 1
    assert rows[0].make == "RAM"
