"""GL Phase 1 core — journal integrity triggers (balance / immutability / sealing)

The ``gl_*`` tables themselves are ORM-managed and built by
``create_orm_tables()`` (create_all) BEFORE alembic runs (#41 ordering), same as
every other non-baseline tenant table. This migration adds only the DB-level
integrity that ``create_all`` cannot express — the Postgres triggers defined in
``gdx_dispatch.modules.ledger.ddl`` (one source of truth, shared with the
trigger tests):

1. **Immutability** — lines reject UPDATE/DELETE; entries reject DELETE and
   permit UPDATE only for ``status: posted->reversed`` /
   ``reversed_by_entry_id: NULL->value``.
2. **Sealing** — a line may only be inserted in the transaction that created its
   entry (``entry.created_txid = txid_current()``).
3. **Balance invariant** — a DEFERRABLE INITIALLY DEFERRED constraint trigger
   asserts ``SUM(amount_cents)=0`` with ``>=2`` lines and ``>=1`` debit/credit
   per entry, at commit.

Idempotent (CREATE OR REPLACE / DROP … IF EXISTS) — re-runnable across the
multi-container boot.

⚠ RESTORE CAVEAT (also in the /backup runbook): the sealing trigger checks
``txid_current()``. ``pg_restore --data-only`` / per-table COPY repairs replay
historical lines under a NEW txid and will be rejected. Such restores MUST run
with ``--disable-triggers`` (superuser). A full schema+data ``pg_dump`` restore
is fine — triggers are emitted post-data.

Revision ID: 012_gl_core
Revises: 011_encrypt_vendor_pii
"""
from alembic import op

from gdx_dispatch.modules.ledger.ddl import drop_gl_triggers, install_gl_triggers

revision = "012_gl_core"
down_revision = "011_encrypt_vendor_pii"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Loud precondition: create_orm_tables() must have built the gl_* tables
    # already (#41). Fail with a clear message rather than a cryptic
    # "relation does not exist" on the first CREATE TRIGGER.
    bind.exec_driver_sql(
        """
        DO $guard$
        BEGIN
          IF to_regclass('public.gl_journal_entries') IS NULL
             OR to_regclass('public.gl_journal_lines') IS NULL THEN
            RAISE EXCEPTION
              'gl_* tables missing: create_orm_tables() must run before migration 012 (#41 ordering)';
          END IF;
        END
        $guard$;
        """
    )

    install_gl_triggers(bind)


def downgrade() -> None:
    # Triggers/functions only; the gl_* tables are ORM-managed (create_all).
    drop_gl_triggers(op.get_bind())
