"""D35 tests — router raw-SQL vs live-tenant-DB gate.

Covers the an earlier session appointments.tech_id trap (canonical-but-empty), the
column-missing case, and the extractor's SQL-vs-docstring discrimination.

The live DB piece uses a fake population_fn so these tests run without any
Postgres. The extractor piece uses temp files with synthetic router code.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from gdx_dispatch.tools.router_sql_live_audit import (
    ColRef,
    audit,
    scan_file,
)


def _write_router(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body).lstrip("\n"))
    return p


# ── extractor tests ─────────────────────────────────────────────────────────

def test_extracts_where_column_with_table_from_same_block(tmp_path):
    """Classic an earlier session shape: FROM + WHERE col = :param in one block."""
    p = _write_router(tmp_path, "mobile.py", '''
        def handler(db):
            db.execute(text("""
                SELECT id
                FROM appointments
                WHERE tech_id = :tech_id
                  AND deleted_at IS NULL
            """))
    ''')
    refs = scan_file(p)
    tech = [r for r in refs if r.column == "tech_id"]
    assert len(tech) == 1, f"expected 1 tech_id ref, got {refs}"
    assert "appointments" in tech[0].table_hints


def test_extracts_insert_column_list(tmp_path):
    p = _write_router(tmp_path, "router.py", '''
        def create(db):
            db.execute(text("""
                INSERT INTO job_photos (id, job_id, kind, url, uploaded_at)
                VALUES (:id, :job_id, :kind, :url, :ts)
            """))
    ''')
    refs = scan_file(p)
    cols = {r.column for r in refs}
    assert "kind" in cols and "url" in cols
    assert all("job_photos" in r.table_hints for r in refs)


def test_extracts_update_set_column(tmp_path):
    p = _write_router(tmp_path, "router.py", '''
        def touch(db):
            db.execute(text("UPDATE jobs SET started_at = :now WHERE id = :id"))
    ''')
    refs = scan_file(p)
    cols = {r.column for r in refs}
    assert "started_at" in cols
    assert any("jobs" in r.table_hints for r in refs)


def test_ignores_plain_docstring_with_sql_words(tmp_path):
    """Docstrings that mention `WHERE` or `UPDATE` but aren't SQL must not match."""
    p = _write_router(tmp_path, "router.py", '''
        def note():
            """
            Update the customer record. We determine WHERE to put it
            based on geography. This is documentation, not SQL.
            """
            pass
    ''')
    refs = scan_file(p)
    assert refs == [], f"docstring should not match as SQL: {refs}"


def test_ignores_information_schema_meta_queries(tmp_path):
    p = _write_router(tmp_path, "router.py", '''
        def check(db):
            db.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'customers'
            """))
    ''')
    refs = scan_file(p)
    assert refs == [], f"info_schema meta-query should be skipped: {refs}"


def test_concatenated_info_schema_strings(tmp_path):
    """Two adjacent literals still skip via the info_schema column allowlist."""
    p = _write_router(tmp_path, "router.py", '''
        def check(db):
            db.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ))
    ''')
    refs = scan_file(p)
    leaked = {r.column for r in refs} & {"column_name", "table_name"}
    assert not leaked, f"info_schema col names leaked through: {leaked}"


def test_allowlisted_columns_are_skipped(tmp_path):
    """deleted_at is always expected to be sparse — don't flag it."""
    p = _write_router(tmp_path, "router.py", '''
        def fetch(db):
            db.execute(text("SELECT id FROM jobs WHERE deleted_at IS NULL"))
    ''')
    refs = scan_file(p)
    cols = {r.column for r in refs}
    assert "deleted_at" not in cols


# ── audit() tests: the core logic ──────────────────────────────────────────

def _ref(column: str, table: str = "", file: str = "r.py", line: int = 1) -> ColRef:
    return ColRef(file=file, line=line, column=column,
                  table_hints={table} if table else set())


def test_canonical_but_empty_is_flagged():
    """The an earlier session trap: column exists, table has rows, column populated in zero."""
    refs = [_ref("tech_id", "appointments")]
    column_index = {"tech_id": {"appointments"}, "technician_id": {"appointments"}}

    def pop(table, col):
        if (table, col) == ("appointments", "tech_id"):
            return (7, 0)  # 7 rows total, 0 populated with tech_id
        return (7, 7)

    violations = audit(refs, column_index, pop)
    assert len(violations) == 1
    v = violations[0]
    assert v.kind == "canonical_but_empty"
    assert v.table == "appointments"
    assert v.column == "tech_id"
    assert "0 rows" in v.detail


def test_fully_populated_column_is_clean():
    refs = [_ref("tech_id", "appointments")]
    column_index = {"tech_id": {"appointments"}}

    def pop(table, col):
        return (7, 7)  # every row has tech_id populated

    assert audit(refs, column_index, pop) == []


