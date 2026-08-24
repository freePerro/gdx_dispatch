"""Migration 076 adds `payments.voided_reason`, on both engines, reversibly.

M15 needs to tell a dispute's void from a refund's or the office's before it
can safely put a payment back. `voided_at` alone cannot, and an adversarial
review proved that un-voiding the wrong one invents money.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import create_engine, inspect, text

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/076_payment_voided_reason.py"
)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m076", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    fake = types.ModuleType("alembic")

    class _Op:
        def get_bind(self):
            return conn

    fake.op = _Op()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return mod


SEED = "CREATE TABLE payments (id TEXT PRIMARY KEY, amount REAL, voided_at TEXT)"


@pytest.fixture
def conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm076.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED)
    with eng.begin() as c:
        yield c
    eng.dispose()


def _cols(conn):
    return {c["name"] for c in inspect(conn).get_columns("payments")}


def test_upgrade_adds_the_column(conn):
    m = _load(conn)
    assert "voided_reason" not in _cols(conn)
    m.upgrade()
    assert "voided_reason" in _cols(conn)


def test_existing_voided_rows_get_NULL_not_a_guess(conn):
    """No backfill, deliberately. What nobody recorded cannot be
    reconstructed, and the reinstate path reads NULL as UNKNOWN and refuses —
    which is the whole point. Prod carries 3 such rows.
    """
    conn.exec_driver_sql(
        "INSERT INTO payments (id, amount, voided_at) VALUES ('p1', 100, '2026-01-01')"
    )
    _load(conn).upgrade()
    assert conn.execute(
        text("SELECT voided_reason FROM payments WHERE id='p1'")
    ).scalar() is None


def test_re_runnable_and_reversible(conn):
    m = _load(conn)
    m.upgrade()
    m.upgrade()
    assert "voided_reason" in _cols(conn)
    m.downgrade()
    assert "voided_reason" not in _cols(conn)
    m.upgrade()
    assert "voided_reason" in _cols(conn)


def test_downgrade_keeps_the_money(conn):
    conn.exec_driver_sql(
        "INSERT INTO payments (id, amount, voided_at) VALUES ('p1', 149.00, '2026-01-01')"
    )
    m = _load(conn)
    m.upgrade()
    conn.exec_driver_sql("UPDATE payments SET voided_reason='charge.refunded'")
    m.downgrade()
    assert conn.execute(text("SELECT amount FROM payments WHERE id='p1'")).scalar() == 149.00
    assert conn.execute(text("SELECT voided_at FROM payments WHERE id='p1'")).scalar() is not None


def test_the_declared_reasons_fit_the_column(conn):
    """VARCHAR(64). Postgres rejects an over-long value, SQLite silently
    accepts it — the split that passes every test and fails on deploy."""
    from gdx_dispatch.core.payments import _DISPUTE_VOID_REASONS

    for r in _DISPUTE_VOID_REASONS | {"charge.refunded", "office_void"}:
        assert len(r) <= 64, f"{r!r} does not fit voided_reason VARCHAR(64)"


def test_on_postgres(pg_test_engine):
    """The other engine. Skips when no Postgres is reachable."""
    with pg_test_engine.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS payments CASCADE")
        c.exec_driver_sql(
            "CREATE TABLE payments (id TEXT PRIMARY KEY, amount NUMERIC(12,2), voided_at TIMESTAMPTZ)"
        )
        m = _load(c)
        m.upgrade()
        assert "voided_reason" in _cols(c)
        m.upgrade()
        m.downgrade()
        assert "voided_reason" not in _cols(c)
