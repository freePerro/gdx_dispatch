from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.admin
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.modules import _load_user_permissions
from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.models.tenant_models import Base as TenantModelsBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, TenantRole, UserRoleAssignment
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


def _owner_user() -> dict:
    return {"user_id": "owner-1", "tenant_id": "tenant-1", "role": "owner"}


def _seed_user(
    db: Session, *, username: str, email: str, role: str = "viewer", active: bool = True, company_id: str = "tenant-test",
) -> str:
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
            "company_id": company_id,
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

    # Granting the admin role is owner-exclusive.
    body = admin_ops.patch_user(user_id, admin_ops.UserPatch(role="admin", active=False), _owner_user(), db)

    assert body["id"] == user_id
    assert body["role"] == "admin"
    assert body["active"] is False


def _tenant_request() -> Request:
    request = Request({"type": "http", "headers": [], "method": "PATCH"})
    request.state.tenant = {"id": company_id()}
    return request


def _tenant_role(db: Session, name: str, perms: list[str]) -> TenantRole:
    role = TenantRole(id=uuid.uuid4(), company_id=company_id(), name=name, permissions=json.dumps(perms), is_system=True)
    db.add(role)
    db.flush()
    return role


def _assign(db: Session, user_id: str, role: TenantRole) -> None:
    db.add(UserRoleAssignment(id=uuid.uuid4(), company_id=company_id(), user_id=user_id, role_id=role.id))
    db.commit()


def _assigned(db: Session, user_id: str) -> list:
    return db.execute(select(UserRoleAssignment.role_id).where(UserRoleAssignment.user_id == user_id)).scalars().all()


def test_patch_admin_user_demotion_rewrites_role_assignment(db: Session):
    """GDXA-1453: the resolver trusts a UserRoleAssignment row over users.role,
    so a demotion that left the admin row behind kept admin permissions."""
    user_id = _seed_user(db, username="demoteme", email="demoteme@example.com", role="admin", company_id=company_id())
    admin_role = _tenant_role(db, "admin", ["jobs.read_all", "users.write"])
    tech_role = _tenant_role(db, "technician", ["jobs.read_own"])
    _assign(db, user_id, admin_role)

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=2) as revoke:
        admin_ops.patch_user(user_id, admin_ops.UserPatch(role="technician"), _owner_user(), db)

    assert _assigned(db, user_id) == [tech_role.id]
    # A stale JWT still saying admin must not matter either.
    perms = _load_user_permissions(db, _tenant_request(), {"user_id": user_id, "role": "admin"})
    assert perms == {"jobs.read_own"}
    revoke.assert_called_once_with(user_id, reason="role_changed")
    audit = db.execute(select(AuditLog).where(AuditLog.action == "user_updated")).scalars().one()
    details = audit.details if isinstance(audit.details, dict) else json.loads(audit.details)
    assert details["old_role"] == "admin"
    assert details["role"] == "technician"
    assert details["sessions_revoked"] == 2


def test_patch_admin_user_role_change_audits_the_custom_role_it_drops(db: Session):
    # GDXA-1461: the role change deletes the custom assignment; the audit row
    # is then the only record the user ever held it.
    user_id = _seed_user(db, username="leadtech", email="leadtech@example.com", role="technician", company_id=company_id())
    _tenant_role(db, "technician", ["jobs.read_own"])
    dispatcher = _tenant_role(db, "dispatcher", ["jobs.read_all"])
    lead = TenantRole(id=uuid.uuid4(), company_id=company_id(), name="Lead Tech",
                      permissions=json.dumps(["jobs.read_own", "invoices.write"]), is_system=False)
    db.add(lead)
    db.flush()
    _assign(db, user_id, lead)

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=0):
        admin_ops.patch_user(user_id, admin_ops.UserPatch(role="dispatcher"), _owner_user(), db)

    assert _assigned(db, user_id) == [dispatcher.id]
    audit = db.execute(select(AuditLog).where(AuditLog.action == "user_updated")).scalars().one()
    details = audit.details if isinstance(audit.details, dict) else json.loads(audit.details)
    assert details["dropped_assignments"] == [{"role_id": str(lead.id), "role_name": "Lead Tech"}]


def test_patch_admin_user_same_role_keeps_a_custom_assignment(db: Session):
    # A custom role leaves users.role alone by design (role_permissions.assign_role),
    # so re-sending the unchanged role must not strip it.
    user_id = _seed_user(db, username="custom", email="custom@example.com", role="technician", company_id=company_id())
    _tenant_role(db, "technician", ["jobs.read_own"])
    lead = TenantRole(id=uuid.uuid4(), company_id=company_id(), name="Lead Tech",
                      permissions=json.dumps(["jobs.read_own", "invoices.write"]), is_system=False)
    db.add(lead)
    db.flush()
    _assign(db, user_id, lead)

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions") as revoke:
        admin_ops.patch_user(user_id, admin_ops.UserPatch(role="technician"), _owner_user(), db)

    assert _assigned(db, user_id) == [lead.id]
    revoke.assert_not_called()


def _stored_role(db: Session, user_id: str) -> str:
    return db.execute(text("SELECT role FROM users WHERE id = :id"), {"id": uuid.UUID(user_id).hex}).scalar_one()


