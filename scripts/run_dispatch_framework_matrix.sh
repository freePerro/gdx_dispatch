#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISPATCH_DIR="$ROOT_DIR/archive/dispatch_flask"
PYTEST_BIN="${PYTEST_BIN:-$ROOT_DIR/.venv/bin/pytest}"
MODE="${1:-all}"

if [[ ! -x "$PYTEST_BIN" ]]; then
  echo "pytest not found at $PYTEST_BIN" >&2
  exit 1
fi

cd "$DISPATCH_DIR"

case "$MODE" in
  all)
    exec "$PYTEST_BIN" tests/frameworks -q
    ;;
  enabled)
    exec env \
      GDX_RUN_FRAMEWORK_01_API_CONTRACT=1 \
      GDX_RUN_FRAMEWORK_02_BROWSER=1 \
      GDX_RUN_FRAMEWORK_03_LOAD=1 \
      GDX_RUN_FRAMEWORK_04_SECURITY=1 \
      GDX_RUN_FRAMEWORK_05_PROPERTY_FUZZ=1 \
      GDX_RUN_FRAMEWORK_06_MOCKING=1 \
      GDX_RUN_FRAMEWORK_07_DATA_MIGRATION=1 \
      GDX_RUN_FRAMEWORK_08_MUTATION=1 \
      "$PYTEST_BIN" tests/frameworks -q
    ;;
  *)
    cat >&2 <<'EOF'
Usage:
  scripts/run_dispatch_framework_matrix.sh all
  scripts/run_dispatch_framework_matrix.sh enabled

Required environment for full framework 1 contract execution:
  GDX_SCHEMATHESIS_SCHEMA_URL
  GDX_SCHEMATHESIS_BASE_URL

Each framework test group uses its own enable flag:
  GDX_RUN_FRAMEWORK_01_API_CONTRACT
  GDX_RUN_FRAMEWORK_02_BROWSER
  GDX_RUN_FRAMEWORK_03_LOAD
  GDX_RUN_FRAMEWORK_04_SECURITY
  GDX_RUN_FRAMEWORK_05_PROPERTY_FUZZ
  GDX_RUN_FRAMEWORK_06_MOCKING
  GDX_RUN_FRAMEWORK_07_DATA_MIGRATION
  GDX_RUN_FRAMEWORK_08_MUTATION
EOF
    exit 2
    ;;
esac
