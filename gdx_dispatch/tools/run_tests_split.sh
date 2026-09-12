#!/usr/bin/env bash
# Run the pytest suite in N parallel pytest-split shards.
# Each shard is a fully independent pytest invocation — no xdist worker
# protocol, no shared fixtures across shards. Replaces `pytest -n N`.
#
# Usage:
#   gdx_dispatch/tools/run_tests_split.sh [pytest args]
#   N=4 gdx_dispatch/tools/run_tests_split.sh gdx_dispatch/tests/test_auth_*.py
#   PYTEST="docker run --rm --entrypoint python -e JWT_SECRET=<32+ bytes> \
#     -v $PWD:/app -w /app docker-app -m pytest" gdx_dispatch/tools/run_tests_split.sh
#
# Sweet spot from 2026-04-24 benchmark: N=7 on this laptop (14 cores).
# Beyond ~7 the per-process startup tax outpaces the parallelism gain.
#
# 2026-08-04 rewrite (assessment §7 item 3): the old script wiped the entire
# inherited addopts line (`-o addopts=`) to strip a long-gone `-n`/`--dist`.
# That also dropped the `-m "not e2e and not load and not health"` marker
# filter AND `-p no:schemathesis_xdist` — so a bare run collected tests/e2e/,
# whose test_schemathesis.py makes a NETWORK CALL at import time. addopts is
# now inherited from pytest.ini untouched; the explicit --ignore below is
# load-bearing (the marker filter alone runs after collection/import).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

N="${N:-7}"
LOG_DIR="${LOG_DIR:-/tmp/gdx_split}"
mkdir -p "$LOG_DIR"

