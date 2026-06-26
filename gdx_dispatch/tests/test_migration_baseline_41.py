"""#41 — the create_all-before-alembic fix relies on one invariant: the squashed
baseline (control-plane) and the ORM-managed (tenant-plane) table sets are
DISJOINT. If a future model's __tablename__ collides with a baseline table,
create_all-first would build it and then migration 001's CREATE TABLE would
fail. This locks the invariant so that regression is caught at test time, not on
a fresh-DB boot.
"""
from __future__ import annotations

import re
from pathlib import Path

import gdx_dispatch.models  # noqa: F401 — register every model on the metadata
from gdx_dispatch.core.audit import TenantBase

_BASELINE_SQL = (
    Path(gdx_dispatch.models.__file__).resolve().parent.parent
    / "migrations" / "baseline_squashed.sql"
)


def _baseline_tables() -> set[str]:
    sql = _BASELINE_SQL.read_text()
    names = re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?\"?([a-z_]+)\"?", sql, re.I)
    return {n.lower() for n in names}


def test_baseline_and_orm_tables_are_disjoint():
    baseline = _baseline_tables()
    assert baseline, "expected to parse some CREATE TABLE from baseline_squashed.sql"
    orm = set(TenantBase.metadata.tables)
    overlap = baseline & orm
    assert not overlap, (
        f"baseline (control-plane) and ORM (tenant-plane) tables overlap: {sorted(overlap)} — "
        "create_all-before-alembic (#41) would collide with migration 001's CREATE TABLE"
    )


def test_create_orm_tables_helper_exists():
    # The entrypoint calls this before alembic; keep it importable.
    from gdx_dispatch.tools.bootstrap_app import create_orm_tables
    assert callable(create_orm_tables)
