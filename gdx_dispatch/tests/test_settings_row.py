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

from gdx_dispatch.core.audit import TenantBase, _get_db_dep
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.settings_row import read_settings_row, settings_column
from gdx_dispatch.routers.auth import get_current_user

TID = "11111111-1111-1111-1111-111111111111"
REPO = Path(__file__).resolve().parents[2]

# sqlite3 cannot bind Decimal (billing_terms rows carry them); psycopg can.
sqlite3.register_adapter(Decimal, float)


def _engine(columns):
    """SQLite with a hand-built tenant_settings — the table is not in ORM
    metadata (see core/settings_row.py for why). Untyped columns so values
    round-trip as written."""
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
        db.execute(text("UPDATE tenant_settings SET alpha = 7 WHERE tenant_id = :tid"), {"tid": TID})
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
    app.dependency_overrides[_get_db_dep] = _override_db
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRE-EXISTING, carried over from all seven copies: the tenant id is bound "
        "dashed, an ORM-built SQLite schema stores Uuid as dashless hex, so the "
        "read misses the row and the seed inserts a second one. Fixing it means "
        "binding the same spelling on the read and on settings_audit's write "
        "together. When that lands this XPASSes and the marker must go."
    ),
)
def test_uuid_binding_matches_an_orm_built_sqlite_schema():
    """The sharp edge CLAUDE.md names, pinned where it now lives. Builds the
    schema from the ORM (not by hand), inserts a settings row through the ORM,
    and reads it back through the helper."""
    from uuid import UUID

    from gdx_dispatch.core.tenant_settings import TenantSettings

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantSettings.metadata.create_all(engine, tables=[TenantSettings.__table__])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        db.add(TenantSettings(tenant_id=UUID(TID), session_idle_timeout_minutes=42))
        db.commit()
        row = read_settings_row(db, UUID(TID), ("session_idle_timeout_minutes",))
        assert row[0] == 42, "read the row the ORM wrote, not a freshly seeded default"
        assert _rows(engine) == 1, "no second row seeded under the other uuid spelling"
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
