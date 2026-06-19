"""Alembic environment for GDX control plane database."""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from gdx_dispatch.control.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Migration URL resolution (post D97 Phase 1):
#   ALEMBIC_DATABASE_URL  — preferred override; should point at a DDL-capable
#                           role (the "gdx" superuser) since CREATE TABLE,
#                           CREATE POLICY, etc. require schema-level privileges
#                           that the runtime "gdx_app" role does NOT have.
#   CONTROL_DATABASE_URL  — runtime app URL (NOSUPERUSER NOBYPASSRLS in
#                           Phase 1); falls back here when ALEMBIC_DATABASE_URL
#                           is unset, but new schema-changing migrations will
#                           fail with "permission denied for schema public"
#                           when this is the path.
#   sqlalchemy.url (ini)  — last-resort default for offline/test envs.
db_url = (
    os.getenv("ALEMBIC_DATABASE_URL")
    or os.getenv("CONTROL_DATABASE_URL")
    or config.get_main_option("sqlalchemy.url")
)
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline():
    context.configure(url=db_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
