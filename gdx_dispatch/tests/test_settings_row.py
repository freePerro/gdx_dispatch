"""`core/settings_row.read_settings_row` — the one create-on-read for `tenant_settings`.

Seven routers carried the same twenty lines (SELECT → seed ON CONFLICT DO NOTHING
→ COMMIT → SELECT), each with a `# noqa: S608` resting on a comment. This file
pins the three things the helper has to keep true for all of them:

* the row is seeded exactly once, and the COMMIT happens only on that branch —
  `core/settings_audit.py` stages an audited upsert plus a re-read in one
  transaction and documents that a reader that commits on the found-row path
  would break that atomicity;
* the column gate is a refusal, not a comment: a name that is not an identifier
  raises before any SQL is built;
* every router that used to carry its own copy now reads through the helper —
  proven by driving each GET on an empty table, and by the copy count in source
  (asserting text is ABSENT is the one source assertion that proves something).
"""
from __future__ import annotations

import importlib
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.settings_row import (
    read_settings_row,
    settings_column,
    settings_sql,
    tenant_id_value,
)
from gdx_dispatch.routers.auth import get_current_user

TID = "11111111-1111-1111-1111-111111111111"
REPO = Path(__file__).resolve().parents[2]

# sqlite3 cannot bind Decimal (billing_terms rows carry them); psycopg can.
sqlite3.register_adapter(Decimal, float)


