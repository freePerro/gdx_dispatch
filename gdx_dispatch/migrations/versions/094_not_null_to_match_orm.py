"""Tighten the three live columns the ORM declares NOT NULL but databases don't.

Same class as 093, opposite direction: a column whose live nullability
disagrees with its ORM declaration. Nothing reveals it from source — the
source is the side that is right — so it was found by comparing every ORM
column (`TenantBase` and the Alembic `Base`) against the live schema:

  | column                           | prod 2026-09-13 | demo  | dev 2026-09-07 |
  |----------------------------------|-----------------|-------|----------------|
  | `invoices.customer_id`           | nullable        | ok    | nullable, 21 NULL |
  | `invoices.totals_locked`         | ok              | ok    | nullable       |
  | `game_events.created_by_user_id` | nullable        | nullable | —           |

That is 2 mismatches in 3121 columns on prod and 1 on demo. How each arose:

  * `invoices` is built by `create_all`, which creates but never ALTERs, so a
    long-lived table keeps an old annotation's shape while the model moves on.
  * `totals_locked` is added by 056 as `NOT NULL DEFAULT false`, and every ORM
    revision of it says `nullable=False`; how the dev database came to hold a
    nullable copy is unexplained. It matches on prod and demo, so there it is
    a no-op.
  * `game_events.created_by_user_id` is created nullable by
    `baseline_squashed.sql`, so *every* install has it, fresh ones included.

Why it matters: the model promises every invoice has a customer, and code
reads it that way (the send path 422s on a customerless invoice). A nullable
column only holds that promise for as long as every writer happens to keep it;
the NOT NULL makes the database refuse the next NULL instead. (#676 also named
a restore through `tools/pave_tenant_db.py` as the trap. That tool cannot
rebuild an entrypoint-built database with or without this migration, so 094 is
not claimed as a restore fix.)

Existing rows: none are written; only the constraint changes. Prod's 3 NULL
`customer_id` rows were soft-deleted April drafts the owner had removed on
2026-09-13 (audit action `invoice.hard_deleted`), so prod holds 0 NULLs in all
three. `game_events` holds 0 rows on prod and demo. No write path can produce a
NULL: every `Invoice(...)` site has set or refused a customer since 2026-05-11
(`InvoiceCreateIn.customer_id: UUID`; the mobile, close-out and deposit paths
refuse a customerless job/estimate first), and the one `GameEvent(...)` writer
always sets `created_by_user_id`.

**This migration never raises over data.** A column still holding NULLs is left
alone and the skip is logged: the entrypoint runs `alembic upgrade head` under
`set -e` before serving `/health`, so raising would crash-loop the app. That
branch does not fire on prod or demo (0 NULLs); it does on a dev database that
still carries #676's 21 rows. Once `alembic_version` reads 094 a later upgrade
never revisits a skipped column, so resolve its NULLs and then either apply the
ALTER by hand or `alembic stamp 093_segments_deleted_at_nullable` and upgrade.

Portability: `batch_alter_table`, as in 093 — Postgres ALTERs in place. The
SQLite arm satisfies the both-engines rule; no deploy runs Alembic on SQLite
(env.py takes a Postgres advisory lock). The `has_table` guard matters only
for `GDX_SKIP_BOOTSTRAP=1` or a bare `alembic upgrade`: the entrypoint runs
`create_all` before Alembic, so the tables normally exist.

Rollback: pin the previous `APP_VERSION`. An older image reads and writes all
three columns unchanged — every writer already supplies a value — so there is
nothing to undo for compatibility, and `downgrade()` deliberately does nothing.
It cannot restore "the shape before 094" because there was no single one:
`customer_id` was nullable on prod and NOT NULL on demo and every fresh install,
so any relaxing downgrade leaves some install looser than it has ever been.

Revision ID: 094_not_null_to_match_orm
Revises: 093_segments_deleted_at_nullable
Create Date: 2026-09-13
"""

import logging
from typing import NamedTuple

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "094_not_null_to_match_orm"
down_revision = "093_segments_deleted_at_nullable"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


class _Target(NamedTuple):
    table: str
    column: str
    type_: sa.types.TypeEngine


_TARGETS = (
    _Target("invoices", "customer_id", sa.Uuid(as_uuid=True)),
    _Target("invoices", "totals_locked", sa.Boolean()),
    _Target("game_events", "created_by_user_id", sa.String(100)),
)


def _nullability(bind, table: str) -> dict[str, bool]:
    """{column: nullable} for every column of `table`, or {} when it is absent."""
    insp = inspect(bind)
    if not insp.has_table(table):
        return {}
    return {col["name"]: bool(col["nullable"]) for col in insp.get_columns(table)}


def _tighten(bind, pick) -> None:
    """Set NOT NULL on every target `pick(target, live_nullable)` accepts, one
    batch per table. Absent tables and columns are skipped."""
    for table in dict.fromkeys(t.table for t in _TARGETS):
        live = _nullability(bind, table)
        targets = [
            t for t in _TARGETS
            if t.table == table and t.column in live and pick(t, live[t.column])
        ]
        if not targets:
            continue
        with op.batch_alter_table(table) as batch:
            for t in targets:
                batch.alter_column(t.column, existing_type=t.type_, nullable=False)


def upgrade() -> None:
    bind = op.get_bind()

    def ready(t: _Target, live_nullable: bool) -> bool:
        if not live_nullable:
            return False
        nulls = bind.exec_driver_sql(
            f"SELECT COUNT(*) FROM {t.table} WHERE {t.column} IS NULL"  # noqa: S608 — module constants
        ).scalar()
        if nulls:
            log.warning(
                "094: %s.%s holds %s NULL row(s); leaving it nullable. Resolve "
                "those rows, then set NOT NULL by hand.",
                t.table, t.column, nulls,
            )
            return False
        return True

    _tighten(bind, ready)


def downgrade() -> None:
    """Deliberately a no-op — see "Rollback" in the module docstring."""
