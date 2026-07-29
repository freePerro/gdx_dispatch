"""Door listings — doors for sale, published to the public website

Two halves, and they need different treatment:

1. `door_listings` / `door_listing_photos` are ORM-managed and brand new, so
   `create_orm_tables()` — which the entrypoint runs BEFORE alembic
   (docker/entrypoint.sh) — has already built them by the time this runs. A
   plain `op.create_table()` here would raise "relation already exists" and,
   under `set -e`, crash-loop the container. Hence raw
   `CREATE TABLE IF NOT EXISTS`, the same idiom as 030_customer_contacts.

2. `app_settings.customer_listings_enabled` and
   `customers.can_submit_listings` are new columns on EXISTING tables, and
   `create_all` never alters an existing table. For those this migration is
   the only thing that works — on every already-deployed environment a
   missing column would 500 the settings page on first read.

Both halves are guarded and idempotent, so re-running is a no-op.

Revision ID: 040_door_listings
Revises: 039_vendor_statement_source
"""
from alembic import op

revision = "040_door_listings"
down_revision = "039_vendor_statement_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS door_listings (
            id                        uuid PRIMARY KEY,
            listing_type              varchar(20)  NOT NULL DEFAULT 'used',
            title                     varchar(200) NOT NULL,
            slug                      varchar(220) NOT NULL,
            description               text,
            status                    varchar(20)  NOT NULL DEFAULT 'draft',
            condition                 varchar(20),
            width_in                  numeric(6,2),
            height_in                 numeric(6,2),
            material                  varchar(60),
            color                     varchar(60),
            insulation_r              numeric(5,2),
            has_windows               boolean      NOT NULL DEFAULT false,
            brand                     varchar(80),
            model                     varchar(80),
            qty                       integer      NOT NULL DEFAULT 1,
            featured                  boolean      NOT NULL DEFAULT false,
            price                     numeric(10,2),
            compare_at_price          numeric(10,2),
            price_display             varchar(20)  NOT NULL DEFAULT 'fixed',
            source                    varchar(20)  NOT NULL DEFAULT 'office',
            submitted_by_user_id      uuid,
            submitted_by_customer_id  uuid,
            submitted_at              timestamptz,
            reviewed_by_user_id       uuid,
            reviewed_at               timestamptz,
            rejection_reason          text,
            source_job_id             uuid,
            published_at              timestamptz,
            sold_at                   timestamptz,
            created_at                timestamptz  NOT NULL DEFAULT now(),
            updated_at                timestamptz,
            deleted_at                timestamptz
        );
        """
    )
    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS door_listing_photos (
            id           uuid PRIMARY KEY,
            listing_id   uuid NOT NULL REFERENCES door_listings(id),
            filename     varchar(255) NOT NULL,
            content_type varchar(80)  NOT NULL DEFAULT 'image/jpeg',
            sort_order   integer      NOT NULL DEFAULT 0,
            created_at   timestamptz  NOT NULL DEFAULT now()
        );
        """
    )

    # The public page's only query: published listings, GDX stock first.
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_door_listings_status ON door_listings (status);"
    )
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_door_listings_slug ON door_listings (slug);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_door_listings_source ON door_listings (source);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_door_listings_submitted_by_customer_id "
        "ON door_listings (submitted_by_customer_id);"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_door_listing_photos_listing_id "
        "ON door_listing_photos (listing_id);"
    )

    # ── The half create_all cannot do: columns on existing tables ────────────
    # Both default OFF. Customer-submitted listings stay dark until Doug turns
    # them on company-wide AND for the individual customer.
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.app_settings') IS NOT NULL THEN
            ALTER TABLE app_settings
              ADD COLUMN IF NOT EXISTS customer_listings_enabled boolean NOT NULL DEFAULT false;
          END IF;
        END $$;
        """
    )
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.customers') IS NOT NULL THEN
            ALTER TABLE customers
              ADD COLUMN IF NOT EXISTS can_submit_listings boolean NOT NULL DEFAULT false;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        DO $$ BEGIN
          IF to_regclass('public.customers') IS NOT NULL THEN
            ALTER TABLE customers DROP COLUMN IF EXISTS can_submit_listings;
          END IF;
          IF to_regclass('public.app_settings') IS NOT NULL THEN
            ALTER TABLE app_settings DROP COLUMN IF EXISTS customer_listings_enabled;
          END IF;
        END $$;
        """
    )
    bind.exec_driver_sql("DROP TABLE IF EXISTS door_listing_photos;")
    bind.exec_driver_sql("DROP TABLE IF EXISTS door_listings;")
