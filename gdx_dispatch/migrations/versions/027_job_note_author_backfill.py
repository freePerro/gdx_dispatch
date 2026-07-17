"""Backfill job_notes.author_name — every note was attributed to nobody

Both note writers resolved the author's display name off the auth dict:

    user.get("name") or user.get("full_name") or user.get("email")

The access token carries sub/role/tenant_id/exp/jti/typ and none of those keys,
so the expression was always None and `author_name` was written NULL for every
note ever created. Production at the time of this migration: 11 notes, 11 with
an author_id, **0 with a name** — and JobDetailView renders
``note.author_name || 'Unknown'``, so the office could not tell who wrote any
note on any job. It stayed invisible because NULL is legal for the column and
"Unknown" reads as a display default rather than a defect.

The writers are fixed (core/user_display.resolve_author_name resolves from the
user's id, which the token does carry). This migration repairs the history: the
id was recorded correctly all along, so the names are recoverable by joining
users.

Rows whose author_id is not a real user id — "system", or an email from an old
code path — match nothing and stay NULL. That is correct: inventing a name for
them would be worse than admitting we don't know.

``job_notes`` is ORM-created (not in the squashed baseline), so on a fresh DB
this is a guarded no-op — there are no rows to repair. Idempotent: it only
touches rows where author_name IS NULL, so re-running changes nothing, and it
never overwrites a name a fixed writer has since stored.

Revision ID: 027_job_note_author_backfill
Revises: 026_vendor_bill_allowlist
"""
from alembic import op

revision = "027_job_note_author_backfill"
down_revision = "026_vendor_bill_allowlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'job_notes'
          ) AND EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'users'
          ) THEN
            -- Same preference order as core/user_display.resolve_author_name,
            -- so a backfilled name and a freshly written one agree.
            -- NULLIF(...,'') because a blank column is not a name.
            UPDATE job_notes n
               SET author_name = COALESCE(
                     NULLIF(btrim(u.name), ''),
                     NULLIF(btrim(u.full_name), ''),
                     NULLIF(btrim(u.username), ''),
                     NULLIF(btrim(u.email), '')
                   )
              FROM users u
             WHERE n.author_name IS NULL
               AND n.author_id IS NOT NULL
               -- author_id is varchar; users.id is uuid. Cast the uuid to text
               -- rather than the text to uuid: author_id legitimately holds
               -- non-uuid values ('system'), and text::uuid RAISES on those,
               -- which would abort the whole migration.
               AND n.author_id = u.id::text;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Deliberately not reversible. The "before" state is NULL — data loss we
    # just repaired — and there is no way to tell a backfilled name from one a
    # fixed writer stored afterwards, so a downgrade would blank real data.
    pass
