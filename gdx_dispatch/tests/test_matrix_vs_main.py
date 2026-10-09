"""matrix_vs_main.py sorts a matrix run's reds into NEW / FLAKY / ALREADY ON MAIN.

Its whole value is that an agent can believe the label without re-proving it,
so the tests pin the cases where a wrong label costs something: a NEW failure
must never be called pre-existing, a failure with no baseline must never be
called anything but unclassified, and a captured log line must never be read
as a failing test.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "matrix_vs_main.py"
_spec = importlib.util.spec_from_file_location("matrix_vs_main", TOOL)
mvm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mvm)


class FakeRepo:
    """main's first-parent line is m2 -> m1 -> m0; the branch adds `mine` on top of m2.

    The docker-app image ships no git binary, so the tool's one git seam
    (`_git`) is replaced instead of building a real repository.
    """

    main = ["m2", "m1", "m0"]

    def __call__(self, repo, *args):
        if args[:1] == ("merge-base",):
            return "m2"
        if args[:1] == ("rev-list",):
            return "\n".join(self.main[self.main.index(args[-1]):])
        if args[:1] == ("rev-parse",):
            return {"origin/main": "m2", "origin/main~1": "m1", "origin/main~2": "m0", "HEAD": "mine"}[args[1]]
        raise AssertionError(args)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    fake = FakeRepo()
    monkeypatch.setattr(mvm, "_git", fake)
    return tmp_path


def _git(repo, *args):
    return mvm._git(repo, *args)


def _logs(tmp_path: Path, *lines: str) -> Path:
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    (d / "group_1.log").write_text("\n".join(lines) + "\n3 failed, 10 passed in 1.0s\n")
    return d


def _baseline(monkeypatch, tmp_path: Path, sha: str, *tests: str) -> None:
    d = tmp_path / "baselines"
    d.mkdir(exist_ok=True)
    monkeypatch.setenv("MATRIX_BASELINE_DIR", str(d))
    (d / f"{sha}.txt").write_text("# main x\n# recorded 2026-10-09\n" + "".join(t + "\n" for t in tests))


def test_summary_lines_are_parsed_and_captured_log_lines_are_not(tmp_path):
    logs = _logs(tmp_path,
                 "ERROR    gdx_dispatch.core.modules:modules.py:419 user_role_lookup_failed",
                 "FAILED gdx_dispatch/tests/test_a.py::test_one - AssertionError: 1 != 2",
                 "FAILED gdx_dispatch/tests/test_a.py::test_p[a b] - boom",
                 "ERROR gdx_dispatch/tests/test_broken.py",
                 "ERROR gdx_dispatch/tests/test_b.py::test_fx - fixture error")
    assert mvm.failures_in_logs(logs) == {
        "gdx_dispatch/tests/test_a.py::test_one",
        "gdx_dispatch/tests/test_a.py::test_p[a b]",
        "gdx_dispatch/tests/test_broken.py",
        "gdx_dispatch/tests/test_b.py::test_fx",
    }


def test_new_flaky_and_preexisting_are_told_apart(tmp_path, repo, monkeypatch, capsys):
    tip = _git(repo, "rev-parse", "origin/main")
    older = _git(repo, "rev-parse", "origin/main~1")
    _baseline(monkeypatch, tmp_path, tip, "t.py::known")
    (tmp_path / "baselines" / f"{older}.txt").write_text("t.py::sometimes\n")
    logs = _logs(tmp_path, "FAILED t.py::known - x", "FAILED t.py::sometimes - x", "FAILED t.py::mine - x")
    assert mvm.main(["compare", "--logs", str(logs), "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    new = out.split("NEW")[1].split("FLAKY")[0]
    assert "t.py::mine" in new and "t.py::known" not in new and "t.py::sometimes" not in new
    assert "t.py::sometimes" in out.split("FLAKY ON MAIN")[1].split("ALREADY")[0]
    assert "t.py::known" in out.split("ALREADY FAILING ON MAIN")[1]
    assert "1 NEW failure(s)" in out


def test_all_preexisting_says_no_new_failures(tmp_path, repo, monkeypatch, capsys):
    _baseline(monkeypatch, tmp_path, _git(repo, "rev-parse", "origin/main"), "t.py::known")
    mvm.main(["compare", "--logs", str(_logs(tmp_path, "FAILED t.py::known - x")), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "NO NEW FAILURES" in out and "✗ NEW" not in out


def test_falls_back_to_an_older_baseline_and_says_how_old(tmp_path, repo, monkeypatch, capsys):
    _baseline(monkeypatch, tmp_path, _git(repo, "rev-parse", "origin/main~2"), "t.py::known")
    mvm.main(["compare", "--logs", str(_logs(tmp_path, "FAILED t.py::known - x")), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "2 commit(s) older than your merge-base" in out
    # A stale baseline must never clear a red outright: main may have fixed the
    # test since and this branch broken it again.
    assert "WAS RED ON MAIN 2 commit(s)" in out and "NOT proven" in out
    assert "ALREADY FAILING ON MAIN" not in out and "NO NEW FAILURES" not in out


def test_no_baseline_is_unclassified_never_pre_existing(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("MATRIX_BASELINE_DIR", str(tmp_path / "empty"))
    mvm.main(["compare", "--logs", str(_logs(tmp_path, "FAILED t.py::x - y")), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "No main baseline found" in out and "? t.py::x" in out
    assert "ALREADY FAILING" not in out and "VERDICT" not in out


def test_a_baseline_on_the_feature_branch_is_never_used(tmp_path, repo, monkeypatch, capsys):
    # Only main's first-parent line counts: a file keyed to the branch's own
    # commit must not launder the branch's failures into "pre-existing".
    _baseline(monkeypatch, tmp_path, _git(repo, "rev-parse", "HEAD"), "t.py::mine")
    mvm.main(["compare", "--logs", str(_logs(tmp_path, "FAILED t.py::mine - x")), "--repo", str(repo)])
    assert "No main baseline found" in capsys.readouterr().out


def test_record_then_compare_round_trips(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("MATRIX_BASELINE_DIR", str(tmp_path / "b"))
    tip = _git(repo, "rev-parse", "origin/main")
    main_logs = tmp_path / "main_logs"
    main_logs.mkdir()
    (main_logs / "group_1.log").write_text("FAILED t.py::known - x\n1 failed in 1s\n")
    assert mvm.main(["record", "--logs", str(main_logs), "--sha", tip, "--image", "sha256:abc"]) == 0
    assert mvm.read_baseline(tmp_path / "b" / f"{tip}.txt") == {"t.py::known"}
    mvm.main(["compare", "--logs", str(_logs(tmp_path, "FAILED t.py::known - x")),
              "--repo", str(repo), "--image", "sha256:def"])
    out = capsys.readouterr().out
    assert "NO NEW FAILURES" in out and "an image change can move failures" in out


def test_compare_never_fails_the_matrix(tmp_path, capsys):
    # Not a git repo, no baselines: still exit 0, still prints something.
    assert mvm.main(["compare", "--logs", str(_logs(tmp_path, "FAILED t.py::x - y")),
                     "--repo", str(tmp_path)]) == 0


def test_a_crashed_shard_is_never_read_as_green(tmp_path, repo, monkeypatch, capsys):
    monkeypatch.setenv("MATRIX_BASELINE_DIR", str(tmp_path / "b"))
    logs = tmp_path / "crash"
    logs.mkdir()
    (logs / "group_1.log").write_text("....\n")  # killed mid-run: no summary line
    (logs / "group_2.log").write_text("5 passed in 1.0s\n")
    assert mvm.main(["record", "--logs", str(logs), "--sha", "m2"]) == 2
    assert not (tmp_path / "b" / "m2.txt").exists()
    mvm.main(["compare", "--logs", str(logs), "--repo", str(repo)])
    assert "This is NOT a pass" in capsys.readouterr().out


def test_a_dead_shard_beside_a_known_red_is_not_cleared(tmp_path, repo, monkeypatch, capsys):
    # One shard names only a red main already has, another dies silently: the
    # verdict must not say "no new failures" over tests that never finished.
    _baseline(monkeypatch, tmp_path, _git(repo, "rev-parse", "origin/main"), "t.py::known")
    logs = _logs(tmp_path, "FAILED t.py::known - x")
    (logs / "group_2.log").write_text("....Segmentation fault\n")
    mvm.main(["compare", "--logs", str(logs), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "group_2.log did not run all its tests" in out and "NOT CLEARED" in out
    assert "NO NEW FAILURES" not in out


# Verbatim tail of a real pytest-split shard whose collection failed (docker-app,
# 2026-10-09): the session stops before any test runs, yet ends on a summary line.
_INTERRUPTED = """==================================== ERRORS ====================================
_______________________ ERROR collecting t/test_bad.py _______________________
ImportError while importing test module '/app/t/test_bad.py'.
=========================== short test summary info ============================
ERROR t/test_bad.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 deselected, 1 error in 0.10s
"""


def test_a_collection_error_that_main_shares_clears_nothing(tmp_path, repo, monkeypatch, capsys):
    # Every shard collects the whole suite, so one import error stops all of
    # them: main's baseline would hold that error, and matching it must not
    # tell an agent "no new failures" about a run in which nothing ran.
    monkeypatch.setenv("MATRIX_BASELINE_DIR", str(tmp_path / "b"))
    logs = tmp_path / "interrupted"
    logs.mkdir()
    (logs / "group_1.log").write_text(_INTERRUPTED)
    assert mvm.main(["record", "--logs", str(logs), "--sha", "m2"]) == 2
    _baseline(monkeypatch, tmp_path, "m2", "t/test_bad.py")
    mvm.main(["compare", "--logs", str(logs), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "NOT CLEARED" in out and "NO NEW FAILURES" not in out


def test_an_empty_shard_is_finished_not_dead(tmp_path, repo, monkeypatch, capsys):
    # A narrowed run with fewer tests than shards leaves some shards empty
    # (pytest exit 5, which run_tests_split.sh accepts): verbatim real tail.
    _baseline(monkeypatch, tmp_path, _git(repo, "rev-parse", "origin/main"), "t.py::known")
    logs = _logs(tmp_path, "FAILED t.py::known - x")
    (logs / "group_2.log").write_text("1 deselected in 0.01s\n")
    assert mvm.unfinished_shards(logs) == []
    mvm.main(["compare", "--logs", str(logs), "--repo", str(repo)])
    assert "NO NEW FAILURES" in capsys.readouterr().out


def test_a_maxfail_stop_is_not_a_finished_shard(tmp_path, repo, monkeypatch, capsys):
    # -x reaches every shard through run_tests_split.sh's "$@": the first red
    # stops the session, so a known red must not clear the untested rest.
    _baseline(monkeypatch, tmp_path, _git(repo, "rev-parse", "origin/main"), "t.py::known")
    logs = tmp_path / "x"
    logs.mkdir()
    (logs / "group_1.log").write_text(
        "FAILED t.py::known - assert 0\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "1 failed in 0.01s\n")
    assert mvm.unfinished_shards(logs) == ["group_1.log"]
    mvm.main(["compare", "--logs", str(logs), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "NOT CLEARED" in out and "NO NEW FAILURES" not in out
