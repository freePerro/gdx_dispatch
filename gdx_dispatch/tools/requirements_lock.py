"""The resolved dependency set, recorded and checked (GDXA-467).

``requirements.txt`` states RANGES (``cryptography>=46.0.6``); 33 of its 50
lines had no upper bound. Every image build and CI run used to re-resolve
them, so the set that shipped was whatever PyPI held that minute and nothing
recorded it. GDXA-14 / #772 was that class biting once: SQLAlchemy 2.1.0
landed under a ``<3.0`` ceiling and every CI shard died at import.

The fix is two lockfiles, each pinning every package (transitive included)
with ``==``, which every installer reads instead of the ranges:

  requirements.lock              app, celery, CI (``ci.yml``, ``Dockerfile``)
  requirements-plugin-host.lock  plugin-host (``Dockerfile.plugin-host``):
                                 requirements.txt + ``requirements-plugin-host.in``

Re-lock (uv, from the repo root) - after any requirements.txt edit, or to take
an upgrade on purpose. ``--upgrade-package X`` takes one package; plain
re-running keeps every pin that still satisfies the ranges::

  uv pip compile gdx_dispatch/requirements.txt --universal --python-version 3.12 \\
      -o gdx_dispatch/requirements.lock
  uv pip compile gdx_dispatch/requirements-plugin-host.in --universal \\
      --python-version 3.12 -o gdx_dispatch/requirements-plugin-host.lock

This module is the checker both directions need:

  ``check``  every requirement in requirements.txt (and the plugin-host .in) is
             satisfied by its lock, every lock line is ``==``, and the two locks
             agree on every package outside the playwright closure. Pinned by
             tests/test_requirements_lock.py in the default suite.
  ``drift``  compares a fresh ``pip install --dry-run --report`` resolution of
             the ranges against the lock and names every package that would
             move, marking major bumps. Run weekly by
             .github/workflows/dependency-drift.yml, which fails on a major bump
             so it arrives as a decision (a re-lock PR), not as a surprise in
             the next image build.

stdlib + ``packaging`` only: the drift workflow runs it on a bare runner.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

GDX_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = GDX_DIR / "requirements.txt"
LOCK = GDX_DIR / "requirements.lock"
PLUGIN_HOST_IN = GDX_DIR / "requirements-plugin-host.in"
PLUGIN_HOST_LOCK = GDX_DIR / "requirements-plugin-host.lock"

# Packages the plugin-host lock may pin differently from the app lock, and
# why. playwright 1.49.1 requires greenlet==3.1.1 exactly, and must stay on
# the base image's browser build (Dockerfile.plugin-host). Before the lock the
# image installed requirements.txt (greenlet 3.5.6) and then playwright, which
# silently downgraded greenlet under SQLAlchemy - and under gevent 26.9.0, which
# needs a newer greenlet, leaving an inconsistent set (release v1.143.3 build
# log, 2026-10-10). gevent/geventhttpclient come in through locust, which only
# tests/load/locustfile.py imports; the plugin-host lock resolves them down to
# the newest pair that accepts greenlet 3.1.1.
PLUGIN_HOST_ONLY = frozenset({"playwright", "pyee", "greenlet", "gevent", "geventhttpclient"})


def canonical(name: str) -> str:
    """PEP 503 name normalisation: ``PyJWT`` == ``pyjwt``, ``zope.event`` == ``zope-event``."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    marker: str  # "" when unconditional
    line: int

    def applies(self, env: dict | None = None) -> bool:
        if not self.marker:
            return True
        return Requirement(f"x; {self.marker}").marker.evaluate(env or default_environment())


def _logical_lines(path: Path):
    """(lineno, text) with ``#`` comments stripped; skips blanks.

    None of these files carries a URL requirement, so every ``#`` starts a comment.
    """
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        text = raw.split("#", 1)[0].strip()
        if text:
            yield n, text


def read_requirements(path: Path) -> list[Requirement]:
    """Requirements from a requirements file, following ``-r`` includes."""
    out: list[Requirement] = []
    for _n, text in _logical_lines(path):
        if text.startswith(("-r ", "--requirement ")):
            out.extend(read_requirements(path.parent / text.split(None, 1)[1]))
            continue
        if text.startswith("-"):
            continue
        out.append(Requirement(text))
    return out


