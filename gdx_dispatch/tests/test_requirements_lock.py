"""The committed locks are what every installer reads, and they satisfy the ranges (GDXA-467).

requirements.txt states ranges; the images and CI used to resolve them fresh on
every build, so what shipped was unrecorded and changed under us (GDXA-14:
SQLAlchemy 2.1 arrived under a ``<3.0`` ceiling and killed every CI shard).
These tests keep the lock honest in both directions:

* the committed tree: each lock pins every line with ``==`` and satisfies its
  requirements file, the two locks agree outside the playwright closure, the
  playwright pin matches the plugin-host base image, and no installer reads
  requirements.txt's ranges any more;
* the checker itself, fed inputs that MUST fail, so a green run means the gate
  looked rather than that it cannot see.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from gdx_dispatch.tools import requirements_lock as rl

REPO = rl.GDX_DIR.parent
# Every file that can install Python packages for an image or a CI job, found
# by glob so a new Dockerfile or workflow is scanned without an edit here.
INSTALLERS = {
    str(p.relative_to(REPO)): p
    for p in sorted([*(REPO / "gdx_dispatch/docker").glob("Dockerfile*"), *(REPO / ".github/workflows").glob("*.y*ml")])
}
RANGE_FILES = ("requirements.txt", "requirements-plugin-host.in")


def _commands(text: str) -> list[str]:
    """Logical lines: comments dropped, ``\\`` continuations joined."""
    out, cur = [], ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        cur += line.rstrip()
        if cur.endswith("\\"):
            cur = cur[:-1] + " "
            continue
        out.append(cur.strip())
        cur = ""
    return out + ([cur.strip()] if cur.strip() else [])


def _range_installs(text: str) -> list[str]:
    """Install commands that resolve the ranges; a ``--dry-run`` (the drift job) installs nothing."""
    return [
        c
        for c in _commands(text)
        if re.search(r"\binstall\b", c) and any(f in c for f in RANGE_FILES) and "--dry-run" not in c
    ]


# ── the committed tree ──────────────────────────────────────────────────────


def test_the_committed_locks_satisfy_their_requirements_and_agree():
    problems = rl.check()
    assert problems == [], (
        "a lock no longer matches requirements.txt. Re-lock with the commands in "
        "gdx_dispatch/tools/requirements_lock.py, then rebuild the image:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize("name", sorted(INSTALLERS))
def test_no_installer_resolves_the_ranges(name):
    """An install from requirements.txt re-resolves at build time: the class this lock closes."""
    offending = _range_installs(INSTALLERS[name].read_text())
    assert offending == [], f"{name} installs from requirements.txt's ranges: {offending}"


def test_the_installer_scan_covers_the_known_installers():
    assert {
        "gdx_dispatch/docker/Dockerfile",
        "gdx_dispatch/docker/Dockerfile.plugin-host",
        ".github/workflows/ci.yml",
        ".github/workflows/security.yml",
    } <= INSTALLERS.keys()


@pytest.mark.parametrize(
    "text",
    [
        "RUN uv pip install --system -r requirements.txt",
        "RUN uv pip install --system --no-cache \\\n    -r requirements.txt",
        "      - run: pip install -r gdx_dispatch/requirements-plugin-host.in",
    ],
)
def test_the_installer_scan_catches_a_range_install(text):
    assert _range_installs(text)


def test_the_installer_scan_passes_the_lock_and_the_drift_dry_run():
    assert not _range_installs("RUN uv pip install --system --no-deps -r requirements.lock \\\n && uv pip check")
    assert not _range_installs("pip install --dry-run --ignore-installed \\\n  --report f.json -r requirements.txt")


def test_plugin_host_playwright_pin_matches_the_base_image():
    """A playwright other than the base image's looks for a browser build the image lacks."""
    base = re.search(
        r"^FROM mcr\.microsoft\.com/playwright/python:v(\S+?)-",
        INSTALLERS["gdx_dispatch/docker/Dockerfile.plugin-host"].read_text(),
        re.M,
    )
    assert base, "Dockerfile.plugin-host no longer starts FROM the Playwright python image"
    pins, _ = rl.read_lock(rl.PLUGIN_HOST_LOCK)
    locked = [p.version for p in pins if p.name == "playwright"]
    assert locked == [base.group(1)], (
        f"Dockerfile.plugin-host is on Playwright v{base.group(1)} but "
        f"{rl.PLUGIN_HOST_LOCK.name} pins playwright {locked}"
    )


def test_the_app_lock_does_not_carry_playwright():
    """The app image never browses; playwright belongs to the plugin-host lock only."""
    pins, _ = rl.read_lock(rl.LOCK)
    assert not [p for p in pins if p.name in {"playwright", "pyee"}]


# ── the checker can fail ────────────────────────────────────────────────────


def _lock(tmp_path: Path, body: str) -> list[rl.Pin]:
    path = tmp_path / "x.lock"
    path.write_text(body)
    pins, problems = rl.read_lock(path)
    assert problems == []
    return pins


