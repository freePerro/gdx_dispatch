"""Sprint 1.x-S1 — verify the gdx_ai_readonly role on a paved Postgres tenant.

Skips on SQLite. Requires TEST_DATABASE_URL to point at a paved tenant DB with
the role already bootstrapped (run pave_tenant_db.py with
TENANT_AI_READONLY_PASSWORD set first).

Two ways to use:
  - Set TEST_DATABASE_URL to a URL whose user is gdx_ai_readonly. The test
    confirms the role's privileges on that connection.
  - Or set TEST_DATABASE_URL to the superuser URL plus
    GDX_AI_READONLY_PASSWORD; the test rebuilds the URL with the readonly user
    and connects with that.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError


def _readonly_engine():
    raw = os.environ.get("TEST_DATABASE_URL", "")
    if not raw:
        pytest.skip("TEST_DATABASE_URL not set")
    if "postgresql" not in raw:
        pytest.skip("test requires PostgreSQL")

    pw = os.environ.get("GDX_AI_READONLY_PASSWORD")
    if pw:
        parts = urlparse(raw)
        host = parts.hostname or "localhost"
        port = f":{parts.port}" if parts.port else ""
        netloc = f"gdx_ai_readonly:{pw}@{host}{port}"
        raw = urlunparse(parts._replace(netloc=netloc))

    return create_engine(raw)


def test_current_user_is_gdx_ai_readonly():
    engine = _readonly_engine()
    with engine.connect() as conn:
        user = conn.execute(text("SELECT current_user")).scalar()
    assert user == "gdx_ai_readonly"


def test_select_succeeds_on_customers():
    engine = _readonly_engine()
    with engine.connect() as conn:
        # Count must succeed; value doesn't matter (paved tenant may be empty).
        conn.execute(text("SELECT count(*) FROM customers")).scalar()


def test_insert_raises_permission_denied():
    engine = _readonly_engine()
    with engine.connect() as conn:
        with pytest.raises((DBAPIError, ProgrammingError)) as excinfo:
            conn.execute(text(
                "INSERT INTO customers (id, first_name) "
                "VALUES (gen_random_uuid(), 'rls-probe')"
            ))
        assert "permission denied" in str(excinfo.value).lower()
