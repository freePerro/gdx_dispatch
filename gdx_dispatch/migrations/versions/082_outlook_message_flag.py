"""outlook_messages learns the Outlook follow-up flag.

The office pins mail in Outlook to keep it on top; Outlook's *pin* has no
Microsoft Graph surface (checked against the v1.0 and beta ``message``
resource on 2026-08-27 — no property mentions it), so it can never sync.
The follow-up **flag** does: ``flag.flagStatus`` is readable and writable via
Graph. This column mirrors it, and the inbox sorts flagged mail first.

Existing rows: FALSE, and that is not a claim about their real state. Graph
bakes ``$select`` inside the opaque ``$deltatoken`` (prod deltaLinks carry no
``$select=`` in the query string — checked 2026-08-27), so every stored link
would keep replaying the pre-flag shape and ``is_flagged`` would stay FALSE
forever. This migration therefore marks every folder ``full_resync_required``
(and drops its token) — the same path the 410-Gone handler uses — so the next
sync re-walks each folder once with the new select and writes the real value.
Upsert is idempotent; the cost is Graph calls, not data.

Revision ID: 082_outlook_message_flag
Revises: 081_pay_period_settings
"""
from __future__ import annotations

import contextlib

from alembic import op

revision = "082_outlook_message_flag"
down_revision = "081_pay_period_settings"
branch_labels = None
depends_on = None

_TABLE = "outlook_messages"
_COLUMN = "is_flagged"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: no ADD COLUMN IF NOT EXISTS; a fresh database already has
        # the column from the ORM metadata, so "duplicate column" is expected.
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} BOOLEAN NOT NULL DEFAULT 0;"
            )
        _force_resync(bind)
        return
    bind.exec_driver_sql(
        f"ALTER TABLE IF EXISTS {_TABLE} "
        f"ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT FALSE;"
    )
    _force_resync(bind)


def _force_resync(bind) -> None:
    """One re-walk per folder so the new field is populated (see docstring)."""
    if bind.dialect.name != "postgresql":
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(
                "UPDATE outlook_folder_sync_state "
                "SET full_resync_required = 1, delta_token = NULL;"
            )
        return
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.outlook_folder_sync_state') IS NOT NULL THEN
            UPDATE outlook_folder_sync_state
               SET full_resync_required = TRUE, delta_token = NULL;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Loses nothing that cannot be rebuilt: the flag lives in the mailbox and
    # a re-add + resync restores every value.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        with contextlib.suppress(Exception):
            bind.exec_driver_sql(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN};")
    else:
        bind.exec_driver_sql(
            f"ALTER TABLE IF EXISTS {_TABLE} DROP COLUMN IF EXISTS {_COLUMN};"
        )
