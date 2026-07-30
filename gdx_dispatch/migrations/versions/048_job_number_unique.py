"""Job number uniqueness + stuck-counter repair — plan §5.

Root cause (found on prod 2026-07-30): tenant_settings.job_number_next_seq was
stuck at 1 (year_seen NULL), so next_job_number() handed out JOB-2026-001, -002…
that were ALREADY in use by older jobs — silent collisions on every new job.
The highest suffix in use was 031 while the counter said 1.

This migration fixes it in one shot and makes recurrence impossible:
  1. Renumber the newer of each colliding pair (keep the EARLIEST by created_at)
     to fresh sequential numbers above the highest in use. No job loses its
     identity; the older job keeps the number it had first.
  2. Advance job_number_next_seq past the new high-water mark and set
     year_seen — so the NEXT job created can't reuse an old number.
  3. Add a partial UNIQUE index on live, numbered rows. Belt-and-suspenders:
     if the counter is ever corrupted again, a duplicate becomes a LOUD insert
     error instead of a silent double-booking to billing.

`jobs` is a create_all (tenant-plane) table not in the baseline — guard with
to_regclass (003/013/019/041 pattern). No `%` anywhere: exec_driver_sql hands
the SQL to psycopg2, which treats `%` as a parameter marker.

Revision ID: 048_job_number_unique
Revises: 047_closeout_billing_reconcile
"""
from alembic import op

revision = "048_job_number_unique"
down_revision = "047_closeout_billing_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Single-tenant DB (Doug 2026-07-30): job_number is unique DB-wide, so the
    # dedup/index/counter are intentionally NOT scoped by company_id. The [0-9]
    # class (not \d) keeps the dynamically-built regex free of backslash-escape
    # surprises. `cur_year` comes from now(), NOT the highest-suffix row's year:
    # legacy QB-import numbers could carry an old year and mis-set year_seen,
    # which would make the next real job reset to seq 1 and re-collide.
    op.get_bind().exec_driver_sql(
        """
        DO $do$
        DECLARE
            cur_year int := extract(year from now())::int;
            maxseq   int;
            losers   int := 0;
        BEGIN
          IF to_regclass('public.jobs') IS NULL THEN
            RETURN;
          END IF;

          -- Highest suffix currently in use IN THE CURRENT-YEAR SERIES.
          SELECT coalesce(max((regexp_match(job_number, 'JOB-[0-9]{4}-([0-9]+)'))[1]::int), 0)
            INTO maxseq
          FROM jobs
          WHERE job_number ~ ('^JOB-' || cur_year || '-[0-9]+$');

          -- Renumber duplicate-losers (keep earliest per number) to fresh numbers
          -- above the current-year high-water mark. Non-destructive: no job lost.
          WITH ranked AS (
            SELECT id, row_number() OVER (
                     PARTITION BY job_number ORDER BY created_at ASC, id ASC
                   ) AS rn
            FROM jobs
            WHERE deleted_at IS NULL AND job_number IS NOT NULL
          ),
          loser AS (
            SELECT id, row_number() OVER (ORDER BY id) AS n FROM ranked WHERE rn > 1
          )
          UPDATE jobs j
             SET job_number = 'JOB-' || cur_year || '-'
                              || lpad((maxseq + loser.n)::text, 3, '0')
            FROM loser
           WHERE j.id = loser.id;
          GET DIAGNOSTICS losers = ROW_COUNT;

          -- Push the counter past the new high-water mark + stamp the year, so
          -- the NEXT job created can't reuse an old number. GREATEST → never
          -- regress a healthy counter.
          IF to_regclass('public.tenant_settings') IS NOT NULL THEN
            UPDATE tenant_settings
               SET job_number_next_seq =
                     GREATEST(COALESCE(job_number_next_seq, 1), maxseq + losers + 1),
                   job_number_year_seen = cur_year;
          END IF;

          CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_job_number_live
            ON jobs (job_number)
            WHERE deleted_at IS NULL AND job_number IS NOT NULL;
        END $do$;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP INDEX IF EXISTS ux_jobs_job_number_live")
