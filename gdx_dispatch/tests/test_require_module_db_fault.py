"""require_module answers 503, not 403 "not enabled", when the grant cannot be read (GDXA-1197).

A DB fault used to be swallowed into ``is_module_enabled() -> False`` and the
gate then said "Module 'x' is not enabled": a definite off state that clients
(SettingsView's isQbModuleOff) render as off. These tests run the real gate on
a real SQLite database; the fault is a real OperationalError from an engine
whose file cannot be opened, not a mock.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import is_module_enabled, require_module

TENANT = str(uuid4())


def _app(session_factory) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _tenant(request: Request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    @app.get("/gated", dependencies=[Depends(require_module("quickbooks"))])
    def _gated() -> dict:
        return {"ok": True}

    def _db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    return app


@pytest.fixture
def good_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'grants.db'}")
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def broken_sessions(tmp_path):
    # A database file inside a directory that does not exist: every connect
    # raises sqlite3.OperationalError ("unable to open database file").
    engine = create_engine(f"sqlite:///{tmp_path / 'missing-dir' / 'grants.db'}")
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_db_fault_answers_503_not_module_off(broken_sessions):
    resp = TestClient(_app(broken_sessions)).get("/gated")
    assert resp.status_code == 503, resp.text
    assert "not enabled" not in resp.text


def test_genuinely_disabled_module_still_403s(good_sessions):
    client = TestClient(_app(good_sessions))
    # First check seeds every module for the company, so the gate opens.
    assert client.get("/gated").status_code == 200
    with good_sessions() as db:
        db.execute(
            text("DELETE FROM company_module_grants WHERE company_id = :c AND module_key = 'quickbooks'"),
            {"c": TENANT},
        )
        db.commit()
    resp = client.get("/gated")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Module 'quickbooks' is not enabled"


def test_is_module_enabled_still_returns_false_on_db_fault(broken_sessions):
    """Non-gate callers (bank_feeds callback, phone_com senders) branch on a
    bool and rely on a fault failing closed; that contract is unchanged."""
    from starlette.requests import Request as StarletteRequest

    req = StarletteRequest({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    req.state.tenant = {"id": TENANT}
    db = broken_sessions()
    try:
        assert is_module_enabled("quickbooks", req, db) is False
    finally:
        db.close()
