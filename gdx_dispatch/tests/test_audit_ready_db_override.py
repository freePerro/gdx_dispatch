"""`audit_ready_db` honors `app.dependency_overrides[get_db]` (GDXA-218).

It used to declare `Depends(_get_db_dep)`, a wrapper that called `get_db()`
inside its own body. FastAPI keys overrides on the callable a route declares,
so overriding `get_db` never reached it: a route on `audit_ready_db` got a
session on the real application engine while every other route in the same
app got the test's. Six test harnesses carried a second override of the
wrapper to work around it.

This test overrides ONLY `get_db`, and checks which engine the handler's
session is bound to. On the old wiring it fails: the handler is bound to
`core.database.engine`, not the test engine.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import audit_ready_db
from gdx_dispatch.core.database import get_db


def test_overriding_get_db_alone_reaches_audit_ready_db() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.get("/audit-ready")
    def _audit_ready(db: Annotated[Session, Depends(audit_ready_db)]) -> dict:
        return {"test_engine": db.get_bind() is engine}

    @app.get("/plain")
    def _plain(db: Annotated[Session, Depends(get_db)]) -> dict:
        return {"test_engine": db.get_bind() is engine}

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as client:
        assert client.get("/plain").json() == {"test_engine": True}  # control
        assert client.get("/audit-ready").json() == {"test_engine": True}

    # And the real `audit_ready_db` ran on that session — the override did not
    # replace it: `ensure_audit_table` created the table on the TEST engine.
    assert "audit_logs" in inspect(engine).get_table_names()
