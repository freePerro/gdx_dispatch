"""Regression test: QB token read path must match the OAuth callback write path.

2026-05-20 incident: the modules-based OAuth callback wrote fresh tokens to
the new `qb_token_store` table, while the legacy `get_qb_client` in
`gdx_dispatch/core/quickbooks.py` read from the old `qb_connections` table. Doug
reconnected QuickBooks → tokens landed in qb_token_store → forecasting
sync (which called the legacy reader) silently saw stale qb_connections
tokens → 401 from Intuit despite a fresh successful OAuth dance.

This test pins the contract: when `save_tokens()` (the canonical writer
called by gdx_dispatch/modules/quickbooks/router.py's `/oauth/callback`) puts
tokens into qb_token_store, the forecasting sync MUST find them there.

If a future change re-introduces a split (a second token table, a writer
that bypasses save_tokens, a reader that ignores qb_token_store) this
test fails before it ships.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
# Side-effect imports: register the models onto TenantBase.metadata so
# create_all picks them up. Without these the tables don't exist.
from gdx_dispatch.modules.forecasting.models import QBRecurringTransaction  # noqa: F401
from gdx_dispatch.modules.forecasting import qb_recurring as qb_recurring_helper
from gdx_dispatch.modules.quickbooks.oauth import QBTokenStore, save_tokens  # noqa: F401


@pytest.fixture()
def tenant_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    yield db
    db.close()
    engine.dispose()


def test_sync_reads_from_canonical_qb_token_store(tenant_db, monkeypatch):
    """The canonical writer (save_tokens) and the forecasting reader
    must share storage. Bug-2026-05-20: they didn't."""
    captured = {}

    def fake_fetch(realm_id: str, access_token: str):
        captured["realm_id"] = realm_id
        captured["access_token"] = access_token
        return []

    monkeypatch.setattr(qb_recurring_helper, "_fetch_recurring", fake_fetch)

    save_tokens(
        tenant_id="tenant-uuid-A",
        realm_id="qbo-realm-9999",
        access_token="canonical-access-token-XYZ",
        refresh_token="canonical-refresh-token-ABC",
        expires_in=3600,
        refresh_expires_in=8726400,
        db=tenant_db,
    )

    qb_recurring_helper.sync_recurring_for_tenant("tenant-uuid-A", tenant_db)

    assert captured["realm_id"] == "qbo-realm-9999", (
        "Forecasting sync did not read realm_id from qb_token_store — "
        "storage path has split. Compare gdx_dispatch/modules/quickbooks/oauth.py "
        "save_tokens() vs the read site."
    )
    assert captured["access_token"] == "canonical-access-token-XYZ", (
        "Forecasting sync did not decrypt+read access_token from "
        "qb_token_store. If save_tokens encrypts but the reader doesn't "
        "decrypt (or vice-versa), this is the canary."
    )


def test_sync_fails_loudly_when_no_token_store_row(tenant_db, monkeypatch):
    """When no row exists (tenant never connected), we want a clear
    QBAuthError, not a silent empty result or a 500."""
    monkeypatch.setattr(qb_recurring_helper, "_fetch_recurring", lambda *a, **kw: [])

    from gdx_dispatch.core.quickbooks import QBAuthError
    with pytest.raises(QBAuthError):
        qb_recurring_helper.sync_recurring_for_tenant("never-connected-tenant", tenant_db)


def test_legacy_qb_connections_path_does_not_shadow_token_store(tenant_db, monkeypatch):
    """Belt-and-braces: even if a row exists in the legacy qb_connections
    table (e.g., during a migration window), the token_store row must
    take precedence. The 2026-05-20 bug was the opposite — legacy won."""
    from gdx_dispatch.core.quickbooks import QBConnection
    from datetime import datetime, timedelta, UTC

    now = datetime.now(UTC)
    tenant_db.add(QBConnection(
        tenant_id="tenant-uuid-B",
        realm_id="LEGACY-realm-0000",
        access_token="STALE-legacy-token",
        refresh_token="STALE-legacy-refresh",
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_expires_at=now + timedelta(days=100),
    ))
    save_tokens(
        tenant_id="tenant-uuid-B",
        realm_id="FRESH-realm-1111",
        access_token="FRESH-token",
        refresh_token="FRESH-refresh",
        expires_in=3600,
        refresh_expires_in=8726400,
        db=tenant_db,
    )
    tenant_db.commit()

    captured = {}

    def fake_fetch(realm_id: str, access_token: str):
        captured["realm_id"] = realm_id
        captured["access_token"] = access_token
        return []

    monkeypatch.setattr(qb_recurring_helper, "_fetch_recurring", fake_fetch)
    qb_recurring_helper.sync_recurring_for_tenant("tenant-uuid-B", tenant_db)

    assert captured["realm_id"] == "FRESH-realm-1111", (
        f"Sync used legacy qb_connections realm ({captured['realm_id']}) "
        "instead of the canonical qb_token_store realm. Storage split "
        "reintroduced."
    )
    assert captured["access_token"] == "FRESH-token"


def test_sync_table_absent_raises_qbautherror_not_500(monkeypatch):
    """Audit follow-up 2026-05-20: if a tenant's DB predates qb_token_store
    (never paved or never migrated), the select raises ProgrammingError.
    The sync MUST surface QBAuthError with an attributable message — not
    a 500 with a poisoned session."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Deliberately DO NOT create the qb_token_store table.
    from gdx_dispatch.models.tenant_models import Base as _TB
    # Create only the tables qb_recurring needs (NOT qb_token_store).
    QBRecurringTransaction.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    monkeypatch.setattr(qb_recurring_helper, "_fetch_recurring", lambda *a, **kw: [])

    from gdx_dispatch.core.quickbooks import QBAuthError
    with pytest.raises(QBAuthError) as excinfo:
        qb_recurring_helper.sync_recurring_for_tenant("tenant-no-table", db)
    assert "qb_token_store missing" in str(excinfo.value)
    db.close()
    engine.dispose()
