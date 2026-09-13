"""Who / what / when for the workflows + error-sink mutations (#558, invariant #1).

Three surfaces, one issue:

* ``modules/workflows`` — create / update / soft-delete of a WorkflowRule. The
  engine implements exactly one action (``IMPLEMENTED_ACTIONS ==
  ("send_email",)``), so every rule written through these routes is a standing
  order to send customer-facing email. None of the three wrote an audit row.
  They also take no ``user`` parameter — auth is a router-level dependency — so
  the actor has to come from ``request.state.user``, which
  ``routers/auth/core.py`` stashes on every authenticated request. That is the
  risky half of this change, and ``test_*_actor_is_the_real_user`` is what
  proves it: the assertion is "user-42", never "system".
* ``modules/error_sink`` — PATCH .../resolve. Not unattributed (it already
  stamps ``resolved_by``/``resolved_at`` on every row), but ``resolve_group``
  makes it a BULK operation, and no per-row column can say "one request closed
  three of these". The audit row carries the count.

The control test is the point of the file's shape: without one, "0 audit rows"
could just mean the table was never wired. ``test_control_*`` proves the
harness can see a row at all.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
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
USER = "user-42"

SERVER_ERRORS_DDL = """
CREATE TABLE server_errors (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    method TEXT,
    path TEXT,
    status_code INTEGER,
    exception_class TEXT,
    exception_message TEXT,
    request_id TEXT,
    user_id TEXT,
    user_email TEXT,
    query_string TEXT,
    referer TEXT,
    user_agent TEXT,
    traceback TEXT,
    git_sha TEXT,
    group_fingerprint TEXT,
    occurred_at TIMESTAMP,
    resolved_at TIMESTAMP,
    resolved_by TEXT,
    resolution_note TEXT
)
"""


def _env(router, *, extra_ddl: str | None = None, stash_principal: bool = True):
    """SQLite TestClient over one router, with `audit_logs` from ORM metadata.

    ``stash_principal`` mirrors production: ``routers/auth/core.py`` sets
    ``request.state.user`` on every authenticated request so audit helpers find
    the actor "without per-route plumbing". The workflows handlers have no
    ``user`` parameter at all, so that stash IS their actor source — the
    override here is written to set it exactly the way the real dependency
    does. Passing ``stash_principal=False`` models a request that reached the
    handler with no principal on the state, which is how the actor assertion is
    shown to be load-bearing rather than decorative.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    if extra_ddl:
        with engine.begin() as conn:
            conn.execute(text(extra_ddl))
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

    def _override_user(request: Request):
        principal = {"user_id": USER, "role": "admin", "tenant_id": TID}
        if stash_principal:
            # Exactly what routers/auth/core.py does before returning.
            request.state.user = principal
        return principal

    # Override `_get_db_dep`, NOT `audit_ready_db`. `audit_ready_db` declares
    # `Depends(_get_db_dep)` — a separate function that calls `get_db()`
    # directly (core/audit.py) — so overriding only `get_db` leaves the handler
    # on the REAL database. Overriding `audit_ready_db` itself is worse: it
    # replaces the dependency wholesale, `ensure_audit_table` never runs, and
    # these tests would pass identically against handlers still wired to plain
    # `get_db` — the atomicity claim would have zero coverage.
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_get_db_dep] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    # The workflows router is gated on the `workflows` module being enabled, and
    # module state lives in a control-plane table this SQLite app has no copy of.
    # `require_module(...)` builds a NEW closure per call, so the override has to
    # be keyed on the object the router actually holds — looking it up here, not
    # re-calling the factory (which would silently override nothing).
    for dep in router.dependencies:
        if "require_module" in getattr(dep.dependency, "__qualname__", ""):
            app.dependency_overrides[dep.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=True), Session, engine


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


def _details(row: AuditLog) -> dict:
    return row.details if row.details is not None else (row.payload or {})


def _workflows_env(**kw):
    from gdx_dispatch.modules.workflows.router import router as r

    return _env(r, **kw)


