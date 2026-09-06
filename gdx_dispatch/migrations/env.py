"""Alembic environment for the GDX application database."""
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

# Migration URL resolution:
#   ALEMBIC_DATABASE_URL  — preferred; the container entrypoint exports it from
#                           DATABASE_URL before running `alembic upgrade head`.
#                           Point it at a DDL-capable role: CREATE TABLE etc.
#                           need schema-level privileges a restricted runtime
#                           role may not have.
#   DATABASE_URL          — the application database; used when the override
#                           is unset (a bare `alembic` shell run).
#   sqlalchemy.url (ini)  — last-resort default for offline/test envs.
db_url = (
    os.getenv("ALEMBIC_DATABASE_URL")
    or os.getenv("DATABASE_URL")
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
    from sqlalchemy import text

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            # Single-migrator gate. Alembic has NO built-in concurrency control,
            # so every migrator (container entrypoint AND the in-app DB admin
            # panel) takes the same transaction-scoped advisory lock here. A
            # second migrator blocks until the first commits, then proceeds.
            # xact-scoped so it auto-releases at commit and is pooler-safe.
            connection.execute(text("SELECT pg_advisory_xact_lock(823641723)"))
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
