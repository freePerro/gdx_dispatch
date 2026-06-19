from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.modules.inventory import models as _inventory_models  # noqa: F401
from gdx_dispatch.routers import gps as _gps  # noqa: F401  (registers TechnicianLocation)
from gdx_dispatch.routers import mobile as mobile_router

_TEST_USER = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}


def _as_json(response) -> dict:
    return json.loads(response.body)


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


@pytest.fixture()
def session_factory(tmp_path):
    db_file = tmp_path / "mobile_full.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    _seed_job_bundle(db)
    db.close()
    yield SessionLocal
    engine.dispose()


_JOB_UUID = uuid4()
_JOB_ID = _JOB_UUID.hex
_CUST_UUID = uuid4()
_CUST_ID = _CUST_UUID.hex
_APPT_UUID = uuid4()
_APPT_ID = _APPT_UUID.hex


def _seed_job_bundle(db: Session, scheduled_dt: datetime | None = None) -> dict[str, str]:
    now = scheduled_dt or datetime(2026, 4, 3, 9, 0, tzinfo=UTC)
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, phone, email, address, company_id)
            VALUES (:id, :name, :phone, :email, :address, 'tenant-a')
            """
        ),
        {
            "id": _CUST_ID,
            "name": "Acme Customer",
            "phone": "555-1111",
            "email": "a@example.com",
            "address": "123 Main",
        },
    )
    db.execute(
        text(
            """
            INSERT INTO technicians (id, company_id, user_id, active, created_at)
            VALUES ('tech-1', 'tenant-a', 'user-1', 1, :created_at)
            """
        ),
        {"created_at": now},
    )
    db.execute(
        text(
            """
            INSERT INTO jobs (
                id, company_id, customer_id, title, description, dispatch_status,
                scheduled_at, created_at, deleted_at
            ) VALUES (
                :id, 'tenant-a', :customer_id, 'Garage Door Repair', 'Broken spring',
                'assigned', :scheduled_at, :created_at, NULL
            )
            """
        ),
        {"id": _JOB_ID, "customer_id": _CUST_ID, "scheduled_at": now, "created_at": now},
    )
    db.execute(
        text(
            """
            INSERT INTO appointments (
                id, company_id, job_id, tech_id, title, start_at, end_at, notes,
                created_at, updated_at, deleted_at
            ) VALUES (
                :appt_id, 'tenant-a', :job_id, 'tech-1', 'Service Call', :start_at, :end_at,
                'Bring parts', :created_at, :created_at, NULL
            )
            """
        ),
        {
            "appt_id": _APPT_ID,
            "job_id": _JOB_ID,
            "start_at": now,
            "end_at": now + timedelta(hours=2),
            "created_at": now,
        },
    )
    db.commit()
    return {"job_id": _JOB_ID, "today": now.date().isoformat()}


def test_schedule_returns_todays_jobs(session_factory):
    db = session_factory()
    try:
        r = mobile_router.get_mobile_schedule(
            request=_request(),
            date=date(2026, 4, 3),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        body = _as_json(r)
        assert body["count"] == 1
        assert body["jobs"][0]["customer"]["name"] == "Acme Customer"
        assert body["jobs"][0]["navigation_link"].startswith("https://")
        assert body["jobs"][0]["time_window"]["start"]
    finally:
        db.close()


def test_en_route_updates_status_and_notifies(session_factory):
    db = session_factory()
    try:
        r = mobile_router.mobile_job_en_route(
            job_id=_JOB_ID,
            payload=mobile_router.EnRouteBody(eta_minutes=20),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        body = _as_json(r)
        assert body["dispatch_status"] == "en_route"
        assert body["customer_notified"] is True

        status = db.execute(text("SELECT dispatch_status FROM jobs WHERE id = :jid"), {"jid": _JOB_ID})
        assert status.scalar_one() == "en_route"
    finally:
        db.close()


def test_arrived_auto_clocks_in(session_factory):
    db = session_factory()
    try:
        r = mobile_router.mobile_job_arrived(
            job_id=_JOB_ID,
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        assert _as_json(r)["dispatch_status"] == "on_site"

        row = db.execute(
            text(
                """
                SELECT id
                FROM time_entries
                WHERE company_id='tenant-a' AND user_id='user-1' AND job_id = :jid AND clock_out IS NULL
                """
            ),
            {"jid": _JOB_ID},
        ).mappings().first()
        assert row is not None
    finally:
        db.close()


def test_complete_requires_signature(session_factory):
    db = session_factory()
    try:
        r = mobile_router.mobile_job_complete(
            job_id=_JOB_ID,
            payload=mobile_router.CompleteBody(completion_notes="done"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 400
    finally:
        db.close()


def test_photo_upload_attaches_to_job(session_factory):
    class _DummyUpload:
        filename = "site.jpg"
        content_type = "image/jpeg"

        async def read(self) -> bytes:
            return b"\xff\xd8\xff\xdb\x00C"

    async def _call() -> object:
        db = session_factory()
        try:
            return await mobile_router.upload_mobile_job_photo(
                job_id=_JOB_ID,
                request=_request(),
                file=_DummyUpload(),  # type: ignore[arg-type]
                current_user=_TEST_USER,
                db=db,
            )
        finally:
            db.close()

    r = asyncio.run(_call())
    assert r.status_code == 201
    assert _as_json(r)["job_id"] == _JOB_ID


def test_parts_used_deducts_inventory(session_factory):
    db = session_factory()
    _part_id = uuid4().hex
    try:
        db.execute(
            text(
                """
                INSERT INTO parts (
                    id, sku, name, unit_cost, unit_price, qty_on_hand, reorder_point,
                    created_at, deleted_at
                )
                VALUES (
                    :pid, 'SPRING-01', 'Spring', 20, 0, 5, 0, :created_at, NULL
                )
                """
            ),
            {"pid": _part_id, "created_at": datetime.now(UTC)},
        )
        db.commit()

        r = mobile_router.mobile_job_parts_used(
            job_id=_JOB_ID,
            payload=mobile_router.PartsUsedBody(parts=[mobile_router.PartUsageItem(part_id=_part_id, qty=2)]),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        assert _as_json(r)["recorded"] == 1

        qty = db.execute(text("SELECT qty_on_hand FROM parts WHERE id = :pid"), {"pid": _part_id})
        assert qty.scalar_one() == 3
    finally:
        db.close()


def test_clock_in_creates_time_entry(session_factory):
    """Day-level clock-in writes the canonical timeclock_entries_router row.

    Post-S3 reconciliation (commit 9cd67f7d, 2026-04-29) the day clock
    surface writes ``timeclock_entries_router`` rather than the legacy
    ``time_entries`` table — see mobile_timecard's docstring. Per-job
    clock endpoints still use time_entries for payroll.
    """
    db = session_factory()
    try:
        r = mobile_router.mobile_day_clock_in(
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 201

        # _get_technician_id resolves user-1 → tech-1 via the seeded
        # Technician row (see _seed_job_bundle); the handler stamps
        # technician_id='tech-1' on the timeclock row.
        row = db.execute(
            text(
                """
                SELECT id
                FROM timeclock_entries_router
                WHERE tenant_id='tenant-a'
                  AND technician_id='tech-1'
                  AND entry_type='clock'
                  AND clock_out_at IS NULL
                  AND deleted_at IS NULL
                """
            )
        ).mappings().first()
        assert row is not None
    finally:
        db.close()


def test_location_stored(session_factory):
    db = session_factory()
    try:
        r = mobile_router.report_mobile_location(
            payload=mobile_router.LocationBody(lat=30.2672, lng=-97.7431, timestamp="2026-04-03T13:00:00Z"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200

        row = db.execute(
            text(
                """
                SELECT lat, lng
                FROM technician_locations
                WHERE company_id='tenant-a' AND tech_id='tech-1'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
        assert row is not None
        assert row["lat"] == pytest.approx(30.2672)
    finally:
        db.close()


