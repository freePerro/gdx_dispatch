"""bootstrap_app step 6: the default lead intake fields are seeded on boot.

The seeder is unit-tested in test_lead_intake_pr_a.py; this pins the WIRING —
that `bootstrap_app.main()` actually calls it. The step is wrapped so a
failure cannot crash-loop the app, which also means a dropped or broken call
would only ever show as a log line and an intake form with no fields. This
runs the real `main()` twice against a throwaway Postgres database — not
SQLite, where main()'s `db.get(Tenant, <str id>)` cannot bind a string to a
Uuid column (the harness limit that kept main() untested until now).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import gdx_dispatch.core.database as database
from gdx_dispatch.tools import bootstrap_app


@pytest.fixture()
def boot_db(monkeypatch):
    """A fresh, EMPTY Postgres database built the way the entrypoint builds
    one — ORM tables, plus the Alembic-owned `tenants` row target — not the
    shared structure.sql template, which lags the ORM."""
    import uuid

    import gdx_dispatch.models  # noqa: F401 — register every model on the metadata
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.tenant_settings import Tenant
    from gdx_dispatch.tests.fixtures import pg

    try:
        pg._admin_conn().close()
    except Exception as exc:  # noqa: BLE001 — any connect failure means "no PG here"
        pg._skip_unless_ci(f"postgres not reachable: {exc}")
    name = f"gdx_boot_{uuid.uuid4().hex[:12]}"
    pg._create_db(name)
    engine = create_engine(
        f"postgresql+psycopg2://{pg.PG_USER}:{pg.PG_PASSWORD}@{pg.PG_HOST}:{pg.PG_PORT}/{name}",
        future=True,
    )

    def _create_tables() -> None:
        TenantBase.metadata.create_all(engine, checkfirst=True)
        Tenant.__table__.create(engine, checkfirst=True)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine, future=True))
    monkeypatch.setattr(bootstrap_app, "create_orm_tables", _create_tables)
    monkeypatch.setenv("GDX_ADMIN_PASSWORD", "boot-test-password-123")
    monkeypatch.delenv("GDX_SKIP_BOOTSTRAP", raising=False)
    try:
        yield engine
    finally:
        engine.dispose()
        pg._drop_db(name)


def _count(engine, sql: str) -> int:
    with engine.connect() as c:
        return c.execute(text(sql)).scalar_one()


def test_main_seeds_the_lead_fields_once(boot_db) -> None:
    assert bootstrap_app.main() == 0
    assert _count(boot_db, "SELECT count(*) FROM custom_field_definitions WHERE entity_type = 'lead'") == 5
    assert _count(boot_db, "SELECT count(*) FROM audit_logs WHERE action = 'lead_intake_fields_seeded'") == 1

    assert bootstrap_app.main() == 0  # the next boot
    assert _count(boot_db, "SELECT count(*) FROM custom_field_definitions WHERE entity_type = 'lead'") == 5
    assert _count(boot_db, "SELECT count(*) FROM audit_logs WHERE action = 'lead_intake_fields_seeded'") == 1
