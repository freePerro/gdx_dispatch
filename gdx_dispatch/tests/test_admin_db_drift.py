"""The admin_db drift check covers both declarative bases (GDXA-1537).

It compared TenantBase alone and suppressed the Alembic Base's tables, so a
nullability mismatch on game_events.created_by_user_id (migration 094) could
never be reported. These run against a real SQLite database built from the
ORM: a mock of compare_metadata would prove only what we passed it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine, text

import gdx_dispatch.models  # noqa: F401 — registers every model on TenantBase
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.tenant_settings import Base as AlembicBase
from gdx_dispatch.routers import admin_db


def _build(tmp_path, *, nullable_created_by: bool):
    eng = create_engine(f"sqlite:///{tmp_path / 'drift.db'}")
    TenantBase.metadata.create_all(eng)
    AlembicBase.metadata.create_all(eng)
    with eng.begin() as c:
        # migration 001's baseline table that no model declares
        c.execute(text("CREATE TABLE server_errors (id INTEGER PRIMARY KEY, message TEXT)"))
    if nullable_created_by:
        # Rebuild game_events the way baseline_squashed.sql shipped it before 094.
        with eng.begin() as c:
            c.execute(text("DROP TABLE game_events"))
        md = MetaData()
        AlembicBase.metadata.tables["game_definitions"].to_metadata(md)
        ge = AlembicBase.metadata.tables["game_events"].to_metadata(md)
        ge.c.created_by_user_id.nullable = True
        ge.create(eng)
    return eng


@pytest.fixture
def status_for(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_db, "_BACKUP_DIR", tmp_path / "no_backups")

    def run(**kw):
        monkeypatch.setattr(admin_db, "engine", _build(tmp_path, **kw))
        return admin_db._status_payload()

    return run


def test_nullable_mismatch_on_alembic_base_table_is_reported_and_sets_verdict(status_for):
    s = status_for(nullable_created_by=True)
    rows = [r for r in s["tenant_drift"]["items"] if r["object"] == "game_events.created_by_user_id"]
    assert rows == [{
        "severity": "mismatch", "kind": "modify_nullable",
        "object": "game_events.created_by_user_id",
        "detail": "column 'game_events.created_by_user_id' — DB nullable, ORM NOT NULL",
    }]
    assert s["tenant_drift"]["mismatch_count"] >= 1
    # No alembic_version table, so migrations are also pending; drift outranks it.
    assert s["verdict"] == "drift"


def test_db_built_from_both_bases_has_no_verdict_affecting_drift(status_for):
    s = status_for(nullable_created_by=False)
    drift = s["tenant_drift"]
    assert drift["missing_count"] == 0, drift["items"][:10]
    assert drift["mismatch_count"] == 0, drift["items"][:10]
    alembic_tables = set(AlembicBase.metadata.tables)
    # The Base tables are now modelled, not suppressed and not "stale".
    assert not [r for r in drift["items"] if r["object"] in alembic_tables], drift["items"][:10]
    # The one unmodelled baseline table stays suppressed.
    assert not [r for r in drift["items"] if "server_errors" in r["object"]]
    assert s["verdict"] == "pending"


def test_suppression_covers_only_tables_no_base_models():
    modelled = set(TenantBase.metadata.tables) | set(AlembicBase.metadata.tables)
    assert not (admin_db._TABLES_WITHOUT_ORM_MODEL & modelled)


def test_schema_drift_tool_reads_the_alembic_base_too():
    from gdx_dispatch.tools.tenant_schema_drift_check import _orm_schema

    schema = _orm_schema()
    assert "created_by_user_id" in schema["game_events"]
    assert {"tenants", "tenant_settings", "game_definitions", "game_state"} <= set(schema)
    assert "invoices" in schema
