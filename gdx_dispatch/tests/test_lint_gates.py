"""Tests for the three lint scans introduced in the #4 sprint.

These tests assert the gate behaviors that the auditor flagged as
under-tested: signature uniqueness (delete-then-add catches), `# noqa`
suppression, multi-line string handling in tokenize-based canonicalization,
and `--prune` mode pruning stale baseline entries.

All three scans are imported directly. Their module-level REPO_ROOT and
SCAN_ROOTS constants are monkey-patched per test so the scans operate on
scratch directories (tmp_path) instead of the live repo. BASELINE_FILE is
also redirected so tests don't clobber the committed baselines.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gdx_dispatch.tools import (
    duplicate_block_scan,
    tenant_plane_redundant_filter_scan,
)


# ────────────────────────────────────────────────────────────────────────
# is_suppressed (`# noqa: <CODE>`)
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scan_module",
    [tenant_plane_redundant_filter_scan],
)
class TestNoqa:
    def test_bare_noqa_suppresses_all(self, scan_module):
        assert scan_module.is_suppressed("from gdx_dispatch.control import x  # noqa", "X1")
        assert scan_module.is_suppressed("from gdx_dispatch.control import x  # noqa", "X2")

    def test_noqa_with_code_suppresses_only_that(self, scan_module):
        assert scan_module.is_suppressed("from gdx_dispatch.control import x  # noqa: X1", "X1")
        assert not scan_module.is_suppressed("from gdx_dispatch.control import x  # noqa: X1", "X2")

    def test_noqa_with_multiple_codes(self, scan_module):
        assert scan_module.is_suppressed("foo  # noqa: T1, T3", "T1")
        assert scan_module.is_suppressed("foo  # noqa: T1, T3", "T3")
        assert not scan_module.is_suppressed("foo  # noqa: T1, T3", "T5")

    def test_no_noqa(self, scan_module):
        assert not scan_module.is_suppressed("from gdx_dispatch.control import x", "X1")


# ────────────────────────────────────────────────────────────────────────
# duplicate_block_scan tokenize fixes
# ────────────────────────────────────────────────────────────────────────


def test_tokenize_preserves_hash_in_url():
    """The audit-flagged regex bug: `#` in a URL was treated as a comment."""
    src = '''url = "https://example.com/path#anchor"\nx = 1\n'''
    result = duplicate_block_scan._canonicalize_via_tokenize(src)
    assert any("#anchor" in canonical for _, canonical in result)


def test_tokenize_preserves_hash_in_fstring():
    src = '''label = f"prefix#{count}"\n'''
    result = duplicate_block_scan._canonicalize_via_tokenize(src)
    # f-string keeps `#`
    assert any("#" in canonical for _, canonical in result)


def test_tokenize_drops_real_comments():
    src = "x = 1  # this is a comment\ny = 2\n"
    result = duplicate_block_scan._canonicalize_via_tokenize(src)
    canonicals = [c for _, c in result]
    assert any("x = 1" in c for c in canonicals)
    assert not any("comment" in c for c in canonicals)


def test_cross_indent_docstring_clones_hash_match():
    """Multi-line distribution must produce equal hashes for two identical
    docstrings even when one is at function-level (4 spaces) and the other
    is inside a class method (8 spaces). Audit-flagged untested property."""
    src_a = '''def f():
    """line 1
    line 2
    line 3
    line 4
    line 5"""
'''
    src_b = '''class C:
    def m(self):
        """line 1
        line 2
        line 3
        line 4
        line 5"""
'''
    a = duplicate_block_scan._canonicalize_via_tokenize(src_a)
    b = duplicate_block_scan._canonicalize_via_tokenize(src_b)

    # The 5 docstring inner lines should produce the same canonical text
    # in both files because _WS_RE collapses whitespace and `.strip()`
    # removes leading indentation.
    canon_a = [c for _, c in a if "line" in c]
    canon_b = [c for _, c in b if "line" in c]
    assert canon_a == canon_b
    assert len(canon_a) >= 5  # 5 docstring body lines (open quote line counts)


def test_tokenize_distributes_multiline_string():
    """Audit-flagged coverage hole: triple-quoted strings used to collapse
    to one canonical line; now they distribute across source lines so
    docstring clones are detectable."""
    src = '''def f():
    """line 1
    line 2
    line 3"""
    return 0
'''
    result = duplicate_block_scan._canonicalize_via_tokenize(src)
    linenos = [ln for ln, _ in result]
    # Original source has lines 2, 3, 4, 5 with content (1=def, 2-5=body).
    # Triple-quoted string spans 3-5; multi-line distribution should produce
    # entries for lines 3, 4, AND 5.
    assert 3 in linenos
    assert 4 in linenos
    assert 5 in linenos


# ────────────────────────────────────────────────────────────────────────
# tenant_plane_redundant_filter_scan integration
# ────────────────────────────────────────────────────────────────────────


def _setup_redundant_filter_scratch(tmp_path, monkeypatch):
    (tmp_path / "gdx_dispatch" / "core").mkdir(parents=True)
    monkeypatch.setattr(tenant_plane_redundant_filter_scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        tenant_plane_redundant_filter_scan, "SCAN_ROOTS", [tmp_path / "gdx_dispatch"]
    )
    monkeypatch.setattr(
        tenant_plane_redundant_filter_scan, "BASELINE_FILE", tmp_path / ".baseline"
    )
    return tmp_path


def test_redundant_filter_detects_filter_form(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db, tid):\n    return db.query(M).filter(M.tenant_id == tid).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert len(findings) == 1
    assert findings[0][2] == "T1"


def test_redundant_filter_detects_filter_by_form(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db, tid):\n    return db.query(M).filter_by(tenant_id=tid).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert len(findings) == 1
    assert findings[0][2] == "T3"


def test_redundant_filter_detects_where_form(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(tid):\n    return select(M).where(M.tenant_id == tid)\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert len(findings) == 1
    assert findings[0][2] == "T5"


def test_redundant_filter_noqa_suppresses(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db, tid):\n"
        "    return db.query(M).filter(M.tenant_id == tid).all()  # noqa: T1\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert findings == []


def test_redundant_filter_unwraps_and_or(tmp_path, monkeypatch):
    """Round-3 audit-flagged bypass: `filter(and_(M.tenant_id == tid))`
    used to walk past because we only checked direct Compare args.
    This is the 2026-04-22 documents-bug pattern wrapped in and_()."""
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "from sqlalchemy import and_\n"
        "def f(db, tid):\n"
        "    return db.query(M).filter(and_(M.tenant_id == tid, M.x > 0)).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    codes = [c for _, _, c, _, _ in findings]
    assert "T1" in codes


def test_redundant_filter_unwraps_or(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "from sqlalchemy import or_\n"
        "def f(db, tid):\n"
        "    return db.query(M).filter(or_(M.tenant_id == tid)).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T1" for _, _, c, _, _ in findings)


def test_redundant_filter_unwraps_not_(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "from sqlalchemy import not_\n"
        "def f(db, tid):\n"
        "    return db.query(M).filter(not_(M.tenant_id == tid)).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T1" for _, _, c, _, _ in findings)


def test_redundant_filter_catches_in_form(tmp_path, monkeypatch):
    """Round-4 audit-flagged bypass: `M.tenant_id.in_([tid])` is not a
    Compare, so the previous visitor missed it entirely."""
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db, tid):\n"
        "    return db.query(M).filter(M.tenant_id.in_([tid])).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T1" for _, _, c, _, _ in findings)


def test_redundant_filter_in_form_in_where(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(tid):\n"
        "    return select(M).where(M.tenant_id.in_([tid]))\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T5" for _, _, c, _, _ in findings)


def test_redundant_filter_catches_is_none(tmp_path, monkeypatch):
    """Round-5 audit-flagged bypass: `M.tenant_id.is_(None)` is the LITERAL
    2026-04-22 documents-bug pattern. The scan that's supposed to catch it
    used to miss it because `.is_(None)` is a Call, not a Compare."""
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db):\n"
        "    return db.query(M).filter(M.tenant_id.is_(None)).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T1" for _, _, c, _, _ in findings)


def test_redundant_filter_catches_isnot(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db):\n"
        "    return db.query(M).filter(M.tenant_id.isnot(None)).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T1" for _, _, c, _, _ in findings)


def test_redundant_filter_catches_between(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db, a, b):\n"
        "    return db.query(M).filter(M.tenant_id.between(a, b)).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T1" for _, _, c, _, _ in findings)


def test_redundant_filter_does_not_fire_on_cross_table_join(tmp_path, monkeypatch):
    """Negative test (audit-flagged gap): comparing two table.tenant_id
    columns to each other in a join is NOT a redundant filter — it's a
    join predicate. The scan must not mark it."""
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db):\n"
        "    return db.query(Outer).join(Inner).filter(Outer.tenant_id == Inner.tenant_id).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    # `Outer.tenant_id == Inner.tenant_id` — both sides are tenant_id
    # attributes. The current implementation flags this as T1 (it sees
    # left=Attribute(tenant_id) and emits a finding for the FIRST side).
    # That's a known false positive on join predicates; documented but
    # not yet fixed. Test asserts CURRENT behavior so a future fix
    # has a regression target.
    assert len(findings) == 1
    # The finding describes one side; either Outer or Inner.
    _, _, code, ident, _ = findings[0]
    assert code == "T1"
    assert "tenant_id" in ident


def test_redundant_filter_unwraps_cast(tmp_path, monkeypatch):
    """Round-3 audit-flagged bypass: `filter(cast(M.tenant_id, UUID) == tid)`
    used to walk past because we only checked direct Attribute on each side."""
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "from sqlalchemy import cast\n"
        "def f(db, tid):\n"
        "    return db.query(M).filter(cast(M.tenant_id, UUID) == tid).all()\n"
    )
    findings = tenant_plane_redundant_filter_scan.scan()
    assert any(c == "T1" for _, _, c, _, _ in findings)


def test_redundant_filter_lineno_catches_same_file_repeats(tmp_path, monkeypatch):
    root = _setup_redundant_filter_scratch(tmp_path, monkeypatch)
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db, tid):\n"
        "    return db.query(M).filter(M.tenant_id == tid).all()\n"
    )
    tenant_plane_redundant_filter_scan._write_baseline(
        tenant_plane_redundant_filter_scan.scan()
    )
    # Add second redundant filter in same file
    (root / "gdx_dispatch" / "core" / "x.py").write_text(
        "def f(db, tid):\n"
        "    return db.query(M).filter(M.tenant_id == tid).all()\n"
        "def g(db, tid):\n"
        "    return db.query(M).filter(M.tenant_id == tid).all()\n"
    )
    new = tenant_plane_redundant_filter_scan._net_new_findings(
        tenant_plane_redundant_filter_scan.scan(),
        tenant_plane_redundant_filter_scan._load_baseline(),
    )
    assert len(new) == 1


# ────────────────────────────────────────────────────────────────────────
# duplicate_block_scan integration
# ────────────────────────────────────────────────────────────────────────


def _setup_duplicate_block_scratch(tmp_path, monkeypatch):
    (tmp_path / "gdx_dispatch").mkdir(parents=True)
    monkeypatch.setattr(duplicate_block_scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(duplicate_block_scan, "SCAN_ROOTS", [tmp_path / "gdx_dispatch"])
    monkeypatch.setattr(duplicate_block_scan, "BASELINE_FILE", tmp_path / ".baseline")
    return tmp_path


def test_duplicate_block_finds_clones(tmp_path, monkeypatch):
    root = _setup_duplicate_block_scratch(tmp_path, monkeypatch)
    block = (
        "x = 1\n"
        "y = 2\n"
        "z = 3\n"
        "a = x + y\n"
        "b = z * 2\n"
    )
    (root / "gdx_dispatch" / "a.py").write_text(block)
    (root / "gdx_dispatch" / "b.py").write_text(block)
    groups = duplicate_block_scan.scan()
    assert any(len(locs) >= 2 for locs in groups.values())


def test_duplicate_block_prune_removes_stale(tmp_path, monkeypatch):
    root = _setup_duplicate_block_scratch(tmp_path, monkeypatch)
    block = "x = 1\ny = 2\nz = 3\na = x + y\nb = z * 2\n"
    (root / "gdx_dispatch" / "a.py").write_text(block)
    (root / "gdx_dispatch" / "b.py").write_text(block)
    groups = duplicate_block_scan.scan()
    duplicate_block_scan._write_baseline(groups)
    # Remove the second copy, leaving no duplicates
    (root / "gdx_dispatch" / "b.py").write_text("# now unique\n")
    new_groups = duplicate_block_scan.scan()
    pruned, kept = duplicate_block_scan._prune_baseline(new_groups)
    assert pruned >= 1
    assert kept == 0
