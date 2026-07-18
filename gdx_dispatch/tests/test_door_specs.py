"""Tests for gdx_dispatch.core.door_specs — reading captured CHI door specs off
a job's estimate lines' line_metadata (the estimate→job→downstream thread)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.door_specs import (
    door_specs_for_job,
    flatten_door_spec,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

# Import the full model graph so every FK the estimates tables reference
# (customers, jobs, labor_price_items, …) is registered on TenantBase.metadata
# before create_all — mirrors test_estimates.py.
import gdx_dispatch.models.tenant_models  # noqa: E402,F401
import gdx_dispatch.routers.estimates  # noqa: E402,F401

# A realistic captured CHI door spec, shaped like what the plugin's
# _estimate_line_draft() writes into EstimateLine.line_metadata.
CHI_SPEC = {
    "source": "chi_hubx",
    "Number": "QCD3807063",
    "Cart Name": "Malcolm Whynott",
    "Price": "1462.68",
    "Model": "Timeless 2283",
    "Description": "Insulated steel, short panel",
    "Size": "9'0\" x 7'6\"",
    "Width": "108",
    "Height": "90",
    "Color": "Walnut",
    "Spring": "Torsion",
    "Track": "12 IN.",
    "Rollers": "Nylon",
    "Cyclage": "25000",
    "Sprung Weight": "162.41",
    "Shipping Weight": "251.30",
    "Load Information": {"# Section Bundles": "5"},
    "Sections": [{"section": "Section 5", "window": "None"}],
    # Internal keys that must never surface downstream.
    "_raw": "Door Summary ... big page blob",
    "_url": "https://hubx.example/door/QCD3807063",
    "_image": "data:image/png;base64,AAAA",
}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db, job_id, *, chi=True, extra_line=True, deleted=False):
    """Seed an accepted estimate linked to job_id, with one CHI door line (and,
    by default, one ordinary non-CHI line that must be excluded)."""
    est = Estimate(
        id=uuid4(),
        job_id=job_id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        company_id="tenant-test",
        public_token=uuid4().hex,
        status="accepted",
        total=0,
        deleted_at=None,
    )
    if deleted:
        from gdx_dispatch.core.audit import utcnow
        est.deleted_at = utcnow()
    db.add(est)
    db.flush()
    if chi:
        db.add(EstimateLine(
            id=uuid4(), estimate_id=est.id, company_id="tenant-test",
            description="Timeless 2283 — 9'0 x 7'6 — Walnut",
            quantity=1, unit_price=2000, sort_order=1, line_metadata=dict(CHI_SPEC),
        ))
    if extra_line:
        # An ordinary labor line — no CHI source, must not be treated as a door.
        db.add(EstimateLine(
            id=uuid4(), estimate_id=est.id, company_id="tenant-test",
            description="Install labor", quantity=2, unit_price=95, sort_order=2,
            line_metadata={"sku": "LABOR", "vendor": "internal"},
        ))
    db.commit()
    return est


def test_returns_role_split_captured_door(db):
    job_id = uuid4()
    _seed(db, job_id)

    doors = door_specs_for_job(db, job_id)
    assert len(doors) == 1, "only the CHI line is a door; the labor line is excluded"
    door = doors[0]

    # identity — who/what/how-much
    assert door["identity"]["Model"] == "Timeless 2283"
    assert door["identity"]["Color"] == "Walnut"
    assert door["identity"]["Price"] == "1462.68"
    assert door["identity"]["Number"] == "QCD3807063"

    # installer — the build detail, and NOT the identity/receiving/window fields
    assert door["installer"]["Spring"] == "Torsion"
    assert door["installer"]["Track"] == "12 IN."
    assert door["installer"]["Rollers"] == "Nylon"
    assert "Sprung Weight" not in door["installer"]
    assert "Load Information" not in door["installer"]
    assert "Sections" not in door["installer"]
    assert "Model" not in door["installer"]

    # receiving — what arrives / how heavy
    assert door["receiving"]["Sprung Weight"] == "162.41"
    assert door["receiving"]["Shipping Weight"] == "251.30"
    assert door["receiving"]["Load Information"] == {"# Section Bundles": "5"}

    # windows — the Sections rows, verbatim
    assert door["windows"] == [{"section": "Section 5", "window": "None"}]

    assert door["quantity"] == 1


def test_internal_keys_never_surface(db):
    job_id = uuid4()
    _seed(db, job_id)
    door = door_specs_for_job(db, job_id)[0]
    for group in ("identity", "installer", "receiving"):
        keys = set(door[group])
        assert not any(k.startswith("_") for k in keys), f"leaked internal key in {group}"
        assert "source" not in keys


def test_flatten_produces_readable_strings(db):
    job_id = uuid4()
    _seed(db, job_id)
    door = door_specs_for_job(db, job_id)[0]
    flat = flatten_door_spec(door)

    assert flat["Model"] == "Timeless 2283"
    assert flat["Spring"] == "Torsion"
    # nested Load Information collapses to a readable string, not "[object Object]"
    assert flat["Load Information"] == "# Section Bundles: 5"
    assert flat["Windows"] == "1 section(s)"
    # every value is a string; nothing internal leaked
    assert all(isinstance(v, str) for v in flat.values())
    assert "_raw" not in flat and "source" not in flat


def test_no_estimate_returns_empty(db):
    # A job with no linked estimate at all.
    assert door_specs_for_job(db, uuid4()) == []


def test_non_chi_estimate_returns_empty(db):
    job_id = uuid4()
    _seed(db, job_id, chi=False)  # estimate exists, but no CHI door line
    assert door_specs_for_job(db, job_id) == []


def test_soft_deleted_estimate_is_ignored(db):
    job_id = uuid4()
    _seed(db, job_id, deleted=True)
    assert door_specs_for_job(db, job_id) == []


def test_bad_job_id_returns_empty(db):
    assert door_specs_for_job(db, "not-a-uuid") == []
    assert door_specs_for_job(db, None) == []


def test_accepts_str_job_id(db):
    job_id = uuid4()
    _seed(db, job_id)
    # Callers like the mobile endpoint pass the job id as a string path param.
    doors = door_specs_for_job(db, str(job_id))
    assert len(doors) == 1


# --- Office install-specs endpoint: prefer the captured door over the catalog ---

@pytest.fixture()
def install_client():
    """Mounts the real install_sheet router so we exercise the wired endpoint,
    not just the helper — this is the prefer-captured-over-ILIKE-catalog branch."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers.auth import get_current_user
    from gdx_dispatch.routers.install_sheet import router as install_router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    for ddl in (
        "CREATE TABLE IF NOT EXISTS tenant_module_grants (id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT)",
        "CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT, UNIQUE(company_id, module_key))",
    ):
        setup.execute(text(ddl))
    setup.execute(text("INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at) VALUES ('g1','tenant-test','jobs',datetime('now'),datetime('now'))"))
    setup.execute(text("INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) VALUES ('g2','tenant-test','jobs',datetime('now'),datetime('now'))"))
    setup.commit(); setup.close()

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(install_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1", "role": "admin", "tenant_id": "tenant-test"}
    tc = TestClient(app, raise_server_exceptions=True)
    tc._Session = Session  # for seeding
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def test_install_specs_endpoint_prefers_captured_door(install_client):
    job_id = uuid4()
    db = install_client._Session()
    try:
        _seed(db, job_id)
    finally:
        db.close()

    r = install_client.get(f"/api/jobs/{job_id}/install-specs")
    assert r.status_code == 200, r.text
    body = r.json()
    ds = body["door_specs"]
    assert ds is not None, "captured door should populate door_specs"
    # The REAL quoted door (from line_metadata), not a fuzzy catalog match.
    assert ds["Model"] == "Timeless 2283"
    assert ds["Color"] == "Walnut"
    assert ds["Spring"] == "Torsion"
    assert ds["Windows"] == "1 section(s)"
