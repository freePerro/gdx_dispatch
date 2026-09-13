"""Customers CSV: export only the selected customers, with readable addresses (#673).

The Segments bulk toolbar's "Export CSV" built a raw link to
`/api/customers/export?ids=…` — a route that does not exist, carrying no
bearer token, against an export with no id filter. The owner chose (2026-09-13)
to keep the export server-side and admin-only: `GET /api/exports/customers`
takes `ids`, and the button is shown only to the roles that endpoint allows.

The same export read `customers.address` — an EncryptedString — with raw SQL,
which skips the ORM's decrypt, so every encrypted address left as `gAAAA…`
ciphertext (34 of 413 customers on prod, 2026-09-13). The button would have
handed that file straight to the office.

The schema is built from the ORM, not hand-written DDL: `customers.id` is a
Uuid (32-hex on SQLite) and `address` must be the real EncryptedString.
"""
from __future__ import annotations

import csv
import io
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.core.pii as pii
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.exports import MAX_EXPORT_IDS, router

TENANT = "tenant-673"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(pii, "_FERNET", Fernet(Fernet.generate_key()))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    customers = [
        Customer(id=uuid4(), company_id=TENANT, name=f"Customer {n}",
                 email=f"c{n}@example.test", phone=f"555-000{n}", address=f"{n} Main St")
        for n in range(4)
    ]
    setup.add_all(customers)
    setup.commit()
    ids = [str(c.id) for c in customers]
    setup.close()

    role = {"value": "admin"}

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1", "sub": "user-1", "role": role["value"], "tenant_id": TENANT,
    }
    yield TestClient(app), Session, ids, role
    engine.dispose()


def _rows(resp):
    assert resp.status_code == 200, resp.text
    return list(csv.DictReader(io.StringIO(resp.text)))


def test_addresses_are_stored_encrypted_in_this_fixture(env):
    """The decrypt assertion below means nothing unless the column holds ciphertext."""
    _, Session, _, _ = env
    db = Session()
    raw = db.execute(text("SELECT address FROM customers")).scalars().all()
    db.close()
    assert raw and all(v.startswith("gAAAA") for v in raw)


def test_selected_ids_export_only_those_customers(env):
    client, _, ids, _ = env
    rows = _rows(client.get("/api/exports/customers", params={"ids": f"{ids[1]},{ids[3]}"}))
    assert sorted(r["name"] for r in rows) == ["Customer 1", "Customer 3"]


def test_without_ids_the_whole_table_still_exports(env):
    """The Data Export page calls this with no filter; it must be unchanged."""
    client, _, ids, _ = env
    assert len(_rows(client.get("/api/exports/customers"))) == len(ids)


def test_addresses_export_decrypted(env):
    client, _, ids, _ = env
    rows = _rows(client.get("/api/exports/customers", params={"ids": ids[2]}))
    assert rows[0]["address"] == "2 Main St"


def test_one_id_spelled_three_ways_is_one_customer(env):
    client, Session, ids, _ = env
    spellings = [ids[0], ids[0].upper(), ids[0].replace("-", "")]
    rows = _rows(client.get("/api/exports/customers", params={"ids": ",".join(spellings)}))
    assert len(rows) == 1
    db = Session()
    row = db.query(AuditLog).filter(AuditLog.action == "export_downloaded").one()
    db.close()
    assert row.details["selected_ids"] == [ids[0]]


def test_a_selected_export_is_audited_with_exactly_which_customers(env):
    client, Session, ids, _ = env
    _rows(client.get("/api/exports/customers", params={"ids": ",".join(ids[:2])}))
    db = Session()
    row = db.query(AuditLog).filter(AuditLog.action == "export_downloaded").one()
    db.close()
    assert row.user_id == "user-1"
    assert row.details == {"entity": "customers", "row_count": 2, "selected_ids": ids[:2]}


@pytest.mark.parametrize("role", ["dispatcher", "technician", "sales"])
def test_only_admin_and_owner_can_export(env, role):
    """Refused by the router's require_role("admin", "owner") before the
    handler's own _require_admin runs — this pins the endpoint, not which of
    the two gates answers."""
    client, _, ids, r = env
    r["value"] = role
    assert client.get("/api/exports/customers", params={"ids": ids[0]}).status_code == 403


def test_owner_can_export(env):
    client, _, ids, r = env
    r["value"] = "owner"
    assert len(_rows(client.get("/api/exports/customers", params={"ids": ids[0]}))) == 1


@pytest.mark.parametrize(
    ("ids", "detail"),
    [
        ("not-a-uuid", "ids must be customer ids"),
        (" , ", "ids is empty"),
        (",".join(str(uuid4()) for _ in range(MAX_EXPORT_IDS + 1)), f"at most {MAX_EXPORT_IDS}"),
    ],
)
def test_bad_ids_are_refused_with_a_reason(env, ids, detail):
    client, _, _, _ = env
    resp = client.get("/api/exports/customers", params={"ids": ids})
    assert resp.status_code == 422
    assert detail in resp.text


def test_the_largest_allowed_selection_fits_an_nginx_request_line(env):
    """nginx's default buffer is 8 KB for the whole request line."""
    ids = ",".join(str(uuid4()) for _ in range(MAX_EXPORT_IDS))
    line = f"GET /api/exports/customers?ids={ids.replace(',', '%2C')} HTTP/1.1"
    assert len(line.encode()) < 8 * 1024
