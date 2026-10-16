"""app_settings gains the "text the customer when a tech is on the way" opt-in.

One additive column, ``on_my_way_sms_enabled boolean NOT NULL DEFAULT false``
(GDXA-1201). While it is false, neither en-route route texts anyone — which is
what every install did before this migration, so the default changes nothing.
Turning it on is a deliberate act on Settings → Integrations → Phone.com.

Existing rows: every ``app_settings`` row reads false. Nothing is backfilled
and no other table is read or written.

Portability: the same two-arm shape as 084 — SQLite ``ADD COLUMN`` under
``suppress`` (a stamped rerun finds the column present), Postgres
``ADD COLUMN IF NOT EXISTS`` guarded on the table existing (``create_all``
builds it from the model on a fresh database but never adds a column to an
existing table).

Rollback: ``downgrade()`` drops the column. It holds one switch of
configuration, re-enterable in a click; texts already queued or sent stay in
``scheduled_sms`` and ``phone_com_messages``. Code that reads the column must
be rolled back first.

Revision ID: 128_on_my_way_sms_opt_in
Revises: 127_closeout_is_restatement
Create Date: 2026-10-15
"""
import contextlib

from alembic import op

revision = "128_on_my_way_sms_opt_in"
down_revision = "127_closeout_is_restatement"
branch_labels = None
depends_on = None

# (name, sqlite type+default, postgres type+default). SQLite has no boolean
# literal before 3.23, so its arm stores 0; Postgres takes ``false``.
_COLUMNS = [
    ("on_my_way_sms_enabled", "boolean DEFAULT 0 NOT NULL", "boolean DEFAULT false NOT NULL"),
]


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name != "postgresql":
        for name, sqlite_def, _pg_def in _COLUMNS:
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE app_settings ADD COLUMN {name} {sqlite_def};")
        return

    for name, _sqlite_def, pg_def in _COLUMNS:
        bind.exec_driver_sql(
            f"""
            DO $$ BEGIN
                IF to_regclass('app_settings') IS NOT NULL THEN
                    ALTER TABLE app_settings
                        ADD COLUMN IF NOT EXISTS {name} {pg_def};
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for name, _sqlite_def, _pg_def in reversed(_COLUMNS):
            with contextlib.suppress(Exception):
                bind.exec_driver_sql(f"ALTER TABLE app_settings DROP COLUMN {name};")
        return

    for name, _sqlite_def, _pg_def in reversed(_COLUMNS):
        bind.exec_driver_sql(
            f"""
            DO $$ BEGIN
                IF to_regclass('app_settings') IS NOT NULL THEN
                    ALTER TABLE app_settings DROP COLUMN IF EXISTS {name};
                END IF;
            END $$;
            """
        )
