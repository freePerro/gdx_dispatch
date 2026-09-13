"""Every Outlook mutation answers who / what / when (#558, invariant #1).

Before this, ``modules/outlook/views_router.py`` contained zero occurrences of
the string "audit" and ``admin_settings_router.py`` claimed in its own module
docstring to be "audit-logged on change" while containing no audit call at all.
Eight handlers committed state changes with no row in ``audit_logs``:

  admin_settings_router: patch_settings, patch_credentials, delete_credentials
  views_router:          set_message_personal, link_message, unlink_message,
                         create_task_from_message, save_attachment_to_job

The harness matters as much as the assertions. Each request gets its OWN
session which is closed WITHOUT committing — exactly what
``core.database.get_db`` does — and every read happens in a separate session
afterwards. A shared, never-closed session would keep a merely-flushed row
visible, so a row that never actually lands would still be "found" (#700).

``test_control_the_harness_can_see_an_audit_row`` is the control: without it,
"0 rows" is indistinguishable from "the table was never wired".
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Imported at module load, before create_all, so every model each router
# touches is registered on TenantBase.metadata.
from gdx_dispatch.core import audit as audit_mod
from gdx_dispatch.core.audit import AuditLog, TenantBase, _get_db_dep, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.tenant_settings import Base as ControlBase
from gdx_dispatch.core.tenant_settings import TenantSettings
from gdx_dispatch.models.tenant_models import Customer, Document, Job, PlannerTask, User
from gdx_dispatch.modules.outlook import admin_settings_router as admin_mod
from gdx_dispatch.modules.outlook import views_router as views_mod
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage
from gdx_dispatch.routers.auth import get_current_user

TENANT = "11111111-1111-1111-1111-111111111558"
ADMIN_ID = "00000000-0000-0000-0000-000000000558"
# The shape prod's get_current_user actually returns (finalize_login_jwt):
# user_id / tenant_id / role. No `sub` claim — which is exactly why a
# `.get("sub")`-first actor chain writes "system" for a real person (#701).
ADMIN = {"user_id": ADMIN_ID, "tenant_id": TENANT, "role": "admin"}


@pytest.fixture
def SessionLocal(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    # tenant_settings lives on the CONTROL-plane Base, not TenantBase. Under
    # the single-tenant collapse both planes are one database, which is how
    # the credentials endpoints reach it through the same session.
    ControlBase.metadata.create_all(engine, checkfirst=True)
    # autoflush=False matches core/database.py's SessionLocal exactly.
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    # Seed the tenant_settings row. Production always has one (bootstrap
    # writes it, and every settings router upserts into it), and without it
    # patch_credentials hits a PRE-EXISTING duplicate-INSERT bug that has
    # nothing to do with auditing: the handler adds a TenantSettings, then
    # key_storage._ensure_tenant_settings calls db.get() again, which cannot
    # see a pending object under autoflush=False, and adds a second — the
    # commit then fails on the tenant_id unique constraint. Unchanged by
    # #558 and left alone here on purpose.
    with Session() as db:
        db.add(TenantSettings(tenant_id=UUID(TENANT)))
        db.commit()
    yield Session
    engine.dispose()


def _client(SessionLocal, router, user: dict = ADMIN) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    def _like_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()  # no commit — exactly what core.database.get_db does

    # Override `_get_db_dep`, NOT `audit_ready_db` itself, and NOT the routers'
    # own `get_db_for_views` / `get_db_for_admin`.
    #
    # `audit_ready_db` declares `Depends(_get_db_dep)` — a separate function in
    # core/audit.py that calls `get_db()` directly — so overriding only `get_db`
    # leaves the handler on the REAL database. But overriding `audit_ready_db`
    # (or the router's wrapper) replaces the dependency wholesale, so
    # `ensure_audit_table` never runs and these tests would pass identically
    # against handlers still wired to plain `get_db` — the atomicity claim
    # would have zero coverage.
    app.dependency_overrides[get_db] = _like_get_db
    app.dependency_overrides[_get_db_dep] = _like_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_module("email")] = lambda: None

    @app.middleware("http")
    async def _inject_tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def _rows(SessionLocal, *, action: str | None = None, entity_type: str | None = None) -> list[AuditLog]:
    with SessionLocal() as db:
        q = select(AuditLog)
        if action is not None:
            q = q.where(AuditLog.action == action)
        if entity_type is not None:
            q = q.where(AuditLog.entity_type == entity_type)
        return list(db.execute(q.order_by(AuditLog.created_at, AuditLog.id)).scalars())


def _details(row: AuditLog) -> dict:
    return row.details if row.details is not None else (row.payload or {})


def _seed_message(SessionLocal, **overrides) -> tuple[UUID, UUID]:
    """One OutlookAccount owned by ADMIN + one message in it."""
    account_id, message_id = uuid4(), uuid4()
    with SessionLocal() as db:
        db.add(OutlookAccount(id=account_id, user_id=ADMIN_ID, provider="outlook",
                              upn="office@example.com"))
        msg = OutlookMessage(
            id=message_id,
            account_id=account_id,
            graph_message_id=f"g-{message_id}",
            subject="Re: estimate",
            from_address="alice@example.com",
            to_addresses=["office@example.com"],
            direction="inbound",
            received_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            body_preview="the door still binds",
        )
        for k, v in overrides.items():
            setattr(msg, k, v)
        db.add(msg)
        db.commit()
    return account_id, message_id


def _seed_job(SessionLocal) -> tuple[UUID, UUID]:
    customer_id, job_id = uuid4(), uuid4()
    with SessionLocal() as db:
        db.add(Customer(id=customer_id, name="Acme Storage", company_id=TENANT))
        db.add(Job(id=job_id, customer_id=customer_id, title="Opener replacement",
                   company_id=TENANT))
        db.commit()
    return customer_id, job_id


# ── the control ──────────────────────────────────────────────────────────────


def test_control_the_harness_can_see_an_audit_row(SessionLocal):
    """Without this, every '0 rows' below could just mean the table isn't wired."""
    with SessionLocal() as db:
        log_audit_event_sync(
            db, tenant_id=TENANT, user_id="control", action="control_probe",
            entity_type="outlook_message", entity_id="probe", details={},
        )
        db.commit()
    rows = _rows(SessionLocal, action="control_probe")
    assert len(rows) == 1
    assert rows[0].user_id == "control"


