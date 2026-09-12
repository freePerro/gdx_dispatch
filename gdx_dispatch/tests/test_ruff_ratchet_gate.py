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


def _stub_ruff(bin_dir: Path, stdout: str, rc: int, version: str = "0.0.0") -> None:
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
        # The ratchet asks `ruff --version` to decide whether the count is
        # trustworthy enough to write back. Without this the stub replays the
        # scenario text, the version reads as unparseable, and every
        # baseline-lowering path is unreachable from a test.
        f'if [ "$1" = "--version" ]; then echo "ruff {version}"; exit 0; fi\n'
        f"cat <<'OUT'\n{stdout}\nOUT\n"
        f"exit {rc}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _stub_git(bin_dir: Path, *, dirty: bool) -> None:
    """A `git` that reports a clean or dirty tree on demand.

    The ratchet refuses to lower the baseline from a dirty tree — the count
    would describe uncommitted edits rather than the committed tree. Stubbing
    git here lets the tests drive both sides without a production backdoor that
    could switch the real guard off.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "git"
    porcelain = " M gdx_dispatch/example.py" if dirty else ""
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  case "$a" in rev-parse) exit 0 ;; esac\n'
        "done\n"
        f'for a in "$@"; do\n'
        f'  case "$a" in --porcelain) printf "%s" "{porcelain}"; exit 0 ;; esac\n'
        f"done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run(
    tmp_path: Path,
    stdout: str,
    rc: int,
    baseline: str = "3",
    version: str = "0.0.0",
    extra_env: dict[str, str] | None = None,
    dirty: bool = False,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _stub_ruff(bin_dir, stdout, rc, version=version)
    _stub_git(bin_dir, dirty=dirty)
    baseline_file = tmp_path / "baseline"
    baseline_file.write_text(baseline, encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "RUFF_BASELINE_FILE": str(baseline_file),
        "RUFF_TARGET": "gdx_dispatch/",
        "HOME": str(tmp_path),
    }
    env.update(extra_env or {})
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


# ── the ratchet half: the baseline must come DOWN when the count does ────
#
# Until 2026-09-12 this gate was a one-way ceiling — it failed on an increase
# and did nothing on a decrease, and nothing in the repo ever wrote
# `.ruff_baseline`. Measured on merged main that day: baseline 980, real count
# 974, six violations of slack a later regression could spend while the gate
# stayed green. These pin the write, and every condition that must suppress it.

_CI_PIN = "0.15.18"  # must match the pin ci.yml installs


def _baseline_after(result, tmp_path: Path) -> str:
    return (tmp_path / "baseline").read_text(encoding="utf-8").strip()


def test_baseline_is_lowered_when_the_count_drops(tmp_path: Path) -> None:
    result = _run(tmp_path, "Found 1 error.", rc=1, baseline="3", version=_CI_PIN)
    assert result.returncode == 0, result.stdout
    assert _baseline_after(result, tmp_path) == "1", (
        "the gate is named a ratchet; a decrease must tighten it"
    )
    assert "lowered 3 -> 1" in result.stdout


def test_a_clean_tree_ratchets_to_zero(tmp_path: Path) -> None:
    result = _run(tmp_path, "All checks passed!", rc=0, baseline="3", version=_CI_PIN)
    assert result.returncode == 0, result.stdout
    assert _baseline_after(result, tmp_path) == "0"


def test_an_older_ruff_must_not_lower_the_baseline(tmp_path: Path) -> None:
    """The one that actually bites.

    An older ruff knows fewer rules and reports FEWER violations. Writing that
    count would set a baseline CI's pinned ruff can never meet, turning a local
    convenience into a repo-wide red.
    """
    result = _run(tmp_path, "Found 1 error.", rc=1, baseline="3", version="0.15.8")
    assert result.returncode == 0, result.stdout
    assert _baseline_after(result, tmp_path) == "3", "stale-version count was written"
    assert "not writing" in result.stdout


def test_a_narrowed_target_must_not_lower_the_baseline(tmp_path: Path) -> None:
    """A partial measurement is not a baseline for the whole tree."""
    result = _run(
        tmp_path, "Found 1 error.", rc=1, baseline="3", version=_CI_PIN,
        extra_env={"RUFF_TARGET": "gdx_dispatch/tools/"},
    )
    assert result.returncode == 0, result.stdout
    assert _baseline_after(result, tmp_path) == "3", "partial count was written"
    assert "not writing" in result.stdout


def test_no_write_opt_out_is_honoured(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "Found 1 error.", rc=1, baseline="3", version=_CI_PIN,
        extra_env={"RUFF_RATCHET_NO_WRITE": "1"},
    )
    assert result.returncode == 0, result.stdout
    assert _baseline_after(result, tmp_path) == "3"


def test_an_increase_still_fails_and_leaves_the_baseline_alone(tmp_path: Path) -> None:
    """The ceiling half must survive the ratchet half."""
    result = _run(tmp_path, "Found 9 errors.", rc=1, baseline="3", version=_CI_PIN)
    assert result.returncode != 0
    assert "increased" in result.stdout
    assert _baseline_after(result, tmp_path) == "3", "a regression must never raise the bar"


def test_the_pin_this_file_asserts_matches_ci() -> None:
    """If ci.yml bumps ruff, the tests above silently stop exercising the
    write path — they would take the version-drift branch instead and still
    pass. Fail loudly here instead."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"pip install ruff=={_CI_PIN}" in ci, (
        f"ci.yml no longer pins ruff {_CI_PIN}; update _CI_PIN and re-baseline"
    )


