"""Plugin column reconcile — the plugin-layer twin of #41.

create_all never ALTERs an existing table, so a plugin model that gains a column
leaves the live table missing it (the CHI `folder` 500). reconcile_plugin_columns
adds the missing column additively. This locks that behavior.
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from gdx_dispatch.plugin_host.schema_reconcile import reconcile_plugin_columns


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def test_reconcile_adds_missing_column():
    engine = _engine()
    # Simulate the deployed (old) table: no `folder` column.
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE plug_specs (id INTEGER PRIMARY KEY, company_id VARCHAR(36))")

    # The current model gained `folder`.
    md = MetaData()
    Table(
        "plug_specs", md,
        Column("id", Integer, primary_key=True),
        Column("company_id", String(36)),
        Column("folder", String(120)),
    )

    added = reconcile_plugin_columns(engine, md)
    assert "plug_specs.folder" in added
    cols = {c["name"] for c in inspect(engine).get_columns("plug_specs")}
    assert "folder" in cols
    # Inserting with the new column works now.
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO plug_specs (id, company_id, folder) VALUES (1, 't', 'Acme')")


def test_reconcile_is_idempotent_and_noops_when_current():
    engine = _engine()
    md = MetaData()
    Table("plug_specs", md, Column("id", Integer, primary_key=True), Column("folder", String(120)))
    md.create_all(engine)  # table already has every column

    assert reconcile_plugin_columns(engine, md) == []   # nothing to add
    assert reconcile_plugin_columns(engine, md) == []   # idempotent


def test_reconcile_skips_nonexistent_table():
    # A model whose table doesn't exist yet is create_all's job, not ours.
    engine = _engine()
    md = MetaData()
    Table("never_created", md, Column("id", Integer, primary_key=True), Column("x", String(10)))
    assert reconcile_plugin_columns(engine, md) == []
