"""`run_tests_split.sh --scans` must run every committed-baseline scan test.

The pre-check is only worth running if it cannot miss a baseline guard: a
`--scans` that is green while the matrix goes red for a stale baseline is the
GDXA-408 failure with an extra step. So these tests drive the runner itself
(`--scans --list`, a real subprocess, not its source text) and compare what it
would run against an oracle that does not share its code: a plain regex over
every test file, where the runner's helper walks the AST. It does share the
naming convention, so this proves the runner honours the convention, not that
every baseline in the repo is covered: `.doc_link_baseline`, `.pii_literal_baseline`
and the authz baselines are deliberately outside `--scans` (CLAUDE.md says so).

They also pin `--refreeze-baselines`: it forwards `--allow-new` only when the
caller passes it, because that flag is the one that blesses growth.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "gdx_dispatch" / "tools" / "run_tests_split.sh"
TESTS_ROOT = REPO_ROOT / "gdx_dispatch" / "tests"

_GUARD_DEF = re.compile(
    r"^\s*(?:async\s+)?def\s+(test_the_committed_baseline_is_what_the_tree_scans|test\w*scan_refreeze\w*)\s*\(",
    re.MULTILINE,
)


def _runner(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(RUNNER), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
        cwd=REPO_ROOT,
    )


def _listed(*, root: Path | None = None) -> set[Path]:
    proc = _runner("--scans", "--list", env={"SCANS_TESTS_ROOT": str(root)} if root else None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return {(REPO_ROOT / line).resolve() for line in proc.stdout.splitlines() if line.strip()}


def _regex_oracle(tests_root: Path) -> set[Path]:
    hits = set()
    for path in tests_root.rglob("test_*.py"):
        if "e2e" in path.relative_to(tests_root).parts[:-1]:
            continue
        if "scan_refreeze" in path.name or _GUARD_DEF.search(path.read_text(encoding="utf-8")):
            hits.add(path.resolve())
    return hits


def test_scans_covers_every_refreeze_test_in_the_tree() -> None:
    oracle = _regex_oracle(TESTS_ROOT)
    # Vacuity canary: the oracle must see the two guards this exists for, or
    # "every oracle hit is listed" would hold over an empty set.
    names = {p.name for p in oracle}
    assert {
        "test_duplicate_block_scan_refreeze.py",
        "test_tenant_plane_redundant_filter_scan_refreeze.py",
    } <= names, names

    listed = _listed()
    assert oracle - listed == set(), f"--scans would not run: {sorted(oracle - listed)}"
    assert TESTS_ROOT / "test_saas_surfaces_retired.py" in listed


def _plant(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path.resolve()


def test_a_fourth_baseline_scan_joins_without_an_edit(tmp_path: Path) -> None:
    """The falsifier: a new guard lands in a tree, and --scans picks it up."""
    saas = _plant(tmp_path, "test_saas_surfaces_retired.py", "def test_x():\n    pass\n")
    by_name = _plant(tmp_path, "test_widget_scan_refreeze.py", "def test_y():\n    pass\n")
    by_shared = _plant(
        tmp_path,
        "sub/test_widgets.py",
        "class TestW:\n    def test_the_committed_baseline_is_what_the_tree_scans(self):\n        pass\n",
    )
    by_func = _plant(tmp_path, "test_gadgets.py", "async def test_gadget_scan_refreeze_cli():\n    pass\n")
    _plant(tmp_path, "test_unrelated.py", "def test_z():\n    pass\n")
    # A mention in prose or a string is not a test; only a definition counts.
    _plant(tmp_path, "test_mentions.py", '"""see test_the_committed_baseline_is_what_the_tree_scans"""\n')
    _plant(tmp_path, "e2e/test_e2e_scan_refreeze.py", "def test_e():\n    pass\n")

    assert _listed(root=tmp_path) == {saas, by_name, by_shared, by_func}
    # And the independent oracle agrees on the same tree, so the real-tree test
    # above is comparing two instruments that measure the same thing.
    assert _regex_oracle(tmp_path) == {by_name, by_shared, by_func}


def test_a_renamed_pinned_file_fails_loudly(tmp_path: Path) -> None:
    _plant(tmp_path, "test_widget_scan_refreeze.py", "def test_y():\n    pass\n")
    proc = _runner("--scans", "--list", env={"SCANS_TESTS_ROOT": str(tmp_path)})
    assert proc.returncode != 0
    assert "test_saas_surfaces_retired.py" in proc.stdout + proc.stderr


# ── --refreeze-baselines ────────────────────────────────────────────────────


@pytest.fixture
def fake_python(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in interpreter that records its argv and succeeds."""
    log = tmp_path / "argv.log"
    stub = tmp_path / "fakepy"
    stub.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, log


def test_refreeze_runs_both_scanners_without_allow_new(fake_python) -> None:
    stub, log = fake_python
    proc = _runner("--refreeze-baselines", env={"REFREEZE_PYTHON": str(stub)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.read_text().splitlines() == [
        "-m gdx_dispatch.tools.duplicate_block_scan --baseline",
        "-m gdx_dispatch.tools.tenant_plane_redundant_filter_scan --baseline",
    ]


def test_refreeze_forwards_allow_new_only_when_asked(fake_python) -> None:
    stub, log = fake_python
    proc = _runner("--refreeze-baselines", "--allow-new", env={"REFREEZE_PYTHON": str(stub)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert all(line.endswith("--baseline --allow-new") for line in log.read_text().splitlines())


def test_refreeze_rejects_any_other_flag(fake_python) -> None:
    stub, log = fake_python
    proc = _runner("--refreeze-baselines", "--prune", env={"REFREEZE_PYTHON": str(stub)})
    assert proc.returncode == 2
    assert not log.exists(), "a rejected call must not have run a scanner"


def test_a_refusal_is_a_nonzero_exit(tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    stub = tmp_path / "refusingpy"
    stub.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 2\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    proc = _runner("--refreeze-baselines", env={"REFREEZE_PYTHON": str(stub)})
    assert proc.returncode == 2
    assert "REFUSED" in proc.stdout
    assert len(log.read_text().splitlines()) == 2, "one scanner's refusal must not skip the other"


def test_a_crash_is_not_reported_as_a_refusal(tmp_path: Path) -> None:
    # A traceback exits 1; telling the caller to retry with --allow-new would
    # send them into the same crash.
    stub = tmp_path / "crashingpy"
    stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    proc = _runner("--refreeze-baselines", env={"REFREEZE_PYTHON": str(stub)})
    assert proc.returncode == 1
    assert "CRASHED" in proc.stdout
    assert "REFUSED" not in proc.stdout
