# PG Integration Gate — Commit-Time Trigger

The PG integration gate runs PostgreSQL integration tests during commit if changes affect database schema or test infrastructure.

## What It Does

The pre-commit hook automatically runs the PG integration gate (`gdx_dispatch/tools/run_pg_integration_tests.sh`) when you commit changes to high-risk paths that affect database schema or test infrastructure.

## Trigger Paths

| Glob | Why |
|------|-----|
| `gdx_dispatch/migrations/versions/*.py` | Control-plane migration DDL — must be valid PostgreSQL |
| `gdx_dispatch/models/platform*.py` | Control-plane ORM — migrations must match these |
| `gdx_dispatch/models/tenant_models.py` | Tenant-plane ORM — source of truth for `TenantBase.create_all()` |
| `gdx_dispatch/tests/factories/*.py` | Factories exercise the schema under test |

## What Happens When Triggered

1. Hook detects staged files matching trigger paths
2. Checks Docker is available
3. Spins up a throwaway `postgres:16-alpine` container on a random port
4. Runs `alembic upgrade head` against it
5. Runs 4 integration tests (`test_cross_tenant_isolation_golden`, `test_share_scenarios`, `test_share_path_isolation`, `test_capability_inheritance`)
6. Tears down the container
7. Blocks commit if any test fails

Total time: ~25 seconds when triggered.

## When It Does NOT Trigger

- Changes to routers, frontend, docs, config — no PG gate
- Changes to non-platform models (e.g., `gdx_dispatch/models/user.py`) — no PG gate
- Only platform models, alembic versions, and test factories trigger it

## Bypass

```bash
GDX_SKIP_PG_GATE=1 git commit -m "your message"
```

**When to bypass:**
- Docker is not installed or broken
- You're on a machine without Docker (CI will catch it)

**When NOT to bypass:**
- The PG gate found a real failure — fix it instead

## Installation

The hook is part of `gdx_dispatch/tools/pre_commit_test_gate.sh`. Install:

```bash
cp gdx_dispatch/tools/pre_commit_test_gate.sh .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

## Manual Run

Run the PG gate directly (without committing):

```bash
./gdx_dispatch/tools/run_pg_integration_tests.sh
```

Keep PostgreSQL running after tests for debugging:

```bash
./gdx_dispatch/tools/run_pg_integration_tests.sh --no-teardown
