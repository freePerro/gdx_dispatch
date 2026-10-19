"""install_specs' catalog fallback (GDXA-1647).

For a job with no captured CHI door, ``GET /api/jobs/{id}/install-specs``
matches estimate lines against the door catalog. The match used ``ILIKE``,
which SQLite does not have, and it ran once per line inside a bare
``except Exception: pass``, so on Postgres one failure poisoned the
transaction and every later line failed and logged again.
"""
from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401  (full FK graph for create_all)
import gdx_dispatch.routers.estimates  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    ChiDoorCatalog,
    CustomCatalog,
    CustomCatalogItem,
    DoorSpec,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers import install_sheet
from gdx_dispatch.routers.auth import get_current_user

LOGGER = "gdx_dispatch.routers.install_sheet"


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(text(
        "CREATE TABLE IF NOT EXISTS company_module_grants (id TEXT PRIMARY KEY, company_id TEXT, "
        "module_key TEXT, granted_at TEXT, created_at TEXT, expires_at TEXT, UNIQUE(company_id, module_key))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
        "VALUES ('g1','tenant-test','jobs',datetime('now'),datetime('now'))"
    ))
    setup.commit()
    setup.close()

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

    app.include_router(install_sheet.router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1", "role": "admin", "tenant_id": "tenant-test"}
    tc = TestClient(app, raise_server_exceptions=True)
    tc._Session = Session
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_estimate(Session, job_id, descriptions):
    """An estimate on job_id whose lines carry no captured CHI spec."""
    db = Session()
    try:
        est = Estimate(
            id=uuid4(), job_id=job_id, estimate_number=f"EST-{uuid4().hex[:8]}",
            company_id="tenant-test", public_token=uuid4().hex, status="accepted", total=0,
        )
        db.add(est)
        db.flush()
        for i, desc in enumerate(descriptions):
            db.add(EstimateLine(
                id=uuid4(), estimate_id=est.id, company_id="tenant-test",
                description=desc, quantity=1, unit_price=100, sort_order=i,
                line_metadata={},
            ))
        db.commit()
    finally:
        db.close()


def test_chi_catalog_door_matched_case_insensitively(client):
    db = client._Session()
    try:
        db.add(ChiDoorCatalog(
            sku="CHI-4283", brand="CHI", model_number="4283", description="Raised Panel Steel Door",
            color="White", is_active=True,
        ))
        db.commit()
    finally:
        db.close()
    job_id = uuid4()
    # Mixed case on both sides: the match must not depend on ILIKE.
    _seed_estimate(client._Session, job_id, ["Install labor", "RAISED PANEL steel"])

    r = client.get(f"/api/jobs/{job_id}/install-specs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doors"] == []
    assert body["door_specs"] is not None
    assert body["door_specs"]["model_number"] == "4283"
    assert body["door_specs"]["color"] == "White"


def test_custom_catalog_door_matched_by_sku(client):
    db = client._Session()
    try:
        cat = CustomCatalog(name="House doors", product_class="door")
        db.add(cat)
        db.flush()
        item = CustomCatalogItem(
            catalog_id=cat.id, sku="HD-9070", name="House 9x7", product_class="door", active=True,
        )
        db.add(item)
        db.flush()
        db.add(DoorSpec(catalog_item_id=item.id, manufacturer="House", model_number="H97", color="Almond"))
        db.commit()
    finally:
        db.close()
    job_id = uuid4()
    _seed_estimate(client._Session, job_id, ["hd-9070"])

    r = client.get(f"/api/jobs/{job_id}/install-specs")
    assert r.status_code == 200, r.text
    ds = r.json()["door_specs"]
    assert ds["model_number"] == "H97"
    assert ds["brand"] == "House"


def test_retired_chi_door_is_not_matched(client):
    db = client._Session()
    try:
        db.add(ChiDoorCatalog(sku="CHI-OLD", model_number="OLD1", description="Retired door", is_active=False))
        db.commit()
    finally:
        db.close()
    job_id = uuid4()
    _seed_estimate(client._Session, job_id, ["Retired door"])

    r = client.get(f"/api/jobs/{job_id}/install-specs")
    assert r.status_code == 200, r.text
    assert r.json()["door_specs"] is None


def test_query_failure_stops_after_one_line_and_logs_once(client, monkeypatch, caplog):
    """A broken catalog query (here: a table that does not exist) degrades to
    door_specs null with one log line, not one per estimate line. SQLite never
    poisons a transaction, so this proves the stop, not the containment; the
    Postgres test below proves the containment."""
    monkeypatch.setattr(install_sheet, "_CATALOG_DOOR_SQL", text("SELECT * FROM no_such_table WHERE :q = :q"))
    job_id = uuid4()
    _seed_estimate(client._Session, job_id, ["Door one", "Door two", "Door three"])

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        r = client.get(f"/api/jobs/{job_id}/install-specs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["door_specs"] is None
    assert len(body["lines"]) == 3
    failures = [rec for rec in caplog.records if rec.name == LOGGER and "catalog door lookup failed" in rec.getMessage()]
    assert len(failures) == 1


def test_malformed_job_id_is_404_without_traceback(client, caplog):
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        r = client.get("/api/jobs/not-a-uuid/install-specs")
    assert r.status_code == 404
    assert not [rec for rec in caplog.records if rec.name == LOGGER]


def test_install_sheet_renders_catalog_door_for_non_chi_job(client):
    """The printable sheet runs the same lookup; on SQLite its ILIKE used to 500."""
    from gdx_dispatch.models.tenant_models import Job

    job_id = uuid4()
    db = client._Session()
    try:
        db.add(Job(id=job_id, title="Door install", company_id="tenant-test", is_return_visit=False))
        db.add(ChiDoorCatalog(sku="CHI-2250", model_number="2250-SHEET", description="Flush Steel", is_active=True))
        db.commit()
    finally:
        db.close()
    _seed_estimate(client._Session, job_id, ["flush STEEL"])

    r = client.get(f"/api/jobs/{job_id}/install-sheet")
    assert r.status_code == 200, r.text[:300]
    assert "2250-SHEET" in r.text


def test_pg_failed_lookup_leaves_the_transaction_committable(pg_test_engine, caplog):
    """On Postgres a failed statement aborts the transaction. The lookup must
    contain that: the request's session still commits work it already held.
    Remove the contained_read around the lookup and the commit below does NOT
    raise: Postgres turns COMMIT on an aborted transaction into ROLLBACK, so
    the row is silently gone and the count assertion is what goes red."""
    from sqlalchemy.orm import Session

    with pg_test_engine.begin() as c:
        c.execute(text("CREATE TABLE gdxa1647_row (id integer PRIMARY KEY)"))
        c.execute(text("ALTER TABLE chi_door_catalog RENAME TO chi_door_catalog_gone"))

    with Session(pg_test_engine, autoflush=False) as db:
        db.execute(text("INSERT INTO gdxa1647_row (id) VALUES (1)"))
        lines = [{"description": "Door one"}, {"description": "Door two"}]
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert install_sheet._catalog_door(db, lines, "job-1") is None
        db.commit()

    with pg_test_engine.connect() as other:
        assert other.execute(text("SELECT count(*) FROM gdxa1647_row")).scalar() == 1
    failures = [rec for rec in caplog.records if rec.name == LOGGER and "catalog door lookup failed" in rec.getMessage()]
    assert len(failures) == 1
