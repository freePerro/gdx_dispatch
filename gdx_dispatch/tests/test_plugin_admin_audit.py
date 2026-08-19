"""The plugin admin surface must leave an audit trail (invariant #1).

Installing a plugin is code execution with backend access, so "who did it, what
changed, when" is load-bearing here in a way it isn't for a cosmetic setting.
The router's own docstrings claimed "owner-only + audited" while the module
contained zero `log_audit_event` calls — install, upload, restart and consent
all committed silently. These tests are the guard against that returning.

`test_every_mutating_route_audits` is the sibling sweep in executable form: a
NEW mutation endpoint added to this router without an audit call fails here.

Imports the router module (core.database etc.) → runs in the docker image.
"""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import audit as audit_core
from gdx_dispatch.core.audit import AuditLog, TenantBase, verify_audit_chain
from gdx_dispatch.routers import admin_plugins as ap
from gdx_dispatch.routers import browser_proxy as bp

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture
def db():
    """Isolated in-memory SQLite session carrying the audit table."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _rows(db) -> list[AuditLog]:
    return list(db.execute(select(AuditLog)).scalars().all())


def test_audit_writes_a_chain_valid_row_with_the_real_actor(db):
    ap._audit(
        db,
        None,
        {"sub": "user-42", "role": "owner"},
        "plugin.registered",
        entity_type="plugin",
        entity_id="gdx-plugin-example",
        details={"package": "gdx-plugin-example", "version": "1.0"},
    )
    db.commit()

    rows = _rows(db)
    assert len(rows) == 1, "the mutation must leave exactly one audit row"
    row = rows[0]
    assert row.action == "plugin.registered"
    assert row.entity_type == "plugin"
    assert row.entity_id == "gdx-plugin-example"
    # The acting owner, not "system" — an install attributed to nobody is the
    # failure this whole file exists to prevent.
    assert row.user_id == "user-42"
    assert row.details["version"] == "1.0"
    assert verify_audit_chain(db) is True


def test_audit_failure_undoes_the_pending_mutation(db, monkeypatch):
    """No silent writes: if the trail can't be written, the change must not stand.

    Asserts the real thing — that a pending row is GONE afterwards — rather than
    that rollback() was called, which would pass even if rollback did nothing.
    """
    db.execute(text("CREATE TABLE demo_registry (package TEXT)"))
    db.commit()
    db.execute(text("INSERT INTO demo_registry (package) VALUES ('gdx-plugin-example')"))
    assert db.execute(text("SELECT count(*) FROM demo_registry")).scalar() == 1

    def _boom(*a, **k):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(audit_core, "log_audit_event_sync", _boom)

    with pytest.raises(ap.HTTPException) as e:
        ap._audit(db, None, {"sub": "u1"}, "plugin.registered", entity_type="plugin")

    assert e.value.status_code == 500
    # The uncommitted INSERT must be gone — the change did not survive its
    # missing audit row.
    assert db.execute(text("SELECT count(*) FROM demo_registry")).scalar() == 0


def _consent_rows(db) -> list:
    return list(db.execute(text("SELECT plugin_key FROM plugin_consent")).fetchall())


def test_consent_grant_and_its_audit_row_commit_together(db, monkeypatch):
    """The success path, on an engine whose audit table is NOT pre-initialized.

    This is the case that hides bugs: `ensure_audit_table` commits the first time
    it runs for an engine, so if it ran mid-handler it would harden whatever was
    staged. `audit_ready_db` moves it before the handler — here we call it
    explicitly, exactly as the FastAPI dependency does.
    """
    monkeypatch.setattr(ap, "fetch_permissions", lambda key: ["browser"])
    audit_core.audit_ready_db(db)

    ap.consent_plugin("chipricing", request=None, user={"sub": "u1", "role": "owner"}, db=db)

    assert [r.action for r in _rows(db)] == ["plugin.consent_granted"]
    assert _consent_rows(db) == [("chipricing",)]


def test_failed_audit_undoes_a_staged_consent_grant(db, monkeypatch):
    """The other direction, and the one a previous round of this fix got wrong.

    With the grant staged and the audit write failing, the grant must NOT
    survive. It used to: `_log_audit_event_impl` calls `ensure_audit_table`,
    which commits the first time it runs for an engine — hardening the staged
    consent a moment before the audit row failed. `audit_ready_db` runs that
    initialization up front so nothing is pending when it commits.
    """
    monkeypatch.setattr(ap, "fetch_permissions", lambda key: ["browser"])
    audit_core.audit_ready_db(db)

    # Fail at the FLUSH, not by stubbing log_audit_event_sync: the real helper
    # must run so that ensure_audit_table's transaction control is in play. That
    # is the whole bug — stubbing the helper skips it and the test proves nothing.
    def _flush_fails(*a, **k):
        raise RuntimeError("audit flush failed")

    monkeypatch.setattr(db, "flush", _flush_fails)

    with pytest.raises(ap.HTTPException) as e:
        ap.consent_plugin("chipricing", request=None,
                          user={"sub": "u1", "role": "owner"}, db=db)
    assert e.value.status_code == 500

    monkeypatch.undo()
    db.rollback()
    assert _consent_rows(db) == [], (
        "the consent grant survived even though its audit row failed"
    )


def test_failed_consent_insert_leaves_no_audit_row(db, monkeypatch):
    """The real atomicity proof: the genuine INSERT fails, not a stubbed callable.

    Round 1 of this fix audited BEFORE record_consent, and record_consent's
    ensure_consent_table commits its DDL — which hardened the audit row on its
    own and left a record of a grant that never happened. A pre-made table with
    a CHECK constraint makes the actual INSERT fail so that regression cannot
    return unnoticed.
    """
    monkeypatch.setattr(ap, "fetch_permissions", lambda key: ["browser"])
    audit_core.audit_ready_db(db)
    # ensure_consent_table's CREATE TABLE IF NOT EXISTS becomes a no-op against
    # this, so record_consent runs its real INSERT and the CHECK rejects it.
    db.execute(text("""
        CREATE TABLE plugin_consent (
            plugin_key TEXT PRIMARY KEY CHECK (plugin_key <> 'chipricing'),
            permissions TEXT NOT NULL,
            consented_by TEXT,
            consented_at TIMESTAMP,
            declared_events TEXT,
            declared_fingerprint TEXT
        )
    """))
    db.commit()

    with pytest.raises(Exception):
        ap.consent_plugin("chipricing", request=None,
                          user={"sub": "u1", "role": "owner"}, db=db)
    db.rollback()

    assert _consent_rows(db) == [], "the grant should not have been recorded"
    assert [r.action for r in _rows(db)] == [], (
        "an audit row survived a consent grant that never happened"
    )


#: Both plugin routers, and both spellings of the call — admin_plugins goes
#: through its local `_audit` wrapper, browser_proxy calls the core helper
#: directly. A sweep that knew only one of each would pass by construction.
_PLUGIN_ROUTERS = (ap.router, bp.router)
_AUDIT_CALLS = ("_audit(", "audit_or_rollback(")


def _mutating_routes():
    for router in _PLUGIN_ROUTERS:
        for route in router.routes:
            methods = getattr(route, "methods", set()) or set()
            if methods & _MUTATING:
                yield route, methods


def test_every_mutating_route_audits():
    """Sibling sweep, enforced: every state-changing route on the plugin surface
    records who did it. Adding a POST/DELETE here without an audit call is an
    incomplete mutation endpoint, not a finished one.

    Honest about its own reach: this is a STATIC check on handler source. It
    proves an audit call is present, not that it is reached on every branch —
    a call after an early return would still satisfy it. The execution proof
    lives in the per-endpoint tests (here and in test_browser_credentials.py);
    this is the net that catches a whole endpoint added with no audit at all.
    """
    missing = []
    for route, methods in _mutating_routes():
        source = inspect.getsource(route.endpoint)
        if not any(call in source for call in _AUDIT_CALLS):
            missing.append(f"{sorted(methods)} {route.path} -> {route.endpoint.__name__}")

    assert not missing, "mutating plugin routes with no audit call: " + "; ".join(missing)


def test_the_sweep_actually_sees_both_routers():
    """Guard the guard: an empty or single-router sweep would be vacuously green."""
    seen = list(_mutating_routes())
    assert len(seen) >= 8, f"expected the known mutating routes, saw {len(seen)}"
    modules = {r.endpoint.__module__ for r, _ in seen}
    assert any("browser_proxy" in m for m in modules), "browser_proxy is not being swept"
    assert any("admin_plugins" in m for m in modules), "admin_plugins is not being swept"


def test_sweep_would_fail_on_an_unaudited_route():
    """Prove the sweep can actually fail — a green test that cannot go red is a prop."""

    def unaudited_handler():
        return {"ok": True}

    source = inspect.getsource(unaudited_handler)
    assert not any(call in source for call in _AUDIT_CALLS)
