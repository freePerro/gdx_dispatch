"""D44 tests — test-residue sweeper with classification.

Covers the three categories:
  1. deletable_residue — row matches a RESIDUE_PATTERNS entry, safe to DELETE
  2. unattributed — no tenant scope but no residue match, requires BACKFILL
  3. transitive-scoped — skipped entirely via TRANSITIVE_SCOPED_TABLES

Also covers the --delete safety rails: only residue is ever deleted,
unattributed rows survive even in --delete --confirm-delete mode.
"""
from __future__ import annotations

import json
import re
import sys

import pytest
from sqlalchemy import create_engine, text

from gdx_dispatch.tools.test_residue_sweep import (
    Finding,
    TenantSweep,
    build_orphan_clause,
    classify_table,
    delete_residue_in_transaction,
    main,
    snapshot_before_delete,
)


def _mkengine():
    return create_engine("sqlite:///:memory:", future=True)


def _seed(engine, ddl: str, rows: list[dict] | None = None, insert_sql: str | None = None):
    with engine.begin() as conn:
        conn.execute(text(ddl))
        if rows and insert_sql:
            for r in rows:
                conn.execute(text(insert_sql), r)


# ── build_orphan_clause (unchanged) ────────────────────────────────────────

def test_build_orphan_clause_both_columns():
    assert build_orphan_clause({"company_id", "tenant_id"}) == \
        ("company_id IS NULL AND tenant_id IS NULL", "both")


def test_build_orphan_clause_company_only():
    assert build_orphan_clause({"company_id"}) == \
        ("company_id IS NULL", "company_id")


def test_build_orphan_clause_tenant_only():
    assert build_orphan_clause({"tenant_id"}) == \
        ("tenant_id IS NULL", "tenant_id")


def test_build_orphan_clause_neither_is_skip():
    assert build_orphan_clause(set()) is None
    assert build_orphan_clause({"some_other_col"}) is None


# ── classify_table — transitive-scoped skip ────────────────────────────────

def test_classify_skips_transitive_scoped_tables():
    """estimate_lines / invoice_lines are scoped via parent FK — don't report."""
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE estimate_lines (id INTEGER, company_id TEXT)",
          [{"id": 1, "company_id": None}],
          "INSERT INTO estimate_lines VALUES (:id, :company_id)")
    result = classify_table(eng, text, "estimate_lines", {"company_id"})
    assert result is None, "transitive-scoped table should not be reported"


# ── classify_table — pattern matching ──────────────────────────────────────

def test_classify_users_table_separates_residue_from_unattributed():
    """Only rows matching the users RESIDUE_PATTERNS are deletable.
    Other NULL-company_id users (real accounts) are unattributed."""
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)",
          [
              # deletable residue matches
              {"id": 1, "email": "qa-test@example.com", "company_id": None},
              {"id": 2, "email": "someone@example.com", "company_id": None},
              # unattributed — real email, no scope
              {"id": 3, "email": "real@user.com", "company_id": None},
              # scoped — shouldn't count
              {"id": 4, "email": "owner@example.com", "company_id": "gdx"},
          ],
          "INSERT INTO users VALUES (:id, :email, :company_id)")
    # SQLite's regex operator requires a plugin; patch sqlalchemy text(...)
    # behavior by switching to LIKE-equivalent patterns is complex. Simpler:
    # use Postgres-compatible regex via SQLite's REGEXP if available.
    # Workaround: monkey-check by calling classify logic directly and
    # trust the integration behavior against real PG (tested live).
    # For now, test with a seeded engine that supports REGEXP via sqlalchemy
    # DB session-level function. Register one.

    def _regexp(pattern, value):
        if value is None:
            return False
        return re.search(pattern, value) is not None

    raw = eng.raw_connection()
    try:
        raw.create_function("REGEXP", 2, _regexp)
        # sqlalchemy's ~ operator maps to REGEXP on SQLite when the function
        # is registered.
        result = classify_table(eng, text, "users", {"company_id"})
    finally:
        raw.close()
    assert result is not None
    # 3 unscoped total; 2 residue (qa-test@ and someone@example.com); 1 unattributed
    assert result.deletable_residue_count == 2, \
        f"expected 2 residue, got {result}"
    assert result.unattributed_count == 1
    assert result.null_column == "company_id"