def test_a_pin_outside_the_range_is_refused(tmp_path):
    pins = _lock(tmp_path, "sqlalchemy==2.1.0\n")
    problems = rl.unsatisfied([Requirement("sqlalchemy>=2.0.49,<2.1")], pins, "x.lock")
    assert problems and "does not satisfy" in problems[0]


def test_a_requirement_with_no_pin_is_refused(tmp_path):
    pins = _lock(tmp_path, "bcrypt==5.0.0\n")
    assert rl.unsatisfied([Requirement("redis>=7.4.0")], pins, "x.lock") == ["x.lock: redis>=7.4.0 has no pin"]


def test_an_excluded_release_is_refused(tmp_path):
    """requirements.txt's ``Mako!=1.4.0`` is a range too."""
    pins = _lock(tmp_path, "mako==1.4.0\n")
    assert rl.unsatisfied([Requirement("Mako!=1.4.0")], pins, "x.lock")


def test_names_compare_normalised(tmp_path):
    pins = _lock(tmp_path, "pyjwt==2.10.1\nzope-event==6.2\n")
    reqs = [Requirement("PyJWT[crypto]>=2.8.0"), Requirement("zope.event>=5")]
    assert rl.unsatisfied(reqs, pins, "x.lock") == []


@pytest.mark.parametrize("line", ["redis>=8.1.0", "redis", "redis==8.*", "redis>=8,<9"])
def test_a_lock_line_that_is_not_an_exact_pin_is_refused(tmp_path, line):
    path = tmp_path / "x.lock"
    path.write_text(line + "\n")
    _pins, problems = rl.read_lock(path)
    assert problems and "not pinned" in problems[0]


def test_the_locks_disagreeing_outside_playwright_is_refused(tmp_path):
    app = _lock(tmp_path, "sqlalchemy==2.0.54\ngreenlet==3.5.6\n")
    host = _lock(tmp_path, "sqlalchemy==2.0.53\ngreenlet==3.1.1\n")
    assert rl.locks_disagree(app, host, rl.PLUGIN_HOST_ONLY) == [
        "sqlalchemy: app lock [('2.0.54', '')] vs plugin-host lock [('2.0.53', '')]"
    ]
    host_missing = _lock(tmp_path, "greenlet==3.1.1\n")
    assert rl.locks_disagree(app, host_missing, rl.PLUGIN_HOST_ONLY) == [
        "sqlalchemy is in the app lock but missing from the plugin-host lock"
    ]


# ── drift: the scheduled job's comparison ───────────────────────────────────


def _report(**versions: str) -> dict:
    return {"install": [{"metadata": {"name": n, "version": v}} for n, v in versions.items()]}


def test_drift_names_a_major_bump_and_fails_the_cli(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("bcrypt==4.3.0\nredis==8.0.0\nhttpx==0.28.1\n")
    report = tmp_path / "fresh.json"
    report.write_text(json.dumps(_report(bcrypt="5.0.0", redis="8.1.0", httpx="0.28.1")))
    pins, _ = rl.read_lock(lock)
    moves = rl.drift(pins, rl.fresh_from_report(json.loads(report.read_text())))
    assert [str(m) for m in moves] == ["MAJOR bcrypt: 4.3.0 -> 5.0.0", "redis: 8.0.0 -> 8.1.0"]
    assert rl.main(["drift", "--report", str(report), "--lock", str(lock)]) == 1


@pytest.mark.parametrize(
    ("old", "new", "major"),
    [
        ("2.0.54", "2.1.0", False),  # GDXA-14's move: minor by number; the lock, not this rule, holds it
        ("2.0.54", "3.0.0", True),
        ("0.115.0", "0.136.0", True),  # 0.x: the minor is the breaking slot
        ("0.28.1", "0.28.2", False),
        ("0.9", "1.0", True),
        ("5", "5.0.1", False),
    ],
)
def test_drift_major_rule(old, new, major):
    assert rl.Move("x", old, new).major is major


def test_drift_minor_moves_report_but_pass(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("redis==8.0.0\n")
    report = tmp_path / "fresh.json"
    report.write_text(json.dumps(_report(redis="8.1.0")))
    assert rl.main(["drift", "--report", str(report), "--lock", str(lock)]) == 0


def test_drift_treats_an_added_or_dropped_package_as_major():
    pins = [rl.Pin("redis", "8.1.0", "", 1)]
    moves = rl.drift(pins, {"valkey": "6.0.0"})
    assert {str(m) for m in moves} == {"MAJOR redis: 8.1.0 -> (dropped)", "MAJOR valkey: (absent) -> 6.0.0"}


def test_drift_ignores_a_pin_whose_marker_excludes_the_resolver():
    """A Linux resolution never installs pywin32; that is not drift."""
    pins = [rl.Pin("pywin32", "312", "sys_platform == 'win32'", 1), rl.Pin("redis", "8.1.0", "", 2)]
    linux = dict(rl.default_environment(), sys_platform="linux")
    assert rl.drift(pins, {"redis": "8.1.0"}, env=linux) == []
