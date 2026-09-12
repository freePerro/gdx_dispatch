"""Every tenant-settings write answers who / what / when (#558, invariant #1).

Six module routers — billing_terms, catalog_policy, dispatch_settings,
numbering, maps_provider, workflow — were the same twenty-line upsert with no
audit call anywhere in the path. A settings change had no actor and no record.

These tests drive each router through a TestClient and read `audit_logs` back.
The control is the point: without one, "0 rows" could just mean the table was
never wired. `test_control_*` proves the harness can see a row.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import (
    AuditLog,
    TenantBase,
    _get_db_dep,
    log_audit_event_sync,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

TID = "11111111-1111-1111-1111-111111111111"

# These routers bind raw parameters through `text()`. On Postgres psycopg binds
# a Decimal natively; sqlite3 has no adapter for it and raises
# "type 'decimal.Decimal' is not supported", which is why money-valued settings
# routers like billing_terms had no test coverage at all. Registering the
# adapter is a property of this test process only — no production path changes.
sqlite3.register_adapter(Decimal, float)

# (module, mount prefix, PATCH path, payload, expected action, settings columns)
CASES = [
    (
        "billing_terms", "/api/billing", "/terms",
        # late_fee_percent is a FRACTION (le=1), not a percentage.
        {"default_payment_terms_days": 45, "late_fee_percent": 0.015},
        "billing_terms_updated",
    ),
    (
        "catalog_policy", "/api/catalog-policy", "",
        {"catalog_require_description": True},
        "catalog_policy_updated",
    ),
    (
        "dispatch_settings", "/api/dispatch-settings", "",
        {"dispatch_block_save_no_tech": True},
        "dispatch_settings_updated",
    ),
    (
        "numbering", "/api/numbering", "/config",
        {"job_number_format": "JOB-{seq:04d}", "job_number_next_seq": 77},
        "numbering_config_updated",
    ),
    (
        # The highest-impact of the set: these flags gate the QuickBooks money
        # pull and signature-on-complete. Note the payload/column naming split —
        # the DB column is `workflow_require_signature_on_complete`, the flag a
        # reader sees is `require_signature_on_complete`, and the audit diff is
        # keyed the way the reader sees it.
        "workflow", "/api/workflow", "/flags",
        {"require_signature_on_complete": True, "qb_money_pull_paused": True},
        "workflow_flags_updated",
    ),
]


def _env(router, columns):
    """SQLite TestClient over one settings router.

    `tenant_settings` is a control-plane table and is NOT in ORM metadata — it
    is built by hand here from the router's OWN column list, because `_read`
    selects every one of them, not just the ones a given PATCH writes.
    `TenantBase.create_all` supplies `audit_logs`.

    The columns are declared with no type on purpose: SQLite then applies no
    affinity, so booleans and numbers round-trip as written and the before/after
    diff compares like with like.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tenant_settings (tenant_id TEXT PRIMARY KEY, "
            + ", ".join(columns)
            + ")"
        ))
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def _inject_tenant(request, call_next):
        request.state.tenant = {"id": TID}
        return await call_next(request)

    app.include_router(router)
    # Override `_get_db_dep`, NOT `audit_ready_db` itself.
    #
    # `audit_ready_db` declares `Depends(_get_db_dep)` — a separate function
    # that calls `get_db()` directly (core/audit.py), so overriding only
    # `get_db` leaves the handler on the REAL database and surfaces as
    # "no such table: tenant_settings". But overriding `audit_ready_db` is
    # worse: it replaces the dependency wholesale, so `ensure_audit_table`
    # never runs and these tests pass identically against handlers wired to
    # plain `get_db` — the change's central claim would have zero coverage.
    # Overriding the inner dependency keeps the real `audit_ready_db` in the
    # path while still pointing it at the test database.
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_get_db_dep] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-42", "role": "admin", "tenant_id": TID,
    }
    return TestClient(app, raise_server_exceptions=True), Session, engine


def _columns_of(module):
    """The tenant_settings columns a given router touches.

    Not one name: `_COLS` in most, `_FLAG_COLUMNS` in workflow, and numbering
    names its two inline. The test table has to carry every column the router's
    reader SELECTs, not just the ones a PATCH writes.
    """
    for attr in ("_COLS", "_FLAG_COLUMNS"):
        cols = getattr(module, attr, None)
        if cols:
            return list(cols)
    return ["job_number_format", "job_number_next_seq", "job_number_year_seen"]


def _audit_rows(Session, action: str) -> list[AuditLog]:
    db = Session()
    try:
        return (
            db.query(AuditLog)
            .filter(AuditLog.action == action)
            .order_by(AuditLog.created_at.desc())
            .all()
        )
    finally:
        db.close()


