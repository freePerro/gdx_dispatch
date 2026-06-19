"""Render parameterized RLS policy SQL for critical tenant-scoped tables.

Pure stdlib string templating — no SQLAlchemy, psycopg, alembic, or DB
connection. The caller (ss16-b alembic migration) is responsible for
executing the rendered SQL.

See: plans/platform-sprints/SS-16_rls_critical_tables.md

DEPRECATED UNDER THREE-PLANE (2026-04-24, Phase A4)
---------------------------------------------------
CRITICAL_TABLES below (jobs/customers/invoices/leads) targets the TENANT
plane — tables that live in each tenant's own Postgres database. Under
the three-plane model (ARCHITECTURAL_STATE.md), tenant-plane isolation
is *the connection itself* (one tenant per session by construction);
RLS there is an architectural no-op.

This module is kept for:
  * SS-16 migration history (imports CRITICAL_TABLES).
  * Test schema baseline (test_rls_integration.py creates throwaway
    tables with the same column shape).

For CURRENT RLS work:
  * Control-plane → gdx_dispatch/tools/control_plane_rls_targets.py + migration 024.
  * Commerce-plane → gdx_dispatch/tools/commerce_plane_rls_targets.py + migration 025.

Do NOT add tenant-plane tables here for new RLS work.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

_TEMPLATE_PATH = Path(__file__).with_name("rls_policy_templates.sql")


# (table_name, select_extra_predicate, soft_delete_column) triples for the
# 4 critical tables.
#
# ``select_extra_predicate`` is the additional SQL predicate that grants
# SELECT access to non-admin/non-owner roles. Use ``"FALSE"`` to restrict
# reads to admins/owners only.
#
# ``soft_delete_column`` is the column name (e.g. ``"deleted_at"``) that
# must be ``IS NULL`` for the row to be visible, or ``None`` if the table
# has no soft-delete column. When provided, a ``" AND <col> IS NULL"``
# clause is added to BOTH the SELECT and WRITE policies so logically-
# deleted rows do not leak to ordinary queries (red-team Pattern 3).
#
# 2026-04-21 (an earlier session): jobs + leads lost their per-tech visibility
# predicate. The template referenced ``assigned_to_identity_id`` — a
# column that does not exist in ``gdx_dispatch/models/tenant_models.py`` (jobs
# uses ``assigned_to: String(50)`` and leads uses ``assigned_to:
# String(200)``, both 0%-populated per D42). The predicate was
# fabricated schema that only passed tests because throwaway rls_jobs
# tables were hand-crafted to include the column. All 4 criticals now
# use the same ``"FALSE"`` admin/owner-only shape as the 114
# REMAINING_TABLES. Tech-visibility deferred to D73.
CRITICAL_TABLES: list[tuple[str, str, str | None]] = [
    ("jobs", "FALSE", "deleted_at"),
    ("customers", "FALSE", "deleted_at"),
    ("invoices", "FALSE", "deleted_at"),
    ("leads", "FALSE", "deleted_at"),
]


def render_policies(
    table: str,
    select_extra_predicate: str,
    soft_delete_column: str | None = None,
) -> str:
    """Render RLS policy SQL for one table.

    Reads the .sql template alongside this module and substitutes
    ``${table}``, ``${select_extra_predicate}`` and ``${soft_delete_clause}``
    placeholders via ``string.Template.safe_substitute``.

    When ``soft_delete_column`` is provided, the rendered policies filter
    out rows where that column IS NOT NULL (i.e. logically-deleted rows
    become invisible to every query path — ordinary SELECT/UPDATE/DELETE
    included). When ``None`` (default), the behaviour matches the
    original SS-16 policy shape exactly.
    """
    template_text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    if soft_delete_column is None:
        soft_delete_clause = ""
    else:
        soft_delete_clause = f" AND {soft_delete_column} IS NULL"
    return Template(template_text).safe_substitute(
        table=table,
        select_extra_predicate=select_extra_predicate,
        soft_delete_clause=soft_delete_clause,
    )