# ── admin_settings_router ────────────────────────────────────────────────────


def test_patch_settings_records_who_changed_which_setting(SessionLocal):
    client = _client(SessionLocal, admin_mod.router)
    resp = client.patch(
        "/api/admin/outlook/settings",
        json={"backfill_days": 30, "ai_tag_threshold": 0.4,
              "vendor_bill_sender_allowlist": ["ar@supplier.com"]},
    )
    assert resp.status_code == 200, resp.text

    rows = _rows(SessionLocal, action="outlook_settings_updated")
    assert len(rows) == 1, f"one PATCH, one audit row — got {len(rows)}"
    row = rows[0]
    assert (row.user_id or row.actor_id) == ADMIN_ID, "the signed-in admin, not 'system'"
    assert row.entity_type == "outlook_settings"
    assert str(row.entity_id) == TENANT

    changed = _details(row)["changed"]
    assert changed["backfill_days"] == {"from": 90, "to": 30}
    # The OLD value is the forensic half: "who set the AI tag threshold to 0.4"
    # is useless without "from 0.85".
    assert changed["ai_tag_threshold"]["to"] == 0.4
    # The allowlist governs whose attachments GDX downloads and files
    # unattended — the single highest-impact field on this endpoint.
    assert changed["vendor_bill_sender_allowlist"] == {
        "from": [], "to": ["ar@supplier.com"],
    }


def test_a_noop_settings_save_records_an_empty_diff(SessionLocal):
    """Saving the same values twice must not replay every column as 'changed'."""
    client = _client(SessionLocal, admin_mod.router)
    payload = {"backfill_days": 30}
    assert client.patch("/api/admin/outlook/settings", json=payload).status_code == 200
    assert client.patch("/api/admin/outlook/settings", json=payload).status_code == 200
    rows = _rows(SessionLocal, action="outlook_settings_updated")
    assert len(rows) == 2, "both saves are recorded"
    assert _details(rows[1])["changed"] == {}, "a no-op save recorded a diff"


