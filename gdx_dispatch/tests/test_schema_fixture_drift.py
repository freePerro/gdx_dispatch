"""Meta-test: inline CREATE TABLE blocks in test fixtures must have the
same column set as the matching SQLAlchemy ORM model.

This catches the class of bug that ate 38 tests on 2026-04-15: ORM
was updated, migration enforced NOT NULL, but test fixtures using
raw `CREATE TABLE ...` weren't updated → tests failed with
"table X has no column named Y".

Per sprint SS-4d (plans/platform-sprints/SS-4d_test_fixture_orm_migration.md),
fixtures are migrating to `Base.metadata.create_all()` gradually. Until
that lands for each file, this test keeps the inline versions honest.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO / "gdx_dispatch/tests"
MODELS_FILE = REPO / "gdx_dispatch/models/tenant_models.py"

# Tables where ORM drift is intentional (e.g., a leaner test-only schema).
# Add with a comment citing why; review quarterly.
ALLOWLIST: dict[str, set[str]] = {
    # tablename -> columns allowed to be missing from the fixture
    # example: "audit_logs": {"tenant_id"},
}


def _extract_inline_creates(src: str) -> dict[str, set[str]]:
    """Return {tablename: {column_names}} from CREATE TABLE blocks in src."""
    out: dict[str, set[str]] = {}
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(\w+)\s*\(([^;]+?)\)\s*(?:;|\"\"\"|''')",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(src):
        table = m.group(1).lower()
        body = m.group(2)
        cols: set[str] = set()
        # Skip constraint lines (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK, CONSTRAINT)
        for raw in body.split(","):
            line = raw.strip()
            if not line:
                continue
            first = line.split()[0].upper()
            if first in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}:
                continue
            name = line.split()[0].strip().lower()
            if name and re.match(r"^[a-z_][a-z0-9_]*$", name):
                cols.add(name)
        if cols:
            # Merge when multiple files create the same table
            out.setdefault(table, set()).update(cols)
    return out


def _extract_orm_tables() -> dict[str, set[str]]:
    """Parse gdx_dispatch/models/tenant_models.py for `__tablename__` + `Mapped[...]` columns."""
    src = MODELS_FILE.read_text()
    out: dict[str, set[str]] = {}
    # Split into class bodies
    class_rx = re.compile(r"^class\s+\w+\(Base\):(.*?)(?=^class \w|\Z)", re.DOTALL | re.MULTILINE)
    tablename_rx = re.compile(r'__tablename__\s*=\s*["\'](\w+)["\']')
    col_rx = re.compile(r"^\s+(\w+)\s*:\s*Mapped\[", re.MULTILINE)
    for m in class_rx.finditer(src):
        body = m.group(1)
        tm = tablename_rx.search(body)
        if not tm:
            continue
        table = tm.group(1).lower()
        cols = {cm.group(1).lower() for cm in col_rx.finditer(body)}
        if cols:
            out[table] = cols
    return out


def _collect_fixture_tables() -> dict[str, set[str]]:
    """Union inline CREATE TABLE columns across all test files."""
    acc: dict[str, set[str]] = {}
    for f in TESTS_DIR.rglob("test_*.py"):
        if "__pycache__" in str(f):
            continue
        try:
            src = f.read_text()
        except OSError:
            continue
        if "CREATE TABLE" not in src:
            continue
        for table, cols in _extract_inline_creates(src).items():
            acc.setdefault(table, set()).update(cols)
    return acc


@pytest.mark.xfail(
    strict=False,
    reason="Expected until SS-4d (test fixture ORM migration) is complete. "
    "When SS-4d lands, flip strict=True to fail CI on any new drift.",
)
def test_inline_create_tables_match_orm() -> None:
    """For every table created inline in test fixtures, the column set
    should be a superset of (or equal to) the ORM's column set.

    Failure message lists every (table, missing_cols) pair so a dev
    can fix all of them in one pass.
    """
    orm = _extract_orm_tables()
    fixtures = _collect_fixture_tables()

    drifts: list[str] = []
    for table, orm_cols in orm.items():
        if table not in fixtures:
            continue  # table not created inline by any test; skip
        fixture_cols = fixtures[table]
        missing = orm_cols - fixture_cols - ALLOWLIST.get(table, set())
        if missing:
            drifts.append(f"  {table}: ORM has {sorted(missing)[:6]}")

    if drifts:
        pytest.fail(
            "Inline CREATE TABLE fixtures drifted from ORM (SS-4d). "
            "Add the missing columns to each test fixture's CREATE TABLE "
            "OR migrate that file to Base.metadata.create_all(engine).\n"
            + "\n".join(drifts[:20])
            + (f"\n  … and {len(drifts) - 20} more" if len(drifts) > 20 else "")
        )
