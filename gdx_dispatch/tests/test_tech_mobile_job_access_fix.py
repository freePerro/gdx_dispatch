"""2026-07-16 tech-mobile job-access fix — regression coverage.

A technician could not get job information from a phone. Four verified
defects, each pinned here:

1. ``/api/mobile/jobs`` rendered Fernet ciphertext where the customer
   address should be (raw SQL bypassed EncryptedString decryption).
2. ``/api/mobile/my-jobs/{id}`` 500'd on prod — its raw SQL selected
   ``job_photos.photo_type``, a column that has never existed
   (``kind`` is the real name).
3. ``GET /api/settings/integrations/google-maps`` 403'd technicians (the
   settings router's router-level admin gate overrode the endpoint's
   documented any-authenticated-user intent), blanking the mobile map.
4. ``/api/mobile/job/{id}`` returned photo metadata without ``url`` so a
   detail view had nothing to render.

The union/area/tz behaviour of /api/mobile/today is covered separately in
``test_mobile_today.py``; the job_assignments ownership gate in
``test_authz_regression.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core import pii
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Job, JobPhoto, Technician
from gdx_dispatch.routers import branding_public as branding_public_router
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers import settings as settings_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"


def _build_app(db, role: str = "technician") -> TestClient:
    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    # Same include order as gdx_dispatch/app.py: public settings BEFORE the
    # admin-gated settings router (FastAPI route lookup is first-match-wins).
    app.include_router(branding_public_router.router)
    app.include_router(settings_router.router)
    app.include_router(mobile_router.router)
    user = {"user_id": USER, "tenant_id": TENANT, "role": role}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = user
        return await call_next(request)

    return TestClient(app)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _seed_tech_job(db, *, address="123 Main St") -> Job:
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    c = Customer(
        id=uuid4(), name="Acme", phone="555-1111", email="a@example.com",
        address=address, company_id=TENANT,
    )
    db.add(c)
    db.commit()
    j = Job(
        id=uuid4(), company_id=TENANT, customer_id=c.id,
        title="Spring replacement", description="d",
        scheduled_at=datetime.now(UTC), assigned_to=TECH,
        dispatch_status="assigned",
    )
    db.add(j)
    db.commit()
    return j


# ── 1. ciphertext address ────────────────────────────────────────────


def test_decrypt_if_ciphertext_passthrough_and_decrypt(monkeypatch):
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(pii, "_FERNET", fernet)
    token = fernet.encrypt(b"456 Oak Ave").decode()
    assert pii.decrypt_if_ciphertext(token) == "456 Oak Ave"
    assert pii.decrypt_if_ciphertext("plain text stays") == "plain text stays"
    assert pii.decrypt_if_ciphertext(None) is None


def test_decrypt_if_ciphertext_no_key_is_identity(monkeypatch):
    monkeypatch.setattr(pii, "_FERNET", None)
    assert pii.decrypt_if_ciphertext("gAAAA-looking-value") == "gAAAA-looking-value"


def test_mobile_jobs_list_decrypts_address_at_rest(db, monkeypatch):
    """End-to-end: with encryption active, the ORM stores ciphertext; the
    raw-SQL /api/mobile/jobs reader must hand back plaintext, not gAAAA…."""
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(pii, "_FERNET", fernet)
    _seed_tech_job(db, address="789 Pine Rd")

    # Sanity: the at-rest bytes really are ciphertext (the bug's precondition).
    from sqlalchemy import text
    raw = db.execute(text("SELECT address FROM customers")).scalar()
    assert raw.startswith("gAAAA")

    client = _build_app(db)
    body = client.get("/api/mobile/jobs").json()
    assert body["count"] == 1
    assert body["jobs"][0]["customer_address"] == "789 Pine Rd"


def test_mobile_jobs_list_plaintext_rows_pass_through(db, monkeypatch):
    """Mixed-state reality: 249 of 254 prod rows are plaintext — they must
    render unchanged even with the key loaded."""
    monkeypatch.setattr(pii, "_FERNET", None)  # write plaintext
    _seed_tech_job(db, address="12 Elm St")
    monkeypatch.setattr(pii, "_FERNET", Fernet(Fernet.generate_key()))  # read w/ key
    client = _build_app(db)
    body = client.get("/api/mobile/jobs").json()
    assert body["jobs"][0]["customer_address"] == "12 Elm St"


# ── 2. photo_type → kind ─────────────────────────────────────────────


# NOTE on the ``.hex`` path params below: these endpoints compare the path
# id against jobs.id in RAW SQL. SQLAlchemy's Uuid type stores 32-hex
# (no dashes) on SQLite but native uuid on PG — so the SQLite test DB only
# matches the undashed form, while prod PG accepts the dashed form the SPA
# sends (verified live 2026-07-16). The bug under test (photo_type →
# UndefinedColumn 500) is independent of the id format.


def test_my_job_detail_with_photo_no_longer_500s(db):
    j = _seed_tech_job(db)
    db.add(JobPhoto(
        id=uuid4(), company_id=TENANT, job_id=j.id, kind="before",
        url="/uploads/p1.jpg", filename="p1.jpg", caption="before shot",
        created_at=datetime.now(UTC),
    ))
    db.commit()
    client = _build_app(db)
    r = client.get(f"/api/mobile/my-jobs/{j.id.hex}")
    assert r.status_code == 200, r.text
    photos = r.json()["photos"]
    assert len(photos) == 1
    # Response shape keeps the documented photo_type key, served from `kind`.
    assert photos[0]["photo_type"] == "before"


def test_job_detail_photos_include_url_for_rendering(db):
    j = _seed_tech_job(db)
    db.add(JobPhoto(
        id=uuid4(), company_id=TENANT, job_id=j.id, kind="after",
        url="/uploads/p2.jpg", filename="p2.jpg", caption="done",
        created_at=datetime.now(UTC),
    ))
    db.commit()
    client = _build_app(db)
    r = client.get(f"/api/mobile/job/{j.id.hex}")
    assert r.status_code == 200, r.text
    photo = r.json()["photos"][0]
    assert photo["url"] == "/uploads/p2.jpg"
    assert photo["kind"] == "after"
    assert photo["caption"] == "done"


# ── 3. google-maps key reachable by technicians ──────────────────────


def test_google_maps_key_readable_by_technician(db):
    client = _build_app(db, role="technician")
    r = client.get("/api/settings/integrations/google-maps")
    assert r.status_code == 200, (
        f"{r.status_code}: {r.text} — the public read in branding_public.py "
        "must win over the admin-gated settings router (include order)."
    )
    assert set(r.json().keys()) == {"key", "configured"}


def test_google_maps_key_patch_stays_admin_gated(db):
    client = _build_app(db, role="technician")
    r = client.patch(
        "/api/settings/integrations/google-maps", json={"key": "sneaky"}
    )
    assert r.status_code == 403


def test_google_maps_key_readable_by_admin_too(db):
    client = _build_app(db, role="admin")
    assert client.get("/api/settings/integrations/google-maps").status_code == 200
