# Role Permissions

This document describes the RBAC system that backs `/role-permissions`. It covers the permission catalog, the seven builtin roles, the enforcement contract, and the recipe for adding a new permission.

> **See also:** [Role & Nav Naming Conventions](ROLE_AND_NAV_NAMING_CONVENTIONS.md) — canonical role spellings + `normalize_role`, and the permission-driven nav-visibility tiers (`nav.office` / `nav.admin`). Read it before adding a role comparison or a nav module.

## TL;DR

- **Catalog** lives in [`gdx_dispatch/core/permissions.py`](../core/permissions.py) — single source of truth for both backend enforcement and frontend UI.
- **Backend gate** is `Depends(require_permission("resource.action"))` — defined in [`gdx_dispatch/core/modules.py`](../core/modules.py).
- **Frontend gate** is `usePermission().hasPermission("resource.action")` — defined in [`gdx_dispatch/frontend/src/composables/usePermission.js`](../frontend/src/composables/usePermission.js). UX only — backend is the only enforcer.
- **Owner/admin escape hatch**: a JWT carrying `role: owner` or `role: admin` always passes. This prevents lock-out if an admin misclicks. Documented loudly in `permissions.py`.
- **Wildcard**: a role's permission list containing `"*"` grants every key (owner default).
- **Audit**: every permission change is logged to `audit_logs` with `action` prefixed `role_…`. Compliance feed: `GET /api/admin/permission-audit`.

## Permission key shape

Keys are `resource.action` — short, greppable, matches industry (ServiceTitan, Housecall Pro, Auth0). Examples:

| Key | Where it gates |
|---|---|
| `jobs.read_own` / `jobs.read_all` | tech-style "own" vs dispatcher-style "all" split |
| `invoices.write` | `+ New Invoice`, edit, line-item changes |
| `payroll.export` | CSV/QBO export of payroll runs |
| `settings.write` | `/role-permissions`, `/feature-flags`, `/admin-ops`, etc |
| `users.read` / `users.write` | `/users` view + invite/edit/delete actions |
| `webhooks.manage` | `/webhooks` |

The full list lives in `PERMISSIONS` in [`gdx_dispatch/core/permissions.py`](../core/permissions.py). The frontend reads it at runtime via `GET /api/role-permissions/permissions/catalog`.

## Builtin roles

Defined as `BUILTIN_ROLES` in `permissions.py`. Seeded per-tenant on provisioning (slice 1.4) and on first hit to `GET /api/role-permissions/roles` for legacy tenants.

| Role | Description | Notes |
|---|---|---|
| `owner` | `["*"]` — full access | Always passes via legacy escape hatch even with `[]` perms |
| `admin` | every key except `billing.write` | Always passes via legacy escape hatch |
| `dispatcher` | jobs/scheduling/customers/estimates "all" + invoice/payment read | The "shop floor coordinator" preset |
| `technician` | jobs/customers/scheduling "own" + inventory + mobile | Field tech preset |
| `sales` | customers/estimates write + send + invoice/jobs read | Quote-and-close preset |
| `accounting` | invoices write/send/refund + payments + payroll + accounting + reports | Books / billing preset |
| `viewer` | everything `.read*` | Read-only auditor / bookkeeper |

Builtin role rows have `is_system=true`. They are editable (perms can be toggled) but their `name` is locked. A "Reset to default" button on each row reloads the canonical seed via `POST /api/role-permissions/roles/{id}/reset`.

Custom (non-system) roles are unconstrained — admins can add "Lead Tech", "Lead Sales", anything (`is_system=false`). Custom-role permissions are unaffected by `Reset` and by the backfill script.

## Enforcement contract

### Backend

```python
from fastapi import Depends
from gdx_dispatch.core.modules import require_permission

@router.post("/api/invoices", dependencies=[Depends(require_permission("invoices.write"))])
def create_invoice(...): ...
```

`require_permission` resolves the caller's permission set in this order:

1. JWT claim `role in ("owner", "admin")` → pass (escape hatch).
2. `request.state.user_permissions` (per-request cache) → check.
3. `_load_user_permissions(db, request, user)` → join `tenant_roles` × `user_role_assignments` for the caller's user_id; if no row, fall back to `BUILTIN_ROLES[legacy_role_claim]`.
4. Wildcard `*` in the resolved set → pass.
5. Required keys ⊆ resolved set → pass; else `403 Missing permission: [...]`.