def test_classify_table_without_patterns_all_unattributed():
    """Tables not in RESIDUE_PATTERNS have zero deletable residue by design."""
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE audit_logs (id INTEGER, tenant_id TEXT)",
          [
              {"id": 1, "tenant_id": None},
              {"id": 2, "tenant_id": None},
              {"id": 3, "tenant_id": "gdx"},
          ],
          "INSERT INTO audit_logs VALUES (:id, :tenant_id)")
    result = classify_table(eng, text, "audit_logs", {"tenant_id"})
    assert result is not None
    assert result.deletable_residue_count == 0
    assert result.unattributed_count == 2
    assert result.null_column == "tenant_id"


def test_classify_returns_none_on_empty_table():
    eng = _mkengine()
    _seed(eng, "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)")
    assert classify_table(eng, text, "users", {"company_id"}) is None


def test_classify_returns_none_when_all_scoped():
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)",
          [{"id": 1, "email": "x@y.com", "company_id": "gdx"}],
          "INSERT INTO users VALUES (:id, :email, :company_id)")
    assert classify_table(eng, text, "users", {"company_id"}) is None


# ── delete_residue_in_transaction — only deletes residue ──────────────────

def test_delete_only_removes_residue_never_unattributed():
    """THE critical safety test: --delete must NEVER touch unattributed rows."""
    import re as _re
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)",
          [
              {"id": 1, "email": "qa-test@example.com", "company_id": None},
              {"id": 2, "email": "real@user.com", "company_id": None},
          ],
          "INSERT INTO users VALUES (:id, :email, :company_id)")

    raw = eng.raw_connection()
    try:
        raw.create_function("REGEXP", 2,
                            lambda p, v: v is not None and _re.search(p, v) is not None)
        findings = [Finding(
            table="users",
            deletable_residue_count=1,
            unattributed_count=1,
            null_column="company_id",
        )]
        deleted = delete_residue_in_transaction(eng, text, findings)
    finally:
        raw.close()

    assert deleted == 1
    with eng.connect() as conn:
        remaining = conn.execute(text("SELECT id, email FROM users ORDER BY id")).fetchall()
        assert len(remaining) == 1
        # Real user must survive
        assert remaining[0][1] == "real@user.com"


def test_delete_rolls_back_on_rowcount_mismatch():
    """Pre-scan said 2 residue matches; DELETE only finds 1 → rollback."""
    import re as _re
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)",
          [
              {"id": 1, "email": "qa-test@example.com", "company_id": None},
              # only one residue row; scan claimed 2
          ],
          "INSERT INTO users VALUES (:id, :email, :company_id)")
    raw = eng.raw_connection()
    try:
        raw.create_function("REGEXP", 2,
                            lambda p, v: v is not None and _re.search(p, v) is not None)
        findings = [Finding(
            table="users",
            deletable_residue_count=2,  # lie: actually 1
            unattributed_count=0,
            null_column="company_id",
        )]
        with pytest.raises(RuntimeError, match="expected 2"):
            delete_residue_in_transaction(eng, text, findings)
    finally:
        raw.close()
    # Rollback: the 1 real residue row should still be there
    with eng.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        assert count == 1


def test_delete_skips_tables_without_residue_patterns():
    """A table with no RESIDUE_PATTERNS entry cannot have deletable residue.
    If deletable_residue_count is ever >0 for such a table (shouldn't happen),
    the guard must prevent the DELETE from running."""
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE audit_logs (id INTEGER, tenant_id TEXT)",
          [{"id": 1, "tenant_id": None}],
          "INSERT INTO audit_logs VALUES (:id, :tenant_id)")
    findings = [Finding(
        table="audit_logs",
        deletable_residue_count=1,  # forcibly positive to test the guard
        unattributed_count=0,
        null_column="tenant_id",
    )]
    deleted = delete_residue_in_transaction(eng, text, findings)
    assert deleted == 0  # guard should skip
    with eng.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
        assert count == 1