@pytest.mark.parametrize("stored, sent", [("technician", "Technician"), ("technician", " TECH "), ("tech", "technician")])
def test_patch_admin_user_spelling_variant_of_current_role_is_a_no_op(db: Session, stored: str, sent: str):
    """GDXA-1460: an un-normalized UserPatch.role made "Technician" a role change
    against "technician": custom assignment dropped, users.role rewritten,
    sessions revoked."""
    user_id = _seed_user(db, username="variant", email="variant@example.com", role=stored, company_id=company_id())
    _tenant_role(db, "technician", ["jobs.read_own"])
    lead = TenantRole(id=uuid.uuid4(), company_id=company_id(), name="Lead Tech",
                      permissions=json.dumps(["jobs.read_own", "invoices.write"]), is_system=False)
    db.add(lead)
    db.flush()
    _assign(db, user_id, lead)

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions") as revoke:
        body = admin_ops.patch_user(user_id, admin_ops.UserPatch(role=sent), _owner_user(), db)

    assert _assigned(db, user_id) == [lead.id]
    revoke.assert_not_called()
    assert _stored_role(db, user_id) == "technician"
    assert body["role"] == "technician"


def test_patch_admin_user_canonical_spelling_repairs_a_legacy_short_role(db: Session):
    # The resolver looks users.role up in BUILTIN_ROLES as stored, so a "tech"
    # row with no assignment resolves to no permissions; PATCH "technician"
    # must still write the canonical spelling.
    user_id = _seed_user(db, username="legacy", email="legacy@example.com", role="tech", company_id=company_id())
    stale = {"user_id": user_id, "role": "tech"}
    assert _load_user_permissions(db, _tenant_request(), stale) == set()

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions") as revoke:
        admin_ops.patch_user(user_id, admin_ops.UserPatch(role="technician"), _owner_user(), db)

    assert _stored_role(db, user_id) == "technician"
    assert _load_user_permissions(db, _tenant_request(), stale)
    revoke.assert_not_called()


@pytest.mark.parametrize("model", [admin_ops.UserPatch, admin_ops.UserCreate])
@pytest.mark.parametrize("role", ["manager", "user", "accounting", "superadmin", "Lead Tech", ""])
def test_admin_user_role_input_outside_the_assignable_vocabulary_is_refused(model, role: str):
    from pydantic import ValidationError

    fields = {"role": role}
    if model is admin_ops.UserCreate:
        fields |= {"username": "u", "email": "u@example.com", "password": "password1234"}
    with pytest.raises(ValidationError):
        model(**fields)


def test_admin_user_role_input_is_normalized():
    assert admin_ops.UserPatch(role="Dispatch").role == "dispatcher"
    assert admin_ops.UserPatch().role is None
    created = admin_ops.UserCreate(username="u", email="u@example.com", password="password1234", role="Technician")
    assert created.role == "technician"


def test_post_admin_users_stores_the_canonical_role(db: Session):
    request = Request({"type": "http", "headers": [], "method": "POST"})
    request.state.tenant = {"id": "tenant-test"}
    payload = admin_ops.UserCreate(username="tc", email="tc@example.com", password="password1234", role="Tech")

    body = admin_ops.create_user(payload, _admin_user(), db, request=request)

    assert body["role"] == "technician"
    assert _stored_role(db, body["id"]) == "technician"


def test_patch_admin_user_active_only_leaves_assignment_and_sessions(db: Session):
    user_id = _seed_user(db, username="keepme", email="keepme@example.com", role="admin", company_id=company_id())
    admin_role = _tenant_role(db, "admin", ["jobs.read_all"])
    _assign(db, user_id, admin_role)

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions") as revoke:
        admin_ops.patch_user(user_id, admin_ops.UserPatch(active=True), _owner_user(), db)

    assert _assigned(db, user_id) == [admin_role.id]
    revoke.assert_not_called()


def test_patch_admin_user_refuses_to_demote_the_last_owner(db: Session):
    user_id = _seed_user(db, username="soleowner", email="soleowner@example.com", role="owner", company_id=company_id())
    owner_role = _tenant_role(db, "owner", ["*"])
    _assign(db, user_id, owner_role)

    with pytest.raises(HTTPException) as exc:
        admin_ops.patch_user(user_id, admin_ops.UserPatch(role="technician"), _owner_user(), db)
    assert exc.value.status_code == 400
    db.rollback()
    role = db.execute(text("SELECT role FROM users WHERE id = :id"), {"id": uuid.UUID(user_id).hex}).scalar_one()
    assert role == "owner"
    assert _assigned(db, user_id) == [owner_role.id]


def test_patch_admin_user_demotes_an_owner_when_another_remains(db: Session):
    _seed_user(db, username="otherowner", email="otherowner@example.com", role="owner", company_id=company_id())
    user_id = _seed_user(db, username="exowner", email="exowner@example.com", role="owner", company_id=company_id())

    with patch("gdx_dispatch.core.auth_revoke.revoke_user_sessions", return_value=0):
        body = admin_ops.patch_user(user_id, admin_ops.UserPatch(role="technician"), _owner_user(), db)
    assert body["role"] == "technician"


def test_patch_admin_user_role_grant_denied_for_admin(db: Session):
    """An admin (not owner) cannot grant the admin role — owner-exclusive."""
    user_id = _seed_user(db, username="grantme", email="grantme@example.com", role="viewer", active=True)

    with pytest.raises(HTTPException) as exc:
        admin_ops.patch_user(user_id, admin_ops.UserPatch(role="admin"), _admin_user(), db)
    assert exc.value.status_code == 403


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

    # Tier-8: get_audit_log grew the AuditLogViewer contract (limit/offset +
    # filters + chain_integrity); page/page_size stay accepted for back-compat.
    body = admin_ops.get_audit_log(page=2, page_size=2, _=_admin_user(), db=db)
    assert body["page"] == 2
    assert body["page_size"] == 2
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert "chain_integrity" in body


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
    assert 'admin_ops_router = _load_router("gdx_dispatch.routers.admin_ops", "router")' in app_py
    assert "app.include_router(admin_ops_router)" in app_py