def test_control_the_harness_can_see_an_audit_row():
    """Without this, a '0 rows' result below could mean the table isn't wired."""
    from gdx_dispatch.modules.dispatch_settings.router import _COLS
    from gdx_dispatch.modules.dispatch_settings.router import router as r
    tc, Session, engine = _env(r, _COLS)
    try:
        db = Session()
        try:
            log_audit_event_sync(
                db, tenant_id=TID, user_id="control", action="control_probe",
                entity_type="tenant_settings", entity_id=TID, details={},
            )
            db.commit()
        finally:
            db.close()
        assert len(_audit_rows(Session, "control_probe")) == 1
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize("mod,prefix,path,payload,action", CASES)
def test_settings_patch_writes_an_audit_row(mod, prefix, path, payload, action):
    """The PATCH records who changed which setting, to what."""
    import importlib
    m = importlib.import_module(f"gdx_dispatch.modules.{mod}.router")
    tc, Session, engine = _env(m.router, _columns_of(m))
    try:
        resp = tc.patch(f"{prefix}{path}", json=payload)
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(Session, action)
        assert len(rows) == 1, f"one PATCH, one audit row — got {len(rows)}"
        row = rows[0]
        assert (row.user_id or row.actor_id) == "user-42", "the signed-in user, not 'system'"
        assert row.entity_type == "tenant_settings"
        assert str(row.entity_id) == TID

        details = row.details if row.details is not None else (row.payload or {})
        changed = details.get("changed") or {}
        for col in payload:
            assert col in changed, f"{col} moved but is not in the audit diff: {changed}"
            # The OLD value is the forensic half — "who set the job counter to
            # 77" is useless without "from 4021".
            assert "from" in changed[col] and "to" in changed[col], changed[col]
            assert changed[col]["to"] == payload[col], changed[col]
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize("mod,prefix,path,payload,action", CASES)
def test_a_noop_save_records_an_empty_diff(mod, prefix, path, payload, action):
    """Saving the same values twice must not replay every column as 'changed'.

    The second save is a no-op; recording it as a full-column change would make
    the trail useless for answering what actually moved.
    """
    import importlib
    m = importlib.import_module(f"gdx_dispatch.modules.{mod}.router")
    tc, Session, engine = _env(m.router, _columns_of(m))
    try:
        assert tc.patch(f"{prefix}{path}", json=payload).status_code == 200
        assert tc.patch(f"{prefix}{path}", json=payload).status_code == 200
        rows = _audit_rows(Session, action)
        assert len(rows) == 2, "both saves are recorded"
        second = rows[0]
        details = second.details if second.details is not None else (second.payload or {})
        assert (details.get("changed") or {}) == {}, (
            f"no-op save recorded a diff: {details.get('changed')}"
        )
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_maps_provider_patch_writes_an_audit_row():
    """maps_provider is the odd one out: the payload field is `provider` while
    the column is `maps_provider`, and it has no `_COLS` tuple. Lowest impact of
    the six (no frontend caller, and only one provider is wired), fixed for
    consistency — but it still has to leave a trail."""
    from gdx_dispatch.modules.maps_provider.router import router as r

    tc, Session, engine = _env(r, ["maps_provider"])
    try:
        resp = tc.patch("/api/maps/provider", json={"provider": "google_maps"})
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(Session, "maps_provider_updated")
        assert len(rows) == 1, f"one PATCH, one audit row — got {len(rows)}"
        row = rows[0]
        assert (row.user_id or row.actor_id) == "user-42"
        assert row.entity_type == "tenant_settings"
        details = row.details if row.details is not None else (row.payload or {})
        assert (details.get("changed") or {}).get("maps_provider", {}).get("to") == "google_maps"
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_a_failed_audit_write_takes_the_setting_change_with_it(monkeypatch):
    """The property the helper exists to provide: no unaudited change.

    `core/audit.py` states the contract — stage the audit row inside the
    caller's transaction so the change and its trail commit together. The
    `estimates_features` template this replaces commits the change FIRST and
    then writes the audit row, so an audit failure there leaves the setting
    changed with no record; that is the shape being fixed, and a test that only
    counts rows on the happy path cannot tell the two designs apart.

    Here the audit write raises. The setting must not survive.
    """
    from gdx_dispatch.modules.dispatch_settings.router import _COLS
    from gdx_dispatch.modules.dispatch_settings.router import router as r

    tc, Session, engine = _env(r, list(_COLS))
    try:
        import gdx_dispatch.core.settings_audit as sa

        def _boom(*a, **kw):
            raise RuntimeError("audit backend down")

        monkeypatch.setattr(sa, "log_audit_event_sync", _boom)

        with pytest.raises(RuntimeError):
            tc.patch("/api/dispatch-settings", json={"dispatch_block_save_no_tech": True})

        db = Session()
        try:
            row = db.execute(
                text("SELECT dispatch_block_save_no_tech FROM tenant_settings "
                     "WHERE tenant_id = :t"),
                {"t": TID},
            ).first()
        finally:
            db.close()
        assert row is None or not row[0], (
            "the settings change committed even though its audit row failed — "
            "that is an unaudited mutation, invariant #1"
        )
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()
