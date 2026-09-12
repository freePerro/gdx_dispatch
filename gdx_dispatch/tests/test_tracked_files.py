"""Guards for `tools/tracked_files.py` — the git-index reader the repo-wide
scans use instead of walking the working tree.

It replaced `rglob` in three guards after gitignored local files
(`gdx_dispatch/docker/demo/`) made them red locally and green in CI on the same
commit — a standing red that went on to mask a real regression (2026-09-12).
If this reader is wrong, those guards are wrong, so it gets its own tests.
"""

from __future__ import annotations

import shutil
import struct

import pytest

from gdx_dispatch.tools.tracked_files import (
    REPO_ROOT,
    TrackedFilesUnavailable,
    read_tracked_files,
    tracked_paths,
)


def test_reads_this_repos_index_and_finds_known_tracked_files():
    tracked = read_tracked_files()
    assert len(tracked) > 1000, "implausibly small tracked set — parser is likely wrong"
    # Files that are certainly tracked, one per shape (package, test, dotfile).
    assert "gdx_dispatch/app.py" in tracked
    assert "gdx_dispatch/tests/test_tracked_files.py" in tracked
    assert "gdx_dispatch/tools/tracked_files.py" in tracked


def test_excludes_gitignored_paths():
    """The failure this whole change exists to remove.

    `gdx_dispatch/docker/demo/` is gitignored (.gitignore) and present on a
    maintainer's checkout. It must never appear, whether or not it exists on
    the machine running this.
    """
    tracked = read_tracked_files()
    leaked = sorted(p for p in tracked if p.startswith("gdx_dispatch/docker/demo/"))
    assert not leaked, f"gitignored paths in the tracked set: {leaked}"


def test_every_returned_path_is_repo_relative_and_posix():
    tracked = read_tracked_files()
    bad = [p for p in tracked if p.startswith("/") or "\\" in p]
    assert not bad, f"non-relative or non-posix entries: {bad[:5]}"


def test_tracked_paths_returns_existing_files_only():
    paths = tracked_paths()
    assert paths, "no tracked files resolved on disk"
    assert all(p.is_file() for p in paths)
    assert all(p.is_absolute() for p in paths)


# ── failure modes: refuse, never approximate ──────────────────────────────


def test_missing_index_raises_rather_than_falling_back(tmp_path):
    """A silent fallback to walking the tree would restore the original defect."""
    with pytest.raises(TrackedFilesUnavailable):
        read_tracked_files(tmp_path)


def test_bad_signature_raises(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(b"NOPE" + b"\x00" * 32)
    with pytest.raises(TrackedFilesUnavailable):
        read_tracked_files(tmp_path)


def test_unsupported_index_version_raises_rather_than_misparsing(tmp_path):
    """v4 path-compresses entries. Mis-parsing it would yield a plausible but
    wrong file list, which is worse than refusing."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(b"DIRC" + struct.pack(">II", 4, 0))
    with pytest.raises(TrackedFilesUnavailable):
        read_tracked_files(tmp_path)


def test_truncated_index_raises(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(b"DIRC" + struct.pack(">II", 2, 5) + b"\x00" * 10)
    with pytest.raises(TrackedFilesUnavailable):
        read_tracked_files(tmp_path)


def test_worktree_gitdir_file_is_followed(tmp_path):
    """`git worktree add` writes `.git` as a FILE containing `gitdir: <path>`.

    Reproducing CI's tracked-only tree locally is done with a worktree, so this
    path is exercised in practice, not hypothetically.
    """
    real_gitdir = REPO_ROOT / ".git"
    if not real_gitdir.is_dir():  # this checkout is itself a worktree
        pytest.skip("REPO_ROOT is not a normal checkout; nothing to point at")

    # Reproduce the REAL layout, not a shortcut to the main gitdir: git puts a
    # linked worktree's index at .git/worktrees/<name>/index, and the `.git`
    # file points at that directory. An earlier version of this test pointed
    # straight at the main gitdir and so never exercised the nested path.
    linked = tmp_path / "gitdir_home" / "worktrees" / "wt"
    linked.mkdir(parents=True)
    shutil.copy2(real_gitdir / "index", linked / "index")

    fake = tmp_path / "wt"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {linked}\n", encoding="utf-8")

    assert read_tracked_files(fake) == read_tracked_files(REPO_ROOT)


def test_relative_gitdir_pointer_is_resolved(tmp_path):
    """`gitdir:` may be a RELATIVE path; resolve it against the worktree."""
    real_gitdir = REPO_ROOT / ".git"
    if not real_gitdir.is_dir():
        pytest.skip("REPO_ROOT is not a normal checkout")
    fake = tmp_path / "wt"
    (fake / "gd").mkdir(parents=True)
    shutil.copy2(real_gitdir / "index", fake / "gd" / "index")
    (fake / ".git").write_text("gitdir: gd\n", encoding="utf-8")

    assert read_tracked_files(fake) == read_tracked_files(REPO_ROOT)
