"""Guard: the matrix runner must name its skips WITHOUT muting its failures.

``run_tests_split.sh`` is the single point every shard, every skill and every
unattended matrix passes through, so its ``-r`` flag decides what the whole
repo's default test report says. Two requirements pull in opposite directions
and one flag has to satisfy both:

1. **Skips must be named.** A matrix that prints "102 skipped" and not one
   reason cannot distinguish a Postgres arm that ran from one that was never
   reachable — CLAUDE.md's "the gap is invisible" (102 tests, 2026-09-14).
2. **Failures and collection errors must stay named.** The script's
   collection-error report greps ``^ERROR [^ ]+\\.py`` out of pytest's short
   test summary. That section only exists for the report chars you ask for.

The trap is that pytest's ``-r`` is a **store** option, not an append one:
``_pytest.terminal._REPORTCHARS_DEFAULT`` is ``"fE"`` and ``getreportopt()``
iterates only the chars supplied, so the obvious ``-rs`` REPLACES the default
and switches the #679 collection-error guard off in silence.

A third requirement is that an xfail which starts PASSING must be named; see
``test_an_xpass_is_named_under_the_runners_flag``.

These tests pin the *behaviour*, never the letters — pinning letters would veto
a future better flag and, worse, would pass a flag that had stopped working.
Measured against the three candidates 2026-09-27: ``-ra`` (pytest's own alias
for ``sxXEf``) passes every flag test; ``-rfEs`` fails only the XPASS test; ``-rs`` fails
5, including the collection-error guard. ``test_bare_rs_is_the_trap`` is the
control: it proves ``-rs`` really does delete those lines from real pytest
output, so these tests cannot quietly decay into tautologies if pytest's
defaults change.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "gdx_dispatch" / "tools" / "run_tests_split.sh"

# A failing test, a skipped-with-reason test and an xfail that passes, in one
# module: pytest reports FAILED, SKIPPED and XPASS from a single run.
MIXED_PROBE = """
import pytest


def test_probe_failure():
    assert 1 == 2


@pytest.mark.skip(reason="probe skip reason marker")
def test_probe_skipped():
    pass


@pytest.mark.xfail(reason="probe xfail that now passes")
def test_probe_xpass():
    assert True
