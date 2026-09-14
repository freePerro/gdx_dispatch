"""
Smoke test for the PG-truth fixture (Phase 0 of test suite rebuild).

Proves:
  1. structure.sql loads into a template DB.
  2. Per-test clones are isolated.
  3. The schema we test against is the ORM (TenantBase.metadata.create_all),
     which is the source of truth for tenant-plane schema. GDX post-pave
     matches the ORM exactly.

Regenerate structure.sql via gdx_dispatch/tools/refresh_test_schema.sh whenever the
ORM changes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from gdx_dispatch.tests.fixtures import pg


def _column_type(session, schema: str, table: str, column: str) -> tuple[str, int | None]:
    row = session.execute(
        text(
            "SELECT data_type, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
        ),
        {"s": schema, "t": table, "c": column},
    ).first()
    assert row is not None, f"{schema}.{table}.{column} missing in test DB"
    return row[0], row[1]


def test_template_loaded_with_public_schema(pg_test_session):
    rows = pg_test_session.execute(
        text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    ).scalar()
    assert rows is not None and rows > 50, f"expected >50 public tables, got {rows}"


def test_technicians_id_matches_orm(pg_test_session):
    """technicians.id is varchar(36) per ORM — closes the C4-class drift."""
    dtype, length = _column_type(pg_test_session, "public", "technicians", "id")
    assert dtype == "character varying" and length == 36, (
        f"expected varchar(36), got {dtype}({length})"
    )


def test_technicians_company_id_matches_orm(pg_test_session):
    """technicians.company_id is varchar(36) per ORM (tenant_models.py:2004)."""
    dtype, length = _column_type(pg_test_session, "public", "technicians", "company_id")
    assert dtype == "character varying" and length == 36, (
        f"expected varchar(36), got {dtype}({length})"
    )


def test_jobs_assigned_to_joins_technicians_id(pg_test_session):
    """
    Regression guard for the an earlier session C4 bug — jobs.assigned_to (varchar)
    JOINed against technicians.id (uuid) threw 'operator does not exist'.
    With ORM-truth, both sides are varchar(36) and the join runs clean.
    """
    rows = pg_test_session.execute(
        text(
            "SELECT j.id FROM public.jobs j "
            "LEFT JOIN public.technicians t ON j.assigned_to = t.id "
            "LIMIT 1"
        )
    ).all()
    assert rows == []  # empty DB, but the JOIN must type-check


def test_per_test_isolation(pg_test_engine):
    """Two tests writing to the same table must not see each other's rows."""
    with pg_test_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public.technicians (id, company_id, name) "
                "VALUES (gen_random_uuid()::text, 'probe-tenant', 'isolation-probe')"
            )
        )
        n = conn.execute(
            text("SELECT count(*) FROM public.technicians WHERE name = 'isolation-probe'")
        ).scalar()
        assert n == 1


def test_per_test_isolation_pair(pg_test_engine):
    """If isolation works, this test sees zero rows from the previous one."""
    with pg_test_engine.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM public.technicians WHERE name = 'isolation-probe'")
        ).scalar()
        assert n == 0


# ---------------------------------------------------------------------------
# The skip-or-fail gate (#440). These two need no Postgres, so they run
# everywhere — including the laptops where every test above skips.
# ---------------------------------------------------------------------------


def test_an_unreachable_pg_fails_under_ci(monkeypatch):
    """CI provides a Postgres, so a skip there means broken wiring. For weeks
    every test above skipped green in CI against the wrong port (#440)."""
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="must run, not skip"):
        pg._skip_unless_ci("PostgreSQL not reachable")


def test_an_unreachable_pg_skips_on_a_laptop(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        pg._skip_unless_ci("PostgreSQL not reachable")


def _fixture_body(fixture):
    """The plain function under @pytest.fixture — pytest refuses a direct call."""
    body = getattr(fixture, "__wrapped__", None)
    if body is None:  # pytest < 8.4
        body = fixture.__pytest_wrapped__.obj
    return body


def test_the_real_template_fixture_fails_under_ci_when_postgres_is_unreachable(monkeypatch):
    """The two tests above call the helper directly, so they stay green if a call
    site goes back to `pytest.skip(`. This drives the REAL fixture body."""
    import psycopg2

    def _refuse(*a, **kw):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(pg, "_admin_conn", _refuse)
    monkeypatch.setenv("CI", "true")
    # Not `pytest.raises(pytest.fail.Exception)`: a reverted call site raises
    # pytest's Skipped, which escapes that and reports THIS test as skipped —
    # the very false green it exists to catch.
    try:
        _fixture_body(pg.pg_template_db)(request=None)
    except pytest.fail.Exception as exc:
        assert "must run, not skip" in str(exc)
    except pytest.skip.Exception as exc:
        pytest.fail(f"pg_template_db SKIPPED under CI instead of failing: {exc}")
    else:
        pytest.fail("pg_template_db neither failed nor skipped with Postgres unreachable")


def test_no_postgres_test_turns_an_unreachable_server_into_a_skip():
    """Class guard (#440): any test that meets an unreachable Postgres must go
    through `_skip_unless_ci`, never a bare `pytest.skip`, or CI reports the
    missing Postgres arm as green. Catches a reverted call site and a new file
    copying the old pattern alike."""
    import re
    from pathlib import Path

    tests_root = Path(__file__).resolve().parent
    bare = re.compile(r"pytest\.skip\(\s*f?[\"'][^\"']*not reachable", re.IGNORECASE)
    offenders = [
        f"{path.relative_to(tests_root)}:{text[: m.start()].count(chr(10)) + 1}"
        for path in sorted(tests_root.rglob("*.py"))
        for text in [path.read_text(encoding="utf-8", errors="replace")]
        for m in bare.finditer(text)
    ]
    assert offenders == [], f"route these through fixtures.pg._skip_unless_ci: {offenders}"
