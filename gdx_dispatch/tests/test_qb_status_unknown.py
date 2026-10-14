"""GDXA-1135 — a token-store read error is "status unknown", not connected:false.

A tenant connected after the QBTokenStore rewrite has no legacy QBConnection
row, so /api/qb/status and /api/qb/dashboard falling back to the legacy table on
ANY token-store error answered a definite ``connected: false`` and the UI
offered a fresh Connect. Only a missing table may fall back; anything else is a
503, which SettingsView and QuickbooksView render as "Status unknown".

The non-missing-table error here is a real one, not a mock: the token table
exists but lacks a column the model selects, so SQLite raises
``OperationalError: no such column``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.quickbooks import QBConnection, QBEntityMap
from gdx_dispatch.modules.ledger.service import ensure_gl_seed
from gdx_dispatch.modules.quickbooks.oauth import QBTokenStore
from gdx_dispatch.modules.quickbooks.router import (
    _is_missing_table,
    qb_dashboard,
    qb_status,
)

TENANT = "tenant-1"
_USER = {"sub": "t"}


@pytest.fixture()
def db(monkeypatch) -> Session:
    monkeypatch.delenv("GDX_ENV", raising=False)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    QBConnection.__table__.create(bind=engine, checkfirst=True)
    QBEntityMap.__table__.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        ensure_gl_seed(session, TENANT)
        session.commit()
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({
        "type": "http", "method": "GET", "path": "/not-a-route/direct-handler-call",
        "headers": [], "query_string": b"",
    })
    req.state.tenant = {"id": TENANT}
    return req


def _create_token_store(db: Session) -> None:
    QBTokenStore.__table__.create(bind=db.get_bind(), checkfirst=True)


def _break_token_store(db: Session) -> None:
    """The table exists, so this is not the missing-table case; the select
    still fails, as any timeout or lock would."""
    _create_token_store(db)
    db.execute(text("ALTER TABLE qb_token_store DROP COLUMN auth_state"))
    db.commit()


def _seed_token(db: Session, realm_id: str = "realm-1") -> None:
    now = datetime.now(timezone.utc)
    db.add(QBTokenStore(
        tenant_id=TENANT, realm_id=realm_id, environment="sandbox",
        access_token_enc="x", refresh_token_enc="y",
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_expires_at=now + timedelta(days=90),
    ))
    db.commit()


# ── the defect: a read error must not read as "not connected" ─────────────

def test_status_token_read_error_with_no_legacy_row_is_503_not_disconnected(db):
    _break_token_store(db)
    with pytest.raises(HTTPException) as exc_info:
        qb_status(request=_request(), current_user=_USER, db=db)
    assert exc_info.value.status_code == 503


def test_status_legacy_read_error_is_503(db, monkeypatch):
    """The legacy fallback query was outside any try: same rule there."""
    real_execute = db.execute

    def _execute(stmt, *a, **kw):
        if "qb_connections" in str(stmt):
            raise OperationalError(str(stmt), {}, Exception("database is locked"))
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", _execute)
    with pytest.raises(HTTPException) as exc_info:
        qb_status(request=_request(), current_user=_USER, db=db)
    assert exc_info.value.status_code == 503


def test_dashboard_token_read_error_is_503_not_disconnected(db):
    _break_token_store(db)
    with pytest.raises(HTTPException) as exc_info:
        qb_dashboard(request=_request(), current_user=_USER, db=db)
    assert exc_info.value.status_code == 503


# ── what must keep working ────────────────────────────────────────────────

def test_status_missing_token_table_falls_back_to_legacy(db):
    # No qb_token_store table and no legacy row: genuinely not connected.
    out = qb_status(request=_request(), current_user=_USER, db=db)
    assert out["connected"] is False


def test_status_and_dashboard_token_row_reads_connected(db):
    _create_token_store(db)
    _seed_token(db)
    assert qb_status(request=_request(), current_user=_USER, db=db)["connected"] is True
    assert qb_dashboard(request=_request(), current_user=_USER, db=db)["connected"] is True


def test_two_realms_for_one_tenant_read_connected_not_503(db):
    """The store is unique on (tenant, realm), so a reconnect to a second
    company leaves two rows; that is a connected tenant, not a read error."""
    _create_token_store(db)
    _seed_token(db, "realm-1")
    _seed_token(db, "realm-2")
    assert qb_status(request=_request(), current_user=_USER, db=db)["connected"] is True
    assert qb_dashboard(request=_request(), current_user=_USER, db=db)["connected"] is True


def test_dashboard_missing_token_table_reads_not_connected(db):
    out = qb_dashboard(request=_request(), current_user=_USER, db=db)
    assert out["connected"] is False


# ── the classifier ────────────────────────────────────────────────────────

class _PgError(Exception):
    def __init__(self, pgcode: str) -> None:
        super().__init__(f"pg {pgcode}")
        self.pgcode = pgcode


def test_is_missing_table_matches_only_a_missing_table():
    assert _is_missing_table(ProgrammingError("s", {}, _PgError("42P01")))
    assert not _is_missing_table(ProgrammingError("s", {}, _PgError("42703")))  # column
    assert not _is_missing_table(OperationalError("s", {}, _PgError("57014")))  # timeout
    assert _is_missing_table(OperationalError("s", {}, Exception("no such table: qb_token_store")))
    assert not _is_missing_table(OperationalError("s", {}, Exception("database is locked")))
    assert not _is_missing_table(OperationalError("s", {}, Exception("no such column: auth_state")))
