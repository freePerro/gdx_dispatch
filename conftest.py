"""Repo-root conftest — last-line enforcement of the pytest-split rule.

Layer C of the pytest-split guardrail (an earlier session, 2026-05-09):
- Layer A: CLAUDE.md docs point at run_tests_split.sh
- Layer B: ~/.claude/hooks/pytest_split_guard_pre.py blocks Claude's
  Bash invocations of full-suite pytest before they run
- Layer C (this file): if anyone — Claude, a human, CI, a forgotten
  cron — invokes `pytest gdx_dispatch/tests/` directly, abort with a clear
  redirect message before collection burns 30s of imports.

Allowed
-------
- pytest gdx_dispatch/tests/test_<feature>.py     (single file, serial is fine)
- pytest gdx_dispatch/tests/test_a.py gdx_dispatch/tests/test_b.py   (multiple files)
- pytest -k <pattern>                    (no path arg)
- pytest with --splits/--group           (the canonical fast path)

Blocked
-------
- pytest gdx_dispatch/tests/        (bare directory = full suite, ~5 min serial)
- pytest gdx_dispatch/tests         (same, no slash)
- pytest                   (no args; collects everything serially)

Override
--------
PYTEST_FULL_SERIAL=1 prefix the command. Use ONLY when debugging
cross-test pollution where parallel splits would mask the bug.
"""
from __future__ import annotations

import os
import sys


def _looks_like_full_suite(args: list[str]) -> bool:
    """Return True iff the pytest invocation is a bare full-suite run.

    True when:
      - no positional args (pytest collects from testpaths)
      - the only positional path arg is `gdx_dispatch/tests` (with or without slash)
    False when:
      - any positional path points at a specific .py file
      - --splits/--group is present (split runner)
    """
    has_split_flag = any(a.startswith("--splits") or a.startswith("--group") for a in args)
    if has_split_flag:
        return False

    # Strip flags and their values to find positional paths.
    positionals: list[str] = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a.startswith("-"):
            # crude: assume "-k pattern" / "-m mark" take a value
            if a in {"-k", "-m", "-p", "--splits", "--group", "-c", "-o", "--ignore", "--tb"}:
                skip_next = True
            continue
        positionals.append(a)

    if not positionals:
        # Bare `pytest` with no path → testpaths in pytest.ini = gdx_dispatch/tests/
        return True

    # If any positional is a specific .py file, it's targeted.
    if any(p.endswith(".py") for p in positionals):
        return False

    # If a positional is a deeper subdir, also OK (e.g. gdx_dispatch/tests/cc/).
    suite_paths = {"gdx_dispatch/tests", "gdx_dispatch/tests/", "."}
    if any(p in suite_paths for p in positionals):
        return True

    return False


def pytest_configure(config):  # noqa: D401 — pytest hook
    if os.environ.get("PYTEST_FULL_SERIAL") == "1":
        return
    if os.environ.get("PYTEST_DISABLE_SPLIT_GUARD") == "1":
        return

    # invocation_params.args is the user's CLI args after pytest's own
    # `addopts` is folded in. Check ONLY user-supplied args.
    raw_args = list(getattr(config, "invocation_params", None).args or ())
    if not _looks_like_full_suite(raw_args):
        return

    msg = (
        "\n"
        "🐢 PYTEST-SPLIT GUARD — full-suite serial pytest blocked\n"
        "\n"
        "    Bare `pytest gdx_dispatch/tests/` runs SERIALLY (~5 min wall-clock).\n"
        "    pytest.ini explicitly disables xdist; the canonical fast\n"
        "    path is pytest-split, N=7, ~7× faster, identical coverage.\n"
        "\n"
        "    Use:    bash gdx_dispatch/tools/run_tests_split.sh\n"
        "\n"
        "    For ONE file, serial is fine and faster than split startup:\n"
        "            .venv/bin/pytest gdx_dispatch/tests/test_<feature>.py -v\n"
        "\n"
        "    Override (rare — debugging cross-test pollution):\n"
        "            PYTEST_FULL_SERIAL=1 .venv/bin/pytest gdx_dispatch/tests/\n"
        "\n"
        "    Why this guardrail exists: earlier sessions–110 repeatedly\n"
        "    defaulted to the slow form. CLAUDE.md + a Claude-side\n"
        "    Bash hook + this conftest are the three layers.\n"
    )
    print(msg, file=sys.stderr)
    raise SystemExit(2)
