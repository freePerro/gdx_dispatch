#!/usr/bin/env python3
"""Real-repo ratchet for the report-only scanners (GDXA-404).

Four scanners in this directory — ``drift_scanner``, ``silent_failure_scanner``,
``comment_drift_scan`` and ``frontend_contract_scan`` — had unit tests on
``tmp_path`` fixtures and nothing else. No test and no CI step ever ran them
on the real tree, so each could report anything, or crash, and every gate
stayed green. On 2026-10-09 all four exited 1 on main: 326, 94, 76 and 5
findings. A scanner nobody runs is a comment, not a guard.

This module turns each into a ratchet: run it on the real tree, compare to a
frozen baseline at the repo root, fail when they differ in EITHER direction: a
finding the baseline does not hold, or a baseline entry the tree no longer
produces (``stale_entries``). The baseline is pinned exactly, not a ceiling: a
fixed finding left behind is budget for the next one, and a scanner that quietly
stops walking half the tree would stay green. ``tests/test_scanner_real_repo_ratchet.py`` runs it in the default suite.

Signatures
----------
``<file>|<code>|sha256(<finding text>)[:16]``, one line per finding. The text
is the finding's own words and source line, never its line NUMBER: a
line-keyed baseline (``.tenant_plane_redundant_filter_baseline``) reddens when
someone edits above a recorded finding. This one does not.

The comparison is a MULTISET. Two identical ``except Exception: pass`` blocks
in one file share a signature; the baseline records it twice, and a third copy
is new. Know what that buys: most drift and silent_failure findings hash the
same text (320 of 326 drift hits are a message plus ``except Exception:``), so
for those the gate is in effect a per-file, per-shape COUNT. A new swallowed
except in a file that already has one fails because the count rises, not
because the text differs; one fixed and one added in the same file nets to no
change and is not seen. comment_drift and frontend_contract findings carry
distinct text, so for them an edit to the flagged line itself reads as new.

Only files git tracks are counted. ``drift_scanner`` and
``silent_failure_scanner`` walk the working tree, so a stray untracked file on
one machine would otherwise fail the gate there and pass in CI — the defect
``tools/tracked_files.py`` exists to stop.

Usage (``frontend_contract_scan`` needs the app to import, so use docker-app)::

    python -m gdx_dispatch.tools.scanner_baseline                      # report
    python -m gdx_dispatch.tools.scanner_baseline --write drift_scanner

Re-freezing to make the gate pass is the one wrong answer, the same as for
every other baseline here. Fix the finding, or mark it in source where the
scanner supports a marker (``# noqa: silent-failure``).
"""
import argparse
import hashlib
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gdx_dispatch.tools.tracked_files import tracked_or_none

# Run as ``python -m gdx_dispatch.tools.scanner_baseline`` from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Hit:
    """One finding, reduced to what the baseline needs."""

    file: str    # repo-relative, forward slashes
    code: str    # short rule id, readable in the baseline diff
    text: str    # what is hashed: the finding's words, no line number
    shown: str   # human-readable, for the failure message

    @property
    def signature(self) -> str:
        digest = hashlib.sha256(self.text.encode()).hexdigest()[:16]
        return f"{self.file}|{self.code}|{digest}"


def _source_line(rel: str, line: int) -> str:
    try:
        lines = (REPO_ROOT / rel).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""