def test_patch_credentials_records_the_rotation_and_never_the_secret(SessionLocal):
    """The highest-severity handler in the set: this writes the Entra client
    secret, the credential granting GDX access to the whole mailbox."""
    client = _client(SessionLocal, admin_mod.router)
    resp = client.patch(
        "/api/admin/outlook/credentials",
        json={"client_id": "app-client-id", "client_secret": "s3cr3t-value-long-enough"},
    )
    assert resp.status_code == 200, resp.text

    rows = _rows(SessionLocal, action="outlook_credentials_updated")
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id or row.actor_id) == ADMIN_ID, "the signed-in admin, not 'system'"
    assert row.entity_type == "outlook_credentials"
    assert str(row.entity_id) == TENANT
    d = _details(row)
    assert d["secret_rotated"] is True
    assert d["client_id_set"] is True
    assert d["secret_was_set"] is False, "there was no prior secret to replace"
    # The secret must never reach the audit feed, in any field, in any form.
    blob = repr(d) + repr(row.payload)
    assert "s3cr3t-value-long-enough" not in blob

    # and the credential really did get written
    with SessionLocal() as db:
        stored = db.get(TenantSettings, UUID(TENANT))
        assert stored is not None and stored.outlook_client_secret_enc


def test_delete_credentials_records_that_a_live_secret_was_revoked(SessionLocal):
    client = _client(SessionLocal, admin_mod.router)
    assert client.patch(
        "/api/admin/outlook/credentials",
        json={"client_secret": "s3cr3t-value-long-enough"},
    ).status_code == 200
    assert client.delete("/api/admin/outlook/credentials").status_code == 204

    rows = _rows(SessionLocal, action="outlook_credentials_cleared")
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id or row.actor_id) == ADMIN_ID
    assert row.entity_type == "outlook_credentials"
    # Captured BEFORE the clear — afterwards nothing distinguishes revoking a
    # live credential from a no-op DELETE.
    assert _details(row)["secret_was_set"] is True

    with SessionLocal() as db:
        stored = db.get(TenantSettings, UUID(TENANT))
        assert stored is not None and stored.outlook_client_secret_enc is None


def test_a_failed_audit_write_takes_the_credential_change_with_it(SessionLocal, monkeypatch):
    """The property ``audit_or_rollback`` exists to provide, and the reason the
    credential handlers use it instead of a bare log call: on a credential
    store, an unaudited change is worse than a failed one."""
    client = _client(SessionLocal, admin_mod.router)

    def _boom(*a, **kw):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(audit_mod, "log_audit_event_sync", _boom)
    resp = client.patch(
        "/api/admin/outlook/credentials",
        json={"client_secret": "s3cr3t-value-long-enough"},
    )
    assert resp.status_code == 500

    with SessionLocal() as db:
        stored = db.get(TenantSettings, UUID(TENANT))
    assert stored is None or stored.outlook_client_secret_enc is None, (
        "the client secret was stored even though its audit row failed — "
        "that is an unaudited credential mutation, invariant #1"
    )


# ── views_router ─────────────────────────────────────────────────────────────


def test_marking_a_message_personal_records_who_hid_it(SessionLocal):
    """is_personal hides the message from everyone but the mailbox owner, so
    "who hid this, and when" has to be answerable."""
    _, message_id = _seed_message(SessionLocal)
    client = _client(SessionLocal, views_mod.router)
    with patch.object(views_mod, "can_view", return_value=True):
        resp = client.post(f"/api/outlook/messages/{message_id}/personal",
                           json={"is_personal": True})
    assert resp.status_code == 200, resp.text

    rows = _rows(SessionLocal, action="outlook_message_personal_updated")
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id or row.actor_id) == ADMIN_ID, "the signed-in user, not 'system'"
    assert row.entity_type == "outlook_message"
    assert str(row.entity_id) == str(message_id)
    assert _details(row) == {"from": False, "to": True}


