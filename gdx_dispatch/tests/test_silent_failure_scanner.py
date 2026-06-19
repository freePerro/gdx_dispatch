"""Unit tests for silent_failure_scanner — the meta-detector.

These tests are themselves the contract: seed known-bad code, assert the
scanner catches it. Shape-by-shape.
"""
from __future__ import annotations

import ast
import textwrap

from gdx_dispatch.tools.silent_failure_scanner import (
    Shape1Visitor,
    Shape2Visitor,
    Shape3Visitor,
    scan_file,
)


def _scan(visitor_cls, src: str, file: str = "test.py"):
    tree = ast.parse(textwrap.dedent(src))
    v = visitor_cls(file)
    v.visit(tree)
    return v.findings


# ── Shape 1 ────────────────────────────────────────────────────────────────

def test_shape1_catches_log_and_return_empty_dict():
    findings = _scan(Shape1Visitor, """
        def fetch_config():
            try:
                return db.execute("SELECT * FROM cfg").fetchone()
            except Exception as e:
                log.exception("fetch_config failed: %s", e)
                return {}
    """)
    assert len(findings) == 1
    assert findings[0].shape == 1
    assert findings[0].rule == "except_logs_and_returns_empty"


def test_shape1_catches_log_and_return_empty_list():
    findings = _scan(Shape1Visitor, """
        def list_users():
            try:
                return db.execute("SELECT * FROM users").fetchall()
            except Exception as e:
                log.exception("list_users failed")
                return []
    """)
    assert len(findings) == 1


def test_shape1_catches_log_and_return_none():
    findings = _scan(Shape1Visitor, """
        def get_one():
            try:
                return db.execute("SELECT 1").scalar()
            except Exception:
                logger.error("get_one failed")
                return None
    """)
    assert len(findings) == 1


def test_shape1_passes_when_raises():
    """If the except body raises, it's not silent."""
    findings = _scan(Shape1Visitor, """
        def fetch():
            try:
                return db.execute("SELECT *").fetchone()
            except Exception as e:
                log.exception("failed")
                raise
    """)
    assert findings == []


def test_shape1_passes_when_no_log():
    """Bare except: pass is a different pattern (drift_scanner catches it).
    This scanner specifically finds the log-AND-hide pattern."""
    findings = _scan(Shape1Visitor, """
        def fetch():
            try:
                return expensive()
            except Exception:
                pass
    """)
    assert findings == []


def test_shape1_passes_when_raises_after_log():
    findings = _scan(Shape1Visitor, """
        def fetch():
            try:
                return expensive()
            except Exception as e:
                log.exception("failed")
                raise RuntimeError("wrapped") from e
    """)
    assert findings == []


# ── Shape 2 ────────────────────────────────────────────────────────────────

def test_shape2_catches_empty_dialect_branch():
    """The D45 class: function branches on dialect but leaves one empty."""
    findings = _scan(Shape2Visitor, """
        def ensure_audit_table(db):
            dialect = db.bind.dialect.name
            if dialect == "sqlite":
                db.execute("CREATE TRIGGER ...")
            elif dialect == "postgresql":
                pass  # TODO: implement
    """)
    assert len(findings) == 1
    assert findings[0].shape == 2
    assert findings[0].rule == "dialect_branch_not_implemented"


def test_shape2_catches_comment_only_branch():
    findings = _scan(Shape2Visitor, """
        def guard_writes(db):
            dialect = db.bind.dialect.name
            if dialect == "sqlite":
                db.execute("CREATE TRIGGER ...")
            elif dialect == "postgresql":
                "TODO: plpgsql trigger here"
    """)
    assert len(findings) == 1


def test_shape2_passes_when_both_branches_implemented():
    findings = _scan(Shape2Visitor, """
        def ensure_audit_table(db):
            dialect = db.bind.dialect.name
            if dialect == "sqlite":
                db.execute("CREATE TRIGGER sqlite_trigger ...")
            elif dialect == "postgresql":
                db.execute("CREATE TRIGGER pg_trigger ...")
    """)
    assert findings == []


def test_shape2_only_scans_ensure_guard_verify_functions():
    """Non-ensure/guard functions are out of scope."""
    findings = _scan(Shape2Visitor, """
        def helper(db):
            dialect = db.bind.dialect.name
            if dialect == "sqlite":
                return 1
            elif dialect == "postgresql":
                pass
    """)
    assert findings == []


# ── Shape 3 ────────────────────────────────────────────────────────────────

def test_shape3_catches_get_in_dispatch_fstring():
    """The health_monitor bug: f-string renders None when dict.get misses."""
    findings = _scan(Shape3Visitor, """
        alert = {
            "signal": f"DISPATCH SWEEP: {r.get('count')} bugs — sprint {r.get('sprint_file')} — run `{r.get('audit_cmd')}`",
        }
    """)
    assert len(findings) >= 1
    assert findings[0].shape == 3


def test_shape3_passes_when_no_operator_keyword():
    """f-strings without DISPATCH/ALERT/etc are not flagged — too noisy."""
    findings = _scan(Shape3Visitor, """
        x = f"row {row.get('id')}: {row.get('name')}"
    """)
    assert findings == []


def test_shape3_passes_when_get_has_default():
    """dict.get with a default value isn't an unguarded access."""
    findings = _scan(Shape3Visitor, """
        alert = {
            "signal": f"DISPATCH SWEEP: {r.get('count', 0)} bugs — sprint {r.get('sprint_file', '<unknown>')}",
        }
    """)
    # Should pass — both .get() calls have defaults
    assert findings == []


# ── noqa: silent-failure marker ──────────────────────────────────────────

def test_noqa_marker_suppresses_finding(tmp_path):
    """# noqa: silent-failure on or adjacent to an except line drops the
    finding. Lets operators acknowledge intentional utility patterns
    (pure parsers, health probes) without rewriting the code."""
    f = tmp_path / "example.py"
    f.write_text(textwrap.dedent("""
        def parse_timestamp(v):
            try:
                return float(v)
            except ValueError as e:  # noqa: silent-failure
                log.debug("not a timestamp: %s", e)
                return None
    """).lstrip("\n"))
    findings = scan_file(f, {1})
    assert findings == [], f"noqa should suppress, got {findings}"


def test_noqa_marker_different_line_ignored(tmp_path):
    """The marker only suppresses findings on the same line (± 1). A
    marker elsewhere in the file does not grant blanket suppression."""
    f = tmp_path / "example.py"
    f.write_text(textwrap.dedent("""
        # noqa: silent-failure   <-- not on the except line, shouldn't count
        def parse_timestamp(v):
            try:
                return float(v)
            except ValueError as e:
                log.debug("not a timestamp: %s", e)
                return None
    """).lstrip("\n"))
    findings = scan_file(f, {1})
    assert len(findings) == 1, \
        "marker must be adjacent to the except line to suppress"


def test_noqa_marker_case_insensitive(tmp_path):
    f = tmp_path / "example.py"
    f.write_text(textwrap.dedent("""
        def parse(v):
            try:
                return int(v)
            except ValueError as e:  # NOQA: Silent-Failure
                log.debug("%s", e)
                return None
    """).lstrip("\n"))
    findings = scan_file(f, {1})
    assert findings == []
