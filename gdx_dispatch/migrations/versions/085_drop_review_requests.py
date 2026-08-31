"""Drop review_requests — a table with one writer nothing called and no reader.

`review_requests` was written only by `routers/marketing.py::
schedule_review_request_for_completed_job`, a function with no production
caller, and read by nothing. Prod and demo both hold 0 rows. The function,
the ORM model and this table go together (2026-08-31); the one real review
table, `customer_reviews`, is untouched.

Rollback: `downgrade()` recreates the table with its nine columns (all text,
matching the ORM it had) — empty, which is exactly what it was.

Revision ID: 085_drop_review_requests
Revises: 084_google_review_url
"""
from alembic import op

revision = "085_drop_review_requests"
down_revision = "084_google_review_url"
branch_labels = None
depends_on = None

_TABLE = "review_requests"

# One DDL string, two dialects — both accept it verbatim.
# IF NOT EXISTS rather than try/except: on Postgres a failed CREATE inside
# alembic's transaction poisons it (InFailedSqlTransaction on the next
# statement), so "already present" must be handled by the DDL itself.
_CREATE = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    customer_id TEXT,
    status TEXT NOT NULL,
    message TEXT,
    google_reviews_link TEXT,
    scheduled_for TEXT,
    sent_at TEXT,
    created_at TEXT
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    # IF EXISTS on both engines: an install whose bootstrap never created the
    # table (create_all is checkfirst, the model is gone) must not abort here.
    bind.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLE};")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_CREATE)
