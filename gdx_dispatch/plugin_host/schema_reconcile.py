"""Additive column reconcile for plugin tables.

`PluginBase.metadata.create_all` (plugin_host/main.py) creates *missing tables*
but — like all of SQLAlchemy's create_all — never ALTERs an existing one. So
when a plugin's model gains a column on an already-deployed table, the column
is silently absent and every query referencing it 500s with UndefinedColumn
(the CHI `plug_chipricing_specs.folder` outage, 2026-06-26). This is the
plugin-layer twin of core issue #41.

`reconcile_plugin_columns` closes that gap: after create_all, it diffs each
plugin table's model columns against the live table and `ADD COLUMN`s any that
are missing. Strictly additive (never drops/retypes), added NULLABLE so it
can't fail on tables that already have rows — a NOT NULL model column is added
nullable and the plugin/app backfills. Still not a substitute for real
per-plugin migrations (data backfills, type changes, drops) — those remain the
Alembic-branch upgrade path noted in ADR-013 — but it stops the common
"added a column → every request 500s" failure.
"""
from __future__ import annotations

import logging

from sqlalchemy import MetaData, inspect
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def reconcile_plugin_columns(engine: Engine, metadata: MetaData) -> list[str]:
    """ADD COLUMN any model column missing from its existing plugin table.

    Returns the list of ``"table.column"`` strings that were added (for logging
    / tests). Tables that don't exist yet are skipped — create_all owns those,
    and a freshly-created table already has every column.
    """
    added: list[str] = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    # IF NOT EXISTS guards a concurrent boot adding the column between our check
    # and our ALTER (Postgres only; other dialects rely on the column-missing
    # check below, which is already authoritative for a single plugin-host).
    if_not_exists = "IF NOT EXISTS " if engine.dialect.name == "postgresql" else ""

    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all builds it with the full current column set
        db_cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in db_cols:
                continue
            coltype = col.type.compile(dialect=engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN {if_not_exists}"{col.name}" {coltype}'
            # Carry a simple server_default through (e.g. booleans/ints) so the
            # column is useful immediately; otherwise add nullable. We never add
            # NOT NULL here — that would fail on a table that already has rows.
            default = getattr(col.server_default, "arg", None)
            if default is not None:
                default_sql = getattr(default, "text", None) or str(default)
                ddl += f" DEFAULT {default_sql}"
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(ddl)
                added.append(f"{table.name}.{col.name}")
                log.warning(
                    "plugin schema reconcile: added missing column %s.%s (%s)",
                    table.name, col.name, coltype,
                )
            except Exception:
                # One bad column must not abort the whole boot — log and move on.
                log.exception(
                    "plugin schema reconcile failed for %s.%s", table.name, col.name
                )
    if added:
        log.warning("plugin schema reconcile added %d column(s): %s", len(added), ", ".join(added))
    return added