def test_offline_sync_processes_batch(session_factory):
    db = session_factory()
    try:
        r = mobile_router.mobile_sync(
            payload=mobile_router.SyncBatchBody(
                actions=[
                    mobile_router.SyncAction(
                        type="job_note",
                        entity_id=_JOB_ID,
                        data={"note": "First"},
                        queued_at="2026-04-03T15:00:00Z",
                    ),
                    mobile_router.SyncAction(
                        type="job_note",
                        entity_id=_JOB_ID,
                        data={"note": "First"},
                        queued_at="2026-04-03T15:00:00Z",
                    ),
                    mobile_router.SyncAction(
                        type="location",
                        entity_id="tech-1",
                        data={"lat": 30.3, "lng": -97.7, "timestamp": "2026-04-03T15:02:00Z"},
                        queued_at="2026-04-03T15:02:00Z",
                    ),
                ]
            ),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        body = _as_json(r)
        assert body["processed"] == 2
        assert body["skipped_duplicates"] == 1
    finally:
        db.close()


def test_audit_logged(session_factory):
    db = session_factory()
    try:
        r = mobile_router.mobile_job_en_route(
            job_id=_JOB_ID,
            payload=mobile_router.EnRouteBody(eta_minutes=10),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        row = db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM audit_logs
                WHERE event_type='en_route' AND entity_type='job' AND entity_id = :jid
                """
            ),
            {"jid": _JOB_ID},
        )
        assert row.scalar_one() == 1
    finally:
        db.close()


def test_timecard_returns_todays_entries(session_factory):
    db = session_factory()
    try:
        in_r = mobile_router.mobile_day_clock_in(
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert in_r.status_code == 201

        r = mobile_router.mobile_timecard(
            request=_request(),
            date=datetime.now(UTC).date(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        assert _as_json(r)["count"] >= 1
    finally:
        db.close()


def test_complete_with_signature_marks_complete(session_factory):
    db = session_factory()
    try:
        signature_data = "data:image/png;base64," + base64.b64encode(b"signed").decode()
        r = mobile_router.mobile_job_complete(
            job_id=_JOB_ID,
            payload=mobile_router.CompleteBody(
                completion_notes="fixed",
                signature_data=signature_data,
                signed_by="Jane Customer",
            ),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        # Job.dispatch_status enum is (unassigned, assigned, en_route,
        # on_site, done) — earlier code wrote the invalid "completed"
        # which would raise on PG. S1-B2 (sprint_tech_mobile) corrected
        # it to "done"; completed_at carries the timestamp axis.
        assert _as_json(r)["dispatch_status"] == "done"
    finally:
        db.close()


def test_mobile_router_has_module_gate():
    assert require_module("mobile") in [dep.dependency for dep in mobile_router.router.dependencies]