def test_linking_a_message_records_the_attribution_it_replaced(SessionLocal):
    customer_id, job_id = _seed_job(SessionLocal)
    _, message_id = _seed_message(SessionLocal, tag_strategy="auto_match")
    client = _client(SessionLocal, views_mod.router)
    with patch.object(views_mod, "can_view", return_value=True):
        resp = client.post(
            f"/api/outlook/messages/{message_id}/link",
            json={"customer_id": str(customer_id), "job_id": str(job_id)},
        )
    assert resp.status_code == 200, resp.text

    rows = _rows(SessionLocal, action="outlook_message_linked")
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id or row.actor_id) == ADMIN_ID
    assert row.entity_type == "outlook_message"
    assert str(row.entity_id) == str(message_id)
    d = _details(row)
    assert d["from"] == {"customer_id": None, "job_id": None, "tag_strategy": "auto_match"}
    assert d["to"]["customer_id"] == str(customer_id)
    assert d["to"]["job_id"] == str(job_id)


def test_unlinking_records_the_link_it_destroyed(SessionLocal):
    """unlink writes a DURABLE human override — it pins the message so the
    hourly retag never re-links it. Without the prior ids in the row, which
    customer and job the email belonged to is gone for good."""
    customer_id, job_id = _seed_job(SessionLocal)
    _, message_id = _seed_message(
        SessionLocal, linked_customer_id=customer_id, linked_job_id=job_id,
        tag_strategy="job_thread",
    )
    client = _client(SessionLocal, views_mod.router)
    with patch.object(views_mod, "can_view", return_value=True):
        resp = client.delete(f"/api/outlook/messages/{message_id}/link")
    assert resp.status_code == 200, resp.text

    rows = _rows(SessionLocal, action="outlook_message_unlinked")
    assert len(rows) == 1
    row = rows[0]
    assert (row.user_id or row.actor_id) == ADMIN_ID
    d = _details(row)
    assert d["from"]["customer_id"] == str(customer_id)
    assert d["from"]["job_id"] == str(job_id)
    assert d["from"]["tag_strategy"] == "job_thread"
    assert d["suppresses_retag"] is True

    # and the override really landed
    with SessionLocal() as db:
        msg = db.get(OutlookMessage, message_id)
        assert msg.linked_customer_id is None and msg.tag_strategy == "manual"


def test_create_task_from_email_uses_the_planner_action_not_a_parallel_one(SessionLocal):
    """routers/planner.py:create_task audits the identical PlannerTask write as
    action="create_task" / entity_type="planner_task" (#700). The email path is
    a second door onto the same table; a trail that answers "who created this
    task" for only one of the two doors is not a trail."""
    _, message_id = _seed_message(SessionLocal)
    client = _client(SessionLocal, views_mod.router)
    with patch.object(views_mod, "can_view", return_value=True):
        resp = client.post(f"/api/outlook/messages/{message_id}/create-task",
                           json={"priority": "high"})
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    rows = _rows(SessionLocal, action="create_task")
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "planner_task"
    assert str(row.entity_id) == task_id
    assert (row.user_id or row.actor_id) == ADMIN_ID, "the signed-in user, not 'system'"
    d = _details(row)
    assert d["source"] == "email_capture"
    assert d["outlook_message_id"] == str(message_id)

    with SessionLocal() as db:
        assert db.get(PlannerTask, task_id) is not None


def _save_attachment(client, message_id, job_id):
    listing = [{"id": "a1", "name": "po.pdf", "contentType": "application/pdf", "size": 4}]
    with patch.object(views_mod, "can_view", return_value=True), \
         patch.object(views_mod, "_owner_graph", side_effect=[listing, b"%PDF"]):
        return client.post(
            f"/api/outlook/messages/{message_id}/attachments/a1/save-to-job",
            json={"job_id": str(job_id)},
        )


def test_saving_an_attachment_records_a_document_created_row(SessionLocal, tmp_path, monkeypatch):
    """routers/documents.py audits its Document write as "document_created".
    Same action + entity_type here, for the same reason as create_task above.

    The Graph fetch is mocked (``_owner_graph``) — there is no live mailbox in
    a test — but the storage write, the Document INSERT and the commit are all
    real.
    """
    monkeypatch.setattr(views_mod, "_document_upload_dir", lambda: tmp_path)
    _, job_id = _seed_job(SessionLocal)
    _, message_id = _seed_message(SessionLocal)
    client = _client(SessionLocal, views_mod.router)

    resp = _save_attachment(client, message_id, job_id)
    assert resp.status_code == 201, resp.text
    document_id = resp.json()["document_id"]

    rows = _rows(SessionLocal, action="document_created")
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "document"
    assert str(row.entity_id) == document_id, "entity_id is the Document, not 'None'"
    assert (row.user_id or row.actor_id) == ADMIN_ID, "the signed-in user, not 'system'"
    d = _details(row)
    assert d["original_name"] == "po.pdf"
    assert d["file_size"] == 4
    assert d["job_id"] == str(job_id)
    assert d["source"] == "outlook_attachment"

    with SessionLocal() as db:
        assert db.get(Document, UUID(document_id)) is not None
    assert [p.read_bytes() for p in tmp_path.iterdir()] == [b"%PDF"]


