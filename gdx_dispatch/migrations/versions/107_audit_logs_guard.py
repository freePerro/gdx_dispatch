"""Install the audit_logs immutability guard at upgrade, not only at runtime.

ARCHITECTURAL INVARIANT #2's database-level half is the plpgsql function
``audit_logs_immutable_guard()`` and its BEFORE UPDATE / BEFORE DELETE
triggers on ``audit_logs``. Until now nothing but ``core/audit.py``'s
``ensure_audit_table`` installed them, lazily, on the first audit write — as
the runtime role, which under D97 Phase 1 (``gdx_app``, NOSUPERUSER, no CREATE
on ``public``) cannot issue the DDL. A ``create_all`` database or a paved one
was therefore guard-absent by construction, and stayed that way (GDXA-352).

This migration runs the SAME DDL (``core.audit.install_pg_audit_guard``, the
way 012 shares ``modules/ledger/ddl.py``) as the migration role, which already
creates plpgsql functions and triggers for 012. Idempotent: CREATE OR REPLACE
FUNCTION, DROP TRIGGER IF EXISTS, CREATE TRIGGER.

**It does not refuse the upgrade when it cannot install.** The DDL runs in a
savepoint; a refusal (no privilege, not the table owner, ``lock_timeout``
waiting on a long-lived reader of ``audit_logs``) is logged at ERROR and the
upgrade goes on. The entrypoint runs ``alembic upgrade head`` under ``set -e``
and ``app`` restarts ``unless-stopped``, so a raise here would crash-loop prod
over a guard it has been running without — the failure mode 088/091/092 taught.
The absence stays loud: ``ensure_audit_table`` logs
``audit_guard_missing`` and the app logs ``STARTUP_AUDIT_GUARD_MISSING``
at boot.

Postgres only. SQLite has no roles, and ``ensure_audit_table`` installs its two
triggers on first use there; ``audit_logs`` on SQLite is not ours to shape in a
migration (``create_all`` builds it from the ORM).

Existing rows: untouched — triggers fire only on UPDATE and DELETE, and no code
path in the app updates or deletes ``audit_logs`` (``grep`` for
``update(AuditLog`` / ``delete(AuditLog`` / ``UPDATE audit_logs`` /
``DELETE FROM audit_logs`` outside tests, 2026-10-06: none). From here on a raw
UPDATE or DELETE on the table raises ``audit_logs is immutable``.

Rollback: ``downgrade()`` drops both triggers and the function. That also
removes a guard installed before this migration (by an older runtime or by
hand) — after a downgrade, ``ensure_audit_table`` reinstalls it wherever the
runtime role is privileged. By hand:
``DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs;
DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs;
DROP FUNCTION IF EXISTS audit_logs_immutable_guard();``

Revision ID: 107_audit_logs_guard
Revises: 106_day_close_markers
Create Date: 2026-10-06
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gdx_dispatch.core.audit import audit_guard_present, install_pg_audit_guard

revision = "107_audit_logs_guard"
down_revision = "106_day_close_markers"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(text("SELECT to_regclass('audit_logs')")).scalar() is None:
        # create_orm_tables() builds it before alembic (#41); a database that
        # skipped that gets the guard from ensure_audit_table instead.
        log.warning("107: audit_logs is absent; immutability guard not installed")
        return
    if audit_guard_present(bind):
        return
    try:
        with bind.begin_nested():
            # LOCAL, inside the savepoint: released with it on failure, and
            # reset below on success so later migrations keep the default.
            bind.execute(text("SET LOCAL lock_timeout = '10s'"))
            install_pg_audit_guard(bind)
        bind.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    except DBAPIError as exc:
        # psycopg2 calls it pgcode, psycopg 3 sqlstate.
        code = getattr(exc.orig, "pgcode", None) or getattr(exc.orig, "sqlstate", None)
        first_line = (str(exc.orig).strip().splitlines() or [""])[0]
        log.error(
            f"107: could NOT install the audit_logs immutability guard (sqlstate={code}: "
            f"{first_line}). audit_logs stays UPDATE/DELETE-able. Re-run `alembic "
            "downgrade 106_day_close_markers && alembic upgrade head` as the table owner."
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if bind.execute(text("SELECT to_regclass('audit_logs')")).scalar() is not None:
        bind.execute(text("DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs"))
        bind.execute(text("DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs"))
    bind.execute(text("DROP FUNCTION IF EXISTS audit_logs_immutable_guard()"))
