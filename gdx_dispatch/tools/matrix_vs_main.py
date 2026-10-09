#!/usr/bin/env python3
"""Sort a matrix run's failures into "new" and "already failing on main".

Why this exists: on 2026-10-09, 13 of 28 Paperclip matrix runs over three days
ended FAIL with 1-3 failures each while CI on main was green. Each agent then
spent turns proving the reds were not its own: copying origin/main into a
scratch tree and re-running there (GDXA-375 did it 8 times in one issue), which
is a large part of why runs ended in max_turns. The answer to "was this already
red on main?" is the same for every agent at the same main commit, so it is
computed once per main commit by a host job and read here.

Baselines are plain text, one per main commit, in $MATRIX_BASELINE_DIR
(default ~/.cache/gdx_matrix_baseline/<full sha>.txt): `#` header lines, then
one failing test id (or collection-error file) per line. An empty body means
main was fully green at that commit.

    matrix_vs_main.py record  --logs DIR --sha SHA [--image ID]
    matrix_vs_main.py compare --logs DIR [--repo PATH]

`compare` is a REPORT. It never changes the matrix's exit code: a failure it
calls "already failing on main" still failed, it just is not yours to chase.
It always exits 0 so it cannot turn a run red or green by itself.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# pytest's short test summary: "FAILED path::test[param] - message" and
# "ERROR path.py" / "ERROR path::test - message". Anchored on a path so a
# captured log line like "ERROR    gdx_dispatch.core:..." never matches.
_SUMMARY = re.compile(r"^(FAILED|ERROR) (\S+?\.py(?:::.+?)?)(?: - .*)?$")

# pytest's final line, e.g. "2 failed, 10 passed in 1.23s" or "no tests ran in 0.1s".
_FINISHED = re.compile(r"\b(passed|failed|errors?|skipped|deselected|no tests ran)\b.* in [\d.]+s")

# pytest's "stopped early" banners: "!!! Interrupted: 1 error during collection !!!",
# "!!! stopping after 1 failures !!!" (-x/--maxfail), "!!! KeyboardInterrupt !!!".
# Each still ends the log on a tidy summary line although tests were skipped.
_STOPPED = re.compile(r"^!{3,} .+ !{3,}$", re.M)

# How far back along main's first-parent history to look for a baseline when
# the exact merge-base has none yet (the baseline job throttles itself, so the
# newest main commit often has no file of its own for a while).
_MAX_WALK = 300
# Older baselines also consulted, so a test that is red on main only some of
# the time is labelled flaky instead of "yours".
_FLAKY_WINDOW = 5


def baseline_dir() -> Path:
    return Path(os.environ.get("MATRIX_BASELINE_DIR")
                or Path.home() / ".cache" / "gdx_matrix_baseline")


def failures_in_logs(logs: Path) -> set[str]:
    out: set[str] = set()
    for log in sorted(logs.glob("group_*.log")):
        for line in log.read_text(errors="replace").splitlines():
            m = _SUMMARY.match(line.rstrip())
            if m:
                out.add(m.group(2))
    return out


def unfinished_shards(logs: Path) -> list[str]:
    """Shard logs whose tests did not all run.

    Either the shard died (no summary line: OOM, timeout, crash) or pytest
    stopped the session early (a collection error, -x/--maxfail, Ctrl-C),
    which still ends on a tidy summary line.
    """
    out = []
    for log in sorted(logs.glob("group_*.log")):
        text = log.read_text(errors="replace")
        tail = (text.strip().splitlines() or [""])[-1]
        # An empty shard ("1 deselected in 0.01s", exit 5 when N exceeds the
        # tests selected) finished normally and must not be flagged.
        if not _FINISHED.search(tail) or _STOPPED.search(text):
            out.append(log.name)
    return out


def read_baseline(path: Path) -> set[str]:
    return {ln.strip() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")}


def _git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True)
    except OSError:  # no git binary (the docker-app image ships none)
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def cmd_record(a: argparse.Namespace) -> int:
    logs = Path(a.logs)
    if not list(logs.glob("group_*.log")):
        print(f"record: no group_*.log in {logs}", file=sys.stderr)
        return 2
    # A shard that died (OOM, timeout, native crash) names no failures, and an
    # empty body means "main fully green" - so refuse rather than record one.
    for name in unfinished_shards(logs):
        print(f"record: {name} did not run all its tests (died or collection error); not recording",
              file=sys.stderr)
        return 2
    fails = sorted(failures_in_logs(logs))
    d = baseline_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{a.sha}.tmp"
    head = [f"# main {a.sha}",
            f"# recorded {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"# image {a.image or 'unknown'}",
            f"# failures {len(fails)}"]
    tmp.write_text("\n".join(head + fails) + "\n")
    tmp.replace(d / f"{a.sha}.txt")
    print(f"record: {len(fails)} failure(s) on main {a.sha[:8]} -> {d / (a.sha + '.txt')}")
    return 0


def _header(path: Path, key: str) -> str:
    for ln in path.read_text().splitlines():
        if ln.startswith(f"# {key} "):
            return ln[len(key) + 3:]
    return ""


def cmd_compare(a: argparse.Namespace) -> int:
    logs, repo, d = Path(a.logs), Path(a.repo), baseline_dir()
    mine = failures_in_logs(logs)
    died = unfinished_shards(logs)
    if not mine:
        print()
        print("=== failures vs origin/main ===")
        print("  The matrix failed but no shard named a failing test (a crash, timeout or")
        print("  OOM?). Nothing to compare: read the group logs. This is NOT a pass.")
        return 0
    base = _git(repo, "merge-base", "HEAD", "origin/main")
    walk = _git(repo, "rev-list", "--first-parent", f"-n{_MAX_WALK}", base).split() if base else []
    found = [(i, sha, d / f"{sha}.txt") for i, sha in enumerate(walk) if (d / f"{sha}.txt").exists()]
    print()
    print("=== failures vs origin/main ===")
    if not found:
        print(f"  No main baseline found in {d} (searched {len(walk)} commits back from the")
        print("  merge-base). Every failure below is unclassified, NOT proven yours:")
        for t in sorted(mine):
            print(f"    ? {t}")
        return 0
    behind, sha, path = found[0]
    known = read_baseline(path)
    recent: set[str] = set()
    for _, _, p in found[1:1 + _FLAKY_WINDOW]:
        recent |= read_baseline(p)
    pre = sorted(mine & known)
    flaky = sorted((mine - known) & recent)
    new = sorted(mine - known - recent)
    where = f"origin/main @{sha[:8]}"
    if behind:
        where += f" ({behind} commit(s) older than your merge-base {base[:8]})"
    print(f"  baseline: {where}, recorded {_header(path, 'recorded') or '?'}")
    print(f"  this run: {datetime.now().astimezone().isoformat(timespec='seconds')}"
          f" ({datetime.now(timezone.utc):%H:%M} UTC)")
    img = _header(path, "image")
    if a.image and img and img != "unknown" and img != a.image:
        print(f"  ⚠ baseline ran on image {img[:19]}, this run on {a.image[:19]}:"
              " an image change can move failures in either direction.")
    if new:
        print(f"\n  ✗ NEW — passed on main's baseline run, so probably yours ({len(new)}).")
        print("    Not proof: main's baseline is a single run, so a flaky test can pass there")
        print("    and fail here. Re-run the file once if the failure looks unrelated to your change:")
        for t in new:
            print(f"      {t}")
    if flaky:
        print(f"\n  ~ FLAKY ON MAIN — failed on a recent main baseline, not the nearest ({len(flaky)}).")
        print("    Re-run just these files once; if they pass, they are not yours:")
        for t in flaky:
            print(f"      {t}")
    if pre and not behind:
        print("\n  • ALREADY FAILING ON MAIN — not yours; do not investigate, do not copy")
        print(f"    the tree to re-prove it, do not fix it in this issue ({len(pre)}):")
        for t in pre:
            print(f"      {t}")
    elif pre:
        # The nearest baseline is older than the merge-base, so main may have
        # fixed one of these since and this branch broken it again: likely, not
        # proven. Never say "not yours" off a stale baseline.
        print(f"\n  • WAS RED ON MAIN {behind} commit(s) before your merge-base — probably not yours,")
        print("    NOT proven. Look only if your change touches what the test covers, or main")
        print(f"    touched its file since: git log {sha[:8]}..{base[:8]} -- <test file> ({len(pre)}):")
        for t in pre:
            print(f"      {t}")
    if died:
        # A dead shard's tests never ran, so the named reds are not the whole story.
        print(f"\n  ! {', '.join(died)} did not run all its tests (crash, timeout, OOM,")
        print("    or a collection error stopped the session): read that log — this report cannot clear it.")
    print()
    if new:
        print(f"VERDICT vs main: {len(new)} NEW failure(s) — fix these before committing,"
              " after one re-run of any that look unrelated to your change."
              + (" A shard also did not finish (see above)." if died else ""))
    elif died:
        print("VERDICT vs main: NOT CLEARED — a shard died; read its log before anything else.")
    elif flaky:
        print("VERDICT vs main: no new failures; only known-flaky ones — re-run those files once.")
    elif behind:
        print(f"VERDICT vs main: no new failures against a baseline {behind} commit(s) old;"
              " the WAS RED ones are likely, not proven, pre-existing.")
    else:
        print("VERDICT vs main: NO NEW FAILURES — every red here is already red on main.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--logs", required=True)
    r.add_argument("--sha", required=True)
    r.add_argument("--image", default="")
    c = sub.add_parser("compare")
    c.add_argument("--logs", required=True)
    c.add_argument("--repo", default=".")
    c.add_argument("--image", default="")
    a = p.parse_args(argv)
    try:
        return cmd_record(a) if a.cmd == "record" else cmd_compare(a)
    except Exception as exc:  # a report must never break the matrix
        print(f"matrix_vs_main: {a.cmd} skipped ({type(exc).__name__}: {exc})")
        return 0 if a.cmd == "compare" else 2


if __name__ == "__main__":
    sys.exit(main())
