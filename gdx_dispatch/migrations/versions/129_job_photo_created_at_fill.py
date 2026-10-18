"""job_photos.created_at backfilled from uploaded_at.

``job_photos.created_at`` is a Flask-era column the ORM declared with no
default, so nothing ever wrote it: all 62 production rows read NULL, and a
query that filtered on it reported "0 photos in 90 days" (GDXA-1575). The app
itself orders photos on ``uploaded_at`` everywhere; this closes the trap for
everything else that reads the table. The model now defaults ``created_at``
to now, so the ORM fills it on every new row.

Existing rows: every row whose ``created_at`` is NULL gets its own
``uploaded_at``. Rows that already carry a ``created_at`` are left alone. No
column is added, dropped or retyped.

Portability: ``job_photos`` is not in the squashed baseline — production has
the Flask-era table and a fresh database gets it from ``create_all`` — so the
update runs only when the table and both columns exist. SQLite checks through
``PRAGMA table_info``; Postgres through ``information_schema`` scoped to
``current_schema()``. The ``UPDATE ... SET a = b`` itself has no functions or
casts and is the same statement on both.

Rollback: ``downgrade()`` is a no-op. The NULLs carried no information to
restore, and no app code reads ``created_at``.

Revision ID: 129_job_photo_created_at_fill
Revises: 128_on_my_way_sms_opt_in
Create Date: 2026-10-18
"""
from alembic import op

revision = "129_job_photo_created_at_fill"
down_revision = "128_on_my_way_sms_opt_in"
branch_labels = None
depends_on = None

_FILL = (
    "UPDATE job_photos SET created_at = uploaded_at "
    "WHERE created_at IS NULL AND uploaded_at IS NOT NULL"
)


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name != "postgresql":
        columns = {row[1] for row in bind.exec_driver_sql("PRAGMA table_info(job_photos)")}
        if {"created_at", "uploaded_at"} <= columns:
            bind.exec_driver_sql(_FILL)
        return

    present = bind.exec_driver_sql(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'job_photos' "
        "AND column_name IN ('created_at', 'uploaded_at')"
    ).scalar_one()
    if present == 2:
        bind.exec_driver_sql(_FILL)


def downgrade() -> None:
    pass