RULE = {
    "name": "Thanks for your business",
    "trigger_event": "invoice.paid",
    "actions": [{
        "action_type": "send_email",
        "params": {"subject": "Thank you", "body": "Hello"},
    }],
}


# ── control ──────────────────────────────────────────────────────────────


def test_control_the_harness_can_see_an_audit_row():
    """Without this, every '0 rows' below could mean the table isn't wired."""
    tc, Session, engine = _workflows_env()
    try:
        db = Session()
        try:
            log_audit_event_sync(
                db, tenant_id=TID, user_id="control", action="control_probe",
                entity_type="workflow_rule", entity_id="probe", details={},
            )
            db.commit()
        finally:
            db.close()
        assert len(_audit_rows(Session, "control_probe")) == 1
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_control_the_module_gate_would_be_required_without_the_override():
    """The override above is a test-harness convenience, not a claim that the
    route is ungated. Pin the real dependency so removing it from the router is
    a visible change rather than a silently looser surface."""
    from gdx_dispatch.modules.workflows.router import router as r

    dep_names = {getattr(d.dependency, "__qualname__", "") for d in r.dependencies}
    assert any("require_module" in n for n in dep_names), dep_names
    assert any("get_current_user" in n for n in dep_names), dep_names


# ── workflows ────────────────────────────────────────────────────────────


