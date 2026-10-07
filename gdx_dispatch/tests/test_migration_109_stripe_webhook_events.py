"""Migration 109: ``stripe_webhook_events`` (GDXA-357), on both engines: up,
down and up again; rerunnable; a no-op on a database ``create_all`` built (the
boot path); the event id is a real primary key, so a second insert of the same
event is refused by the database rather than by a read-then-write check."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/109_stripe_webhook_events.py"
TABLE = "stripe_webhook_events"
COLUMNS = {"event_id", "event_type", "result_status", "processed_at"}
INSERT = (
    "INSERT INTO stripe_webhook_events (event_id, event_type, result_status, processed_at) "
    "VALUES ('evt_1', 'payment_intent.succeeded', 'paid', '2026-10-07 02:00:00+00')"
)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m109", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has(conn) -> bool:
    return inspect(conn).has_table(TABLE)


def test_it_chains_onto_108_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "109_stripe_webhook_events"' in source
    assert 'down_revision = "108_invoice_line_qty_decimal"' in source
    assert len("109_stripe_webhook_events") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "108_invoice_line_qty_decimal"', p.read_text(), re.M)
    ]
    assert revising == ["109_stripe_webhook_events.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_the_orm_model_matches_the_migration() -> None:
    from gdx_dispatch.models.tenant_models import StripeWebhookEvent

    table = StripeWebhookEvent.__table__
    assert table.name == TABLE
    assert set(table.columns.keys()) == COLUMNS
    assert [c.name for c in table.primary_key.columns] == ["event_id"]
    assert table.columns["processed_at"].type.timezone is True


def test_sqlite_round_trip_and_the_primary_key_refuses_a_repeat(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm109.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _has(c)
        assert {col["name"] for col in inspect(c).get_columns(TABLE)} == COLUMNS
        m.upgrade()  # rerun is a no-op
        c.exec_driver_sql(INSERT)
    with pytest.raises(IntegrityError), eng.begin() as c:
        c.exec_driver_sql(INSERT)
    with eng.begin() as c:
        m = _load(c)
        m.downgrade()
        assert not _has(c)
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _has(c)
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op_and_the_orm_round_trips(tmp_path):
    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models.tenant_models import StripeWebhookEvent

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng)  # the boot path, every table
    with eng.begin() as c:
        assert _has(c), "create_all already builds the table"
        _load(c).upgrade()  # a no-op
    with Session(eng) as s:
        s.add(StripeWebhookEvent(event_id="evt_orm", event_type="charge.refunded", result_status="reversed"))
        s.commit()
    with Session(eng) as s:
        row = s.get(StripeWebhookEvent, "evt_orm")
        assert row.result_status == "reversed" and row.processed_at is not None
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 109 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m109_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m = _load(c)
            m.upgrade()
            c.commit()
            assert _has(c)
            types = dict(
                c.exec_driver_sql(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'm109_test' AND table_name = 'stripe_webhook_events'"
                ).fetchall()
            )
            assert types == {
                "event_id": "character varying",
                "event_type": "character varying",
                "result_status": "character varying",
                "processed_at": "timestamp with time zone",
            }, types
            c.exec_driver_sql(INSERT)
            c.commit()
            with pytest.raises(IntegrityError):
                c.exec_driver_sql(INSERT)
            c.rollback()
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m = _load(c)
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert not _has(c)
            m.upgrade()
            c.commit()
            assert _has(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
