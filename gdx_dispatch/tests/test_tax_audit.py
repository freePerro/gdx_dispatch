"""Every sales-tax write answers who / what / when (#558, invariant #1).

`modules/tax/router.py` had three mutating handlers — PATCH /api/tax/config,
POST /api/tax/exemptions, DELETE /api/tax/exemptions/{id} — and not one audit
call between them. The rate on `tax_config` decides what every later invoice
charges, and an exemption suppresses sales tax on a customer's invoices
entirely; both moved with no actor and no record.

DELETE is the sharpest of the three: `TaxExemption` carries no `deleted_at`, so
it is a HARD delete and the audit row is the ONLY surviving evidence the rule
ever existed. `test_delete_records_the_whole_row` is the guard on that.

These tests drive the real router through a TestClient and read `audit_logs`
back. The control is the point: without one, "0 rows" could just as easily mean
the table was never wired. `test_control_*` proves the harness can see a row.

The last section guards the other half of the same money-group fix:
`modules/ledger/router.py::initialize_accounting` audited only its
`if not already:` branch while `ensure_gl_seed()` ran — and `db.commit()`
landed — unconditionally, so a re-POST that rewrote a LOCKED_ONCE_LIVE field
committed with zero audit rows. It lives here rather than in its own file
because a fix is done when its guard RUNS, and this is the file this change
owns.
"""
from __future__ import annotations