# Resolve a Python that can actually run the suite. There is usually NO host
# venv for this repo — deps live in the docker-app image (see the PYTEST
# docker example above and docs). Order: explicit $PYTEST > .venv > python3.
if [ -z "${PYTEST:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTEST="$REPO_ROOT/.venv/bin/python -m pytest"
  else
    PYTEST="python3 -m pytest"
  fi
fi
# --version alone isn't enough — a host pytest without the app's deps fails
# every shard with usage errors. Probe the actual imports the suite needs.
# Only possible when $PYTEST is the "<python> -m pytest" form (stripping the
# suffix yields a python we can hand `-c`); for a bare pytest binary or other
# shapes, fall back to --version so we don't misparse (`pytest -c` reads a
# CONFIG FILE, not code — probing that way would fail with a misleading error).
if [[ "$PYTEST" == *" -m pytest" ]]; then
  PYBIN="${PYTEST% -m pytest}"
  if ! $PYBIN -c "import pytest, fastapi, pytest_split" >/dev/null 2>&1; then
    echo "✗ '$PYTEST' lacks the suite's dependencies (pytest/fastapi/pytest-split)."
    echo "  This repo usually has no host venv — run inside the docker-app image:"
    echo "  PYTEST=\"docker run --rm --entrypoint python -e JWT_SECRET=<32+ bytes> -v \$PWD:/app -w /app docker-app -m pytest\" $0"
    exit 2
  fi
elif ! $PYTEST --version >/dev/null 2>&1; then
  echo "✗ '$PYTEST' cannot run pytest at all. See the docker-app example in the header."
  exit 2
fi

# ── dependency drift gate (#679) ───────────────────────────────────────────
# The image bakes gdx_dispatch/requirements.txt at BUILD time; the working tree
# is bind-mounted over /app at RUN time. So source is always current and deps
# never are: add a dependency, and it is absent from the harness until someone
# rebuilds, with nothing to tell you. The failure mode is a COLLECTION error
# ("ModuleNotFoundError: No module named 'freezegun'"), which takes a whole file
# out of the run and reads like noise next to a wall of passes. CI is immune —
# it pip-installs on a fresh runner — which is exactly why this is easy to miss
# on mid-stack PRs, where the local matrix is the only gate (ci.yml does not
# trigger on a PR whose base is another feature branch).
#
# `pip install --dry-run --no-index` is the whole check: --no-index keeps it
# offline (~2s, no network, no resolver round-trips), exit 0 when every
# requirement is satisfied by what is installed, exit 1 otherwise.
#
# Measured 2026-09-12 — this gate CAN fail, which is the point:
#   clean tree ............................................. exit 0
#   version drift (freezegun>=99.0 vs installed 1.5.5) ...... exit 1
#   missing package (the #679 shape) ........................ exit 1
# `pip check` was rejected as the instrument: it verifies that INSTALLED
# packages agree with each other and never reads requirements.txt, so it
# returns "No broken requirements found" / exit 0 with the defect present.
#
# SKIP_DEP_CHECK=1 bypasses it. Deliberately not silent when you do.
REQ_FILE="gdx_dispatch/requirements.txt"
if [ "${SKIP_DEP_CHECK:-0}" = "1" ]; then
  echo "⚠ dependency drift check SKIPPED (SKIP_DEP_CHECK=1)"
elif [ -n "${PYBIN:-}" ] && [ -f "$REQ_FILE" ]; then
  dep_log="$LOG_DIR/dep_drift.log"
  if $PYBIN -m pip install --dry-run --no-index -r "$REQ_FILE" > "$dep_log" 2>&1; then
    :
  else
    echo "✗ dependency drift: the test environment does not satisfy $REQ_FILE"
    echo
    grep -E "^ERROR:|No matching distribution|ResolutionImpossible" "$dep_log" | head -5 | sed 's/^/    /'
    echo
    echo "  The image bakes requirements at build time. Rebuild it:"
    echo "    docker compose -f gdx_dispatch/docker/docker-compose.yml build app"
    echo "  Full log: $dep_log   Bypass (not advised): SKIP_DEP_CHECK=1 $0"
    exit 3
  fi
else
  # Say so rather than skip in silence. `PYBIN` is only set for the
  # "<python> -m pytest" shape of $PYTEST; a bare `pytest` binary gives us no
  # interpreter to ask about its own site-packages, so the check cannot run.
  # An unrunnable gate that prints nothing is indistinguishable from a gate
  # that passed — the exact shape this script now exists to prevent.
  if [ -z "${PYBIN:-}" ]; then
    echo "⚠ dependency drift NOT CHECKED: \$PYTEST is not the '<python> -m pytest' form,"
    echo "  so there is no interpreter to query. A stale dependency set would be invisible."
  elif [ ! -f "$REQ_FILE" ]; then
    echo "⚠ dependency drift NOT CHECKED: $REQ_FILE not found from $REPO_ROOT."
  fi
fi

# addopts comes from pytest.ini (marker filter + -q + -p no:schemathesis_xdist).
# --ignore is REQUIRED on top of it: e2e/test_schemathesis.py performs a
# network call at import time, and marker filtering happens after import.
# FORKED=1 re-enables per-test subprocess isolation. Default matches ci.yml
# (unforked since 2026-08-04, #20 re-test) — keep the two in lockstep.
COMMON_OPTS=(--ignore=gdx_dispatch/tests/e2e --tb=short)
if [ "${FORKED:-0}" = "1" ]; then
  COMMON_OPTS+=(--forked)
fi

pids=()
for g in $(seq 1 "$N"); do
  $PYTEST "${COMMON_OPTS[@]}" --splits "$N" --group "$g" "$@" \
      > "$LOG_DIR/group_${g}.log" 2>&1 &
  pids+=("$!")
done

fail=0
i=0
for pid in "${pids[@]}"; do
  i=$((i + 1))
  set +e
  wait "$pid"
  rc=$?
  set -e
  # pytest exit 5 = "no tests collected" — expected when N > test count.
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then
    fail=1
    echo "✗ group $i failed (exit $rc) — see $LOG_DIR/group_${i}.log"
  fi
done

echo
echo "=== per-shard summary ==="
for g in $(seq 1 "$N"); do
  printf "group %s: %s\n" "$g" "$(tail -1 "$LOG_DIR/group_${g}.log")"
done

# Collection errors are the loudest-consequence, quietest-looking failure here
# (#679): the file never ran at all, and the tail line says "1 error" next to a
# wall of passes. Name the files instead of leaving them in the scroll. This is
# a REPORT, not the gate — pytest exits 2 on a collection error, so `fail` is
# already set above; surfacing it separately means it cannot be skimmed past.
# Anchor on pytest's "short test summary info" line (`ERROR <path>`), NOT on
# the banner `____ ERROR collecting <path> ____` — the banner is padded with
# underscores, so `^ERROR collecting` matches nothing. Verified 2026-09-12 by
# planting a module that fails to import and reading the log.
#
# `|| true` is load-bearing: `set -e` is in force here (re-enabled inside the
# wait loop above), and grep exits 1 when it finds nothing — which is the
# HAPPY path. Without it a clean run dies silently right before "PASS".
collect_errors="$(grep -hE "^ERROR [^ ]+\.py" "$LOG_DIR"/group_*.log 2>/dev/null | sort -u || true)"
missing_mods="$(grep -hoE "ModuleNotFoundError: No module named '[^']+'" "$LOG_DIR"/group_*.log 2>/dev/null | sort -u || true)"
if [ -n "$collect_errors" ]; then
  echo
  echo "✗ COLLECTION ERRORS — these files did NOT run:"
  echo "$collect_errors" | sed 's/^/    /'
  if [ -n "$missing_mods" ]; then
    echo
    echo "  Missing imports:"
    echo "$missing_mods" | sed 's/^/    /'
  fi
  echo "  A collection error removes the whole file from the run. If this is an"
  echo "  ImportError for a package in requirements.txt, rebuild the image."
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "FAIL — at least one shard reported errors. Logs in $LOG_DIR/"
  exit 1
fi
echo
echo "PASS — all $N shards green. Logs in $LOG_DIR/"
