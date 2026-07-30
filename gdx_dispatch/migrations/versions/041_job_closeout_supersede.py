"""Job closeout supersede model — one live snapshot per job (plan §12).

`POST /api/jobs/{id}/closeout` was append-only: a re-closeout inserted a
second `job_closeouts` row with nothing marking which one is current. The
comment claiming "one JobCloseout per job, re-closeout restates it" was only
ever true of the LABOR row. First re-closeout in prod would have:

* raised MultipleResultsFound in any `scalar_one_or_none()` reader keyed on
  job_id (a 500 on the office closeout card being built in plan §1), and
* double-counted hours in the tech-efficiency report's SUMs.

The fix is supersede-never-overwrite: the prior row gets `superseded_at`
stamped and the new row points back via `supersedes_id`; an attestation is
evidence and is never deleted or edited. The partial unique index enforces
exactly one live row per job at the database level, so a future code path
that forgets the stamp fails loudly instead of silently forking history.

Existing data: prod held 8 closeouts across 8 distinct jobs when this was
written (2026-07-29) — but that is a point-in-time observation, and the OLD
code appends a second live row on any re-closeout right up until this deploy.
If anyone re-closes out a job in that window, CREATE UNIQUE INDEX fails on the
duplicate, alembic fails, and the entrypoint's `set -e` crash-loops the whole
app (adversarial audit, round 2). So the index is preceded by a defensive
stamp: for any job with multiple live rows, every row but the newest is marked
superseded. Idempotent, a no-op when the data is already clean, and it also
covers demo/dev databases nobody re-verified. `supersedes_id` is left NULL on
defensively-stamped rows — the true chain is unknowable after the fact, and
NULL-linkage is the honest record of that.

Columns are added with IF NOT EXISTS because create_orm_tables() runs before
alembic (docker/entrypoint.sh) and will have already built the full table on
a FRESH database; on every already-deployed environment this migration is
what adds them (create_all never alters existing tables — same split as
040_door_listings).

Revision ID: 041_job_closeout_supersede
Revises: 040_door_listings
"""
from alembic import op

revision = "041_job_closeout_supersede"
down_revision = "040_door_listings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    bind.exec_driver_sql(
        "ALTER TABLE job_closeouts "
        "ADD COLUMN IF NOT EXISTS superseded_at timestamptz"
    )
    bind.exec_driver_sql(
        "ALTER TABLE job_closeouts "
        "ADD COLUMN IF NOT EXISTS supersedes_id uuid"
    )
    # Defensive stamp BEFORE the index (see docstring): any job that already
    # has multiple live snapshots keeps only its newest one live. Ordering:
    # closed_at, then created_at, then id — the same precedence a reader would
    # use to guess "which one is current".
    bind.exec_driver_sql(
        """
        UPDATE job_closeouts jc
        SET superseded_at = now()
        WHERE jc.superseded_at IS NULL
          AND jc.deleted_at IS NULL
          AND EXISTS (
            SELECT 1 FROM job_closeouts newer
            WHERE newer.job_id = jc.job_id
              AND newer.id <> jc.id
              AND newer.superseded_at IS NULL
              AND newer.deleted_at IS NULL
              AND (newer.closed_at, newer.created_at, newer.id)
                  > (jc.closed_at, jc.created_at, jc.id)
          )
        """
    )

    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_closeouts_live "
        "ON job_closeouts (job_id) "
        "WHERE superseded_at IS NULL AND deleted_at IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS uq_job_closeouts_live")
    bind.exec_driver_sql("ALTER TABLE job_closeouts DROP COLUMN IF EXISTS supersedes_id")
    bind.exec_driver_sql("ALTER TABLE job_closeouts DROP COLUMN IF EXISTS superseded_at")
