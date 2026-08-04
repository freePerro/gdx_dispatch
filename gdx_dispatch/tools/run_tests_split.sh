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

if [ "$fail" -ne 0 ]; then
  echo
  echo "FAIL — at least one shard reported errors. Logs in $LOG_DIR/"
  exit 1
fi
echo
echo "PASS — all $N shards green. Logs in $LOG_DIR/"
