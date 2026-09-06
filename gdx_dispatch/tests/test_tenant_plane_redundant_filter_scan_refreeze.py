"""The scanner's `--baseline` re-freeze refuses to admit a new shape.

The gate in test_saas_surfaces_retired is line-keyed, so a line shift in a
listed file forces a re-freeze; if the re-freeze admitted anything the old
baseline did not hold, a new redundant filter could ride in under the shift
and the gate — whose oracle is the baseline — would never see it (audit,
2026-09-06). `_shapes_beyond_baseline` is that refusal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from gdx_dispatch.tools import tenant_plane_redundant_filter_scan as scanner

ROOT = scanner.REPO_ROOT


def _finding(rel: str, lineno: int, rule: str, ident: str):
    return (ROOT / rel, lineno, rule, ident, "snippet")


def test_a_pure_line_shift_is_admitted() -> None:
    baseline = {"a/x.py:T1:Job.company_id:10", "a/x.py:T1:Job.company_id:20"}
    moved = [_finding("a/x.py", 11, "T1", "Job.company_id"), _finding("a/x.py", 25, "T1", "Job.company_id")]
    assert scanner._shapes_beyond_baseline(baseline, moved) == []


def test_a_new_filter_under_the_shift_is_refused() -> None:
    baseline = {"a/x.py:T1:Job.company_id:10"}
    shifted_plus_one = [_finding("a/x.py", 11, "T1", "Job.company_id"), _finding("a/x.py", 40, "T1", "Job.company_id")]
    assert scanner._shapes_beyond_baseline(baseline, shifted_plus_one) == ["a/x.py:T1:Job.company_id: 2 (baseline 1)"]


def test_a_new_shape_in_another_file_is_refused() -> None:
    baseline = {"a/x.py:T1:Job.company_id:10"}
    assert scanner._shapes_beyond_baseline(baseline, [_finding("b/y.py", 3, "T5", "Tag.company_id")]) == [
        "b/y.py:T5:Tag.company_id: 1 (baseline 0)"
    ]


def test_removals_are_never_beyond() -> None:
    baseline = {"a/x.py:T1:Job.company_id:10", "a/x.py:T1:Job.company_id:20"}
    assert scanner._shapes_beyond_baseline(baseline, [_finding("a/x.py", 10, "T1", "Job.company_id")]) == []


def test_the_committed_baseline_is_what_the_tree_scans() -> None:
    """Zero slack, both directions, on the file that ships with this tree."""
    assert Path(scanner.BASELINE_FILE).exists()
    current = {scanner._to_signature(f) for f in scanner.scan()}
    assert current == scanner._load_baseline()


# ── the CLI, not just the arithmetic ─────────────────────────────────────────
# An earlier cut had the unit cases above and no test of main(): stripping the
# refusal block from main() left every test green (audit, 2026-09-06).


def _run_baseline(monkeypatch, tmp_path, findings, old_baseline, argv):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(sorted(old_baseline)) + "\n")
    monkeypatch.setattr(scanner, "BASELINE_FILE", baseline)
    monkeypatch.setattr(scanner, "scan", lambda: findings)
    monkeypatch.setattr(sys, "argv", ["scan", *argv])
    rc = scanner.main()
    return rc, set(json.loads(baseline.read_text()))


def test_cli_baseline_refuses_a_smuggled_filter(monkeypatch, tmp_path, capsys):
    old = {"a/x.py:T1:Job.company_id:10"}
    shifted_plus_one = [_finding("a/x.py", 11, "T1", "Job.company_id"), _finding("a/x.py", 40, "T1", "Job.company_id")]
    rc, written = _run_baseline(monkeypatch, tmp_path, shifted_plus_one, old, ["--baseline"])
    assert rc == 2
    assert written == old, "a refused re-freeze leaves the baseline byte-for-byte alone"
    assert "a/x.py:T1:Job.company_id: 2 (baseline 1)" in capsys.readouterr().out


def test_cli_baseline_admits_a_pure_shift(monkeypatch, tmp_path):
    old = {"a/x.py:T1:Job.company_id:10"}
    rc, written = _run_baseline(monkeypatch, tmp_path, [_finding("a/x.py", 11, "T1", "Job.company_id")], old, ["--baseline"])
    assert rc == 0
    assert written == {"a/x.py:T1:Job.company_id:11"}


def test_cli_allow_new_is_the_only_way_past(monkeypatch, tmp_path):
    old = {"a/x.py:T1:Job.company_id:10"}
    more = [_finding("a/x.py", 10, "T1", "Job.company_id"), _finding("b/y.py", 3, "T5", "Tag.company_id")]
    rc, written = _run_baseline(monkeypatch, tmp_path, more, old, ["--baseline", "--allow-new"])
    assert rc == 0
    assert written == {"a/x.py:T1:Job.company_id:10", "b/y.py:T5:Tag.company_id:3"}
