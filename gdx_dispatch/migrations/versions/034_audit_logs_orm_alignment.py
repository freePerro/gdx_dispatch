"""034 — align control-plane audit_logs with ORM (D97 Phase 1 follow-up)

The control-plane ``audit_logs`` table predates the unified ORM model
(``gdx_dispatch.core.audit.AuditLog``). When the SPA admin AI-settings PUT
landed (Sprint 1.x-S26) and called ``log_audit_event_sync`` against
the control-plane Session, SQLAlchemy raised ``UndefinedColumn`` on:

  * ``audit_logs.row_hash`` (column was named ``hash``)
  * ``audit_logs.request_id`` (missing entirely)
  * ``audit_logs.event_type / actor_id / actor_role / payload`` (legacy
    duplicate-of-action/user_id/details columns that the ORM still
    INSERTs into for backward compat — see ``audit.py`` SQLite branch)

In addition, the post-D97-Phase-1 runtime role ``gdx_app`` lacks CREATE
on schema public. ``ensure_audit_table`` runs CREATE OR REPLACE
FUNCTION + CREATE TRIGGER on every fresh process; under gdx_app this
raises ``InsufficientPrivilege``. This migration installs the
``audit_logs_immutable_guard`` function + triggers as the migration
role (which has DDL privileges) so the runtime path can fast-skip.

Was applied manually on prod 2026-04-27 ~08:55 UTC (psql as ``gdx``);
this migration backfills the same DDL so dev / lab / future tenants
get the aligned schema.

Revision ID: 034_audit_logs_orm_alignment
Revises: tenant_settings_table
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op


revision = "034_audit_logs_orm_alignment"
down_revision = "tenant_settings_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the immutability triggers (created by audit.py at runtime —
    #    may or may not exist depending on container history). We re-attach
    #    them at the end so this migration is idempotent.
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs")

    # 2. Rename ``hash`` -> ``row_hash`` if the legacy column exists.
    #    On fresh installs the ORM ``metadata.create_all`` already used
    #    ``row_hash`` so this is a no-op.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'audit_logs' AND column_name = 'hash'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'audit_logs' AND column_name = 'row_hash'
            ) THEN
                ALTER TABLE audit_logs RENAME COLUMN hash TO row_hash;
            END IF;
        END $$;
        """
    )

    # 3. Add the columns the ORM expects but the legacy schema lacks.
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS request_id varchar(64)")
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS event_type varchar(128)")
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_id varchar(255)")
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_role varchar(64)")
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS payload jsonb")
    # ``hash`` re-added (legacy duplicate-of-row_hash kept for compat).
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS hash varchar(64)")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_request_id ON audit_logs(request_id)"
    )

    # 4. Install the audit-immutability guard function + triggers.
    #    These are normally created at app startup by audit.py's
    #    ensure_audit_table, but the runtime role gdx_app lacks CREATE
    #    privilege post-D97 Phase 1. Install via this migration which
    #    runs as the superuser ``gdx`` role.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_logs_immutable_guard()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is immutable (op=%)', TG_OP
                USING HINT = 'audit rows are append-only; see D45';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_update
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable_guard()
        """
    )


def downgrade() -> None:
    # Drop triggers + helper function so a future ALTER can succeed.
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_immutable_guard()")

    # We do NOT undo the column add/renames in downgrade — they're
    # ORM-aligning, and downgrading them would leave the running app
    # broken on subsequent INSERTs. Operator can DROP COLUMN manually
    # if a true rollback is required.
