from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
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
    db_file = tmp_path / "mobile_api_test.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SessionLocal
    engine.dispose()


def _seed_job_bundle(SessionLocal, scheduled_dt: datetime | None = None) -> dict[str, str]:
    db = SessionLocal()
    now = scheduled_dt or datetime.now(UTC)
    job_id = "job-1"
    _cust_uuid = uuid4()
    customer_id = _cust_uuid.hex  # 32-char hex for Uuid(as_uuid=True) compat
    try:
        db.execute(
            text(
                """
                INSERT INTO customers (id, name, phone, email, address, company_id)
                VALUES (:id, :name, :phone, :email, :address, 'tenant-a')
                """
            ),
            {
                "id": customer_id,
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
            {
                "id": job_id,
                "customer_id": customer_id,
                "scheduled_at": now,
                "created_at": now,
            },
        )
        db.execute(
            text(
                """
                INSERT INTO appointments (
                    id, company_id, job_id, tech_id, title, start_at, end_at, notes,
                    created_at, updated_at, deleted_at
                ) VALUES (
                    'appt-1', 'tenant-a', :job_id, 'tech-1', 'Service Call', :start_at, :end_at,
                    'Bring parts', :created_at, :created_at, NULL
                )
                """
            ),
            {
                "job_id": job_id,
                "start_at": now,
                "end_at": now + timedelta(hours=2),
                "created_at": now,
            },
        )
        db.commit()
        return {"job_id": job_id, "customer_id": customer_id, "today": now.date().isoformat()}
    finally:
        db.close()


def test_get_schedule_returns_todays_jobs(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        r = mobile_router.get_mobile_schedule(
            request=_request(),
            date=None,
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        data = _as_json(r)
        assert data["date"] == seed["today"]
        assert data["count"] == 1
        assert data["jobs"][0]["id"] == seed["job_id"]
    finally:
        db.close()


def test_get_schedule_filters_by_date(session_factory):
    _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        tomorrow = datetime.now(UTC).date() + timedelta(days=1)
        r = mobile_router.get_mobile_schedule(
            request=_request(),
            date=tomorrow,
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        assert _as_json(r)["count"] == 0
    finally:
        db.close()


def test_get_job_detail_success(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        now = datetime.now(UTC)
        db.execute(
            text(
                """
                INSERT INTO job_notes (
                    id, company_id, job_id, body, author_id, visibility,
                    created_at, updated_at, deleted_at
                )
                VALUES (
                    'note-1', 'tenant-a', :job_id, 'First note', 'user-1', 'internal',
                    :created_at, :created_at, NULL
                )
                """
            ),
            {"job_id": seed["job_id"], "created_at": now},
        )
        db.execute(
            text(
                """
                INSERT INTO job_photos (
                    id, company_id, job_id, kind, url, uploaded_at,
                    filename, content_type, file_size, created_at, deleted_at
                )
                VALUES (
                    'photo-1', 'tenant-a', :job_id, 'during', '/uploads/a.jpg', :created_at,
                    'a.jpg', 'image/jpeg', 12, :created_at, NULL
                )
                """
            ),
            {"job_id": seed["job_id"], "created_at": now},
        )
        db.commit()

        r = mobile_router.get_mobile_job_detail(
            job_id=seed["job_id"],
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        data = _as_json(r)
        assert data["job"]["id"] == seed["job_id"]
        # The customer rides ON the job — the shape the Today cards already
        # use, so a tech action can read `job.customer` without caring which
        # screen mounted it. It is deliberately NOT also emitted as a sibling:
        # two copies of one customer in one payload is a divergence trap.
        assert data["job"]["customer"]["name"] == "Acme Customer"
        assert "customer" not in data, "sibling customer copy is back"
        # Built server-side so both surfaces navigate the same way.
        assert data["job"]["navigation_link"].startswith("https://maps.google.com/?q=")
        assert len(data["notes"]) == 1
        assert len(data["photos"]) == 1
    finally:
        db.close()


def _seed_uuid_job(db) -> tuple[str, str]:
    """A job whose id is PROD-shaped.

    `_seed_job_bundle` uses the literal "job-1", which is fine for handlers that
    only pass the id through raw SQL — but the billed check compares against the
    ORM's Uuid column, and a non-UUID string there raises rather than parses.
    A fixture id that could never exist in prod would prove nothing.
    """
    job_id = uuid4().hex  # 32-char hex — what Uuid(as_uuid=True) stores on SQLite
    customer_id = uuid4().hex
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO customers (id, name, phone, email, address, company_id) "
            "VALUES (:id, 'Acme Customer', '555-1111', 'a@example.com', '123 Main', 'tenant-a')"
        ),
        {"id": customer_id},
    )
    db.execute(
        text(
            """
            INSERT INTO jobs (
                id, company_id, customer_id, title, description, dispatch_status,
                scheduled_at, created_at, deleted_at
            ) VALUES (
                :id, 'tenant-a', :customer_id, 'Garage Door Repair', 'Broken spring',
                'done', :now, :now, NULL
            )
            """
        ),
        {"id": job_id, "customer_id": customer_id, "now": now},
    )
    # The ownership gate 404s a job the tech isn't on — seed the link the same
    # way the real flow does, so this exercises the gate rather than bypassing it.
    db.execute(
        text(
            "INSERT OR IGNORE INTO technicians (id, company_id, user_id, active, created_at) "
            "VALUES ('tech-1', 'tenant-a', 'user-1', 1, :now)"
        ),
        {"now": now},
    )
    db.execute(
        text(
            """
            INSERT INTO appointments (
                id, company_id, job_id, tech_id, title, start_at, end_at,
                created_at, updated_at, deleted_at
            ) VALUES (
                :id, 'tenant-a', :job_id, 'tech-1', 'Service Call', :now, :now,
                :now, :now, NULL
            )
            """
        ),
        {"id": uuid4().hex, "job_id": job_id, "now": now},
    )
    db.commit()
    return job_id, customer_id


def _detail(db, job_id):
    r = mobile_router.get_mobile_job_detail(
        job_id=job_id, request=_request(), current_user=_TEST_USER, db=db,
    )
    assert r.status_code == 200
    return _as_json(r)["job"]


def _add_invoice(db, job_id, customer_id, *, status: str, total: float, deleted: bool = False):
    """Insert through the ORM so the row matches the real schema rather than a
    hand-rolled column list (invoices has no `updated_at`, for one)."""
    from gdx_dispatch.models.tenant_models import Invoice

    db.add(
        Invoice(
            id=uuid4(),
            company_id="tenant-a",
            job_id=UUID(job_id) if isinstance(job_id, str) else job_id,
            customer_id=UUID(customer_id) if isinstance(customer_id, str) else customer_id,
            invoice_number=f"INV-{uuid4().hex[:8]}",
            public_token=uuid4().hex,
            status=status,
            subtotal=total,
            total=total,
            balance_due=total,
            deleted_at=datetime.now(UTC) if deleted else None,
        )
    )
    db.commit()


def test_job_detail_billed_flag_is_derived_from_invoices(session_factory):
    """`billed` must come from real invoices, never from Job.billing_status.

    That column looks like the answer and is a dead cache — nothing has ever
    written anything but "unbilled", so every reader that trusted it counted
    paid jobs as unbilled (core/billing_predicates.py). The mobile job screen
    opens ANY job, including one invoiced months ago, and uses this flag to
    decide whether to offer "Bill / collect". Getting it wrong means inviting a
    second invoice on a paid job.
    """
    db = session_factory()
    try:
        job_id, customer_id = _seed_uuid_job(db)

        # No invoice yet — billable.
        assert _detail(db, job_id)["billed"] is False

        # A sent invoice bills the job.
        _add_invoice(db, job_id, customer_id, status="sent", total=250.0)
        assert _detail(db, job_id)["billed"] is True
    finally:
        db.close()


def test_job_detail_billed_flag_ignores_void_and_zero_drafts(session_factory):
    """The two deliberate exclusions in the canonical predicate.

    A voided invoice doesn't bill a job (the old queries lost such jobs from
    Ready-for-Billing forever), and the $0 draft that create_invoice_from_job
    fabricates when a job has no estimate isn't billing either — treating it as
    billed would silently hide the job from the tech AND every alert.
    """
    db = session_factory()
    try:
        void_job, void_cust = _seed_uuid_job(db)
        _add_invoice(db, void_job, void_cust, status="void", total=250.0)
        assert _detail(db, void_job)["billed"] is False, "a voided invoice billed the job"

        draft_job, draft_cust = _seed_uuid_job(db)
        _add_invoice(db, draft_job, draft_cust, status="draft", total=0.0)
        assert _detail(db, draft_job)["billed"] is False, "the fabricated $0 draft billed the job"

        # A $0 invoice deliberately SENT does count — warranty work, stop nagging.
        sent_job, sent_cust = _seed_uuid_job(db)
        _add_invoice(db, sent_job, sent_cust, status="sent", total=0.0)
        assert _detail(db, sent_job)["billed"] is True
    finally:
        db.close()


def test_get_job_detail_404_for_missing_job(session_factory):
    db = session_factory()
    # A technician requesting a job that doesn't exist (or isn't assigned to
    # them) is denied 404 by the ownership gate, raised as an HTTPException.
    from fastapi import HTTPException

    try:
        with pytest.raises(HTTPException) as ei:
            mobile_router.get_mobile_job_detail(
                job_id="missing",
                request=_request(),
                current_user=_TEST_USER,
                db=db,
            )
        assert ei.value.status_code == 404
    finally:
        db.close()


def test_post_job_status_updates_dispatch_status(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        r = mobile_router.update_mobile_job_status(
            job_id=seed["job_id"],
            payload=mobile_router.JobStatusUpdate(status="en_route"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        assert _as_json(r)["dispatch_status"] == "en_route"
    finally:
        db.close()


def test_post_job_status_rejects_invalid_status(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        r = mobile_router.update_mobile_job_status(
            job_id=seed["job_id"],
            payload=mobile_router.JobStatusUpdate(status="assigned"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 400
        assert "Invalid status" in _as_json(r)["detail"]
    finally:
        db.close()


def test_clock_in_creates_time_entry(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        r = mobile_router.mobile_clock_in(
            job_id=seed["job_id"],
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 201
        body = _as_json(r)
        assert body["job_id"] == seed["job_id"]
        assert body["entry_id"]
    finally:
        db.close()


def test_clock_in_returns_conflict_if_already_open(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        first = mobile_router.mobile_clock_in(
            job_id=seed["job_id"],
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert first.status_code == 201

        second = mobile_router.mobile_clock_in(
            job_id=seed["job_id"],
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert second.status_code == 409
    finally:
        db.close()


def test_clock_out_closes_time_entry(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        in_r = mobile_router.mobile_clock_in(
            job_id=seed["job_id"],
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert in_r.status_code == 201

        out_r = mobile_router.mobile_clock_out(
            job_id=seed["job_id"],
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert out_r.status_code == 200
        body = _as_json(out_r)
        assert body["duration_minutes"] >= 0
        assert body["clock_out"] is not None
    finally:
        db.close()


def test_clock_out_404_without_open_entry(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        r = mobile_router.mobile_clock_out(
            job_id=seed["job_id"],
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 404
    finally:
        db.close()


def test_photo_upload_saves_photo(session_factory):
    seed = _seed_job_bundle(session_factory)

    class _DummyUpload:
        filename = "site.jpg"
        content_type = "image/jpeg"

        async def read(self) -> bytes:
            return b"\xff\xd8\xff\xdb\x00C"

    async def _call_upload():
        db = session_factory()
        try:
            return await mobile_router.upload_mobile_job_photo(
                job_id=seed["job_id"],
                request=_request(),
                file=_DummyUpload(),  # type: ignore[arg-type]
                current_user=_TEST_USER,
                db=db,
            )
        finally:
            db.close()

    r = asyncio.run(_call_upload())
    assert r.status_code == 201
    body = _as_json(r)
    assert body["filename"].endswith(".jpg")
    assert body["file_size"] > 0


def test_signature_capture_persists_signature(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        data = "data:image/png;base64," + base64.b64encode(b"signed").decode()
        r = mobile_router.capture_mobile_signature(
            job_id=seed["job_id"],
            payload=mobile_router.SignatureBody(signature_data=data, signed_by="Jane Customer"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 200
        body = _as_json(r)
        assert body["job_id"] == seed["job_id"]
        assert body["signed_by"] == "Jane Customer"
    finally:
        db.close()


def test_notes_adds_field_note(session_factory):
    seed = _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        r = mobile_router.add_mobile_job_note(
            job_id=seed["job_id"],
            payload=mobile_router.NoteBody(note="Need ladder"),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert r.status_code == 201
        assert _as_json(r)["note"] == "Need ladder"
    finally:
        db.close()


def test_location_reports_and_upserts(session_factory):
    _seed_job_bundle(session_factory)
    db = session_factory()
    try:
        first = mobile_router.report_mobile_location(
            payload=mobile_router.LocationBody(lat=30.2672, lng=-97.7431, accuracy=5),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        second = mobile_router.report_mobile_location(
            payload=mobile_router.LocationBody(lat=30.2675, lng=-97.7435, accuracy=4),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        first_body = _as_json(first)
        second_body = _as_json(second)
        assert first_body["id"] == second_body["id"]
        assert second_body["lat"] == pytest.approx(30.2675)
    finally:
        db.close()


def test_mobile_router_registered_in_app():
    app_py = Path(__file__).resolve().parents[1] / "app.py"
    content = app_py.read_text()
    assert "from gdx_dispatch.routers import mobile as mobile_router" in content
    assert "app.include_router(mobile_router.router if hasattr(mobile_router, \"router\") else mobile_router)" in content


def test_transition_writes_audit_synchronously(session_factory):
    # #20 — the transition endpoint used to fire a DETACHED create_task(
    # log_audit_event(db, ...)) on the request-scoped session, which
    # Depends(get_db) closes on return → commit-on-closed-SQLite-connection →
    # flaky native SIGSEGV in the harness. It now writes synchronously; assert
    # the audit row is present immediately after the call (no detached task).
    from sqlalchemy import select

    from gdx_dispatch.core.audit import AuditLog

    SessionLocal = session_factory
    admin = {"user_id": "admin-1", "role": "admin", "tenant_id": "tenant-a"}
    job_uuid = uuid4().hex
    db = SessionLocal()
    try:
        db.execute(text(
            "INSERT INTO customers (id, name, phone, email, address, company_id) "
            "VALUES (:id, 'C', '5551112222', 'c@e.co', 'addr', 'tenant-a')"
        ), {"id": uuid4().hex})
        cust = db.execute(text("SELECT id FROM customers LIMIT 1")).scalar()
        db.execute(text(
            "INSERT INTO jobs (id, company_id, customer_id, title, description, "
            "dispatch_status, lifecycle_stage, created_at, deleted_at) "
            "VALUES (:id, 'tenant-a', :cust, 'T', 'D', 'assigned', 'scheduled', :now, NULL)"
        ), {"id": job_uuid, "cust": cust, "now": datetime.now(UTC)})
        db.commit()

        r = mobile_router.mobile_update_job_status(
            job_id=str(__import__("uuid").UUID(job_uuid)),
            status="en_route",
            request=_request(),
            current_user=admin,
            db=db,
        )
        assert r.status_code == 200, _as_json(r)

        # Audit row exists synchronously (proves no detached task / closed-session commit).
        rows = db.execute(
            select(AuditLog).where(AuditLog.action == "job_status_mobile_update")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id == str(__import__("uuid").UUID(job_uuid))
    finally:
        db.close()