def _engine(columns):
    """SQLite with a hand-built tenant_settings carrying exactly the columns
    under test (the ORM model lacks some live columns — see
    core/settings_row.py). Untyped value columns so values round-trip as
    written; `tenant_id` holds whatever the helper's `Uuid`-typed bind writes,
    which on SQLite is 32 dashless hex, the same as an ORM-built table. Raw SQL
    in these tests binds the id through `settings_sql`, never dashed."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tenant_settings (tenant_id TEXT PRIMARY KEY, "
            + ", ".join(columns) + ")"
        ))
    return engine


def _rows(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM tenant_settings")).scalar())


# ── the helper itself ──────────────────────────────────────────────────────


def test_seeds_once_and_commits_only_on_the_create_branch():
    engine = _engine(["alpha", "beta"])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    commits = {"n": 0}
    real_commit = db.commit

    def _counting_commit():
        commits["n"] += 1
        real_commit()

    db.commit = _counting_commit  # instance attribute shadows the method
    try:
        assert _rows(engine) == 0

        first = read_settings_row(db, TID, ("alpha", "beta"))
        assert tuple(first) == (None, None), "a fresh row carries only defaults"
        assert _rows(engine) == 1
        assert commits["n"] == 1, "the seed is the ONE commit"

        second = read_settings_row(db, TID, ("alpha", "beta"))
        assert tuple(second) == (None, None)
        assert _rows(engine) == 1, "no second seed"
        assert commits["n"] == 1, "a found row must not commit — settings_audit.py depends on it"

        # A staged (uncommitted) write is visible to the next read in the same
        # session: that is exactly how settings_audit computes its diff.
        db.execute(
            settings_sql("UPDATE tenant_settings SET alpha = 7 WHERE tenant_id = :tid"),
            {"tid": tenant_id_value(TID)},
        )
        third = read_settings_row(db, TID, ("alpha",))
        assert third[0] == 7
        assert commits["n"] == 1, "reading a staged write must not commit it"
    finally:
        db.close()
        engine.dispose()


def test_column_names_are_a_refusal_not_a_comment():
    class _NoSQL:
        def execute(self, *a, **k):  # pragma: no cover — reaching this IS the failure
            raise AssertionError("SQL must not be built for a rejected column name")

    for good in ("alpha", "_x", "col9", "workflow_lock_schedule_on_start"):
        assert settings_column(good) == good
    for bad in ("", "a b", "alpha; DROP TABLE tenant_settings", 'x"y', "9abc", "a-b", None, "alpha\n"):
        with pytest.raises(ValueError, match="unsafe tenant_settings column name"):
            read_settings_row(_NoSQL(), TID, (bad,))


# ── every router that used to carry its own copy ───────────────────────────

# (module, router attribute, GET path, name of the module's column tuple)
ROUTERS = [
    ("gdx_dispatch.modules.billing_terms.router", "/api/billing/terms", "_COLS"),
    ("gdx_dispatch.modules.catalog_policy.router", "/api/catalog-policy", "_COLS"),
    ("gdx_dispatch.modules.dispatch_settings.router", "/api/dispatch-settings", "_COLS"),
    ("gdx_dispatch.modules.estimates_features.router", "/api/estimates-features", "_COLS"),
    ("gdx_dispatch.modules.workflow.router", "/api/workflow/flags", "_FLAG_COLUMNS"),
    ("gdx_dispatch.modules.numbering.router", "/api/numbering/config", "_COLS"),
    ("gdx_dispatch.routers.session_policy", "/api/session-policy", "_COL"),
]


def _client(router, columns):
    engine = _engine(columns)
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
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-42", "role": "admin", "tenant_id": TID,
    }
    return TestClient(app, raise_server_exceptions=True), engine


@pytest.mark.parametrize("modpath,path,cols_attr", ROUTERS, ids=[r[0].split(".")[-2] if r[0].endswith(".router") else r[0].split(".")[-1] for r in ROUTERS])
def test_each_settings_get_seeds_the_row_through_the_helper(modpath, path, cols_attr):
    """GET on an EMPTY table returns 200 and leaves exactly one settings row;
    a second GET leaves it at one. Before the helper each router did this in
    its own twenty lines; now a regression in any of them is a regression in
    the helper, which the two tests above pin."""
    m = importlib.import_module(modpath)
    cols = getattr(m, cols_attr)
    columns = [cols] if isinstance(cols, str) else list(cols)
    client, engine = _client(m.router, columns)
    try:
        assert _rows(engine) == 0
        r1 = client.get(path)
        assert r1.status_code == 200, r1.text
        assert _rows(engine) == 1, "first read seeds the row"
        r2 = client.get(path)
        assert r2.status_code == 200, r2.text
        assert _rows(engine) == 1, "second read does not seed again"
        assert r1.json() == r2.json(), "a seeded row reads back the same twice"
    finally:
        engine.dispose()


def test_uuid_binding_matches_an_orm_built_sqlite_schema():
    """GDXA-292 — the sharp edge CLAUDE.md names ("SQLite stores a Uuid column
    as 32 dashless hex"). Builds the schema from the ORM (not by hand), inserts
    a settings row through the ORM, reads it back through the helper — with
    both a `UUID` and the dashed string callers hold — then writes through
    `audited_settings_upsert` and checks it UPDATED that row. Before the typed
    bind the read missed, the seed inserted a second row, and the upsert added
    a third."""
    from types import SimpleNamespace
    from uuid import UUID

    from gdx_dispatch.core.settings_audit import audited_settings_upsert
    from gdx_dispatch.core.tenant_settings import TenantSettings

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantSettings.metadata.create_all(engine, tables=[TenantSettings.__table__])
    TenantBase.metadata.create_all(engine, checkfirst=True)  # audit_logs
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        db.add(TenantSettings(tenant_id=UUID(TID), session_idle_timeout_minutes=42))
        db.commit()
        row = read_settings_row(db, UUID(TID), ("session_idle_timeout_minutes",))
        assert row[0] == 42, "read the row the ORM wrote, not a freshly seeded default"
        assert _rows(engine) == 1, "no second row seeded under the other uuid spelling"
        assert read_settings_row(db, TID, ("session_idle_timeout_minutes",))[0] == 42, (
            "the dashed string a request carries reads the same row"
        )

        def _read(d, tid):
            return {"t": read_settings_row(d, tid, ("session_idle_timeout_minutes",))[0]}

        after = audited_settings_upsert(
            db, SimpleNamespace(), {"user_id": "user-42", "role": "admin"},
            tenant_id=TID, values={"session_idle_timeout_minutes": 7},
            action="session_policy_updated", read=_read,
        )
        assert after == {"t": 7}
        assert _rows(engine) == 1, "the audited upsert updated the ORM's row, not a new one"
        db.expire_all()
        assert db.get(TenantSettings, UUID(TID)).session_idle_timeout_minutes == 7
    finally:
        db.close()
        engine.dispose()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GDXA-292 deferred (maintainer ruled the fix narrow, two files): "
        "routers/session_policy.py:79 upserts with a raw str(tid) bind, so on "
        "SQLite its PATCH lands in a second, dashed row that read_settings_row "
        "never reads, and the saved timeout is lost. Postgres is unaffected. When "
        "session_policy binds through settings_sql()/tenant_id_value() this "
        "XPASSes and the marker must go."
    ),
)
def test_session_policy_patch_round_trips_on_an_orm_built_sqlite_schema():
    """The lost-write half of the split: GET seeds through the helper, PATCH
    writes through session_policy's own SQL, a second GET must see it."""
    from gdx_dispatch.core.tenant_settings import TenantSettings
    from gdx_dispatch.routers.session_policy import router

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantSettings.metadata.create_all(engine, tables=[TenantSettings.__table__])
    TenantBase.metadata.create_all(engine, checkfirst=True)
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
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-42", "role": "admin", "tenant_id": TID,
    }
    client = TestClient(app, raise_server_exceptions=True)
    try:
        assert client.get("/api/session-policy").status_code == 200
        r = client.patch("/api/session-policy", json={"idle_timeout_minutes": 5})
        assert r.status_code == 200, r.text
        assert _rows(engine) == 1, "the PATCH must update the seeded row, not add one"
        assert client.get("/api/session-policy").json()["idle_timeout_minutes"] == 5
    finally:
        engine.dispose()