def _slug(msg: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", msg.lower()).strip("-")[:32]


# ── collectors: one per scanner, each returning Hits for the real tree ──────

_DRIFT_ENTRY = re.compile(r"^(?P<file>[^:]+):(?:(?P<line>\d+):)?\s*(?P<msg>.*)$")


def _drift_scanner() -> list[Hit]:
    import contextlib
    import io

    from gdx_dispatch.tools import drift_scanner

    with contextlib.redirect_stdout(io.StringIO()):
        entries = drift_scanner.collect()
    hits = []
    for entry in entries:
        m = _DRIFT_ENTRY.match(entry)
        if not m:
            hits.append(Hit("-", "unparsed", entry, entry))
            continue
        rel, msg = m["file"], m["msg"]
        src = _source_line(rel, int(m["line"])) if m["line"] else ""
        hits.append(Hit(rel, _slug(msg), f"{msg}\n{src}", entry))
    return hits


def _silent_failure_scanner() -> list[Hit]:
    from gdx_dispatch.tools import silent_failure_scanner

    return [
        Hit(
            f.file,
            f"S{f.shape}-{f.rule}",
            f"{f.rule}\n{_source_line(f.file, f.line)}",
            f"{f.file}:{f.line} shape {f.shape} [{f.rule}]",
        )
        for f in silent_failure_scanner.collect()
    ]


def _comment_drift_scan() -> list[Hit]:
    from gdx_dispatch.tools import comment_drift_scan

    return [
        Hit(
            f["file"],
            f["det"],
            f"{f['detail']}\n{f['text'].strip()}",
            f"{f['det']} {f['file']}:{f['line']} {f['detail']}",
        )
        for f in comment_drift_scan.scan(REPO_ROOT, comment_drift_scan.ALL_DETECTORS)
    ]


def _frontend_contract_scan() -> list[Hit]:
    from gdx_dispatch.tools import frontend_contract_scan as fcs

    routes = fcs.routes_from_app()
    if len(routes) < fcs._MIN_PLAUSIBLE_ROUTES:
        raise RuntimeError(
            f"live route table has {len(routes)} routes; a gate run against a "
            f"broken table reads green"
        )
    return [
        Hit(
            f["file"],
            f["check"],
            f"{f['detail']}\n{f['expr']}",
            f"{f['check']} {f['file']}:{f['line']} {f['detail']}",
        )
        for f in fcs.scan(REPO_ROOT, fcs.DEFAULT_CHECKS, routes)
    ]


@dataclass(frozen=True)
class Scanner:
    name: str
    baseline: Path
    collect: Callable[[], list[Hit]]


SCANNERS: dict[str, Scanner] = {
    s.name: s
    for s in (
        Scanner("drift_scanner", REPO_ROOT / ".drift_scanner_baseline", _drift_scanner),
        Scanner("silent_failure_scanner", REPO_ROOT / ".silent_failure_baseline", _silent_failure_scanner),
        Scanner("comment_drift_scan", REPO_ROOT / ".comment_drift_baseline", _comment_drift_scan),
        Scanner("frontend_contract_scan", REPO_ROOT / ".frontend_contract_baseline", _frontend_contract_scan),
    )
}


# ── the ratchet ─────────────────────────────────────────────────────────────

def tracked_only(hits: list[Hit], root: Path = REPO_ROOT) -> list[Hit]:
    """Drop hits in files git does not track. No ``.git`` at all: keep all.
    A hit with no file (``-``) is kept: it cannot be untracked."""
    tracked = tracked_or_none(root)
    if tracked is None:
        return hits
    tracked = set(tracked)
    return [h for h in hits if h.file == "-" or h.file in tracked]


def run(name: str) -> list[Hit]:
    return tracked_only(SCANNERS[name].collect())


def load_baseline(path: Path) -> Counter[str]:
    """Multiset of signatures. A missing file raises: an absent baseline must
    not read as "nothing tolerated" in one place and "everything" in another."""
    return Counter(
        ln.strip()
        for ln in path.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    )


def new_hits(hits: list[Hit], baseline: Counter[str]) -> list[Hit]:
    """Hits beyond what the baseline holds, counting duplicates."""
    budget = Counter(baseline)
    out = []
    for h in sorted(hits, key=lambda h: h.signature):
        if budget[h.signature] > 0:
            budget[h.signature] -= 1
        else:
            out.append(h)
    return out


def stale_entries(hits: list[Hit], baseline: Counter[str]) -> Counter[str]:
    """Baseline entries the tree no longer produces, counting duplicates.

    The gate fails on these too. A fixed finding left in the baseline is
    budget: the next finding with the same signature passes as old, and a
    scanner that silently stops walking half the tree stays green (audit,
    GDXA-404). So the baseline is pinned exactly, as test_authz_route_sweep
    pins its count — fixing a finding means re-freezing in the same change."""
    return baseline - Counter(h.signature for h in hits)


def write_baseline(name: str, hits: list[Hit]) -> None:
    s = SCANNERS[name]
    header = (
        f"# {name} findings on the real tree when this baseline was frozen.\n"
        f"# <file>|<code>|sha256(finding text)[:16] — content-keyed, not line-keyed;\n"
        f"# a repeated line is a repeated finding. Written by\n"
        f"# `python -m gdx_dispatch.tools.scanner_baseline --write {name}`.\n"
        f"# Gated by gdx_dispatch/tests/test_scanner_real_repo_ratchet.py.\n"
        f"# Re-freezing to make that test pass is the wrong answer: fix the finding.\n"
    )
    body = "".join(f"{sig}\n" for sig in sorted(h.signature for h in hits))
    s.baseline.write_text(header + body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("names", nargs="*", default=list(SCANNERS), help=f"subset of {list(SCANNERS)}")
    ap.add_argument("--write", action="store_true", help="re-freeze the named baselines")
    args = ap.parse_args(argv)

    unknown = set(args.names) - set(SCANNERS)
    if unknown:
        print(f"unknown scanner(s): {sorted(unknown)}", file=sys.stderr)
        return 2

    rc = 0
    for name in args.names:
        hits = run(name)
        if args.write:
            write_baseline(name, hits)
            print(f"{name}: wrote {len(hits)} entries -> {SCANNERS[name].baseline.name}")
            continue
        baseline = load_baseline(SCANNERS[name].baseline)
        new = new_hits(hits, baseline)
        stale = stale_entries(hits, baseline)
        print(f"{name}: {len(hits)} findings, {len(new)} not in baseline, "
              f"{sum(stale.values())} stale baseline entries")
        for h in new:
            print(f"  NEW {h.shown}")
        for sig, n in sorted(stale.items()):
            print(f"  STALE x{n} {sig}")
        rc |= bool(new) or bool(stale)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