def test_a_dirty_tree_must_not_lower_the_baseline(tmp_path: Path) -> None:
    """The guard most likely to fire in real use.

    A lint gate is normally run mid-edit. An uncommitted `# ruff: noqa` was
    measured taking the baseline from 980 to 937 — a number describing the
    tree on disk, not the tree that gets committed, which would red CI for
    everyone on the next push.
    """
    result = _run(
        tmp_path, "Found 1 error.", rc=1, baseline="3", version=_CI_PIN, dirty=True
    )
    assert result.returncode == 0, result.stdout
    assert _baseline_after(result, tmp_path) == "3", "dirty-tree count was written"
    assert "working tree is dirty" in result.stdout


def test_no_write_opt_out_accepts_any_truthy_value(tmp_path: Path) -> None:
    """It compared against the literal "1", so `=true` still wrote."""
    result = _run(
        tmp_path, "Found 1 error.", rc=1, baseline="3", version=_CI_PIN,
        extra_env={"RUFF_RATCHET_NO_WRITE": "true"},
    )
    assert result.returncode == 0, result.stdout
    assert _baseline_after(result, tmp_path) == "3"


def test_a_failing_hard_gate_must_not_lower_the_baseline(tmp_path: Path) -> None:
    """F821/F823 hard-fail regardless of the count. The first draft wrote the
    baseline BEFORE that gate, so a run that ultimately failed still banked a
    lower number — a failing gate must never move the bar."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _stub_git(bin_dir, dirty=False)
    # Count is under baseline, but `--select F821,F823` exits non-zero.
    stub = bin_dir / "ruff"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = "--version" ]; then echo "ruff {_CI_PIN}"; exit 0; fi\n'
        'for a in "$@"; do\n'
        '  case "$a" in --select) echo "F821 Undefined name \\`x\\`"; exit 1 ;; esac\n'
        '  case "$a" in --statistics) exit 0 ;; esac\n'
        "done\n"
        "echo 'Found 1 error.'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    baseline_file = tmp_path / "baseline"
    baseline_file.write_text("3", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(RATCHET)],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "RUFF_BASELINE_FILE": str(baseline_file),
            "RUFF_TARGET": "gdx_dispatch/",
            "HOME": str(tmp_path),
        },
    )
    assert result.returncode != 0, "F821 must still fail the gate"
    assert baseline_file.read_text(encoding="utf-8").strip() == "3", (
        "a failing run lowered the bar"
    )