def read_lock(path: Path) -> tuple[list[Pin], list[str]]:
    """(pins, problems). A problem is any line that is not ``name==version[; marker]``."""
    pins: list[Pin] = []
    problems: list[str] = []
    for n, text in _logical_lines(path):
        try:
            req = Requirement(text)
        except InvalidRequirement as e:
            problems.append(f"{path.name}:{n}: unparseable line {text!r} ({e})")
            continue
        specs = list(req.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            problems.append(f"{path.name}:{n}: {text!r} is not pinned with a single '=='")
            continue
        pins.append(Pin(canonical(req.name), specs[0].version, str(req.marker or ""), n))
    return pins, problems


def unsatisfied(requirements: list[Requirement], pins: list[Pin], lock_name: str) -> list[str]:
    """Every way the lock fails to satisfy the requirements. Empty means it does.

    A requirement with no pin, or whose every pin falls outside its specifier, is
    a problem. Multiple pins for one name (marker-split universal locks) must
    each satisfy the specifier, since each may be the one installed.
    """
    by_name: dict[str, list[Pin]] = {}
    for p in pins:
        by_name.setdefault(p.name, []).append(p)
    problems: list[str] = []
    for req in requirements:
        name = canonical(req.name)
        found = by_name.get(name)
        if not found:
            problems.append(f"{lock_name}: {req} has no pin")
            continue
        for p in found:
            if not req.specifier.contains(p.version, prereleases=True):
                problems.append(f"{lock_name}:{p.line}: {name}=={p.version} does not satisfy {req}")
    seen: dict[tuple[str, str], int] = {}
    for p in pins:
        key = (p.name, p.marker)
        if key in seen:
            problems.append(f"{lock_name}:{p.line}: {p.name} pinned twice (also line {seen[key]})")
        seen[key] = p.line
    return problems


def locks_disagree(app: list[Pin], host: list[Pin], allowed: frozenset[str]) -> list[str]:
    """Packages both images install at different versions, outside ``allowed``.

    The plugin-host imports gdx_dispatch, so a package it shares with the app
    must be the version the suite ran against.
    """

    def by_name(pins: list[Pin]) -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = {}
        for p in pins:
            out.setdefault(p.name, []).append((p.version, p.marker))
        return {k: sorted(v) for k, v in out.items()}

    a, h = by_name(app), by_name(host)
    out = []
    for name in sorted(a.keys() - allowed):
        if name not in h:
            out.append(f"{name} is in the app lock but missing from the plugin-host lock")
        elif a[name] != h[name]:
            out.append(f"{name}: app lock {a[name]} vs plugin-host lock {h[name]}")
    return out


def check() -> list[str]:
    problems: list[str] = []
    app_pins, p = read_lock(LOCK)
    problems += p
    problems += unsatisfied(read_requirements(REQUIREMENTS), app_pins, LOCK.name)
    host_pins, p = read_lock(PLUGIN_HOST_LOCK)
    problems += p
    problems += unsatisfied(read_requirements(PLUGIN_HOST_IN), host_pins, PLUGIN_HOST_LOCK.name)
    problems += locks_disagree(app_pins, host_pins, PLUGIN_HOST_ONLY)
    return problems


# ── drift: a fresh resolution against the lock ──────────────────────────────


@dataclass(frozen=True)
class Move:
    name: str
    locked: str | None  # None: the fresh resolution adds a package
    fresh: str | None  # None: the fresh resolution drops a package

    @property
    def major(self) -> bool:
        """A major bump, an added or a dropped package: the kind that needs a decision.

        Below 1.0 the minor is the breaking slot (fastapi 0.115 -> 0.136), so a
        0.x minor move counts as major too.
        """
        if self.locked is None or self.fresh is None:
            return True
        try:
            old, new = Version(self.locked).release, Version(self.fresh).release
            width = 2 if old[0] == 0 or new[0] == 0 else 1
            return (old + (0,))[:width] != (new + (0,))[:width]
        except InvalidVersion:
            return True

    def __str__(self) -> str:
        tag = "MAJOR " if self.major else ""
        return f"{tag}{self.name}: {self.locked or '(absent)'} -> {self.fresh or '(dropped)'}"


def fresh_from_report(report: dict) -> dict[str, str]:
    """name -> version from a ``pip install --dry-run --report`` JSON document."""
    return {canonical(item["metadata"]["name"]): item["metadata"]["version"] for item in report.get("install", [])}


def drift(pins: list[Pin], fresh: dict[str, str], env: dict | None = None) -> list[Move]:
    """Every package the fresh resolution would install at a version the lock does not pin.

    Lock lines whose marker does not apply to ``env`` (the resolving machine) are
    ignored: a Linux resolution never installs pywin32, which is not drift.
    """
    locked = {p.name: p.version for p in pins if p.applies(env)}
    moves = [Move(n, locked.get(n), fresh.get(n)) for n in sorted(locked.keys() | fresh.keys())]
    return [m for m in moves if m.locked != m.fresh]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="the locks satisfy requirements.txt and agree with each other")
    d = sub.add_parser("drift", help="diff a fresh pip --dry-run --report against a lock")
    d.add_argument("--report", required=True, type=Path)
    d.add_argument("--lock", type=Path, default=LOCK)
    args = ap.parse_args(argv)

    if args.cmd == "check":
        problems = check()
        for p in problems:
            print(f"✗ {p}")
        if not problems:
            print(f"✓ {LOCK.name} and {PLUGIN_HOST_LOCK.name} satisfy their requirements and agree")
        return 1 if problems else 0

    pins, problems = read_lock(args.lock)
    if problems:
        for p in problems:
            print(f"✗ {p}")
        return 2
    moves = drift(pins, fresh_from_report(json.loads(args.report.read_text())))
    if not moves:
        print(f"✓ a fresh resolution matches {args.lock.name} exactly")
        return 0
    print(f"{len(moves)} package(s) would resolve differently from {args.lock.name}:")
    for m in moves:
        print(f"  {m}")
    majors = [m for m in moves if m.major]
    if majors:
        print(
            f"\n{len(majors)} MAJOR move(s): adopt each with a re-lock PR that runs the suite"
            " against it, or cap it in requirements.txt with the reason."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