def test_create_workflow_writes_an_audit_row():
    """A rule created here sends customer email on the next matching event.
    The record says who authored it, and what it will do."""
    tc, Session, engine = _workflows_env()
    try:
        resp = tc.post("/api/workflows", json=RULE)
        assert resp.status_code == 200, resp.text
        rule_id = resp.json()["id"]

        rows = _audit_rows(Session, "workflow_rule_created")
        assert len(rows) == 1, f"one POST, one audit row — got {len(rows)}"
        row = rows[0]
        assert row.entity_type == "workflow_rule"
        assert str(row.entity_id) == str(rule_id), "the row must name the rule it created"
        d = _details(row)
        assert d["trigger_event"] == "invoice.paid"
        assert d["action_types"] == ["send_email"], d
        assert d["is_active"] is True
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_create_workflow_actor_is_the_real_user_not_system():
    """The risky half of this change.

    These handlers take neither `user` nor a principal of any kind — auth is a
    router-level dependency — so the actor can only come from
    `request.state.user`. If `resolve_audit_actor(None, request)` misses, the
    row still gets written and still looks fine; it just says "system" for a
    person. That is issue #701's shape, and this is the assertion that catches
    it.
    """
    tc, Session, engine = _workflows_env()
    try:
        assert tc.post("/api/workflows", json=RULE).status_code == 200
        row = _audit_rows(Session, "workflow_rule_created")[0]
        assert (row.user_id or row.actor_id) == USER, (
            "the authenticated user, not 'system' — request.state.user was not read"
        )
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_the_actor_assertion_can_fail():
    """Falsifier for the test above, not for the code.

    With no principal on `request.state`, the same handler must fall through to
    "system". If this came back "user-42" the assertion above would be passing
    for some other reason and would prove nothing about the lookup.
    """
    tc, Session, engine = _workflows_env(stash_principal=False)
    try:
        assert tc.post("/api/workflows", json=RULE).status_code == 200
        row = _audit_rows(Session, "workflow_rule_created")[0]
        assert (row.user_id or row.actor_id) == "system", (
            "with no principal stashed the actor must be 'system'; anything else "
            "means the positive assertion is not reading request.state.user"
        )
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_update_workflow_records_the_old_value_and_the_new():
    """"Who pointed this rule at a new subject line" is useless without what it
    said before."""
    tc, Session, engine = _workflows_env()
    try:
        rule_id = tc.post("/api/workflows", json=RULE).json()["id"]
        resp = tc.put(f"/api/workflows/{rule_id}", json={"name": "Renamed"})
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(Session, "workflow_rule_updated")
        assert len(rows) == 1, f"one PUT, one audit row — got {len(rows)}"
        row = rows[0]
        assert (row.user_id or row.actor_id) == USER
        assert row.entity_type == "workflow_rule"
        assert str(row.entity_id) == str(rule_id)
        changed = _details(row)["changed"]
        assert changed["name"] == {"from": RULE["name"], "to": "Renamed"}, changed
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_update_workflow_noop_records_an_empty_diff():
    """Saving the same values back must not replay every field as 'changed' —
    that would make the trail useless for answering what actually moved."""
    tc, Session, engine = _workflows_env()
    try:
        rule_id = tc.post("/api/workflows", json=RULE).json()["id"]
        assert tc.put(f"/api/workflows/{rule_id}", json={"name": RULE["name"]}).status_code == 200
        row = _audit_rows(Session, "workflow_rule_updated")[0]
        assert _details(row)["changed"] == {}, _details(row)
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_delete_workflow_is_recorded_as_a_deactivation():
    """DELETE flips `is_active` — the row survives. Calling it a delete in the
    trail would misdescribe what happened (invariant #2 shape)."""
    tc, Session, engine = _workflows_env()
    try:
        rule_id = tc.post("/api/workflows", json=RULE).json()["id"]
        assert tc.delete(f"/api/workflows/{rule_id}").status_code == 200

        assert _audit_rows(Session, "workflow_rule_deleted") == [], (
            "the row is not deleted; the action string must not say it was"
        )
        rows = _audit_rows(Session, "workflow_rule_deactivated")
        assert len(rows) == 1, f"one DELETE, one audit row — got {len(rows)}"
        row = rows[0]
        assert (row.user_id or row.actor_id) == USER
        assert row.entity_type == "workflow_rule"
        assert str(row.entity_id) == str(rule_id)
        d = _details(row)
        assert d["soft_delete"] is True and d["was_active"] is True, d

        db = Session()
        try:
            from gdx_dispatch.modules.workflows.models import WorkflowRule

            kept = db.get(WorkflowRule, UUID(str(rule_id)))
            assert kept is not None and kept.is_active is False
        finally:
            db.close()
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_a_failed_audit_write_takes_the_rule_with_it():
    """The atomicity claim: no unaudited rule.

    The audit row is staged before `db.commit()`, inside the handler's
    transaction. Make the audit write raise and the rule must not survive — a
    version that committed first and audited after would leave a live,
    email-sending rule with no record, which is the shape being fixed.
    """
    tc, Session, engine = _workflows_env()
    try:
        import gdx_dispatch.modules.workflows.router as wr

        def _boom(*a, **kw):
            raise RuntimeError("audit backend down")

        original = wr.log_audit_event_sync
        wr.log_audit_event_sync = _boom
        try:
            with pytest.raises(RuntimeError):
                tc.post("/api/workflows", json=RULE)
        finally:
            wr.log_audit_event_sync = original

        db = Session()
        try:
            from gdx_dispatch.modules.workflows.models import WorkflowRule

            assert db.query(WorkflowRule).count() == 0, (
                "the rule committed even though its audit row failed — that is "
                "an unaudited mutation, invariant #1"
            )
        finally:
            db.close()
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


# ── error sink ───────────────────────────────────────────────────────────


def _seed_errors(Session, fingerprint: str, n: int) -> list[str]:
    ids = [str(uuid4()) for _ in range(n)]
    db = Session()
    try:
        for i, eid in enumerate(ids):
            db.execute(
                text(
                    "INSERT INTO server_errors (id, tenant_id, group_fingerprint, "
                    "occurred_at, exception_class, path) "
                    "VALUES (:id, :tid, :fp, :ts, 'ValueError', '/api/thing')"
                ),
                {"id": eid, "tid": TID, "fp": fingerprint,
                 "ts": datetime(2026, 9, 12, 12, i, tzinfo=timezone.utc)},
            )
        db.commit()
    finally:
        db.close()
    return ids


def _error_sink_env():
    from gdx_dispatch.modules.error_sink.router import router as r

    return _env(r, extra_ddl=SERVER_ERRORS_DDL)