def test_job_workflow_gates_read_the_row_the_audited_write_updated(monkeypatch):
    """The silent half of the split: a gate turned ON through the typed
    upsert must read ON where the job-completion path checks it."""
    from types import SimpleNamespace
    from uuid import UUID

    from gdx_dispatch.core.settings_audit import audited_settings_upsert
    from gdx_dispatch.core.tenant_settings import TenantSettings
    from gdx_dispatch.routers import jobs

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantSettings.metadata.create_all(engine, tables=[TenantSettings.__table__])
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    db = Session()
    try:
        db.add(TenantSettings(tenant_id=UUID(TID)))
        db.commit()

        def _read(d, tid):
            return {"inv": read_settings_row(d, tid, ("workflow_require_invoice_on_complete",))[0]}

        audited_settings_upsert(
            db, SimpleNamespace(), {"user_id": "user-42", "role": "admin"},
            tenant_id=TID, values={"workflow_require_invoice_on_complete": True},
            action="workflow_settings_updated", read=_read,
        )
        assert _rows(engine) == 1
        assert jobs._load_workflow_flags(TID)["require_invoice_on_complete"] is True
    finally:
        db.close()
        engine.dispose()


def test_the_create_on_read_lives_in_one_place():
    """Source assertion, absence only: none of the seven routers carries its
    own `INSERT INTO tenant_settings (tenant_id) VALUES` any more, and the
    helper carries exactly one."""
    seed = "INSERT INTO tenant_settings (tenant_id) VALUES"
    routers = [
        "gdx_dispatch/modules/billing_terms/router.py",
        "gdx_dispatch/modules/catalog_policy/router.py",
        "gdx_dispatch/modules/dispatch_settings/router.py",
        "gdx_dispatch/modules/estimates_features/router.py",
        "gdx_dispatch/modules/workflow/router.py",
        "gdx_dispatch/modules/numbering/router.py",
        "gdx_dispatch/routers/session_policy.py",
    ]
    for rel in routers:
        src = (REPO / rel).read_text(encoding="utf-8")
        assert seed not in src, f"{rel} still seeds tenant_settings itself"
        assert "read_settings_row(" in src, f"{rel} does not read through the helper"
    helper = (REPO / "gdx_dispatch/core/settings_row.py").read_text(encoding="utf-8")
    assert helper.count(seed) == 1


def test_billing_terms_stores_a_null_refuse_debit_as_off_and_true_as_on():
    """2026-09-30. `tenant_settings.refuse_debit_cards` is NOT NULL, so an
    explicit null in the PATCH body is stored as off rather than failing the
    write (this fixture's column is nullable, which is what lets a missing
    coercion show up here). True must round-trip as on, and both changes are
    audited."""
    m = importlib.import_module("gdx_dispatch.modules.billing_terms.router")
    client, engine = _client(m.router, list(m._COLS))
    try:
        assert client.get("/api/billing/terms").status_code == 200
        r = client.patch("/api/billing/terms", json={"refuse_debit_cards": None})
        assert r.status_code == 200, r.text
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT refuse_debit_cards FROM tenant_settings")).scalar()
            assert stored is not None and stored in (False, 0), f"null must be stored as off, got {stored!r}"
        r = client.patch("/api/billing/terms", json={"refuse_debit_cards": True})
        assert r.status_code == 200, r.text
        assert r.json()["refuse_debit_cards"] in (True, 1)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT refuse_debit_cards FROM tenant_settings")).scalar() in (True, 1)
            rows = conn.execute(text(
                "SELECT details FROM audit_logs WHERE action = 'billing_terms_updated' ORDER BY created_at"
            )).scalars().all()
        assert len(rows) == 2, rows
        import json as _json

        last = rows[-1] if isinstance(rows[-1], dict) else _json.loads(rows[-1])
        change = last["changed"]["refuse_debit_cards"]
        assert change["to"] in (True, 1) and change["from"] in (False, 0, None), change
    finally:
        engine.dispose()
