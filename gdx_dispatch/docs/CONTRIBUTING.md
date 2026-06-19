# Contributing to GDX Platform

## Code Conventions

- **Python**: PEP 8, type hints on all public functions
- **Auth**: Use `@Depends(get_current_user)` for protected routes
- **DB queries**: Use `_tq(Model)` for tenant isolation, never `Model.query`
- **Commits**: Use `_safe_commit("context_label")` in routes
- **Audit**: Call `log_audit_event()` on all create/update/delete mutations
- **Soft delete**: Set `deleted_at`, never hard delete
- **JSON errors**: `jsonify({"error": "message"}), <status_code>`

## Adding a New Module

1. Create router in `gdx_dispatch/routers/your_module.py`
2. Add `dependencies=[Depends(require_module("your_module"))]` to the router
3. Register in `gdx_dispatch/app.py` with `app.include_router()`
4. Add module key to `gdx_dispatch/core/modules.py` AVAILABLE_MODULES
5. Write tests in `gdx_dispatch/tests/test_your_module.py`

## Testing

```bash
# Unit tests
.venv/bin/pytest gdx_dispatch/tests/ -v --tb=short

# Vue frontend tests
cd gdx_dispatch/frontend && npx vitest run

# E2E tests (requires live VPS)
GDX_BASE_URL=https://dev.example.com pytest gdx_dispatch/tests/ -m e2e -v
```

## PR Process

1. Branch from `main`
2. Write tests first (TDD)
3. All tests must pass before merge
4. Squash merge with descriptive commit message