Backend remains the **only** enforcer. The frontend composable, sidebar filter, and route guard are UX hints. They prevent silent failures and 403 storms but do not gate anything that hasn't already been gated server-side.

### Frontend

```vue
<script setup>
import { usePermission } from '@/composables/usePermission';
const { hasPermission } = usePermission();
</script>

<template>
  <Button v-if="hasPermission('invoices.write')" label="+ New Invoice" />
</template>
```

For routes:

```js
{ path: '/payroll', component: PayrollView, meta: { requiresPermission: 'payroll.read' } }
```

Sidebar (`constants/modules.js`):

```js
{ key: 'payroll', label: 'Payroll', to: '/payroll', permission: 'payroll.read' }
```

## How to add a new permission

1. **Add it to the catalog** — edit `PERMISSIONS` in [`gdx_dispatch/core/permissions.py`](../core/permissions.py):

   ```python
   ("inventory.export", "Export inventory", "inventory"),
   ```

2. **Decide which builtin roles get it** — update the relevant entries in `BUILTIN_ROLES`. `owner` already has `*`; you may want to add it to `admin` and `accounting` (they get most exports).

3. **Gate the route** that does the work:

   ```python
   @router.get(
       "/api/inventory/export",
       dependencies=[Depends(require_permission("inventory.export"))],
   )
   def export_inventory(...): ...
   ```

4. **Gate the UI surface** if you want it hidden (UX, not security):
   - Sidebar entry: add `permission: 'inventory.export'` in `constants/modules.js`.
   - Route guard: add `meta: { requiresPermission: 'inventory.export' }` in `router/index.js`.
   - In-component: `v-if="hasPermission('inventory.export')"` on the action button.

5. **Reseed existing tenants** — run the backfill so live tenants pick up the new key on the builtin roles you updated:

   ```bash
   docker exec -w /app docker-app-1 python -m gdx_dispatch.tools.backfill_role_permissions --all
   ```

   Custom roles are untouched; admins re-customize them via the UI.

6. **Test** — add a row to `gdx_dispatch/tests/test_role_permissions_enforcement.py` proving the gate works for the role(s) that should pass and 403s for the role(s) that shouldn't.

## Operations

| Action | Command |
|---|---|
| List all permission keys | `curl /api/role-permissions/permissions` |
| Catalog (for UI) | `curl /api/role-permissions/permissions/catalog` |
| Resolve current user's perms | `curl /api/users/me/permissions` |
| Recent permission changes (audit) | `curl /api/admin/permission-audit?limit=100` |
| Reset a builtin role | `curl -XPOST /api/role-permissions/roles/{id}/reset` |
| Backfill all tenants | `python -m gdx_dispatch.tools.backfill_role_permissions --all` |
| Backfill one tenant | `python -m gdx_dispatch.tools.backfill_role_permissions --tenant <slug>` |

## Rate limit

Privileged writes (POST/PATCH/DELETE on `/api/role-permissions/...`) are rate-limited to **one per second per actor** via `_privileged_write_rate_limit` in [`gdx_dispatch/routers/role_permissions.py`](../routers/role_permissions.py). 429 with `Retry-After: 1` on overflow; every denial logs at WARNING with actor + IP so abuse shows up in logs.

## Migration banner

When the role-permissions backfill (slice 1.5) resets a tenant's builtin roles to canonical, a banner is set via `tenant_feature_flags.role_permissions_reset_pending=1`. The Roles & Permissions page reads `GET /api/role-permissions/migration-banner` on mount and shows a one-shot banner. An admin clicks "Got it" → `POST /api/role-permissions/migration-banner/ack` clears the flag and writes an audit row.

## Related docs

- [`ARCHITECTURAL_INVARIANTS.md`](../../ARCHITECTURAL_INVARIANTS.md) — the three-plane isolation laws this RBAC system runs on top of.
- [`CLAUDE.md`](../../CLAUDE.md) — "AI Access — Triple-Layer Safety" describes how AI tools layer on top of this same permission set.
- [`gdx_dispatch/core/permissions.py`](../core/permissions.py) — the catalog itself.
- [`gdx_dispatch/core/modules.py`](../core/modules.py) — `require_permission`, `_load_user_permissions`, `require_role` (legacy).
- Sprint plan: [`ai-queue/plans/sprint_role_permissions.md`](../../ai-queue/plans/sprint_role_permissions.md).
