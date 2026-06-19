"""Make pgcrypto an explicit dependency.

Mig 068's cc_bootstrap_super_admin calls gen_random_bytes(32) for the
audit row hash. gen_random_bytes lives in pgcrypto; gen_random_uuid
(also used) is built-in since PG 13. Prod gdx_control already had
pgcrypto from earlier deploys, so 068 worked there. Lab gdx_control
(fresh-paved) did NOT have it — bootstrap raised "function
gen_random_bytes(integer) does not exist" at S86 lab catch-up.

Idempotent CREATE EXTENSION; safe on every environment.
"""
from __future__ import annotations

from alembic import op

revision = "069_pgcrypto_explicit"
down_revision = "068_cc_bootstrap_super_admin_fn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    # Intentional no-op. Other code paths (cc_bootstrap_super_admin, future
    # callers) depend on pgcrypto; dropping it would break them. If you
    # really need to drop the extension, do it manually after auditing
    # every caller.
    pass