def test_resolving_one_error_writes_an_audit_row():
    tc, Session, engine = _error_sink_env()
    try:
        (eid,) = _seed_errors(Session, "fp-single", 1)
        resp = tc.patch(f"/api/admin/errors/{eid}/resolve", json={"note": "fixed in #999"})
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(Session, "server_error_resolved")
        assert len(rows) == 1, f"one PATCH, one audit row — got {len(rows)}"
        row = rows[0]
        assert (row.user_id or row.actor_id) == USER
        assert row.entity_type == "server_error"
        assert str(row.entity_id) == eid
        d = _details(row)
        assert d["rows_affected"] == 1, d
        assert d["swept_group"] is False and d["resolve_group"] is False, d
        assert d["note"] == "fixed in #999", d
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_resolving_a_group_records_how_many_rows_it_closed():
    """The reason this endpoint is audited at all.

    `resolved_by`/`resolved_at` are already stamped on each row, so attribution
    was never missing. What no per-row column can say is that ONE request closed
    N rows, and which fingerprint it swept. A measured request closed 3.
    """
    tc, Session, engine = _error_sink_env()
    try:
        ids = _seed_errors(Session, "fp-group", 3)
        resp = tc.patch(
            f"/api/admin/errors/{ids[0]}/resolve",
            json={"note": "root cause fixed", "resolve_group": True},
        )
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(Session, "server_error_resolved")
        assert len(rows) == 1
        d = _details(rows[0])
        assert d["rows_affected"] == 3, f"three rows shared the fingerprint: {d}"
        assert d["swept_group"] is True and d["group_fingerprint"] == "fp-group", d
        assert str(rows[0].entity_id) == ids[0], "the id the request named"

        db = Session()
        try:
            still_open = db.execute(
                text("SELECT COUNT(*) FROM server_errors WHERE resolved_at IS NULL")
            ).scalar()
        finally:
            db.close()
        assert still_open == 0, "the sweep really closed all three"
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


def test_resolving_a_missing_error_writes_no_audit_row():
    """404 is not a mutation. A trail that records attempts as changes is
    noise that makes the real rows harder to find."""
    tc, Session, engine = _error_sink_env()
    try:
        resp = tc.patch(f"/api/admin/errors/{uuid4()}/resolve", json={})
        assert resp.status_code == 404, resp.text
        assert _audit_rows(Session, "server_error_resolved") == []
    finally:
        tc.app.dependency_overrides.clear()
        engine.dispose()


# ── fleet: the deletion stays deleted ────────────────────────────────────


def test_the_fleet_module_put_route_is_gone():
    """#558, owner-ruled 2026-09-12: deleted rather than audited.

    PUT /api/fleet/vehicles/{id} in `modules/fleet` had no caller anywhere in
    the repo (FleetView.vue:232 sends `api.patch`) and read the `vehicles`
    table, not the `fleet_vehicles_router` table the Fleet page's ids live in,
    so every id the UI holds would have 404'd. The audited live equivalent is
    routers/fleet.py's PATCH (`fleet_vehicle_updated`).

    Asserted against the mounted route table, never source text — the same way
    test_orphan_routes_retired_2026_09_10.py does it — so a re-add through any
    router is caught.
    """
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    puts = [
        full_path
        for full_path, route in iter_app_routes(app)
        if "PUT" in (getattr(route, "methods", None) or ())
        and full_path == "/api/fleet/vehicles/{vehicle_id}"
    ]
    assert puts == [], f"the deleted fleet PUT is registered again: {puts}"

    # And the complement: the ruling kept the audited PATCH. A later cleanup
    # that took both would honour half the ruling.
    patches = [
        full_path
        for full_path, route in iter_app_routes(app)
        if "PATCH" in (getattr(route, "methods", None) or ())
        and full_path == "/api/fleet/vehicles/{vehicle_id}"
    ]
    assert len(patches) == 1, f"the live audited fleet update is gone: {patches}"


def test_the_fleet_module_router_no_longer_references_the_deleted_handler():
    from gdx_dispatch.modules.fleet import router as fleet_router

    assert not hasattr(fleet_router, "put_vehicle")
    assert fleet_router.router.routes == [], (
        "the fleet module router is meant to be empty; a new route here needs "
        "its own audit review (#558)"
    )
