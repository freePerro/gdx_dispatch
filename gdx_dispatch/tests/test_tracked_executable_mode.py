"""A tracked file meant to be executed is tracked executable (mode 100755).

`gdx_dispatch/docker/entrypoint.sh` was tracked 100644 (GDXA-993). Both
Dockerfiles `chmod +x` it at build, so the image worked; the hot-reload overlay's
bind mount (`docker-compose.reload.yml`: `../../gdx_dispatch:/app/gdx_dispatch:ro`)
hides that layer, and the container died with
`exec: "/app/gdx_dispatch/docker/entrypoint.sh": permission denied`. The same
happens to any shebang script run as `./script` from a fresh clone.

The mode is read from the git index, not from disk: the index is what a clone
or a bind mount of a clean checkout gets, and a maintainer's local `chmod +x`
never reaches it unless it is committed.
"""

from __future__ import annotations

import re
from pathlib import Path

from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_modes

_REGULAR = 0o100644
_EXECUTABLE = 0o100755

_DOCKERFILES = (
    "gdx_dispatch/docker/Dockerfile",
    "gdx_dispatch/docker/Dockerfile.plugin-host",
)
# `/app/<path>` inside an ENTRYPOINT or a `chmod` line. Both Dockerfiles copy
# the repo to /app, so the suffix is the repo-relative path.
_APP_PATH = re.compile(r"/app/([\w./-]+)")


def _starts_with_shebang(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"#!"


def shebang_files_not_executable(modes: dict[str, int], root: Path) -> list[str]:
    """Tracked regular, non-executable files whose first bytes are `#!`."""
    return sorted(
        rel
        for rel, mode in modes.items()
        if mode == _REGULAR and (p := root / rel).is_file() and _starts_with_shebang(p)
    )


def dockerfile_exec_targets(root: Path) -> set[str]:
    """Repo paths a Dockerfile runs as ENTRYPOINT or marks `chmod +x`."""
    targets: set[str] = set()
    for rel in _DOCKERFILES:
        for line in (root / rel).read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("ENTRYPOINT") or "chmod" in line:
                targets.update(_APP_PATH.findall(line))
    return targets


# ── the reader reports modes correctly (otherwise the guards are vacuous) ──


def test_reader_reports_known_modes():
    modes = read_tracked_modes()
    assert modes["gdx_dispatch/tools/run_tests_split.sh"] == _EXECUTABLE
    assert modes["gdx_dispatch/app.py"] == _REGULAR


def test_detector_flags_a_non_executable_shebang_file(tmp_path):
    """The guard below must be able to fail for the GDXA-993 shape."""
    (tmp_path / "run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (tmp_path / "ok.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (tmp_path / "lib.py").write_bytes(b"x = 1\n")
    modes = {"run.sh": _REGULAR, "ok.sh": _EXECUTABLE, "lib.py": _REGULAR}
    assert shebang_files_not_executable(modes, tmp_path) == ["run.sh"]


# ── the guards ────────────────────────────────────────────────────────────


def test_every_tracked_shebang_file_is_executable():
    bad = shebang_files_not_executable(read_tracked_modes(), REPO_ROOT)
    assert not bad, (
        "tracked with a shebang but mode 100644 — a fresh clone or a bind "
        "mount cannot exec them. Fix with "
        "`git update-index --chmod=+x <path>` (or drop the shebang if the "
        f"file is never run directly): {bad}"
    )


def test_dockerfile_entrypoint_and_chmod_targets_are_tracked_executable():
    targets = dockerfile_exec_targets(REPO_ROOT)
    assert "gdx_dispatch/docker/entrypoint.sh" in targets, (
        "Dockerfile parse found no entrypoint — the check below would pass vacuously"
    )
    modes = read_tracked_modes()
    bad = {t: oct(modes.get(t, 0)) for t in sorted(targets) if modes.get(t) != _EXECUTABLE}
    assert not bad, f"Dockerfile exec targets not tracked 100755: {bad}"
