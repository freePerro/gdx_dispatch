"""Agent ownership scan — every source file resolves to exactly one subagent.

Bug class this catches
----------------------
The project subagents in ``.claude/agents/*.md`` (decided 2026-09-24, see
``domain-agents-plan`` under ``docs/design/``) each own a slice of the tree. An
ownership map that lives only in prose rots the way every present-tense doc
in this repo has rotted: a router is added, nobody claims it, and the agent
that "owns customers" has never heard of it. ADR-012 named the same failure
for its work-orders — "overlapping ``files_in`` lists → merge conflicts" — and
had nothing enforcing it.

So the map is a file (``gdx_dispatch/tools/agent_ownership.txt``) and this
scan is its guard. Five things must hold:

1. **Every source file under the covered roots has an owner.** A new router,
   view, module or composable without a rule is reported by path. Coverage is
   ``COVERED_ROOTS`` (directory prefixes) plus ``COVERED_ROOT_GLOBS`` (the repo
   root's ratchet baselines).
2. **Every rule still matches a tracked file.** A rule for a renamed or
   deleted file is dead weight that misleads the next reader; it goes red.
3. **Every owner named in the map has an agent file**, ``.claude/agents/<owner>.md``.
4. **Every agent file owns something.** An agent with no territory is a
   definition nobody maintains.
5. **Every repo-root rule is gated or recorded as ungated.** Root coverage is a
   glob list, not a prefix, so a root rule can be documentation that no check
   enforces. That is allowed — ``DOCUMENTED_ONLY_ROOT_RULES`` names them — but
   it may not be accidental, which is how six baselines went unclaimed.

Semantics
---------
Rules are ``<fnmatch glob>  <owner>`` against repo-relative posix paths.
``*`` matches across ``/`` — this is fnmatch, not gitignore. **Last match
wins**, so a broad rule (``views/Mobile*.vue``) is listed before its
exceptions. There is deliberately no catch-all: a catch-all would make check 1
unable to fail, and a check that cannot fail is decoration.

Tracked files only
------------------
Enumeration reads the git index via ``tracked_files.py`` for the reason that
module documents: a gitignored path that exists on one machine must not turn
a guard green locally and red in CI. ``--worktree`` walks the filesystem
instead, for previewing a file you have written but not yet ``git add``-ed;
the test never uses it.

Usage
-----
    python -m gdx_dispatch.tools.agent_ownership_scan               # report
    python -m gdx_dispatch.tools.agent_ownership_scan --owner X     # X's files
    python -m gdx_dispatch.tools.agent_ownership_scan --file PATH   # who owns PATH
    python -m gdx_dispatch.tools.agent_ownership_scan --worktree    # include untracked
    python -m gdx_dispatch.tools.agent_ownership_scan --strict      # exit 1 on any finding
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gdx_dispatch.tools.tracked_files import tracked_or_none

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = REPO_ROOT / "gdx_dispatch" / "tools" / "agent_ownership.txt"
AGENTS_DIR = ".claude/agents/"

# Directories where every file must have an owner. Tests, docs and migrations
# are shared surfaces and are not roots (the map may still list them).
COVERED_ROOTS = (
    "gdx_dispatch/routers/",
    "gdx_dispatch/core/",
    "gdx_dispatch/modules/",
    "gdx_dispatch/tasks/",
    "gdx_dispatch/services/",
    "gdx_dispatch/models/",
    "gdx_dispatch/api/",
    "gdx_dispatch/plugin_api/",
    "gdx_dispatch/plugin_host/",
    "gdx_dispatch/templates/",
    "gdx_dispatch/frontend/src/",
)

# Repo-root files where every match must have an owner. The root cannot be a
# COVERED_ROOTS prefix: the empty string would pull in README.md, LICENSE,
# CLAUDE.md and every other project file, which belong to the maintainer and
# not to any one agent. So the root is covered by glob instead, and these
# globs are anchored to it by the caller's `"/" not in rel` test.
#
# The class this exists for: six ratchet baselines sat UNOWNED until 2026-09-27
# (GDXA-142) because no prefix reached them, so the gate could not redden for a
# missing rule the way it does for an unclaimed router. `*_baseline` catches the
# next one *named that way* — which is the convention all six follow, and the
# only part a glob can know. It does NOT reach a root ratchet artifact named
# some other way (`.foo_ratchet.json`); the two literals below are here because
# the convention did not reach them either. Add a glob when you add a ratchet.
COVERED_ROOT_GLOBS = (
    "*_baseline",
    ".semgrepignore",  # semgrep's ignore list — scanner input, same family
    ".test_durations",  # pytest-split's durations file, read by every matrix run
)

# Repo-root rules the map carries for documentation only: `--file` answers for
# them, but they are outside COVERED_ROOT_GLOBS so deleting the rule reddens
# nothing. Listing them as data rather than as a comment is deliberate — the
# split is checked (see `Report.ungated_root_rules`), so adding a root rule
# forces a choice between gating it and recording it here, instead of silently
# landing in the ungated half. That silent landing is GDXA-142 itself.
DOCUMENTED_ONLY_ROOT_RULES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "ruff.toml",
        "sgconfig.yml",  # ast-grep's project config (vue -> html mapping)
        "pyproject.toml",
        # plugins-host's, and a doc rather than scanner state — gating a root
        # doc is a separate decision and not platform-core's to make. Found by
        # the check below on its first run, which is the argument for having it.
        "PLUGIN-EXCEPTION.md",
    }
)

# Not source: package markers, caches, and tests (which follow their subject).
EXCLUDED = (
    "*/__init__.py",
    "*/__pycache__/*",
    "*.pyc",
    "*/__tests__/*",
    "*.spec.js",
    "*.test.js",
)

WALK_SKIP = {".git", "node_modules", "__pycache__", ".pytest_cache", "dist", ".venv", "build"}


@dataclass
class Report:
    owners: dict[str, list[str]] = field(default_factory=dict)
    unowned: list[str] = field(default_factory=list)
    dead_rules: list[str] = field(default_factory=list)
    owners_without_agent: list[str] = field(default_factory=list)
    agents_without_territory: list[str] = field(default_factory=list)
    ungated_root_rules: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (
            self.unowned
            or self.dead_rules
            or self.owners_without_agent
            or self.agents_without_territory
            or self.ungated_root_rules
        )


def load_rules(path: Path = MAP_PATH) -> list[tuple[str, str]]:
    """``[(glob, owner), ...]`` in file order. A malformed line is an error, not a skip."""
    rules: list[tuple[str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"{path.name}:{lineno}: expected '<glob> <owner>', got {raw!r}")
        rules.append((parts[0], parts[1]))
    return rules


def owner_of(rel: str, rules: Iterable[tuple[str, str]]) -> str | None:
    """Last matching rule wins; ``None`` when nothing matches."""
    owner = None
    for glob, who in rules:
        if fnmatch.fnmatchcase(rel, glob):
            owner = who
    return owner


def is_covered(rel: str) -> bool:
    """Must this path resolve to an owner? Covered roots, plus root globs.

    A repo-root path has no ``/``, which is what anchors ``COVERED_ROOT_GLOBS``
    to the root — fnmatch's ``*`` crosses ``/``, so ``*_baseline`` would
    otherwise match a baseline nested anywhere in the tree.
    """
    at_root = "/" not in rel
    if at_root:
        if not any(fnmatch.fnmatchcase(rel, g) for g in COVERED_ROOT_GLOBS):
            return False
    elif not rel.startswith(COVERED_ROOTS):
        return False
    return not any(fnmatch.fnmatchcase(rel, g) for g in EXCLUDED)


def _walk_worktree(root: Path) -> set[str]:
    out: set[str] = set()
    for dp, dn, fs in os.walk(root):
        dn[:] = [d for d in dn if d not in WALK_SKIP]
        for f in fs:
            out.add(Path(dp, f).relative_to(root).as_posix())
    return out


def enumerate_files(worktree: bool = False) -> set[str]:
    """Tracked set by default; the filesystem only when asked for explicitly."""
    if worktree:
        return _walk_worktree(REPO_ROOT)
    tracked = tracked_or_none(REPO_ROOT)
    if tracked is None:
        # Not a git checkout (a scratch dir in a test). Walking is the only
        # option; say so rather than silently returning nothing.
        return _walk_worktree(REPO_ROOT)
    return set(tracked)


def scan(files: Iterable[str] | None = None, rules: list[tuple[str, str]] | None = None) -> Report:
    """Resolve every covered file to an owner and check the map both ways."""
    rules = load_rules() if rules is None else rules
    files = enumerate_files() if files is None else set(files)
    report = Report()

    matched: set[str] = set()
    for rel in sorted(files):
        if not is_covered(rel):
            continue
        who = owner_of(rel, rules)
        if who is None:
            report.unowned.append(rel)
        else:
            report.owners.setdefault(who, []).append(rel)

    # Dead rules: a rule must match at least one enumerated file — any file,
    # not just a covered one, because the map also names shared surfaces.
    for glob, _who in rules:
        if not any(fnmatch.fnmatchcase(rel, glob) for rel in files):
            report.dead_rules.append(glob)
        else:
            matched.add(glob)

    # A repo-root rule is either gated by COVERED_ROOT_GLOBS or recorded in
    # DOCUMENTED_ONLY_ROOT_RULES. Landing in the ungated half by accident is
    # the defect this whole file exists to stop, so it is reported.
    for glob, _who in rules:
        if "/" in glob or glob in DOCUMENTED_ONLY_ROOT_RULES:
            continue
        if not any(fnmatch.fnmatchcase(glob, g) for g in COVERED_ROOT_GLOBS):
            report.ungated_root_rules.append(glob)

    owners_in_map = {who for _g, who in rules}
    agent_files = {
        posixpath.basename(rel)[: -len(".md")]
        for rel in files
        if rel.startswith(AGENTS_DIR) and rel.endswith(".md") and "/" not in rel[len(AGENTS_DIR):]
    }
    report.owners_without_agent = sorted(owners_in_map - agent_files)
    report.agents_without_territory = sorted(agent_files - owners_in_map)
    return report


def _print_report(report: Report) -> None:
    print("Owner                  files")
    for who in sorted(report.owners):
        print(f"  {who:<22} {len(report.owners[who])}")
    print(f"  {'(total)':<22} {sum(len(v) for v in report.owners.values())}")
    if report.unowned:
        print(f"\nUNOWNED ({len(report.unowned)}) — add a rule to agent_ownership.txt:")
        for rel in report.unowned:
            print(f"  {rel}")
    if report.dead_rules:
        print(f"\nDEAD RULES ({len(report.dead_rules)}) — match no tracked file:")
        for glob in report.dead_rules:
            print(f"  {glob}")
    if report.owners_without_agent:
        print(f"\nOWNERS WITHOUT AN AGENT FILE ({len(report.owners_without_agent)}):")
        for who in report.owners_without_agent:
            print(f"  {who}  (expected {AGENTS_DIR}{who}.md)")
    if report.agents_without_territory:
        print(f"\nAGENT FILES THAT OWN NOTHING ({len(report.agents_without_territory)}):")
        for who in report.agents_without_territory:
            print(f"  {AGENTS_DIR}{who}.md")
    if report.ungated_root_rules:
        print(
            f"\nROOT RULES NEITHER GATED NOR RECORDED ({len(report.ungated_root_rules)}) — add a"
            " glob to COVERED_ROOT_GLOBS or the path to DOCUMENTED_ONLY_ROOT_RULES:"
        )
        for glob in report.ungated_root_rules:
            print(f"  {glob}")
    print("\nclean" if report.clean else "\nfindings above")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", help="list the files this owner resolves to")
    ap.add_argument("--file", help="print the owner of one repo-relative path")
    ap.add_argument("--worktree", action="store_true", help="walk the filesystem instead of the git index")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any finding")
    args = ap.parse_args(argv)

    rules = load_rules()
    if args.file:
        rel = args.file.replace(os.sep, "/")
        print(owner_of(rel, rules) or "UNOWNED")
        return 0

    report = scan(files=enumerate_files(worktree=args.worktree), rules=rules)
    if args.owner:
        for rel in report.owners.get(args.owner, []):
            print(rel)
        if args.owner not in report.owners:
            print(f"no files resolve to {args.owner!r}", file=sys.stderr)
            return 1
        return 0

    _print_report(report)
    return 1 if (args.strict and not report.clean) else 0


if __name__ == "__main__":
    sys.exit(main())