def test_delete_skips_synthetic_error_entries():
    eng = _mkengine()
    _seed(eng, "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)")
    findings = [Finding(
        table="missing_table",
        deletable_residue_count=-1,
        unattributed_count=-1,
        null_column="query_error: OperationalError",
    )]
    assert delete_residue_in_transaction(eng, text, findings) == 0


# ── --delete safety rail ───────────────────────────────────────────────────

def test_delete_without_confirm_exits_3(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["test_residue_sweep.py", "--delete"])
    rc = main()
    assert rc == 3
    captured = capsys.readouterr()
    assert "--confirm-delete" in captured.err


# ── Snapshot behavior ─────────────────────────────────────────────────────

def test_snapshot_captures_deletable_rows_as_json(tmp_path, monkeypatch):
    """Snapshot should write the pre-DELETE row contents to a JSON file,
    usable to reconstruct the rows via re-INSERT if recovery is needed."""
    import re as _re
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)",
          [
              {"id": 1, "email": "qa-test@example.com", "company_id": None},
              {"id": 2, "email": "real@user.com", "company_id": None},
          ],
          "INSERT INTO users VALUES (:id, :email, :company_id)")
    raw = eng.raw_connection()
    monkeypatch.setattr("gdx_dispatch.tools.test_residue_sweep.SNAPSHOT_DIR", tmp_path)
    try:
        raw.create_function("REGEXP", 2,
                            lambda p, v: v is not None and _re.search(p, v) is not None)
        findings = [Finding(
            table="users",
            deletable_residue_count=1,
            unattributed_count=1,
            null_column="company_id",
        )]
        snap_path = snapshot_before_delete(eng, text, "gdx", findings)
    finally:
        raw.close()
    data = json.loads(open(snap_path).read())
    assert data["tenant_slug"] == "gdx"
    assert "users" in data["tables"]
    captured = data["tables"]["users"]
    assert len(captured) == 1
    assert captured[0]["email"] == "qa-test@example.com"
    # Real user's row must NOT be in the snapshot (wasn't going to be deleted)
    emails = [r["email"] for r in captured]
    assert "real@user.com" not in emails


def test_snapshot_write_failure_raises_runtime_error(monkeypatch):
    """If SNAPSHOT_DIR isn't writable, snapshot must raise so --delete aborts."""
    # Point to a path we can't create (e.g., a non-existent device)
    monkeypatch.setattr(
        "gdx_dispatch.tools.test_residue_sweep.SNAPSHOT_DIR",
        type("FakeDir", (), {
            "mkdir": lambda self, **kw: (_ for _ in ()).throw(
                PermissionError("no perms")),
            "__truediv__": lambda self, other: self,
            "open": lambda self, mode: (_ for _ in ()).throw(OSError("no")),
        })()
    )
    eng = _mkengine()
    _seed(eng, "CREATE TABLE users (id INTEGER, email TEXT, company_id TEXT)")
    findings = [Finding("users", deletable_residue_count=1,
                        unattributed_count=0, null_column="company_id")]
    with pytest.raises(RuntimeError, match="not writable"):
        snapshot_before_delete(eng, text, "gdx", findings)


# ── TenantSweep aggregate props ────────────────────────────────────────────

def test_total_deletable_and_total_unattributed_aggregate():
    sweep = TenantSweep(slug="gdx", tenant_id="t", findings=[
        Finding("users", deletable_residue_count=1, unattributed_count=0, null_column="company_id"),
        Finding("audit_logs", deletable_residue_count=0, unattributed_count=296, null_column="tenant_id"),
        Finding("broken", deletable_residue_count=-1, unattributed_count=-1, null_column="query_error: X"),
    ])
    assert sweep.total_deletable == 1
    assert sweep.total_unattributed == 296  # excludes synthetic -1 entries


# ── KNOWN_LEGACY_ROWS acknowledgment ───────────────────────────────────────

