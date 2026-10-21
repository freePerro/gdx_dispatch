"""GDX_REQUIRE_EXISTING_DATA=1: bootstrap refuses instead of seeding a fresh
tenant (one-layout plan, audit finding 1). Prod sets it, so the base compose
run without the prod override, which mounts an empty database volume, stops
in the entrypoint instead of serving an empty install that answers 200."""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.tenant_settings import Tenant
from gdx_dispatch.tools.bootstrap_app import require_existing_tenant

TENANT = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True)
    Tenant.__table__.create(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        yield s
    engine.dispose()


def test_unset_flag_allows_seeding_an_empty_db(db, monkeypatch):
    monkeypatch.delenv("GDX_REQUIRE_EXISTING_DATA", raising=False)
    assert require_existing_tenant(db, Tenant, TENANT) is True


def test_flag_refuses_an_empty_db(db, monkeypatch):
    monkeypatch.setenv("GDX_REQUIRE_EXISTING_DATA", "1")
    assert require_existing_tenant(db, Tenant, TENANT) is False


def test_flag_refuses_a_db_holding_a_different_tenant(db, monkeypatch):
    monkeypatch.setenv("GDX_REQUIRE_EXISTING_DATA", "1")
    db.add(Tenant(id=UUID("00000000-0000-0000-0000-0000000000aa"), slug="x", name="X"))
    db.commit()
    assert require_existing_tenant(db, Tenant, TENANT) is False


def test_flag_passes_when_the_tenant_row_exists(db, monkeypatch):
    monkeypatch.setenv("GDX_REQUIRE_EXISTING_DATA", "1")
    db.add(Tenant(id=TENANT, slug="gdx", name="GDX"))
    db.commit()
    assert require_existing_tenant(db, Tenant, TENANT) is True