def test_empty_table_is_clean_not_flagged():
    """Zero rows total: not a canonical-but-empty case; don't cry wolf."""
    refs = [_ref("tech_id", "appointments")]
    column_index = {"tech_id": {"appointments"}}

    def pop(table, col):
        return (0, 0)

    assert audit(refs, column_index, pop) == []


def test_column_missing_from_every_table():
    """Router references a column that doesn't exist on the live DB."""
    refs = [_ref("widget_id", "jobs")]
    column_index = {"id": {"jobs"}, "status": {"jobs"}}  # no widget_id

    def pop(table, col):
        raise AssertionError("should not be called — column doesn't exist")

    violations = audit(refs, column_index, pop)
    assert len(violations) == 1
    assert violations[0].kind == "column_missing"
    assert violations[0].column == "widget_id"


def test_hint_narrows_to_single_table_when_multi_table_column():
    """If hint matches one live table, audit that; don't fan out to all tables."""
    refs = [_ref("created_at", "jobs")]
    column_index = {
        "created_at": {"jobs", "customers", "invoices"},
    }
    calls: list[tuple[str, str]] = []

    def pop(table, col):
        calls.append((table, col))
        return (5, 5)  # clean everywhere

    audit(refs, column_index, pop)
    assert calls == [("jobs", "created_at")], \
        f"hint should narrow to jobs only, not {calls}"


def test_hint_miss_falls_back_to_all_live_tables():
    """If hint doesn't match any live table, check every table with the column."""
    refs = [_ref("status", "nonexistent_table")]
    column_index = {"status": {"jobs", "invoices"}}

    calls: set[tuple[str, str]] = set()

    def pop(table, col):
        calls.add((table, col))
        return (5, 5)

    audit(refs, column_index, pop)
    assert calls == {("jobs", "status"), ("invoices", "status")}


def test_control_db_columns_suppress_column_missing():
    """Columns that live on the control DB (not tenant DB) are not drift —
    the router is querying platform, not a tenant table."""
    refs = [_ref("provider_type", "identity_providers")]
    column_index: dict[str, set[str]] = {}  # not in tenant DB
    control_columns = {"provider_type", "identity_providers"}  # is in control DB

    def pop(table, col):
        raise AssertionError("should not be called")

    violations = audit(refs, column_index, pop, control_columns=control_columns)
    assert violations == [], \
        f"control-DB column should not be flagged as missing, got {violations}"


def test_column_truly_missing_still_flags_even_with_control_columns():
    refs = [_ref("widget_id", "jobs")]
    column_index: dict[str, set[str]] = {"id": {"jobs"}}
    control_columns = {"tenant_id", "identity_id"}  # unrelated

    violations = audit(refs, column_index, lambda *_: (0, 0),
                       control_columns=control_columns)
    assert len(violations) == 1
    assert violations[0].kind == "column_missing"


def test_population_query_failure_becomes_column_missing():
    """If the live-DB query explodes for some reason, report it as a violation."""
    refs = [_ref("flag", "weird_table")]
    column_index = {"flag": {"weird_table"}}

    def pop(table, col):
        raise RuntimeError("cursor went boom")

    violations = audit(refs, column_index, pop)
    assert len(violations) == 1
    assert violations[0].kind == "column_missing"
    assert "cursor went boom" in violations[0].detail


# ── end-to-end: scan file + audit ──────────────────────────────────────────

def test_session_13_repro_catches_tech_id_trap(tmp_path):
    """Full pipeline: scan a Session-13-shaped router, audit against a
    fake live DB where appointments.tech_id is canonical-but-empty."""
    p = _write_router(tmp_path, "mobile.py", '''
        def list_for_tech(db, tenant_id, technician_id):
            return db.execute(text("""
                SELECT id, job_id
                FROM appointments
                WHERE company_id = :tenant_id
                  AND tech_id = :technician_id
                  AND deleted_at IS NULL
            """), {"tenant_id": tenant_id, "technician_id": technician_id}).all()
    ''')
    refs = scan_file(p)

    # Live DB mirror of an earlier session production state: tech_id exists on
    # appointments with 7 rows total, zero populated. technician_id has
    # the real data. company_id is fine.
    column_index = {
        "tech_id": {"appointments"},
        "technician_id": {"appointments"},
        "company_id": {"appointments"},
        "job_id": {"appointments"},
        "id": {"appointments"},
    }

    def pop(table, col):
        if (table, col) == ("appointments", "tech_id"):
            return (7, 0)
        return (7, 7)

    violations = audit(refs, column_index, pop)
    trap = [v for v in violations if v.column == "tech_id"]
    assert len(trap) == 1, \
        f"must catch the tech_id canonical-but-empty trap, got {violations}"
    assert trap[0].kind == "canonical_but_empty"
    assert trap[0].table == "appointments"
