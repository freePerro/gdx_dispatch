from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid

import pytest

pytestmark = pytest.mark.admin
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import Base as TenantModelsBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job
from gdx_dispatch.routers import admin_ops


@pytest.fixture()
def db() -> Session:
    fd, db_path = tempfile.mkstemp(prefix="gdx-admin-ops-", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    TenantModelsBase.metadata.create_all(engine, checkfirst=True)
    TenantBase.metadata.create_all(engine, checkfirst=True)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME,
                updated_at DATETIME,
                deleted_at DATETIME
            )
            """
        )
    )
    session.commit()

    yield session

    session.close()
    engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


def _admin_user() -> dict:
    return {"user_id": "admin-1", "tenant_id": "tenant-1", "role": "admin"}


def _viewer_user() -> dict:
    return {"user_id": "viewer-1", "tenant_id": "tenant-1", "role": "viewer"}


def _seed_user(db: Session, *, username: str, email: str, role: str = "viewer", active: bool = True) -> str:
    user_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO users (id, username, email, password_hash, role, active, company_id)
            VALUES (:id, :username, :email, :password_hash, :role, :active, :company_id)
            """
        ),
        {
            "id": user_id.hex,
            "username": username,
            "email": email,
            "password_hash": "hashed",
            "role": role,
            "active": 1 if active else 0,
            "company_id": "tenant-test",
        },
    )
    db.commit()
    return str(user_id)


def _seed_export_rows(db: Session) -> None:
    customer = Customer(name="Export Customer", email="export@example.com", phone="555-1000", company_id="tenant-test")
    db.add(customer)
    db.commit()
    db.refresh(customer)

    job = Job(customer_id=customer.id, title="Export Job", company_id="tenant-test")
    db.add(job)
    db.commit()
    db.refresh(job)

    invoice = Invoice(
        customer_id=uuid.uuid4(),
        job_id=job.id,
        invoice_number="INV-ADMIN-001",
        public_token="tok-admin-export-001",
        subtotal=100.0,
        tax_amount=8.0,
        total=108.0,
        company_id="tenant-test",
    )
    db.add(invoice)
    db.commit()


def _make_json_request(payload) -> Request:
    raw = json.dumps(payload).encode("utf-8")
    state = {"sent": False}

    async def _receive() -> dict:
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")],
    }
    return Request(scope, _receive)


def _assert_forbidden(callable_):
    with pytest.raises(HTTPException) as exc:
        callable_()
    assert exc.value.status_code == 403
    assert exc.value.detail == "Insufficient role"


def test_require_admin_blocks_non_admin():
    _assert_forbidden(lambda: admin_ops._require_admin(_viewer_user()))


def test_get_admin_users_lists_all_users(db: Session):
    _seed_user(db, username="alice", email="alice@example.com", role="admin")
    _seed_user(db, username="bob", email="bob@example.com", role="viewer", active=False)

    rows = admin_ops.list_users(_admin_user(), db)
    assert len(rows) == 2
    usernames = {row["username"] for row in rows}
    assert usernames == {"alice", "bob"}


def test_post_admin_users_creates_user(db: Session):
    payload = admin_ops.UserCreate(
        username="new-admin-user",
        email="new-admin-user@example.com",
        password="secret-123",
        role="dispatcher",
    )
    # D103 (an earlier session): create_user now requires real tenant context;
    # empty-string default removed.
    request = Request({"type": "http", "headers": [], "method": "POST"})
    request.state.tenant = {"id": "tenant-test"}

    body = admin_ops.create_user(payload, _admin_user(), db, request=request)

    assert body["username"] == payload.username
    assert body["email"] == payload.email
    assert body["role"] == "dispatcher"
    assert body["active"] is True

    row = db.execute(text("SELECT password_hash FROM users WHERE id = :id"), {"id": uuid.UUID(str(body["id"])).hex}).mappings().first()
    assert row is not None
    assert row["password_hash"] != payload.password


