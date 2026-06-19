"""Sprint 1.0 B2 — reconcile game_definitions ORM vs DB schema.

The 001_baseline migration created `game_definitions` with a `version`
column but no `created_by`. The ORM (gdx_dispatch.control.models.GameDefinition)
was later updated to add `created_by` (non-nullable, default 'system')
and drop the implicit `version` read, but no follow-up migration shipped.

Result: every GET /api/games/catalog raised
    psycopg2.errors.UndefinedColumn: column game_definitions.created_by
and /admin/games 500'd in prod for every tenant.

Surfaced by the Sprint 1.0 B2 route-coverage harness.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "games_schema_drift"
down_revision = "commerce_plane_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("game_definitions")}

    if "created_by" not in cols:
        # Add with server_default so existing rows are backfilled to "system"
        # (matches the ORM default). NOT NULL is safe after the backfill.
        op.add_column(
            "game_definitions",
            sa.Column(
                "created_by",
                sa.String(length=100),
                nullable=False,
                server_default="system",
            ),
        )

    # `version` is no longer referenced by the ORM. Keep it in place to
    # avoid churning rows on legacy dbs; a subsequent migration can drop
    # it once we've confirmed no other consumer reads it.


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("game_definitions")}
    if "created_by" in cols:
        op.drop_column("game_definitions", "created_by")