"""

# The #679 shape: a module that cannot be imported, so the whole file silently
# drops out of the run. pytest aborts the session on a collection error, which
# is why this cannot share a run with MIXED_PROBE.
COLLECT_PROBE = "import gdx_probe_module_that_does_not_exist  # noqa: F401\n"


def _report_flag() -> str:
    """The standalone `-r...` token from the runner's COMMON_OPTS array.

    Matched as a whole shell token, not as a substring: a plain
    ``re.findall(r"-r[a-zA-Z]+", line)`` also matches the ``-report`` inside a
    future ``--cov-report=term``.
    """
    text = RUNNER.read_text(encoding="utf-8")
    lines = [
        ln.strip() for ln in text.splitlines()
        if re.match(r"\s*COMMON_OPTS\+?=\(", ln)
    ]
    assert lines, f"no COMMON_OPTS array found in {RUNNER}"
    line = " ".join(lines)
    body = " ".join(ln.split("(", 1)[1].rstrip(")") for ln in lines)
    flags = [tok for tok in body.split() if re.fullmatch(r"-r[a-zA-Z]+", tok)]
    assert len(flags) == 1, (
        f"expected exactly one -r report flag in COMMON_OPTS, found {flags}: {line}"
    )
    return flags[0]


def _resolved_report_opts() -> str:
    """Resolve the runner's flag through pytest's OWN getreportopt().

    Asking pytest rather than reading the letters is what lets `-ra` (alias for
    "sxXEf") and `-rfEs` both pass while `-rs` fails.
    """
    from _pytest.terminal import getreportopt

    class _Option:
        reportchars = _report_flag()[len("-r") :]
        disable_warnings = False

    class _Config:
        option = _Option()

    return getreportopt(_Config())  # type: ignore[arg-type]


def _run_probe(tmp_path: Path, source: str, *extra: str) -> tuple[str, str]:
    """Run a probe module under a nested pytest; return (output, last line).

    cwd is ``tmp_path`` so no repo ``pytest.ini`` or ``conftest.py`` applies:
    this measures the ``-r`` mechanism itself, not the repo's addopts. The last
    line is returned separately because the script reads exactly
    ``tail -1 "$LOG_DIR/group_N.log"`` for its per-shard summary.
    """
    probe = tmp_path / "test_probe.py"
    probe.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--tb=short",
         *extra, str(probe)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    out = result.stdout + result.stderr
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return out, (lines[-1] if lines else "")


def test_runner_exists() -> None:
    assert RUNNER.is_file(), f"{RUNNER} is the matrix entrypoint"


# ── both requirements, resolved by pytest itself ──────────────────────────


@pytest.mark.parametrize(
    ("char", "why"),
    [
        ("s", "the matrix would report a skip COUNT with no reasons, and an "
              "unreachable Postgres arm would read as green"),
        ("f", "the FAILED short-summary lines would vanish"),
        ("E", "the collection-error report greps '^ERROR [^ ]+\\.py' out of the "
              "short test summary; without 'E' a file that never ran is invisible "
              "and the #679 guard is off"),
    ],
)
def test_runner_flag_resolves_to_required_report_char(char: str, why: str) -> None:
    """`-rs` alone passes for 's' and fails for 'f'/'E' — that is the trap.

    pytest's -r REPLACES its "fE" default rather than adding to it, so asking
    for skips naively costs you failures and errors.
    """
    resolved = _resolved_report_opts()
    assert char in resolved, (
        f"{_report_flag()} resolves to reportopts {resolved!r}, which omits "
        f"{char!r}: {why}. Use -ra (pytest's alias for 'sxXEf')."
    )


# ── the script's own two readers must keep working ───────────────────────


def test_collection_error_line_survives_the_runners_flag(tmp_path: Path) -> None:
    """`grep -hE "^ERROR [^ ]+\\.py"` must still find a planted import failure."""
    out, last = _run_probe(tmp_path, COLLECT_PROBE, _report_flag())
    assert re.search(r"(?m)^ERROR [^ ]+\.py", out), (
        f"the runner's {_report_flag()} hides collection errors from its own "
        f"report; a file that never ran would be invisible. output:\n{out}"
    )
    assert re.search(r"\d+ error", last), f"tail -1 is not the summary: {last!r}"


def test_failed_and_skipped_lines_both_appear_under_the_runners_flag(
    tmp_path: Path,
) -> None:
    out, last = _run_probe(tmp_path, MIXED_PROBE, _report_flag())
    assert re.search(r"(?m)^FAILED ", out), f"no FAILED summary line:\n{out}"
    assert re.search(r"(?m)^SKIPPED \[\d+\]", out), f"no SKIPPED reason line:\n{out}"
    assert "probe skip reason marker" in out, "the skip reason text is not printed"
    # The per-shard summary the script prints with `tail -1` must remain last.
    assert re.search(r"\d+ failed", last), f"tail -1 is not the summary: {last!r}"


def test_an_xpass_is_named_under_the_runners_flag(tmp_path: Path) -> None:
    """A non-strict xfail that starts passing must not stay invisible.

    test_schema_fixture_drift.py:97 is `strict=False` and its reason says to flip
    strict=True once SS-4d lands — so it beginning to pass is the signal, and
    without the 'X' report char nothing says it did. (A `strict=True` xfail like
    test_settings_row.py:179 already fails its shard on XPASS, so it needs no
    help.) Only 'X' covers the non-strict ones, which is why the runner uses -ra
    rather than the narrower -rfEs.
    """
    out, _ = _run_probe(tmp_path, MIXED_PROBE, _report_flag())
    assert re.search(r"(?m)^XPASS ", out), (
        f"{_report_flag()} does not name an XPASS, so an xfail marker that is now "
        f"obsolete would never be reported:\n{out}"
    )


def test_skip_reasons_are_absent_without_any_report_flag(tmp_path: Path) -> None:
    """The before-state, pinned: this is what the matrix printed until 2026-09-27.

    Keeps the tests above honest — if pytest ever started naming skips by
    default, the flag would stop being load-bearing and this test says so.
    """
    out, _ = _run_probe(tmp_path, MIXED_PROBE)
    assert not re.search(r"(?m)^SKIPPED \[\d+\]", out), (
        "pytest now names skips without -r; the flag's purpose has changed:\n" + out
    )
    assert re.search(r"(?m)^FAILED ", out), "pytest's default no longer includes 'f'"


# ── control: prove the trap is real, not theoretical ─────────────────────


@pytest.mark.parametrize(
    ("source", "pattern", "what"),
    [
        (COLLECT_PROBE, r"(?m)^ERROR [^ ]+\.py", "collection error"),
        (MIXED_PROBE, r"(?m)^FAILED ", "test failure"),
    ],
)
def test_bare_rs_is_the_trap(tmp_path: Path, source: str, pattern: str, what: str) -> None:
    """`-rs` names the skips and deletes the failure/error lines. Measured.

    This is why the runner carries -ra. If a future pytest makes -r additive
    this test fails, and the COMMON_OPTS comment can be simplified.
    """
    out, _ = _run_probe(tmp_path, source, "-rs")
    assert not re.search(pattern, out), (
        f"-rs no longer suppresses the {what} summary line — pytest's -r may have "
        f"become additive. Re-read the COMMON_OPTS comment in {RUNNER.name}:\n{out}"
    )


# ── the two places the flag could be undone ──────────────────────────────

CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_pytest_lines_carry_the_runners_report_flag() -> None:
    """ci.yml composes its own shard command; it must stay in lockstep.

    Every ``--splits ... --group`` line in ci.yml is a shard invocation that
    does not go through the runner, so its -r flag is checked against
    COMMON_OPTS here rather than trusted to a comment.
    """
    lines = [ln for ln in CI_YML.read_text(encoding="utf-8").splitlines()
             if "--splits" in ln and "--group" in ln]
    assert lines, f"no shard command found in {CI_YML}"
    for ln in lines:
        flags = [tok for tok in ln.split() if re.fullmatch(r"-r[a-zA-Z]+", tok)]
        assert flags == [_report_flag()], (
            f"ci.yml shard line carries {flags}, the runner carries "
            f"{_report_flag()}: {ln.strip()}"
        )


def _run_runner(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real runner, one shard, real pytest, on the given args."""
    logs = tmp_path / "logs"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTEST": f"{sys.executable} -m pytest",
        "N": "1",
        "LOG_DIR": str(logs),
        "MATRIX_LOCK": "0",
        "SKIP_DEP_CHECK": "1",
    }
    return subprocess.run(
        ["bash", str(RUNNER), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


MUTED = "reports failures but names none"


# A failing test that logs and prints ERROR first: its captured output carries
# `ERROR    ...` lines that must not pass for pytest's own summary.
NOISY_PROBE = """
import logging


def test_probe_noisy_failure():
    logging.getLogger("probe").error("upstream refused")
    print("ERROR connecting to db")
    assert False
"""


@pytest.mark.parametrize("source", [MIXED_PROBE, NOISY_PROBE], ids=["plain", "noisy"])
@pytest.mark.parametrize(
    "flag", ["-rs", "-vrs", "-r=s", "--report-chars=s", "-raN"]
)
def test_runner_says_so_when_a_caller_flag_mutes_the_failures(
    tmp_path: Path, flag: str, source: str
) -> None:
    """Every spelling of a caller -r that drops f/E is caught by its OUTPUT."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(source, encoding="utf-8")
    result = _run_runner(tmp_path, str(probe), flag)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert MUTED in result.stdout, result.stdout


@pytest.mark.parametrize("source", [MIXED_PROBE, NOISY_PROBE], ids=["plain", "noisy"])
@pytest.mark.parametrize("flags", [(), ("-ra",), ("-rfEs",), ("-v",)])
def test_runner_stays_quiet_when_the_failures_are_named(
    tmp_path: Path, flags: tuple[str, ...], source: str
) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(source, encoding="utf-8")
    result = _run_runner(tmp_path, str(probe), *flags)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert MUTED not in result.stdout, result.stdout
    log = (tmp_path / "logs" / "group_1.log").read_text(encoding="utf-8")
    assert re.search(r"(?m)^FAILED ", log), log
