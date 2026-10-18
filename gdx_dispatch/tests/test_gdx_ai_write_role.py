"""Sprint 1.x-S2 — verify gdx_ai_write column-grained GRANTs on a paved tenant.

Same env-var contract as test_gdx_ai_readonly_role.py: skip on SQLite, skip
without ``TEST_DATABASE_URL``. If ``GDX_AI_WRITE_PASSWORD`` is set, the test
rebuilds the URL with user=gdx_ai_write; otherwise it expects the URL to
already point at that role. The GRANTs under test are in
gdx_dispatch/tools/gdx_ai_roles.sql; CI's ai-roles job applies them.

The whitelisted column is ``notes_appended``. This file used to UPDATE
``last_contacted_at`` and INSERT ``first_name``, neither of which the ORM's
customers table has, so against a paved tenant those tests failed on the
column, not the GRANT (GDXA-1564).
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError


def _writer_engine():
    raw = os.environ.get("TEST_DATABASE_URL", "")
    if not raw:
        pytest.skip("TEST_DATABASE_URL not set")
    if "postgresql" not in raw:
        pytest.skip("test requires PostgreSQL")

    pw = os.environ.get("GDX_AI_WRITE_PASSWORD")
    if pw:
        parts = urlparse(raw)
        host = parts.hostname or "localhost"
        port = f":{parts.port}" if parts.port else ""
        netloc = f"gdx_ai_write:{pw}@{host}{port}"
        raw = urlunparse(parts._replace(netloc=netloc))

    return create_engine(raw)


def _ensure_seed_customer(engine) -> uuid.UUID:
    """Pick any customer id, or skip if the tenant has none. The writer role
    cannot INSERT, so we cannot create one; tests assume a paved + seeded DB."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM customers LIMIT 1")).first()
    if row is None:
        pytest.skip("no customers seeded; can't exercise writer GRANTs")
    return row[0]


def test_current_user_is_gdx_ai_write():
    engine = _writer_engine()
    with engine.connect() as conn:
        user = conn.execute(text("SELECT current_user")).scalar()
    assert user == "gdx_ai_write"


def test_update_whitelisted_column_succeeds():
    engine = _writer_engine()
    cid = _ensure_seed_customer(engine)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE customers SET notes_appended = :v WHERE id = :cid"),
            {"v": "rls-probe", "cid": cid},
        )


def test_update_non_whitelisted_column_denied():
    engine = _writer_engine()
    cid = _ensure_seed_customer(engine)
    with engine.connect() as conn:
        with pytest.raises((DBAPIError, ProgrammingError)) as excinfo:
            conn.execute(
                text("UPDATE customers SET email = :v WHERE id = :cid"),
                {"v": "rls-probe@example.com", "cid": cid},
            )
    assert "permission denied" in str(excinfo.value).lower()


def test_insert_into_customers_denied():
    engine = _writer_engine()
    with engine.connect() as conn:
        with pytest.raises((DBAPIError, ProgrammingError)) as excinfo:
            conn.execute(text(
                "INSERT INTO customers (id, name) "
                "VALUES (gen_random_uuid(), 'rls-probe')"
            ))
    assert "permission denied" in str(excinfo.value).lower()


def test_delete_from_customers_denied():
    engine = _writer_engine()
    cid = _ensure_seed_customer(engine)
    with engine.connect() as conn:
        with pytest.raises((DBAPIError, ProgrammingError)) as excinfo:
            conn.execute(
                text("DELETE FROM customers WHERE id = :cid"),
                {"cid": cid},
            )
    assert "permission denied" in str(excinfo.value).lower()


_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")


def test_customers_privileges_are_exactly_the_whitelist():
    """The probes above try one column and one statement each, so a wider
    SELECT or a second UPDATE column would pass them. This reads every
    privilege the role holds on customers (GDXA-1564)."""
    engine = _writer_engine()
    with engine.connect() as conn:
        table = {
            p for p in _TABLE_PRIVILEGES
            if conn.execute(text("SELECT has_table_privilege('customers', :p)"), {"p": p}).scalar()
        }
        columns = conn.execute(text(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'customers'::regclass "
            "AND attnum > 0 AND NOT attisdropped"
        )).scalars().all()
        held = {
            (c, p) for c in columns for p in _COLUMN_PRIVILEGES
            if conn.execute(
                text("SELECT has_column_privilege('customers', :c, :p)"), {"c": c, "p": p},
            ).scalar()
        }
    assert table == set()
    assert held == {("id", "SELECT"), ("notes_appended", "UPDATE")}