def test_a_failed_audit_write_removes_the_saved_attachment(SessionLocal, tmp_path, monkeypatch):
    """The audit call sits INSIDE the handler's existing rollback/unlink try.

    The bytes are on disk before the commit. If the audit write took a
    different exit than a failed commit, a failure here would leave an orphan
    blob in UPLOAD_DIR — or a Document row with no record of who filed a
    supplier's PDF onto a customer's job.
    """
    monkeypatch.setattr(views_mod, "_document_upload_dir", lambda: tmp_path)
    _, job_id = _seed_job(SessionLocal)
    _, message_id = _seed_message(SessionLocal)
    client = _client(SessionLocal, views_mod.router)

    def _boom(*a, **kw):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(views_mod, "log_audit_event_sync", _boom)
    resp = _save_attachment(client, message_id, job_id)
    assert resp.status_code == 500

    assert list(tmp_path.iterdir()) == [], "orphan bytes left in UPLOAD_DIR"
    with SessionLocal() as db:
        assert db.execute(select(Document)).scalars().all() == [], (
            "the Document committed even though its audit row failed — "
            "that is an unaudited mutation, invariant #1"
        )


# ── the whole surface, one assertion ─────────────────────────────────────────


def test_every_outlook_mutation_in_this_set_leaves_exactly_one_row(SessionLocal, tmp_path, monkeypatch):
    """Drive all eight handlers through one app and count the trail.

    This is the shape of the #558 finding: eight mutations, zero rows. The
    count is the regression net — a handler that loses its audit call drops
    the total, and the per-handler tests above say which one.
    """
    monkeypatch.setattr(views_mod, "_document_upload_dir", lambda: tmp_path)
    customer_id, job_id = _seed_job(SessionLocal)
    _, message_id = _seed_message(SessionLocal)
    with SessionLocal() as db:
        db.add(User(id=uuid4(), company_id=TENANT, role="admin", email="a@example.com"))
        db.commit()

    admin_client = _client(SessionLocal, admin_mod.router)
    views_client = _client(SessionLocal, views_mod.router)

    assert admin_client.patch("/api/admin/outlook/settings",
                              json={"backfill_days": 45}).status_code == 200
    assert admin_client.patch(
        "/api/admin/outlook/credentials",
        json={"client_secret": "s3cr3t-value-long-enough"},
    ).status_code == 200
    assert admin_client.delete("/api/admin/outlook/credentials").status_code == 204

    with patch.object(views_mod, "can_view", return_value=True):
        assert views_client.post(f"/api/outlook/messages/{message_id}/personal",
                                 json={"is_personal": False}).status_code == 200
        assert views_client.post(
            f"/api/outlook/messages/{message_id}/link",
            json={"customer_id": str(customer_id)},
        ).status_code == 200
        assert views_client.delete(
            f"/api/outlook/messages/{message_id}/link").status_code == 200
        assert views_client.post(f"/api/outlook/messages/{message_id}/create-task",
                                 json={"priority": "low"}).status_code == 201
    assert _save_attachment(views_client, message_id, job_id).status_code == 201

    actions = [r.action for r in _rows(SessionLocal)]
    assert sorted(actions) == sorted([
        "outlook_settings_updated",
        "outlook_credentials_updated",
        "outlook_credentials_cleared",
        "outlook_message_personal_updated",
        "outlook_message_linked",
        "outlook_message_unlinked",
        "create_task",
        "document_created",
    ]), actions
    # Not one of them may be attributed to the machine.
    assert {r.user_id for r in _rows(SessionLocal)} == {ADMIN_ID}
