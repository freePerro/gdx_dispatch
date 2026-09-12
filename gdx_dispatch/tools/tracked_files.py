"""The set of files git tracks, without requiring the `git` binary.

Why this exists
---------------
Repo-wide guards (``tests/test_saas_surfaces_retired.py``,
``tools/tenant_plane_redundant_filter_scan.py``) answer questions about "every
file that ships". They enumerated with ``Path.rglob("*")``, which walks the
working tree — including **gitignored** paths that exist only on one machine.

Measured 2026-09-12: on a maintainer's checkout, ``gdx_dispatch/docker/demo/``
is gitignored, present locally, and absent from CI's fresh checkout. Three
guards failed locally and passed in CI on the same commit. That is worse than
a flaky test: the standing local red *masked a genuine new failure* — a
line-keyed baseline entry shifted by an edit, caught only by CI, because the
two tests that would have reported it were already red for the demo directory.

Why not `git ls-files`
----------------------
The suite runs inside the docker-app image, which does **not** ship the ``git``
binary (verified 2026-09-12), and no pure-python git library is installed
(``pathspec``, ``git``, ``dulwich``, ``gitdb`` — none available). The bind mount
does expose ``.git``, so the index itself is readable.

So this reads ``.git/index`` directly. That is the authoritative tracked set —
not a ``.gitignore`` re-implementation, which would only approximate it (a
tracked file may match an ignore pattern and still be tracked).

Known blind spot (audit, 2026-09-12)
------------------------------------
A file that is written but not yet ``git add``-ed is invisible to these scans —
including the code you are editing right now. `rglob` saw it. The exposure is
local only and bounded: CI scans a tree where everything is committed, and the
commit gate runs after `git add`. Accepted deliberately, because the alternative
is re-implementing `.gitignore` matching, which only approximates the tracked
set and is the thing this module avoids. Stated here so the next reader is not
surprised by it.

Index format (v2/v3/v4 header, v2 entries — this repo is v2):
    "DIRC" | u32 version | u32 entry-count
    entry: 40B stat fields | 20B sha1 | 2B flags | NUL-terminated path,
           padded with NULs to a multiple of 8 bytes.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_HEADER = b"DIRC"
_ENTRY_FIXED = 62  # 40 stat + 20 sha1 + 2 flags
_EXTENDED_FLAG = 0x4000


class TrackedFilesUnavailable(RuntimeError):
    """No readable git index. Callers must decide loudly, never silently."""



def _index_path(root: Path) -> Path | None:
    """Locate `.git/index`, including from inside a linked worktree.

    In a normal checkout `.git` is a directory. In a worktree created by
    `git worktree add` it is a FILE containing `gitdir: <path>`, and that
    directory holds the worktree's own index — verified 2026-09-12 by running
    these guards in a worktree, which is how CI's tracked-only tree is
    reproduced locally.
    """
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git / "index"
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = (root / target).resolve()
            return target / "index"
    return None




def read_tracked_files(repo_root: Path | None = None) -> set[str]:
    """Repo-relative POSIX paths of every file in the git index.

    Raises `TrackedFilesUnavailable` rather than returning a partial set: a
    guard that silently falls back to walking the working tree is the defect
    this module exists to remove.
    """
    root = repo_root or REPO_ROOT
    index = _index_path(root)
    if index is None or not index.is_file():
        raise TrackedFilesUnavailable(f"no readable git index under {root}")

    data = index.read_bytes()
    if data[:4] != _HEADER:
        raise TrackedFilesUnavailable("not a git index (bad signature)")
    if len(data) < 12:
        raise TrackedFilesUnavailable("git index shorter than its header")
    try:
        version, count = struct.unpack(">II", data[4:12])
    except struct.error as exc:  # malformed header — refuse, never guess
        raise TrackedFilesUnavailable(f"unreadable git index header: {exc}") from exc
    # A SPLIT index keeps most entries in a separate shared file and this one
    # holds a `link` extension plus few or no entries. Parsing it yields a
    # plausible, wrong, much smaller set — every guard would then scan almost
    # nothing and pass vacuously, and `doc_link_scan --write` would freeze an
    # empty baseline. Refuse instead. (`git update-index --split-index`.)
    # (split-index is detected after the entries — see _refuse_split_index)
    if version not in (2, 3):
        # v4 path-compresses entries; this repo is v2. Refuse rather than
        # mis-parse into a plausible-looking wrong answer.
        raise TrackedFilesUnavailable(f"unsupported git index version {version}")

    out: set[str] = set()
    pos = 12
    for _ in range(count):
        if pos + _ENTRY_FIXED > len(data):
            raise TrackedFilesUnavailable("git index truncated")
        mode = struct.unpack(">I", data[pos + 24 : pos + 28])[0]
        if (mode >> 12) == 0o4:
            # A sparse index stores whole DIRECTORIES as entries. Returning
            # those as if they were files silently under-reports the tree.
            raise TrackedFilesUnavailable(
                "sparse index (index.sparse) is not supported — run "
                "`git sparse-checkout disable` or unset index.sparse"
            )
        flags = struct.unpack(">H", data[pos + 60 : pos + 62])[0]
        name_start = pos + _ENTRY_FIXED
        if flags & _EXTENDED_FLAG:  # v3 extended flags add 2 bytes
            name_start += 2
        end = data.index(b"\x00", name_start)
        out.add(data[name_start:end].decode("utf-8", "surrogateescape"))
        entry_len = end - pos + 1
        pos += entry_len + ((8 - (entry_len % 8)) % 8)

    _refuse_split_index(data, pos)
    return out


def _refuse_split_index(data: bytes, pos: int) -> None:
    """Refuse a split index, detected by walking the extensions properly.

    A split index keeps most entries in a shared file and leaves this one with
    a `link` extension and few or no entries of its own. Parsing it yields a
    plausible, much smaller set — every guard would scan almost nothing and
    pass VACUOUSLY, and `doc_link_scan --write` would freeze an empty baseline
    past both of its "may only shrink" guards.

    Extensions are `signature(4) | size(4) | data`, running until the trailing
    20-byte checksum. Walked exactly rather than searched for: a substring
    search over the tail matches any tracked path containing "link" and would
    refuse a perfectly good index.
    """
    end = len(data) - 20  # trailing sha1 over the index
    while pos + 8 <= end:
        signature = data[pos : pos + 4]
        try:
            size = struct.unpack(">I", data[pos + 4 : pos + 8])[0]
        except struct.error:
            return
        if signature == b"link":
            raise TrackedFilesUnavailable(
                "split index (core.splitIndex) is not supported — run "
                "`git update-index --no-split-index` or unset core.splitIndex"
            )
        pos += 8 + size


def tracked_paths(repo_root: Path | None = None) -> list[Path]:
    """Absolute paths of tracked files that still exist on disk."""
    root = repo_root or REPO_ROOT
    return [p for rel in sorted(read_tracked_files(root)) if (p := root / rel).is_file()]


_CACHE: dict[Path, set[str] | None] = {}


def tracked_or_none(root: Path | None = None) -> set[str] | None:
    """Tracked paths for `root`, or None when `root` is not a git checkout.

    Three cases, deliberately distinguished:

    * a checkout with a readable index -> the tracked set;
    * a checkout whose index cannot be read -> RAISE. A silent fallback to
      walking the tree restores the defect this module exists to remove;
    * no `.git` at all -> None, and a LOUD warning on stderr. Callers then walk
      the working tree, which is correct for a test scratch directory but is
      also what happens in the shipped docker image (`.dockerignore` excludes
      `.git`) and in a tarball checkout. Those are real environments, so the
      degradation is announced rather than inferred and forgotten.

    Result is cached per root: `doc_link_scan` resolves thousands of references
    and must not re-read the index for each one.
    """
    key = (root or REPO_ROOT).resolve()
    if key in _CACHE:
        return _CACHE[key]
    try:
        value: set[str] | None = read_tracked_files(key)
    except TrackedFilesUnavailable:
        if (key / ".git").exists():
            raise
        print(
            f"WARNING: {key} has no .git — scanning the WORKING TREE, "
            "so untracked files affect the result and this run may disagree "
            "with CI. (tracked_files.tracked_or_none)",
            file=sys.stderr,
        )
        value = None
    _CACHE[key] = value
    return value
