"""SS-10 Slice D — tests for sandbox lifecycle helpers.

Covers the behaviours the slice owes:

1. ``provision_sandbox`` stages exactly one row with active status and
   the expected ``tenant_id`` / ``subdomain``.
2. ``reset_sandbox`` updates ``last_reset_at`` and keeps status active.
3. ``teardown_sandbox`` updates ``torn_down_at`` and flips status to
   ``"torn_down"``.
4. ``reset_sandbox`` and ``teardown_sandbox`` return ``None`` when the
   given id is not found, and do not create rows.
5. None of the helpers call ``commit`` or ``flush`` (spy-style assertion).

SQLite's foreign-key enforcement is off by default, so rows can be
inserted without seeding the referenced ``tenants`` table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import gdx_dispatch.models.platform  # noqa: F401 — registers SS-2 tables on Base.metadata
import gdx_dispatch.models.platform_extensions  # noqa: F401 — registers SS-3 tables on Base.metadata
from gdx_dispatch.control.models import Base
from gdx_dispatch.core.sandbox import provision_sandbox, reset_sandbox, teardown_sandbox
from gdx_dispatch.models.platform_extensions import SandboxEnv

from uuid import UUID
_TENANT_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# ── provision ───────────────────────────────────────────────────────────────


def test_provision_creates_one_active_row(db):
    row = provision_sandbox(db, tenant_id=_TENANT_ID, subdomain="acme-sbx")
    db.commit()

    assert row.tenant_id == _TENANT_ID
    assert row.subdomain == "acme-sbx"
    assert row.status == "active"

    stored = db.query(SandboxEnv).all()
    assert len(stored) == 1
    assert stored[0].id == row.id
    assert stored[0].status == "active"


# ── reset ───────────────────────────────────────────────────────────────────


def test_reset_updates_last_reset_at_and_keeps_status_active(db):
    row = provision_sandbox(db, tenant_id=_TENANT_ID, subdomain="acme-sbx")
    db.commit()
    assert row.last_reset_at is None

    before = datetime.now(timezone.utc)
    out = reset_sandbox(db, _TENANT_ID, row.id)
    after = datetime.now(timezone.utc)
    db.commit()

    assert out is row
    assert out.status == "active"
    assert out.last_reset_at is not None
    assert before <= out.last_reset_at <= after


def test_reset_missing_id_returns_none_and_creates_no_rows(db):
    missing_id = uuid4()
    out = reset_sandbox(db, _TENANT_ID, missing_id)
    db.commit()

    assert out is None
    assert db.query(SandboxEnv).count() == 0


# ── teardown ────────────────────────────────────────────────────────────────


def test_teardown_updates_torn_down_at_and_sets_status_torn_down(db):
    row = provision_sandbox(db, tenant_id=_TENANT_ID, subdomain="acme-sbx")
    db.commit()
    assert row.torn_down_at is None

    before = datetime.now(timezone.utc)
    out = teardown_sandbox(db, _TENANT_ID, row.id)
    after = datetime.now(timezone.utc)
    db.commit()

    assert out is row
    assert out.status == "torn_down"
    assert out.torn_down_at is not None
    assert before <= out.torn_down_at <= after


def test_teardown_missing_id_returns_none_and_creates_no_rows(db):
    missing_id = uuid4()
    out = teardown_sandbox(db, _TENANT_ID, missing_id)
    db.commit()

    assert out is None
    assert db.query(SandboxEnv).count() == 0


# ── spy assertions: no commit / no flush ────────────────────────────────────


class _SpySession:
    """Records the helper's method calls; intentionally not a real Session."""

    def __init__(self, existing: SandboxEnv | None = None) -> None:
        self.calls: list[str] = []
        self._existing = existing

    def add(self, _row) -> None:
        self.calls.append("add")

    def commit(self) -> None:  # pragma: no cover — helpers must not call
        self.calls.append("commit")

    def flush(self) -> None:  # pragma: no cover — helpers must not call
        self.calls.append("flush")

    def get(self, _model, _pk):
        self.calls.append("get")
        return self._existing


def test_provision_does_not_commit_or_flush():
    spy = _SpySession()
    provision_sandbox(spy, tenant_id=_TENANT_ID, subdomain="spy-sbx")
    assert spy.calls == ["add"]


def test_reset_does_not_commit_or_flush_when_found():
    existing = SandboxEnv(tenant_id=_TENANT_ID, subdomain="spy-sbx", status="active")
    spy = _SpySession(existing=existing)
    out = reset_sandbox(spy, _TENANT_ID, uuid4())
    assert out is existing
    assert existing.status == "active"
    assert isinstance(existing.last_reset_at, datetime)
    assert spy.calls == ["get"]


def test_teardown_does_not_commit_or_flush_when_found():
    existing = SandboxEnv(tenant_id=_TENANT_ID, subdomain="spy-sbx", status="active")
    spy = _SpySession(existing=existing)
    out = teardown_sandbox(spy, _TENANT_ID, uuid4())
    assert out is existing
    assert existing.status == "torn_down"
    assert isinstance(existing.torn_down_at, datetime)
    assert spy.calls == ["get"]


def test_reset_missing_does_not_commit_or_flush():
    spy = _SpySession(existing=None)
    out = reset_sandbox(spy, _TENANT_ID, uuid4())
    assert out is None
    assert spy.calls == ["get"]


def test_teardown_missing_does_not_commit_or_flush():
    spy = _SpySession(existing=None)
    out = teardown_sandbox(spy, _TENANT_ID, uuid4())
    assert out is None
    assert spy.calls == ["get"]


def test_sandbox_id_type_is_uuid(db):
    row = provision_sandbox(db, tenant_id=_TENANT_ID, subdomain="acme-sbx")
    db.commit()
    assert isinstance(row.id, UUID)


# ── cross-tenant access rejection (2026-04-17 supervisor halt) ─────────

_OTHER_TENANT_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def test_reset_rejects_cross_tenant_returns_none(db):
    """Reset helper must NOT touch a sandbox owned by a different tenant.
    Returns None (same shape as missing) to avoid enumeration."""
    row = provision_sandbox(db, tenant_id=_TENANT_ID, subdomain="acme-sbx")
    db.commit()
    assert row.last_reset_at is None

    # Attacker-tenant attempts reset with victim-tenant's sandbox_id
    out = reset_sandbox(db, _OTHER_TENANT_ID, row.id)
    db.commit()

    assert out is None
    # State untouched
    db.refresh(row)
    assert row.last_reset_at is None
    assert row.status == "active"


def test_teardown_rejects_cross_tenant_returns_none(db):
    """Teardown helper must NOT touch a sandbox owned by a different tenant."""
    row = provision_sandbox(db, tenant_id=_TENANT_ID, subdomain="acme-sbx")
    db.commit()

    out = teardown_sandbox(db, _OTHER_TENANT_ID, row.id)
    db.commit()

    assert out is None
    db.refresh(row)
    assert row.torn_down_at is None
    assert row.status == "active"


def test_reset_and_teardown_accept_matching_tenant(db):
    """Positive control: same tenant_id succeeds — proves the guard
    rejects ONLY mismatches, not everything."""
    row = provision_sandbox(db, tenant_id=_TENANT_ID, subdomain="acme-sbx")
    db.commit()

    out_reset = reset_sandbox(db, _TENANT_ID, row.id)
    assert out_reset is row
    assert row.last_reset_at is not None

    out_teardown = teardown_sandbox(db, _TENANT_ID, row.id)
    assert out_teardown is row
    assert row.status == "torn_down"
    assert row.torn_down_at is not None
