#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPATCH_DIR="$ROOT_DIR/dispatch"
PYTEST_BIN="${PYTEST_BIN:-$ROOT_DIR/.venv/bin/pytest}"
MODE="${1:-all}"

if [[ ! -x "$PYTEST_BIN" ]]; then
  echo "pytest not found at $PYTEST_BIN" >&2
  exit 1
fi

cd "$DISPATCH_DIR"

case "$MODE" in
  fast)
    exec "$PYTEST_BIN" \
      tests/phases/test_phase0_baseline.py \
      tests/phases/test_phase1_foundation.py \
      tests/phases/test_phase2_contracts.py \
      -q
    ;;
  optional)
    exec "$PYTEST_BIN" tests/phases/test_phase3_data_integrity_optional.py \
      tests/phases/test_phase4_security_optional.py \
      tests/phases/test_phase5_ui_optional.py \
      tests/phases/test_phase6_performance_optional.py \
      tests/phases/test_phase7_operations_optional.py \
      -q
    ;;
  all)
    exec "$PYTEST_BIN" tests/phases -q
    ;;
  *)
    cat >&2 <<'EOF'
Usage:
  scripts/run_dispatch_phase_tests.sh fast
  scripts/run_dispatch_phase_tests.sh optional
  scripts/run_dispatch_phase_tests.sh all

Optional suite flags:
  ENABLE_PHASE3_DATA_INTEGRITY=1
  ENABLE_PHASE4_SECURITY=1
  ENABLE_PHASE5_UI=1
  ENABLE_PHASE6_PERFORMANCE=1
  ENABLE_PHASE7_OPERATIONS=1

Optional live envs by phase:
  Phase 3: DATABASE_URL or SQLALCHEMY_DATABASE_URI
  Phase 4: GDX_LIVE_BASE_URL GDX_TEST_USERNAME GDX_TEST_PASSWORD
  Phase 5: GDX_LIVE_BASE_URL
  Phase 6: GDX_LIVE_BASE_URL [GDX_HEALTH_PATH] [GDX_PERF_RUNS] [GDX_HEALTH_BUDGET_MS]
  Phase 7: [GDX_POST_DEPLOY_SMOKE_URL]
EOF
    exit 2
    ;;
esac