#!/usr/bin/env python3
"""Fail on an ``Invalid # noqa directive``, or a legacy scanner marker, the repo has not accepted.

The repo's own scanners read ``# noqa:`` comments too (``RAWENC1``,
``SILENTFAIL1``), and ruff parses every one of them. It accepts only uppercase
letters followed by digits; anything else (``RAW_ENC`` until GDXA-302,
``silent-failure`` until GDXA-1892) draws the warning and voids every ruff
code listed after it on the line. That is a warning, not a diagnostic, so the
ruff ratchet never counts it, and ruff prints it only for files it actually
re-reads, which is why this runs with ``--no-cache``.

ruff warns only when the bad code comes first: after a valid code
(``BLE001`` then ``silent-failure``) it says nothing, and 6 of the 11
``silent-failure`` sites were written that way. So a site is also any comment
token naming the legacy ``silent-failure`` marker after ``noqa:``, read with
``tokenize`` so the strings in the scanners' own tests do not count. A line
caught both ways counts once.

``.ruff_invalid_noqa_pending`` lists the sites known to carry one, as
``<count> <path>`` per file, while their owners convert them. The check is
exact: a file with more sites than listed, or one not listed at all, fails;
so does a listed count that is now too high, because a loose allowance is room
for the next one to slip in. Lower or delete the line in the same PR that
converts the site. The file is meant to empty out, not to grow.

Run by the ``lint`` job in ``.github/workflows/ci.yml``, where the pinned ruff
is installed; the docker-app test image has no ruff. Fails closed when ruff is
missing, exits >= 2, or finds no Python files.

    python3 gdx_dispatch/tools/ruff_noqa_guard.py [target]   # default gdx_dispatch/
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PENDING_FILE = REPO_ROOT / ".ruff_invalid_noqa_pending"

# Verbatim shape from ruff 0.15.18 (quoted in a string, not a comment: ruff
# parses a directive in a comment and would warn on this file):
_SAMPLE = "warning: Invalid `# noqa` directive on gdx_dispatch/x.py:304: expected ..."
WARNING_RE = re.compile(r"^warning: Invalid `# noqa` directive on (.+?):(\d+):", re.MULTILINE)


# Still read by silent_failure_scanner / drift_scanner during the transition.
LEGACY_RE = re.compile(r"noqa:[^#]*\bsilent-failure\b", re.IGNORECASE)


def parse_warning_sites(output: str) -> set[tuple[str, int]]:
    """The (path, line) of every invalid-noqa warning in ruff's output."""
    return {(m.group(1), int(m.group(2))) for m in WARNING_RE.finditer(output)}


def parse_warnings(output: str) -> Counter[str]:
    """Count invalid-noqa warnings per file in ruff's output."""
    return per_file(parse_warning_sites(output))


def per_file(sites: set[tuple[str, int]]) -> Counter[str]:
    return Counter(path for path, _ in sites)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def legacy_marker_sites(target: Path) -> set[tuple[str, int]]:
    """The (path, line) of every comment carrying the legacy marker under ``target``.

    A file that will not tokenize fails the run: skipping it would report a
    site-free file this guard never read.
    """
    files = [target] if target.is_file() else sorted(target.rglob("*.py"))
    sites: set[tuple[str, int]] = set()
    for path in files:
        if "node_modules" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            with path.open("rb") as fh:
                for tok in tokenize.tokenize(fh.readline):
                    if tok.type == tokenize.COMMENT and LEGACY_RE.search(tok.string):
                        sites.add((_rel(path), tok.start[0]))
        except (tokenize.TokenError, SyntaxError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"cannot tokenize {_rel(path)} ({exc}); this guard did not read it") from exc
    return sites


def parse_pending(text: str) -> Counter[str]:
    """Read ``<count> <path>`` lines; ``#`` starts a comment."""
    pending: Counter[str] = Counter()
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) < 1:
            raise ValueError(f"{PENDING_FILE.name}:{n}: expected '<count> <path>', got {raw!r}")
        if parts[1] in pending:
            raise ValueError(f"{PENDING_FILE.name}:{n}: {parts[1]} listed twice")
        pending[parts[1]] = int(parts[0])
    return pending


def compare(found: Counter[str], pending: Counter[str]) -> list[str]:
    """Every difference between the sites found and what is accepted."""
    problems = []
    for path in sorted(set(found) | set(pending)):
        have, allowed = found.get(path, 0), pending.get(path, 0)
        if have > allowed:
            problems.append(
                f"{path}: {have} invalid or legacy `# noqa` code(s), {allowed} accepted. "
                "Use an uppercase-letters-then-digits code (e.g. SILENTFAIL1, RAWENC1)."
            )
        elif have < allowed:
            problems.append(
                f"{path}: {allowed} accepted in {PENDING_FILE.name}, {have} found. "
                f"Lower or delete its line in {PENDING_FILE.name}."
            )
    return problems


def run_ruff(target: str) -> str:
    ruff = shutil.which("ruff")
    if ruff is None:
        raise RuntimeError("ruff is not on PATH; this guard measured nothing")
    # --select keeps the run cheap: the warning comes from noqa parsing, which
    # happens whatever rules are selected (checked on ruff 0.15.18). F821 is
    # already a hard zero in ruff_ratchet.sh, so exit 1 here is unexpected
    # but harmless; exit >= 2 means ruff measured nothing.
    proc = subprocess.run(
        [ruff, "check", "--no-cache", "--select", "F821", "--output-format", "concise", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode >= 2:
        raise RuntimeError(f"ruff exited {proc.returncode}; this guard measured nothing:\n{output}")
    if "warning: No Python files found" in output:
        raise RuntimeError(f"ruff found no Python files under {target!r}; this guard measured nothing")
    return output


def main(argv: list[str]) -> int:
    target = argv[1] if len(argv) > 1 else "gdx_dispatch/"
    try:
        pending = parse_pending(PENDING_FILE.read_text(encoding="utf-8") if PENDING_FILE.exists() else "")
        output = run_ruff(target)
        sites = parse_warning_sites(output) | legacy_marker_sites(REPO_ROOT / target)
    except (RuntimeError, ValueError) as exc:
        print(f"❌ {exc}")
        return 2
    found = per_file(sites)
    problems = compare(found, pending)
    if problems:
        print("❌ invalid or legacy `# noqa` codes differ from the accepted list:")
        for p in problems:
            print(f"   {p}")
        return 1
    print(f"invalid or legacy `# noqa` codes: {sum(found.values())} found, all accepted in {PENDING_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
