"""Guard: ``tools/ruff_noqa_guard.py`` fails on an unaccepted invalid noqa directive.

ruff warns ``Invalid # noqa directive`` on a code outside its grammar
(uppercase letters, then digits) and drops every ruff code after it on the
line. The ruff ratchet never counts warnings, so ``silent-failure`` sat on
five lines unnoticed (GDXA-1892). The guard runs in CI's lint job; these tests
prove it can fail, and fail closed, without needing ruff in the test image.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from gdx_dispatch.tools import ruff_noqa_guard as guard

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_parses_ruffs_verbatim_warning():
    out = (
        guard._SAMPLE + "\n"
        "warning: Invalid `# noqa` directive on gdx_dispatch/routers/tags.py:393: expected a "
        "comma-separated list of codes (e.g., `# noqa: F401, F841`).\n"
        "warning: Invalid `# noqa` directive on gdx_dispatch/routers/tags.py:457: expected a "
        "comma-separated list of codes (e.g., `# noqa: F401, F841`).\n"
        "All checks passed!\n"
    )
    assert guard.parse_warnings(out) == Counter({"gdx_dispatch/routers/tags.py": 2, "gdx_dispatch/x.py": 1})


def test_snippet_lines_are_not_warnings():
    """A diagnostic snippet quoting such a line carries a gutter, not column 0."""
    out = "116 | warning: Invalid `# noqa` directive on a.py:1: x\n"
    assert guard.parse_warnings(out) == Counter()


def test_compare_exact_match_passes():
    assert guard.compare(Counter({"a.py": 2}), Counter({"a.py": 2})) == []


def test_compare_fails_on_growth_and_on_unlisted_file():
    problems = guard.compare(Counter({"a.py": 3, "b.py": 1}), Counter({"a.py": 2}))
    assert len(problems) == 2
    assert any(p.startswith("a.py: 3") for p in problems)
    assert any(p.startswith("b.py: 1") for p in problems)


def test_compare_fails_on_stale_allowance():
    """A converted site whose line stays in the pending file leaves room for the next one."""
    problems = guard.compare(Counter(), Counter({"a.py": 1}))
    assert len(problems) == 1 and "Lower or delete" in problems[0]


@pytest.mark.parametrize("bad", ["x a.py", "0 a.py", "1", "1 a.py extra", "1 a.py\n2 a.py"])
def test_pending_file_rejects_malformed_lines(bad):
    with pytest.raises(ValueError):
        guard.parse_pending(bad)


def test_real_pending_file_parses_and_names_real_files():
    pending = guard.parse_pending(guard.PENDING_FILE.read_text(encoding="utf-8"))
    for path in pending:
        assert (REPO_ROOT / path).is_file(), path


def _stub_ruff(bin_dir: Path, stdout: str, rc: int) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "ruff"
    stub.write_text(f"#!/usr/bin/env bash\ncat <<'OUT'\n{stdout}\nOUT\nexit {rc}\n", encoding="utf-8")
    stub.chmod(0o755)


def _run_guard(path_env: str, pending: str, target: str) -> subprocess.CompletedProcess:
    """Run the real script with its pending file at ``pending``, scanning ``target``."""
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from gdx_dispatch.tools import ruff_noqa_guard as g; "
         "from pathlib import Path; g.PENDING_FILE = Path(sys.argv[1]); sys.exit(g.main(['x', sys.argv[2]]))",
         pending, target],
        cwd=REPO_ROOT,
        env={"PATH": path_env, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("stdout", "rc", "want"),
    [
        ("All checks passed!", 0, 0),
        ("warning: Invalid `# noqa` directive on a.py:4: expected x\nAll checks passed!", 0, 1),
        ("error: Failed to parse pyproject.toml", 2, 2),
        ("warning: No Python files found under the given path(s)\nAll checks passed!", 0, 2),
    ],
)
def test_main_exit_codes(tmp_path, stdout, rc, want):
    _stub_ruff(tmp_path / "bin", stdout, rc)
    pending = tmp_path / "pending"
    pending.write_text("", encoding="utf-8")
    proc = _run_guard(f"{tmp_path / 'bin'}:/usr/bin:/bin", str(pending), str(tmp_path / "bin"))
    assert proc.returncode == want, proc.stdout + proc.stderr


def test_main_fails_closed_without_ruff(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    pending = tmp_path / "pending"
    pending.write_text("", encoding="utf-8")
    proc = _run_guard(str(empty), str(pending), str(empty))
    assert proc.returncode == 2 and "not on PATH" in proc.stdout


def test_legacy_marker_counts_after_a_valid_code_and_only_in_comments(tmp_path):
    """ruff is silent on ``BLE001`` then ``silent-failure``; the guard is not.
    A string holding the marker (as the scanners' tests do) is not a site."""
    f = tmp_path / "m.py"
    f.write_text(
        "try:\n    x = 1\nexcept Exception:  # noqa: BLE001  # noqa: silent-failure — logged\n    pass\n"
        "MARKER = '# noqa: silent-failure'\n"
        "y = 1  # noqa: SILENTFAIL1\n"
        "z = 1  # NOQA: BLE001, Silent-Failure\n",
        encoding="utf-8",
    )
    assert {line for _, line in guard.legacy_marker_sites(tmp_path)} == {3, 7}


def test_a_site_caught_both_ways_counts_once(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 1  # noqa: silent-failure\n", encoding="utf-8")
    ruff_out = f"warning: Invalid `# noqa` directive on {f}:1: expected x\n"
    sites = guard.parse_warning_sites(ruff_out) | guard.legacy_marker_sites(f)
    assert guard.per_file(sites) == Counter({str(f): 1})


def test_legacy_marker_in_the_tree_fails_main(tmp_path):
    """ruff (stubbed) reports nothing; the comment alone must fail the run."""
    _stub_ruff(tmp_path / "bin", "All checks passed!", 0)
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text("x = 1  # noqa: E501  # noqa: silent-failure\n", encoding="utf-8")
    pending = tmp_path / "pending"
    pending.write_text("", encoding="utf-8")
    proc = _run_guard(f"{tmp_path / 'bin'}:/usr/bin:/bin", str(pending), str(src))
    assert proc.returncode == 1, proc.stdout + proc.stderr


def test_untokenizable_file_fails_closed(tmp_path):
    (tmp_path / "bad.py").write_text('x = """never closed\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot tokenize"):
        guard.legacy_marker_sites(tmp_path)


def test_ci_lint_job_runs_the_guard():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lint = ci.split("\n  test:", 1)[0]
    assert "python3 gdx_dispatch/tools/ruff_noqa_guard.py" in lint


# The two below need the real ruff. The docker-app image has none, so they
# skip there; CI's lint job runs the guard itself on the whole tree.
_needs_ruff = pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed (docker-app image)")


@_needs_ruff
def test_real_ruff_still_warns_on_the_legacy_marker_and_not_the_new_one(tmp_path):
    """If ruff stopped warning, the guard would be measuring nothing."""
    f = tmp_path / "m.py"
    f.write_text(
        "try:\n    x = 1\nexcept ValueError:  # noqa: silent-failure — legacy\n    pass\n"
        "try:\n    y = 1\nexcept ValueError:  # noqa: SILENTFAIL1 — new\n    pass\n",
        encoding="utf-8",
    )
    out = guard.run_ruff(str(f))
    found = guard.parse_warnings(out)
    assert sum(found.values()) == 1, out
    assert ":3:" in out and ":7:" not in out


@_needs_ruff
def test_tree_matches_the_pending_list():
    assert guard.main(["x"]) == 0
