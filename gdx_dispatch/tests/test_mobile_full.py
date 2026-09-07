from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
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


@pytest.fixture(autouse=True)
def _photo_upload_dir(tmp_path, monkeypatch):
    """Point UPLOAD_DIR somewhere writable for every test in this module.

    The mobile photo route used to write to MOBILE_UPLOAD_DIR (default
    /tmp/gdx_mobile_uploads) and mint a url nothing served. It now stores bytes
    in the SAME flat document root the download route reads, whose default is
    /app/uploads — writable in the dev container, not on a CI runner, where
    these tests failed with PermissionError: '/app'. Tests that write files
    must say where; relying on a default that happens to be writable is how
    this went unnoticed.
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))

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
