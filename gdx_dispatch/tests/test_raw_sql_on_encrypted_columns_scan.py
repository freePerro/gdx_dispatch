"""Unit + integration coverage for ``raw_sql_on_encrypted_columns_scan``.

The scan is the structural enforcement layer that keeps slices 2/3 of
the encryption rollout from regressing: it sources its watch list from
``pii.encryption_status()`` and flags any ``text("…")`` that mentions
both the table name and the column name.

Today (post-S122-1c) the watch list is empty so the scan is a no-op.
These tests pin the matcher logic against synthetic input so the scan
is provably correct the moment a future slice re-adds an EncryptedString
column.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from gdx_dispatch.tools import raw_sql_on_encrypted_columns_scan as scan_mod


def test_no_op_when_zero_columns():
    """The post-S122-1c reality: zero columns → scan returns empty list."""
    assert scan_mod.scan(encrypted=[]) == []


def test_token_regex_matches_word_boundaries():
    """``customers`` matches inside ``FROM customers`` but NOT inside
    ``FROM customers_archive``. False-positive guard."""
    rx = scan_mod._build_token_re("customers")
    assert rx.search("SELECT * FROM customers WHERE id = :id")
    assert rx.search("SELECT customers.name FROM customers")
    assert not rx.search("SELECT * FROM customers_archive")
    assert not rx.search("SELECT * FROM new_customers")


def test_finds_reader_form(tmp_path: Path):
    """The 2026-05-12 S122-1b root cause shape: a SELECT that names
    both the table and the encrypted column. Scan must catch it."""
    src = textwrap.dedent("""
        from sqlalchemy import text

        def list_customers(db):
            return db.execute(
                text("SELECT id, name, email FROM customers")
            ).fetchall()
    """)
    f = tmp_path / "reader.py"
    f.write_text(src)
    # Patch SCAN_ROOTS to point at the tmp_path
    scan_mod.SCAN_ROOTS = [tmp_path]
    findings = scan_mod.scan(encrypted=[("customers", "name")])
    assert len(findings) == 1
    path, lineno, table, column, excerpt = findings[0]
    assert path == f
    assert table == "customers"
    assert column == "name"
    assert "SELECT id, name, email FROM customers" in excerpt


def test_finds_writer_form(tmp_path: Path):
    """The auditor's S122-1c symmetric-bypass shape: an INSERT against
    the encrypted column via raw SQL bind."""
    src = textwrap.dedent("""
        from sqlalchemy import text

        def create_endpoint(db, payload):
            db.execute(
                text(
                    "INSERT INTO webhook_endpoints (id, url, secret, active) "
                    "VALUES (:id, :url, :secret, true)"
                ),
                {"id": payload.id, "url": payload.url, "secret": payload.secret, "active": True},
            )
    """)
    f = tmp_path / "writer.py"
    f.write_text(src)
    scan_mod.SCAN_ROOTS = [tmp_path]
    findings = scan_mod.scan(encrypted=[("webhook_endpoints", "secret")])
    assert len(findings) == 1
    path, lineno, table, column, _excerpt = findings[0]
    assert path == f
    assert (table, column) == ("webhook_endpoints", "secret")


def test_no_match_when_only_table_named(tmp_path: Path):
    """A SELECT that names only the table (not the column) must NOT
    flag — substring matching alone would over-fire."""
    src = textwrap.dedent("""
        from sqlalchemy import text
        db.execute(text("SELECT id FROM customers"))
    """)
    f = tmp_path / "ok.py"
    f.write_text(src)
    scan_mod.SCAN_ROOTS = [tmp_path]
    findings = scan_mod.scan(encrypted=[("customers", "name")])
    assert findings == []


def test_noqa_suppresses(tmp_path: Path):
    """An explicit ``# noqa: RAW_ENC`` annotation suppresses the finding.
    Use for legitimate raw-SQL touches (encryption tools, migrations
    already covered by SKIP_FILE_NAMES, etc.)."""
    src = textwrap.dedent("""
        from sqlalchemy import text
        db.execute(text("SELECT name FROM customers"))  # noqa: RAW_ENC
    """)
    f = tmp_path / "suppressed.py"
    f.write_text(src)
    scan_mod.SCAN_ROOTS = [tmp_path]
    findings = scan_mod.scan(encrypted=[("customers", "name")])
    assert findings == []


def test_skips_known_tool_files(tmp_path: Path):
    """The re-encrypt tools legitimately touch raw bytes — the scan
    excludes them by filename so they don't self-flag."""
    src = textwrap.dedent("""
        from sqlalchemy import text
        db.execute(text("UPDATE customers SET name = :n"))
    """)
    # Use one of the documented SKIP_FILE_NAMES.
    f = tmp_path / "encrypt_customer_pii_rows.py"
    f.write_text(src)
    scan_mod.SCAN_ROOTS = [tmp_path]
    findings = scan_mod.scan(encrypted=[("customers", "name")])
    assert findings == []


def test_baseline_round_trip(tmp_path: Path, monkeypatch):
    """Baseline write → read → net-new diff."""
    src = textwrap.dedent("""
        from sqlalchemy import text
        db.execute(text("UPDATE customers SET name = :n"))
    """)
    f = tmp_path / "leftover.py"
    f.write_text(src)
    scan_mod.SCAN_ROOTS = [tmp_path]
    monkeypatch.setattr(scan_mod, "BASELINE_FILE", tmp_path / ".baseline")
    monkeypatch.setattr(scan_mod, "REPO_ROOT", tmp_path)

    findings = scan_mod.scan(encrypted=[("customers", "name")])
    assert len(findings) == 1

    # Write baseline. Re-run scan — should produce same finding,
    # net-new should be empty.
    scan_mod._write_baseline(findings)
    baseline = scan_mod._load_baseline()
    assert len(baseline) == 1

    new_findings = scan_mod._net_new(findings, baseline)
    assert new_findings == []
