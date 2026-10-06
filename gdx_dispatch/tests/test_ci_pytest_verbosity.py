"""No CI pytest invocation adds -q on top of pytest.ini's addopts -q (GDXA-308).

pytest.ini ``addopts`` already carries ``-q``. A second ``-q`` on the command
line makes it ``-qq``, and at -qq pytest drops the final
``N passed, M skipped`` line: CI then names skips (``-ra``) but never counts
them. Verbosity belongs to pytest.ini alone.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_QUIET = re.compile(r"^(-q+|--quiet)$")


def _pytest_commands(text: str) -> list[str]:
    """Each logical shell line (backslash continuations joined, comment lines
    dropped) that runs pytest — ``python -m pytest`` or a bare ``pytest`` —
    not one that merely names it (``pip install -q pytest``, an ``echo``)."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    run_pytest = re.compile(r"(?:-m\s+pytest|^\s*(?:-\s+)?(?:run:\s*)?pytest)\s")
    return [ln for ln in joined.splitlines() if run_pytest.search(ln)]


def test_pytest_ini_addopts_is_already_quiet():
    """The premise: if addopts stops carrying -q, this guard's reason is gone."""
    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "pytest.ini")
    assert "-q" in cfg["pytest"]["addopts"].split()


def test_ci_yml_runs_pytest():
    """The guard below must have something to inspect, or it passes vacuously."""
    cmds = _pytest_commands((WORKFLOWS / "ci.yml").read_text())
    assert len(cmds) == 2, cmds  # the shard run and its native-crash retry


def test_matcher_ignores_lines_that_only_name_pytest():
    assert _pytest_commands("run: pip install -q pytest pytest-split\n") == []
    assert _pytest_commands('echo "pytest died -q"\n') == []
    assert len(_pytest_commands("run: pytest -q tests/\n")) == 1


def test_no_workflow_pytest_command_repeats_quiet():
    offenders = []
    for wf in sorted(WORKFLOWS.glob("*.y*ml")):
        for cmd in _pytest_commands(wf.read_text()):
            if any(_QUIET.match(tok) for tok in cmd.split()):
                offenders.append(f"{wf.name}: {cmd.strip()}")
    assert offenders == [], (
        "pytest.ini addopts already has -q; another makes -qq and drops the "
        f"pass/skip counts line: {offenders}"
    )
