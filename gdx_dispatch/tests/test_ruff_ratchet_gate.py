"""Guard: the ruff ratchet must FAIL whenever it could not measure.

The gate lives in ``tools/ruff_ratchet.sh`` rather than inline in ``ci.yml``
precisely so these scenarios are reachable from a test at all. Each mode below
would previously have been reported as a PASS:

1. an unanchored count, where a diagnostic snippet line is read as the summary
   (**latent** on the current rule selection — see the control test below);
2. ruff exiting >= 2, which prints no summary, where ``|| echo 0`` made it zero
   (observed live 2026-09-09 on an unwritable ``.ruff_cache``);
3. ruff's ``E902`` unreadable-target diagnostic, which prints "Found 1 error.";
4. a target holding no Python files, where ruff warns and then says
   "All checks passed!".

``test_unanchored_pattern_is_the_bug`` is the pre-fix control: it asserts the
OLD expression really does misread the fixture, so these tests cannot quietly
decay into tautologies if the fixture drifts.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RATCHET = REPO_ROOT / "gdx_dispatch" / "tools" / "ruff_ratchet.sh"

# Captured verbatim from ruff 0.15.18 (the version ci.yml pins), with only the
# temp path rewritten to a repo-relative one. Hand-written approximations of
# this shape have been wrong before: ruff prints two lines of context either
# side of the offending line, which is what puts arbitrary SOURCE TEXT into
# the output stream. The gutter (`116 | `) is the load-bearing detail.
SNIPPET = (
    "F841 Local variable `unused` is assigned to but never used\n"
    "   --> gdx_dispatch/tests/test_example.py:116:5\n"
    "    |\n"
    "114 | # filler 114\n"
    "115 | def f(body):\n"
    '116 |     unused = body["answer"] == "Found 2 customers."\n'
    "    |     ^^^^^^\n"
    "117 |     return 1\n"
    "    |\n"
    "help: Remove assignment to unused variable `unused`\n"
    "\n"
)


def _stub_ruff(bin_dir: Path, stdout: str, rc: int) -> None:
    """Install a fake `ruff` that replays one scenario.

    The ratchet also invokes ruff on its other branches (``--statistics`` when
    over baseline, ``--select F821,F823`` at the end); those succeed quietly so
    the scenario under test is the only thing being measured.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "ruff"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  case "$a" in --statistics|--select) exit 0 ;; esac\n'
        "done\n"
        f"cat <<'OUT'\n{stdout}\nOUT\n"
        f"exit {rc}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run(tmp_path: Path, stdout: str, rc: int, baseline: str = "3") -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _stub_ruff(bin_dir, stdout, rc)
    baseline_file = tmp_path / "baseline"
    baseline_file.write_text(baseline, encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "RUFF_BASELINE_FILE": str(baseline_file),
        "RUFF_TARGET": "gdx_dispatch/",
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        ["bash", str(RATCHET)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def test_ratchet_script_exists() -> None:
    assert RATCHET.is_file(), f"{RATCHET} is the gate; ci.yml invokes it"


# ── mode 1: a snippet line must never be read as the summary ─────────────


def test_snippet_line_does_not_hide_a_real_increase(tmp_path: Path) -> None:
    result = _run(tmp_path, SNIPPET + "Found 5 errors.", rc=1, baseline="3")
    assert result.returncode != 0, (
        f"passed with 5 violations against a baseline of 3 — stdout={result.stdout!r}"
    )
    assert "increased" in result.stdout


def test_snippet_line_does_not_cause_a_false_failure(tmp_path: Path) -> None:
    result = _run(tmp_path, SNIPPET + "Found 1 error.", rc=1, baseline="3")
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "current=1" in result.stdout


def test_unanchored_pattern_is_the_bug() -> None:
    """Pre-fix control: prove the OLD expression misreads this real fixture.

    Note this is the bug's *mechanism*, demonstrated on captured output. On the
    repo's current rule selection ruff emits no diagnostic within two lines of
    the "Found 2 customers." occurrences, so the old expression happens to
    return a single value today — the bug is armed, not firing.
    """
    text = SNIPPET + "Found 5 errors.\n"
    old = subprocess.run(
        ["grep", "-oP", r"Found \K\d+"], input=text, capture_output=True, text=True
    ).stdout.split()
    new = subprocess.run(
        ["grep", "-oP", r"^Found \K\d+"], input=text, capture_output=True, text=True
    ).stdout.split()
    assert old == ["2", "5"], f"fixture no longer triggers the bug: {old}"
    assert new == ["5"], f"anchored pattern should read only the summary: {new}"


# ── modes 2-4: ruff did not measure ──────────────────────────────────────


def test_ruff_hard_failure_fails_the_gate(tmp_path: Path) -> None:
    """Exit >= 2 means ruff never ran; that must not read as zero violations."""
    result = _run(tmp_path, "ruff failed\n  Cause: Permission denied (os error 13)", rc=2)
    assert result.returncode != 0
    assert "measured nothing" in result.stdout


def test_unreadable_target_fails_the_gate(tmp_path: Path) -> None:
    """E902 prints "Found 1 error." — a count that would pass any baseline."""
    result = _run(
        tmp_path,
        "E902 No such file or directory (os error 2)\n --> gdx_dispatch/:1:1\n\nFound 1 error.",
        rc=1,
    )
    assert result.returncode != 0
    assert "E902" in result.stdout


def test_no_python_files_fails_the_gate(tmp_path: Path) -> None:
    """An emptied target warns and then says "All checks passed!" with rc 0."""
    result = _run(
        tmp_path,
        "warning: No Python files found under the given path(s)\nAll checks passed!",
        rc=0,
    )
    assert result.returncode != 0
    assert "no Python files" in result.stdout


def test_silent_ruff_fails_the_gate(tmp_path: Path) -> None:
    """rc 0 with neither a count nor an explicit clean report is not a pass.

    This is the backstop that does not depend on having enumerated the modes
    above: absence of a count is only ever excused by "All checks passed!".
    """
    result = _run(tmp_path, "", rc=0)
    assert result.returncode != 0
    assert "no violation count" in result.stdout


def test_unreadable_baseline_fails_the_gate(tmp_path: Path) -> None:
    result = _run(tmp_path, "Found 1 error.", rc=1, baseline="")
    assert result.returncode != 0
    assert "baseline" in result.stdout


# ── ordinary behaviour still works ───────────────────────────────────────


@pytest.mark.parametrize(
    ("stdout", "rc", "baseline", "expect_pass"),
    [
        ("All checks passed!", 0, "3", True),
        ("Found 3 errors.", 1, "3", True),
        ("Found 4 errors.", 1, "3", False),
    ],
)
def test_ordinary_counts(tmp_path: Path, stdout: str, rc: int, baseline: str, expect_pass: bool) -> None:
    result = _run(tmp_path, stdout, rc=rc, baseline=baseline)
    assert (result.returncode == 0) is expect_pass, f"stdout={result.stdout!r}"