def test_post_admin_users_rejects_duplicate_email(db: Session):
    _seed_user(db, username="existing", email="dupe@example.com", role="viewer")

    # The uniqueness check in create_user scopes by company_id (per-tenant,
    # not global — see 9c93467d). _seed_user plants rows with
    # company_id="tenant-test", so the request must carry the matching
    # tenant for the duplicate-email filter to find the seeded row.
    request = Request({"type": "http", "headers": [], "method": "POST"})
    request.state.tenant = {"id": "tenant-test"}
    request.state.tenant_id = "tenant-test"

    with pytest.raises(HTTPException) as exc:
        admin_ops.create_user(
            admin_ops.UserCreate(username="new-user", email="dupe@example.com", password="password1234", role="viewer"),
            _admin_user(),
            db,
            request=request,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "User with this email already exists"


def test_patch_admin_user_updates_role_and_active(db: Session):
    user_id = _seed_user(db, username="patchme", email="patchme@example.com", role="viewer", active=True)

    body = admin_ops.patch_user(user_id, admin_ops.UserPatch(role="admin", active=False), _admin_user(), db)

    assert body["id"] == user_id
    assert body["role"] == "admin"
    assert body["active"] is False


def test_patch_admin_user_404_when_missing(db: Session):
    with pytest.raises(HTTPException) as exc:
        admin_ops.patch_user(str(uuid.uuid4()), admin_ops.UserPatch(role="admin"), _admin_user(), db)

    assert exc.value.status_code == 404
    assert exc.value.detail == "User not found"


def test_delete_admin_user_deactivates(db: Session):
    user_id = _seed_user(db, username="deleteme", email="deleteme@example.com", active=True)

    body = admin_ops.deactivate_user(user_id, _admin_user(), db)

    assert body == {"deactivated": True, "id": user_id}
    row = db.execute(text("SELECT active FROM users WHERE id = :id"), {"id": uuid.UUID(user_id).hex}).mappings().first()
    assert row is not None
    assert int(row["active"]) == 0


def test_get_admin_export_returns_customers_jobs_invoices(db: Session):
    _seed_export_rows(db)

    body = admin_ops.full_export(_admin_user(), db)
    assert isinstance(body["customers"], list)
    assert isinstance(body["jobs"], list)
    assert isinstance(body["invoices"], list)
    assert len(body["customers"]) == 1
    assert len(body["jobs"]) == 1
    assert len(body["invoices"]) == 1


def test_post_admin_import_customers_json_bulk_upsert(db: Session):
    first_payload = [
        {"name": "Import One", "email": "import1@example.com", "phone": "555-0001"},
        {"name": "Import Two", "email": "import2@example.com", "phone": "555-0002"},
    ]
    first = asyncio.run(admin_ops.import_customers(_make_json_request(first_payload), None, _admin_user(), db))

    assert first["created"] == 2
    assert first["updated"] == 0

    second_payload = [{"name": "Import One", "email": "import1-updated@example.com", "phone": "555-0001"}]
    second = asyncio.run(admin_ops.import_customers(_make_json_request(second_payload), None, _admin_user(), db))

    assert second["created"] == 0
    assert second["updated"] == 1


def test_post_admin_import_customers_csv(db: Session):
    csv_bytes = b"name,email,phone\nCSV One,csv1@example.com,555-0011\nCSV Two,csv2@example.com,555-0012\n"
    class _FakeUpload:
        filename = "customers.csv"

        async def read(self) -> bytes:
            return csv_bytes

    upload = _FakeUpload()
    request = Request({"type": "http", "method": "POST", "headers": []})

    body = asyncio.run(admin_ops.import_customers(request, upload, _admin_user(), db))

    assert body["created"] == 2
    assert body["updated"] == 0


def test_post_admin_import_customers_requires_json_or_file(db: Session):
    request = Request({"type": "http", "method": "POST", "headers": []})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_ops.import_customers(request, None, _admin_user(), db))

    assert exc.value.status_code == 400


def test_get_admin_audit_log_pagination(db: Session):
    for idx in range(5):
        entry = AuditLog(
            event_type=f"event_{idx}",
            actor_id="admin-1",
            actor_role="admin",
            entity_type="user",
            entity_id=str(idx),
            payload={"i": idx},
            hash=f"h{idx}",
            prev_hash=f"p{idx}",
        )
        db.add(entry)
    db.commit()

    body = admin_ops.get_audit_log(2, 2, _admin_user(), db)
    assert body["page"] == 2
    assert body["page_size"] == 2
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_get_admin_permissions_defaults_empty(db: Session):
    assert admin_ops.list_role_permissions(_admin_user(), db) == []


def test_post_admin_permissions_updates_and_lists(db: Session):
    update = admin_ops.update_role_permissions(
        admin_ops.RolePermissionsUpdate(role="dispatcher", permissions=["jobs.read", "jobs.update"]),
        _admin_user(),
        db,
    )
    assert update["role"] == "dispatcher"
    assert update["permissions"] == ["jobs.read", "jobs.update"]

    listing = admin_ops.list_role_permissions(_admin_user(), db)
    assert len(listing) == 1
    assert listing[0]["role"] == "dispatcher"
    assert listing[0]["permissions"] == ["jobs.read", "jobs.update"]


def test_admin_ops_routes_registered_in_main_app():
    app_py = open("gdx_dispatch/app.py", encoding="utf-8").read()
    assert "from gdx_dispatch.routers.admin_ops import router as admin_ops_router" in app_py
    assert "app.include_router(admin_ops_router)" in app_py
