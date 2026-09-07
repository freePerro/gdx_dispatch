"""Drop `inbound_sms`, the table behind the Twilio inbound-SMS webhook.

Twilio was never configured on this install: the integration flag in
`app_settings.integrations` was `false`, no credential row existed, no
`TWILIO_*` variable reached any container, and this table held 0 rows on prod
and on demo (measured 2026-09-06). The webhook, its three admin routes, the
sender module and the signature gate were removed in the same change
(`docs/design/twilio-removal-plan.md`). Phone.com is the SMS system
(`modules/phone_com/`, 165 messages on prod).

No migration ever created this table — `create_all` at boot did — so the
drop is guarded with `has_table`. Like 088, it refuses to drop a table that
holds rows: a row here means the "nobody could reach it" premise is wrong on
this install, so stop, keep the data, let a person look. Be clear about what
"stop" means: `alembic upgrade head` runs from the container entrypoint on
every start, so a refused drop means THE APP WILL NOT START until someone
exports and empties the table. That is the intended trade-off (088 made the
same one). Both dialects: `DROP TABLE IF EXISTS` is portable, and nothing
else runs.

Deliberately NOT done here: stripping the stale `"twilio": false` key from
`app_settings.integrations`. `routers/settings.py::_canonical_integrations`
rebuilds that dict from `_ALLOWED_INTEGRATIONS` on every read and every
write, so the key is invisible now and rewritten out on the next save. A
Python-side strip would also have to cope with the JSON column arriving as a
`dict` on Postgres (psycopg2 registers a typecaster) and a `str` on SQLite —
the trap the adversarial audit of this plan flagged.

Rollback: `downgrade()` recreates the table empty with the model's columns
in portable types (VARCHAR/TIMESTAMP rather than Postgres uuid/timestamptz).
An older image's `GET /api/inbound-sms/{id}` would 500 on that shape; nothing
ever called that route, which is why this is acceptable.

Revision ID: 091_drop_inbound_sms
Revises: 090_dedup_copied_bug_reports
Create Date: 2026-09-06
"""

from alembic import op
from sqlalchemy import inspect

revision = "091_drop_inbound_sms"
down_revision = "090_dedup_copied_bug_reports"
branch_labels = None
depends_on = None

_TABLE = "inbound_sms"

_RECREATE = """
CREATE TABLE IF NOT EXISTS inbound_sms (
    id VARCHAR(36) PRIMARY KEY,
    company_id VARCHAR(64) NOT NULL,
    from_number VARCHAR(30) NOT NULL,
    to_number VARCHAR(30) NOT NULL,
    body TEXT NOT NULL,
    provider VARCHAR(30) NOT NULL,
    provider_message_id VARCHAR(100),
    customer_id VARCHAR(36),
    job_id VARCHAR(36),
    processed_at TIMESTAMP,
    received_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL
)
"""


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    rows = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {_TABLE}").scalar()  # noqa: S608 — name from a module constant
    if rows:
        raise RuntimeError(
            f"091: {_TABLE} holds {rows} row(s) — refusing to drop it. Nothing in the "
            "app reads this table any more. Export the rows if anyone wants them "
            "(pg_dump -t inbound_sms on Postgres; sqlite3 .dump on SQLite), empty "
            "it, then restart the container so the migration reruns."
        )
    bind.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLE};")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_RECREATE)
