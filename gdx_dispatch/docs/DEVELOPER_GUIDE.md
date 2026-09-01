# GDX Platform — Developer Guide

**Status: CURRENT** — architecture section corrected 2026-09-01; it described a
multi-tenant SaaS this project does not build (see below). The rest is
lightly maintained: treat code as the authority, this as orientation.

## Architecture

- **Backend**: FastAPI + SQLAlchemy + PostgreSQL
- **Frontend**: Vue 3 + Vite + PrimeVue + Pinia
- **Background**: Celery + Redis
- **Auth**: JWT with httpOnly refresh cookies
- **Single-tenant, forever** — one tenant per database; **isolation is the
  connection**, not a filter. Do **not** add `tenant_id`/`company_id` columns
  or `WHERE tenant_id = …` predicates to tenant-plane models: they are
  redundant, and they break on NULL (the 2026-04-22 documents bug). See
  `CLAUDE.md` and `BUILD_RULES.md` § Tenant Isolation.

  _This section previously read "PostgreSQL (per-tenant databases)" and
  "Multi-tenant: x-tenant-id header + subdomain resolution", contradicting
  `README.md` and describing an architecture that was never built here._

## Adding a New Module

### 1. Create the Router

```python
# gdx_dispatch/routers/your_module.py
from fastapi import APIRouter, Depends
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.database import get_tenant_db
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(
    prefix="/api/your-module",
    tags=["your-module"],
    dependencies=[Depends(require_module("your_module"))],
)

@router.get("", dependencies=[Depends(require_permission("your_module.read"))])
def list_items(db=Depends(get_tenant_db), user=Depends(get_current_user)):
    ...
```

**Mutations need two more things** the skeleton above omits, and both are
registry invariants, not style: a `require_permission(...)` gate
(`core/modules.py:508`) and a `log_audit_event()` call on every
create/update/delete. See `ARCHITECTURAL_INVARIANTS.md` #1.

### 2. Register in app.py

```python
from gdx_dispatch.routers.your_module import router as your_module_router
app.include_router(your_module_router)
```

### 3. Add Module Key

Add `"your_module"` to `MODULES` in `gdx_dispatch/core/modules.py:17`.
(The dict is named `MODULES`; this guide called it `AVAILABLE_MODULES` until
2026-09-01, and so does `BUILD_RULES.md`.)

### 4. Write Tests

```python
# gdx_dispatch/tests/test_your_module.py
# Follow the pattern in test_estimates.py:
# - Create SQLite in-memory DB
# - Add tenant middleware
# - Create module grant tables
# - Override get_tenant_db and get_current_user
```

### 5. Add Vue View (Optional)

```
gdx_dispatch/frontend/src/views/YourModuleView.vue
```

Add route in `router/index.js`.

## Running Tests

```bash
# Python backend — there is usually NO host .venv; deps live in the
# docker-app image, and gdx_dispatch/tests/conftest.py refuses a bare
# whole-directory run. One file:
docker run --rm --entrypoint python -v $PWD:/app -w /app docker-app \
  -m pytest gdx_dispatch/tests/test_<feature>.py -v
# Full suite (pytest-split, N=7):
PYTEST="docker run --rm --entrypoint python -e JWT_SECRET=<32+ bytes> \
  -v $PWD:/app -w /app docker-app -m pytest" \
  bash gdx_dispatch/tools/run_tests_split.sh

# Vue frontend
cd gdx_dispatch/frontend && npx vitest run

# E2E (requires live VPS)
pytest gdx_dispatch/tests/ -m e2e -v
```

## Conventions

- Audit logging: `log_audit_event()` on every create/update/delete
- Soft delete: set `deleted_at`, never hard delete
- Tenant isolation: use `get_tenant_db()`, never raw connection strings
- Error responses: `{"detail": "message"}` with appropriate HTTP status
