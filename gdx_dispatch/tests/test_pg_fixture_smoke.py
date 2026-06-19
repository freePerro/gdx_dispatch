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

from sqlalchemy import text


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
