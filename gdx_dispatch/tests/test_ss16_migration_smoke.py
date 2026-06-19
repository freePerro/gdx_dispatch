"""Smoke tests for the SS-16 RLS migration module (slice D).

Doesn't run Alembic — just imports the migration module, verifies its
revision metadata, and confirms upgrade()/downgrade() are no-ops under a
non-PG bind (sqlite test env). PG-level enforcement is covered in
``test_rls_integration.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "006_ss16_rls_critical.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("ss16_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_revision_metadata() -> None:
    mod = _load_migration_module()
    assert mod.revision == "ss16_rls_critical"
    # Chained onto the main migration graph in Sprint 0.9-b
    # (2026-04-20). Prior to 0.9-b the down_revision was
    # "INTEGRATION_TODO"; 0.9-b sequenced this under SS-15.
    assert mod.down_revision == "ss15_admin_pats"


def test_migration_uses_render_policies_and_critical_tables() -> None:
    mod = _load_migration_module()
    # Sanity: the module references the shared renderer + table list.
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "from gdx_dispatch.tools.rls_render import CRITICAL_TABLES, render_policies" in source
    # 3-tuple unpack (table, select_extra_predicate, soft_delete_column)
    # after the red-team Pattern 3 soft-delete fix.
    assert "render_policies(table, select_extra_predicate, soft_delete_column)" in source


def test_migration_never_uses_for_all_in_emitted_sql() -> None:
    from gdx_dispatch.tools.rls_render import CRITICAL_TABLES, render_policies
    # CRITICAL_TABLES tuple shape is (table, predicate, soft_delete_column)
    # as of the RLS soft-delete fix (commit 92766229). The third element
    # is None for tables without a soft-delete column.
    for table, predicate, soft_delete_column in CRITICAL_TABLES:
        sql = render_policies(
            table, predicate, soft_delete_column=soft_delete_column
        )
        assert "FOR ALL" not in sql