def test_acknowledged_legacy_narrows_by_date_range(monkeypatch):
    """Rows inside the acknowledged date range count as legacy, not unattributed.
    Rows OUTSIDE the range remain unattributed drift (catches new occurrences)."""
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE audit_logs (id INTEGER, tenant_id TEXT, created_at TEXT)",
          [
              # Inside acknowledged range — should count as legacy
              {"id": 1, "tenant_id": None, "created_at": "2026-04-08T10:00:00+00:00"},
              {"id": 2, "tenant_id": None, "created_at": "2026-04-08T23:00:00+00:00"},
              # Outside range — should count as unattributed drift
              {"id": 3, "tenant_id": None, "created_at": "2026-04-15T08:00:00+00:00"},
              # Has scope — should not count at all
              {"id": 4, "tenant_id": "gdx", "created_at": "2026-04-08T12:00:00+00:00"},
          ],
          "INSERT INTO audit_logs VALUES (:id, :tenant_id, :created_at)")

    # Inject a test acknowledgment (narrow to 2026-04-08)
    monkeypatch.setitem(
        __import__("gdx_dispatch.tools.test_residue_sweep", fromlist=["KNOWN_LEGACY_ROWS"]).KNOWN_LEGACY_ROWS,
        ("testslug", "audit_logs", "tenant_id"),
        {
            "date_range_utc": ("2026-04-08T00:00:00+00:00",
                               "2026-04-08T23:59:59.999+00:00"),
            "reason": "test",
            "acknowledged_at": "2026-04-17",
            "filed_by": "test",
        },
    )

    result = classify_table(eng, text, "audit_logs", {"tenant_id"},
                            tenant_slug="testslug")
    assert result is not None
    assert result.acknowledged_legacy_count == 2  # 2026-04-08 rows
    assert result.unattributed_count == 1         # 2026-04-15 row
    assert result.deletable_residue_count == 0


def test_no_legacy_entry_means_acknowledged_is_zero():
    """A table without a KNOWN_LEGACY_ROWS entry gets acknowledged_legacy=0
    — the caller's tenant_slug doesn't match any registered entry."""
    eng = _mkengine()
    _seed(eng,
          "CREATE TABLE audit_logs (id INTEGER, tenant_id TEXT, created_at TEXT)",
          [{"id": 1, "tenant_id": None, "created_at": "2026-04-08T10:00:00+00:00"}],
          "INSERT INTO audit_logs VALUES (:id, :tenant_id, :created_at)")
    result = classify_table(eng, text, "audit_logs", {"tenant_id"},
                            tenant_slug="unregistered-tenant")
    assert result is not None
    assert result.acknowledged_legacy_count == 0
    assert result.unattributed_count == 1


def test_finding_total_unscoped_sums_all_three_buckets():
    f = Finding(
        table="x",
        deletable_residue_count=2,
        unattributed_count=3,
        null_column="company_id",
        acknowledged_legacy_count=5,
    )
    assert f.total_unscoped == 10


def test_sweep_acknowledged_legacy_alone_does_not_fail_gate(monkeypatch, capsys, tmp_path):
    """If the only findings are acknowledged_legacy, main() should exit 0
    (clean). Acknowledged rows are documented, not actionable."""
    # Set up a sweep with only acknowledged-legacy findings
    import gdx_dispatch.tools.test_residue_sweep as mod

    def fake_discover(slug_filter):
        return [("tid-1", "testslug", "plaintext-url")]

    def fake_scan(tid, slug, db_url_enc):
        return TenantSweep(
            slug=slug, tenant_id=tid,
            findings=[Finding(
                table="audit_logs", deletable_residue_count=0,
                unattributed_count=0, null_column="tenant_id",
                acknowledged_legacy_count=296,
            )],
        )

    monkeypatch.setattr(mod, "discover_tenants", fake_discover)
    monkeypatch.setattr(mod, "scan_tenant", fake_scan)
    monkeypatch.setattr(mod, "REPORT_PATH", tmp_path / "report.json")
    monkeypatch.setattr(sys, "argv", ["test_residue_sweep.py", "--tenant", "testslug"])
    rc = mod.main()
    assert rc == 0, "acknowledged_legacy alone is not an actionable finding"
    captured = capsys.readouterr()
    assert "acknowledged_legacy=296" in captured.out
