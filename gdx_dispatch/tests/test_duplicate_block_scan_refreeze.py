"""The gate `.duplicate_block_baseline` never had (GDXA-83).

`duplicate_block_scan.py`'s docstring claimed the expand-contract pattern of
`tenant_plane_redundant_filter_scan.py` — "gate fails only on NET-NEW duplicate
hashes". There was no gate. No test and no CI step read the committed baseline,
so in three months it drifted to 2882 dead hashes (68% of the file) and 1382
net-new groups without ever going red. Deleting `.duplicate_block_baseline`
outright left all 26 tests in `test_lint_gates.py` green, because both of its
duplicate-block integration tests go through `_setup_duplicate_block_scratch`,
which monkeypatches `BASELINE_FILE` at a `tmp_path`.

This file is deliberately NOT `test_lint_gates.py`: nothing here patches
`REPO_ROOT`, `SCAN_ROOTS` or `BASELINE_FILE` at module scope, so the scratch
setup over there can never swallow the real-tree assertion below.

Two halves, and both are load-bearing:

* `test_the_committed_baseline_is_what_the_tree_scans` reads the shipped
  baseline unpatched and demands exact equality in both directions;
* the CLI tests prove `--baseline` REFUSES to add a net-new hash. Without the
  refusal the guard is theatre — the test goes red, the author reaches for
  `--baseline`, the new clone is blessed in silence, and the ratchet still
  cannot fail for the defect it exists to catch (CLAUDE.md).

What this guard still cannot see
--------------------------------
The scan takes its file LIST from `.git/index` and its CONTENT from the working
tree, so a clone pasted into a file that has not been `git add`-ed yet is
invisible: green here, red at the commit gate or in CI once the file is staged.
Measured, not assumed — a verbatim copy in an untracked `gdx_dispatch/core/*.py`
scanned as `added 0` (audit, 2026-09-27). Inherited from
`tracked_files.tracked_or_none`, which accepts it deliberately and explains why;
shared by every tracked-set guard in the repo. Stated here so nobody reads this
file as a stronger promise than it is.

Also inherited: with no readable git index this errors rather than skipping (the
`tracked_files` contract). In a linked worktree run the suite through
`gdx_dispatch/tools/run_tests_split.sh`, which mounts the gitdir into the docker
`PYTEST`; a bare `docker run -v $PWD:/app` cannot see it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from gdx_dispatch.tools import duplicate_block_scan as scanner


def _group(*locs: tuple[str, int]) -> list[tuple[Path, int]]:
    return [(scanner.REPO_ROOT / rel, lineno) for rel, lineno in locs]


_SCANNED: dict[str, list[tuple[Path, int]]] | None = None


def _real_tree_scan() -> dict[str, list[tuple[Path, int]]]:
    """One real scan for the whole module — it walks ~600 files (~1.5 s)."""
    global _SCANNED
    if _SCANNED is None:
        _SCANNED = scanner.scan()
    return _SCANNED


# ── the real tree ────────────────────────────────────────────────────────────


def test_the_baseline_this_file_reads_is_the_committed_one() -> None:
    """Canary against the failure mode that hid the defect for three months.

    If a future edit gives this module a scratch `BASELINE_FILE` — by fixture,
    by conftest, by import order — the assertion below stops being about the
    repo and the guard silently evaporates. Assert the path itself.
    """
    assert scanner.BASELINE_FILE == scanner.REPO_ROOT / ".duplicate_block_baseline"
    assert scanner.SCAN_ROOTS == [scanner.REPO_ROOT / "gdx_dispatch"]


def _drift(baseline: set[str], groups) -> tuple[list[str], list[str]]:
    """The guard's oracle, as one function both it and its falsifier call.

    An earlier cut had the guard do inline set arithmetic while
    `test_the_real_tree_oracle_can_actually_fail` exercised
    `_hashes_beyond_baseline` — so the "proof it can fail" was about a function
    the guard never calls (audit, 2026-09-27). One oracle, two callers.
    """
    return scanner._hashes_beyond_baseline(baseline, groups), sorted(
        baseline - set(groups)
    )


def test_the_committed_baseline_is_what_the_tree_scans() -> None:
    """Zero slack, both directions, on the file that ships with this tree."""
    assert scanner.BASELINE_FILE.exists(), (
        f"{scanner.BASELINE_FILE.name} is missing — freeze it with "
        "`python -m gdx_dispatch.tools.duplicate_block_scan --baseline`"
    )
    added, removed = _drift(scanner._load_baseline(), _real_tree_scan())
    assert not added and not removed, (
        f"{len(added)} net-new duplicate group(s), {len(removed)} dead baseline "
        "hash(es).\n"
        "  One rule: `python -m gdx_dispatch.tools.duplicate_block_scan "
        "--baseline` re-freezes anything that only SHRANK and refuses anything "
        "that GREW.\n"
        "  If it refuses: extract the shared block, or bless the new clones on "
        "purpose with `--baseline --allow-new`. That is also the fix for a MIXED "
        "add+remove tree — `--prune` drops the dead hashes but adds none, so it "
        "leaves this test red.\n"
        f"  added: {added[:5]}\n  dead: {removed[:5]}"
    )


def test_the_real_tree_oracle_can_actually_fail() -> None:
    """Name the input that turns the guard above red, and prove it does.

    A green ratchet proves nothing unless it can fail for the defect. The
    defect is "the committed baseline disagrees with the tree", so run the
    guard's own oracle against a baseline one hash short in each direction —
    without touching the committed file.
    """
    groups = _real_tree_scan()
    assert groups, "an empty scan would make the guard vacuous"
    assert scanner._load_baseline(), "an empty baseline would make the guard vacuous"

    hashes = sorted(groups)
    missing_one = set(hashes[1:])
    added, removed = _drift(missing_one, groups)
    assert len(added) == 1 and not removed, "a clone the baseline lacks must show as added"

    extra_one = set(hashes) | {"f" * 16}
    added, removed = _drift(extra_one, groups)
    assert not added and removed == ["f" * 16], "a dead baseline hash must show as removed"


# ── the refusal, as arithmetic ───────────────────────────────────────────────


def test_a_subset_is_never_beyond() -> None:
    """Removing a clone is not something a re-freeze needs permission for."""
    groups = {"aaa": _group(("gdx_dispatch/core/x.py", 10))}
    assert scanner._hashes_beyond_baseline({"aaa", "bbb", "ccc"}, groups) == []


def test_a_net_new_hash_is_refused_and_named() -> None:
    groups = {"bbb": _group(("gdx_dispatch/core/x.py", 42), ("gdx_dispatch/core/y.py", 7))}
    assert scanner._hashes_beyond_baseline({"aaa"}, groups) == [
        "bbb: 2 copies, first at gdx_dispatch/core/x.py:42"
    ]


def test_every_net_new_hash_is_listed_not_just_the_first() -> None:
    groups = {
        "bbb": _group(("gdx_dispatch/core/x.py", 1)),
        "ccc": _group(("gdx_dispatch/core/y.py", 2)),
    }
    assert len(scanner._hashes_beyond_baseline(set(), groups)) == 2


# ── the refusal, as the CLI ──────────────────────────────────────────────────
# The arithmetic above stays green if someone strips the refusal out of main():
# that is exactly how the tenant-plane twin was found under-tested (audit,
# 2026-09-06). Drive main() itself.


def _run(monkeypatch, tmp_path, groups, old_baseline, argv):
    baseline = tmp_path / "baseline.json"
    if old_baseline is not None:
        baseline.write_text(json.dumps(sorted(old_baseline), indent=2) + "\n")
    monkeypatch.setattr(scanner, "BASELINE_FILE", baseline)
    monkeypatch.setattr(scanner, "scan", lambda: groups)
    monkeypatch.setattr(sys, "argv", ["scan", *argv])
    rc = scanner.main()
    written = set(json.loads(baseline.read_text())) if baseline.exists() else None
    return rc, written, baseline


def test_cli_baseline_refuses_a_net_new_clone(monkeypatch, tmp_path, capsys):
    groups = {
        "aaa": _group(("gdx_dispatch/core/x.py", 10)),
        "bbb": _group(("gdx_dispatch/routers/jobs.py", 99), ("gdx_dispatch/routers/jobs.py", 140)),
    }
    rc, written, path = _run(monkeypatch, tmp_path, groups, {"aaa"}, ["--baseline"])
    assert rc == 2
    assert written == {"aaa"}, "a refused re-freeze leaves the baseline byte-for-byte alone"
    out = capsys.readouterr().out
    assert "refusing to re-freeze" in out
    assert "bbb: 2 copies, first at gdx_dispatch/routers/jobs.py:99" in out


def test_cli_baseline_admits_a_pure_shrink(monkeypatch, tmp_path):
    """A clone that was extracted needs no --allow-new."""
    groups = {"aaa": _group(("gdx_dispatch/core/x.py", 10))}
    rc, written, _ = _run(monkeypatch, tmp_path, groups, {"aaa", "bbb"}, ["--baseline"])
    assert rc == 0
    assert written == {"aaa"}


def test_cli_allow_new_is_the_only_way_past(monkeypatch, tmp_path):
    groups = {
        "aaa": _group(("gdx_dispatch/core/x.py", 10)),
        "bbb": _group(("gdx_dispatch/core/y.py", 3)),
    }
    rc, written, _ = _run(monkeypatch, tmp_path, groups, {"aaa"}, ["--baseline", "--allow-new"])
    assert rc == 0
    assert written == {"aaa", "bbb"}


def test_cli_first_freeze_needs_no_permission(monkeypatch, tmp_path):
    """No baseline on disk yet is how the file is born, not a smuggled clone."""
    groups = {"aaa": _group(("gdx_dispatch/core/x.py", 10))}
    rc, written, _ = _run(monkeypatch, tmp_path, groups, None, ["--baseline"])
    assert rc == 0
    assert written == {"aaa"}


def test_cli_a_mixed_add_and_remove_tree_needs_allow_new(monkeypatch, tmp_path, capsys):
    """The case the first cut of this fix mis-documented (audit, 2026-09-27).

    1 of the 5 commits before this one added a clone AND killed dead hashes in
    the same breath (`0343b157`: 1 added, 9 dead). `--prune` alone leaves the
    refreeze guard red there, because it adds nothing.
    """
    groups = {
        "aaa": _group(("gdx_dispatch/core/x.py", 10)),
        "bbb": _group(("gdx_dispatch/core/y.py", 3)),
    }
    old = {"aaa", "dead1", "dead2"}

    rc, written, _ = _run(monkeypatch, tmp_path, groups, old, ["--baseline"])
    assert rc == 2 and written == old, "a mixed tree is refused, not half-applied"

    rc, written, _ = _run(monkeypatch, tmp_path, groups, old, ["--prune"])
    assert rc == 0
    assert written == {"aaa"}, "prune drops the dead hashes and adds none"
    assert set(groups) != written, "so the refreeze guard would STILL be red"

    rc, written, _ = _run(monkeypatch, tmp_path, groups, old, ["--baseline", "--allow-new"])
    assert rc == 0
    assert written == set(groups), "--allow-new is the only one-shot fix"
    capsys.readouterr()


def test_cli_prune_only_shrinks(monkeypatch, tmp_path, capsys):
    """--prune is the shrink-only path: it drops dead hashes and adds nothing."""
    groups = {
        "aaa": _group(("gdx_dispatch/core/x.py", 10)),
        "bbb": _group(("gdx_dispatch/core/y.py", 3)),
    }
    rc, written, _ = _run(monkeypatch, tmp_path, groups, {"aaa", "dead"}, ["--prune"])
    assert rc == 0
    assert written == {"aaa"}, "prune must not admit 'bbb' — only --allow-new does that"
    assert "Pruned 1 stale hash(es); kept 1." in capsys.readouterr().out


def test_cli_strict_still_fails_on_net_new(monkeypatch, tmp_path, capsys):
    """The reporting gate and the re-freeze gate are separate; keep both honest."""
    groups = {
        "aaa": _group(("gdx_dispatch/core/x.py", 10)),
        "bbb": _group(("gdx_dispatch/core/y.py", 3)),
    }
    rc, _, _ = _run(monkeypatch, tmp_path, groups, {"aaa"}, ["--strict", "--limit", "0"])
    assert rc == 1
    assert "1 net-new duplicate-block group(s)" in capsys.readouterr().out
