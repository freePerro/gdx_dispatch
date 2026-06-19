#!/usr/bin/env bash
# Run the pytest suite in N parallel pytest-split shards.
# Each shard is a fully independent pytest invocation — no xdist worker
# protocol, no shared fixtures across shards. Replaces `pytest -n N`.
#
# Usage:
#   gdx_dispatch/tools/run_tests_split.sh [pytest args]
#   N=4 gdx_dispatch/tools/run_tests_split.sh gdx_dispatch/tests/test_auth_*.py
#
# Sweet spot from 2026-04-24 benchmark: N=7 on this laptop (14 cores).
# Beyond ~7 the per-process startup tax outpaces the parallelism gain.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

N="${N:-7}"
PYTEST="${PYTEST:-.venv/bin/python -m pytest}"
LOG_DIR="${LOG_DIR:-/tmp/gdx_split}"
mkdir -p "$LOG_DIR"

# Strip any -n / --dist from inherited addopts via -o.
COMMON_OPTS=(-o "addopts=" --tb=short)

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