from datetime import date, timedelta
from importlib import import_module
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import (
    AuditLog,
    TenantBase,
    _get_db_dep,
    log_audit_event_sync,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.router import router as tax_router
from gdx_dispatch.routers.auth import get_current_user

TID = "11111111-1111-1111-1111-111111111111"

# The EXACT shape `routers/auth/core.py::finalize_login_jwt` hands a handler:
# {user_id, tenant_id, role} — no "sub", no "id", no "email" (#701). An actor
# resolver that reads anything else off this dict stores "system" for a real
# person, so the fixture must not hand the code a richer dict than prod does.
_ADMIN = {"user_id": "user-42", "tenant_id": TID, "role": "admin"}
_TECH = {"user_id": "user-tech", "tenant_id": TID, "role": "tech"}


@pytest.fixture()
def env():
    """TestClient over the REAL tax router with an isolated in-memory DB.

    `env(user)` builds a client authenticated as that user; every client from
    one fixture shares the engine, so an admin write is visible to a later
    read.

    `_get_db_dep` is overridden alongside `get_db` on purpose. The handlers
    call `ensure_audit_table(db)` inline today, so only `get_db` is strictly
    needed — but `audit_ready_db` (the alternative wiring) declares
    `Depends(_get_db_dep)`, a separate function that calls `get_db()` directly,
    and a handler moved onto it would silently start talking to the real
    database and surface as a confusing "no such table". Overriding the inner
    dependency keeps this harness honest either way. What it must NOT do is
    override `audit_ready_db` itself — that replaces the dependency wholesale,
    so the real thing never runs and these tests would pass against a handler
    that never initialized the audit table at all.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TaxConfig.__table__.create(bind=engine, checkfirst=True)
    TaxExemption.__table__.create(bind=engine, checkfirst=True)
    # core.audit's own declarative base — NOT the tax models' TenantBase.
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    clients: list[TestClient] = []

    def _make(user: dict | None = _ADMIN) -> TestClient:
        app = FastAPI()
        app.include_router(tax_router)
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[_get_db_dep] = _override_db
        if user is not None:
            app.dependency_overrides[get_current_user] = lambda: user
        tc = TestClient(app, raise_server_exceptions=True)
        clients.append(tc)
        return tc

    yield _make, Session

    for tc in clients:
        tc.app.dependency_overrides.clear()
    engine.dispose()


def _rows(Session, action: str) -> list[AuditLog]:
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


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


def test_control_the_harness_can_see_an_audit_row(env):
    """Without this, every '0 rows' assertion below could just mean the
    audit table was never wired into this engine."""
    make, Session = env
    make()  # build the app so the wiring matches the other tests
    db = Session()
    try:
        log_audit_event_sync(
            db, tenant_id=TID, user_id="control", action="control_probe",
            entity_type="tax_config", entity_id=TID, details={},
        )
        db.commit()
    finally:
        db.close()
    assert len(_rows(Session, "control_probe")) == 1


# ---------------------------------------------------------------------------
# PATCH /api/tax/config
# ---------------------------------------------------------------------------


def test_patch_config_writes_an_audit_row(env):
    """Who moved the default sales-tax rate, from what, to what."""
    make, Session = env
    resp = make().patch(
        "/api/tax/config",
        json={"default_rate": 0.0738, "tax_labor": True, "name": "MN default"},
    )
    assert resp.status_code == 200, resp.text

    rows = _rows(Session, "tax_config_updated")
    assert len(rows) == 1, f"one PATCH, one audit row — got {len(rows)}"
    row = rows[0]
    assert (row.user_id or row.actor_id) == "user-42", "the signed-in user, not 'system'"
    assert row.tenant_id == TID
    assert row.entity_type == "tax_config"
    assert row.entity_id, "the audit row must name the config row it changed"
    # Proves the new `request: Request` parameter actually reaches the audit
    # call — without it the row carries no origin at all.
    assert row.ip_address, "request context did not reach the audit row"

    changed = _details(row).get("changed") or {}
    for field, want in (("default_rate", 0.0738), ("tax_labor", True), ("name", "MN default")):
        assert field in changed, f"{field} moved but is not in the audit diff: {changed}"
        # The OLD value is the forensic half — "who set the rate to 7.38%" is
        # useless without "from 0%".
        assert "from" in changed[field] and "to" in changed[field], changed[field]
        assert changed[field]["to"] == want, changed[field]
    assert changed["default_rate"]["from"] == 0.0


def test_a_noop_config_save_records_an_empty_diff(env):
    """Saving the same rate twice must not replay it as 'changed'.

    `default_rate` is Numeric: it comes back off the row as `Decimal` and
    arrives off the payload as `float`, and `Decimal("0.073800") != 0.0738` in
    Python. An un-normalized compare would mark the rate changed on every
    save, which makes the trail useless for answering what actually moved.
    """
    make, Session = env
    admin = make()
    assert admin.patch("/api/tax/config", json={"default_rate": 0.0738}).status_code == 200
    assert admin.patch("/api/tax/config", json={"default_rate": 0.0738}).status_code == 200

    rows = _rows(Session, "tax_config_updated")
    assert len(rows) == 2, "both saves are recorded"
    assert (_details(rows[0]).get("changed") or {}) == {}, (
        f"no-op save recorded a diff: {_details(rows[0]).get('changed')}"
    )


def test_a_refused_config_patch_writes_nothing(env):
    """The admin gate runs first — a 403 is not a mutation and must not
    manufacture an audit row."""
    make, Session = env
    assert make(_TECH).patch("/api/tax/config", json={"default_rate": 0.07}).status_code == 403
    assert _rows(Session, "tax_config_updated") == []


# ---------------------------------------------------------------------------
# POST /api/tax/exemptions
# ---------------------------------------------------------------------------


def test_create_exemption_writes_an_audit_row(env):
    make, Session = env
    customer_id = str(uuid4())
    resp = make().post(
        "/api/tax/exemptions",
        json={
            "customer_id": customer_id,
            "reason": "non-profit",
            "certificate_id": "ST3-123",
            "exempt_until": (date.today() + timedelta(days=365)).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()

    rows = _rows(Session, "tax_exemption_created")
    assert len(rows) == 1, f"one POST, one audit row — got {len(rows)}"
    row = rows[0]
    assert (row.user_id or row.actor_id) == "user-42", "the signed-in user, not 'system'"
    assert row.tenant_id == TID
    assert row.entity_type == "tax_exemption"
    # The id has to be real: it is how anyone later joins this row to the
    # delete row for the same exemption.
    assert str(row.entity_id) == created["id"]

    details = _details(row)
    assert details["customer_id"] == customer_id
    assert details["exempt"] is True
    assert details["reason"] == "non-profit"
    assert details["certificate_id"] == "ST3-123"


# ---------------------------------------------------------------------------
# DELETE /api/tax/exemptions/{id}
# ---------------------------------------------------------------------------


def test_delete_records_the_whole_row(env):
    """The hard-delete case. `TaxExemption` has no `deleted_at`, so once this
    returns 204 the only place the exemption's terms still exist is the audit
    row — an entity_id alone would leave "which customer stopped being charged
    sales tax, on what certificate, for which dates" permanently unanswerable.
    """
    make, Session = env
    admin = make()
    customer_id = str(uuid4())
    until = (date.today() + timedelta(days=30)).isoformat()
    created = admin.post(
        "/api/tax/exemptions",
        json={
            "customer_id": customer_id,
            "reason": "resale",
            "certificate_id": "ST3-999",
            "exempt_from": date.today().isoformat(),
            "exempt_until": until,
            "notes": "cert on file",
        },
    )
    assert created.status_code == 201, created.text
    exemption_id = created.json()["id"]

    assert admin.delete(f"/api/tax/exemptions/{exemption_id}").status_code == 204

    # Really gone — this is why the audit row is the last copy.
    db = Session()
    try:
        assert db.execute(select(TaxExemption)).scalars().all() == []
    finally:
        db.close()

    rows = _rows(Session, "tax_exemption_deleted")
    assert len(rows) == 1, f"one DELETE, one audit row — got {len(rows)}"
    row = rows[0]
    assert (row.user_id or row.actor_id) == "user-42", "the signed-in user, not 'system'"
    assert row.tenant_id == TID
    assert row.entity_type == "tax_exemption"
    assert str(row.entity_id) == exemption_id

    details = _details(row)
    assert details["customer_id"] == customer_id
    assert details["exempt"] is True
    assert details["reason"] == "resale"
    assert details["certificate_id"] == "ST3-999"
    assert details["exempt_from"] == date.today().isoformat()
    assert details["exempt_until"] == until
    assert details["notes"] == "cert on file"


def test_delete_of_an_unknown_exemption_writes_nothing(env):
    make, Session = env
    admin = make()
    assert admin.delete("/api/tax/exemptions/not-a-uuid").status_code == 404
    assert admin.delete(f"/api/tax/exemptions/{uuid4()}").status_code == 404
    assert _rows(Session, "tax_exemption_deleted") == []


# ---------------------------------------------------------------------------
# Atomicity — the property the row count alone cannot see
# ---------------------------------------------------------------------------


def test_a_failed_audit_write_takes_the_tax_change_with_it(env, monkeypatch):
    """Counting rows on the happy path cannot tell "audit then commit" from
    "commit then audit" — both produce one row. This can: the audit write
    raises, and the rate change must not survive it.

    `core/audit.py` states the contract — stage the audit row inside the
    caller's transaction so the change and its trail commit together. A
    handler that audits AFTER its commit leaves the change standing when the
    audit fails, which is an unaudited mutation of the number every later
    invoice multiplies by.
    """
    make, Session = env
    # `import gdx_dispatch.modules.tax.router as tax_mod` binds the APIRouter,
    # not the module: modules/tax/__init__.py does
    # `from ...tax.router import router`, which rebinds the `router` attribute
    # on the package over the submodule. import_module returns the real module.
    tax_mod = import_module("gdx_dispatch.modules.tax.router")

    def _boom(*a, **kw):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(tax_mod, "log_audit_event_sync", _boom)

    with pytest.raises(RuntimeError):
        make().patch("/api/tax/config", json={"default_rate": 0.0999})

    db = Session()
    try:
        cfg = db.execute(select(TaxConfig)).scalars().first()
    finally:
        db.close()
    assert cfg is None or float(cfg.default_rate) == 0.0, (
        "the tax rate committed even though its audit row failed — that is an "
        "unaudited money mutation, invariant #1"
    )


# ---------------------------------------------------------------------------
# modules/ledger — POST /api/accounting/settings/initialize
#
# House pattern for this router (test_gl_accounting_api.py): call the handler
# directly with a session + user dict.
# ---------------------------------------------------------------------------

COMPANY = TID
# Again the real login shape — {user_id, tenant_id, role}, no "sub" (#701).
_LEDGER_USER = {"user_id": "user-42", "tenant_id": COMPANY, "role": "admin"}


def _init(db):
    from gdx_dispatch.modules.ledger.router import initialize_accounting

    return initialize_accounting(db=db, user=_LEDGER_USER, _perm=None)


def _gl_audit(db, action: str) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)).all())


def test_a_reinit_that_rewrites_a_locked_field_is_audited(tenant_db):
    """The reported gap. `ensure_gl_seed` runs on EVERY call and the commit is
    outside the `if not already:` guard, so a re-POST after a LOCKED_ONCE_LIVE
    field was blanked put the defaults back and committed — silently.

    Blanking it directly is how the state arises in the wild: the seed top-up
    is the writer, and it runs with none of the PATCH path's lock checks.
    """
    from gdx_dispatch.modules.ledger.router import LOCKED_ONCE_LIVE
    from gdx_dispatch.modules.ledger.service import get_gl_settings

    _init(tenant_db)
    assert "payment_method_role_map" in LOCKED_ONCE_LIVE
    baseline = len(_gl_audit(tenant_db, "gl_settings_reinitialized"))

    settings = get_gl_settings(tenant_db, COMPANY)
    settings.payment_method_role_map = {}
    tenant_db.commit()

    _init(tenant_db)

    settings = get_gl_settings(tenant_db, COMPANY)
    assert settings.payment_method_role_map, "the re-init did rewrite the map"

    rows = _gl_audit(tenant_db, "gl_settings_reinitialized")
    assert len(rows) == baseline + 1, (
        "the re-init rewrote a locked field and committed with no audit row"
    )
    row = rows[-1]
    assert (row.user_id or row.actor_id) == "user-42", "the signed-in user, not 'system'"
    assert row.tenant_id == COMPANY
    assert row.entity_type == "gl_settings"
    fields = (_details(row).get("fields")) or []
    assert "payment_method_role_map" in fields, fields


def test_an_idempotent_reinit_writes_no_audit_row(tenant_db):
    """The other half of the same property: a re-POST that changes nothing must
    stay silent, or the trail fills with noise and stops answering what moved.
    """
    _init(tenant_db)
    before = len(list(tenant_db.scalars(select(AuditLog)).all()))
    _init(tenant_db)
    assert len(list(tenant_db.scalars(select(AuditLog)).all())) == before
