# GDX Platform — Developer Guide

## Architecture

- **Backend**: FastAPI + SQLAlchemy + PostgreSQL (per-tenant databases)
- **Frontend**: Vue 3 + Vite + PrimeVue + Pinia
- **Background**: Celery + Redis
- **Auth**: JWT with httpOnly refresh cookies
- **Multi-tenant**: x-tenant-id header + subdomain resolution

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

@router.get("")
def list_items(db=Depends(get_tenant_db), user=Depends(get_current_user)):
    ...
```

### 2. Register in app.py

```python
from gdx_dispatch.routers.your_module import router as your_module_router
app.include_router(your_module_router)
```

### 3. Add Module Key

Add `"your_module"` to `AVAILABLE_MODULES` in `gdx_dispatch/core/modules.py`.

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
# Python backend
.venv/bin/pytest gdx_dispatch/tests/ -v --tb=short

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
