# Role & Nav Naming Conventions

How we name roles and how nav-module visibility is decided. Read this before
touching role strings or adding a nav module. Companion to
[`role_permissions.md`](role_permissions.md).

---

## Part 1 — Role naming

### The two-form problem (history)

The same role has been spelled multiple ways:

| Concept | SHORT (legacy, stored in `users.role`) | LONG (canonical, RBAC catalog) |
|---|---|---|
| Field technician | `tech` | `technician` |
| Shop-floor coordinator | `dispatch` | `dispatcher` |
| Platform superadmin | — | `super_admin` (also seen: `superadmin`, `super-admin`) |

`users.role` stores the SHORT legacy form. `BUILTIN_ROLES` keys and
`tenant_roles.name` use the LONG form. JWTs carry whatever `users.role` holds.
This used to force every comparison to special-case both spellings.

### The rule

**Canonical = the LONG form** (matches `BUILTIN_ROLES` keys). There is ONE
normalizer on each side; always normalize before an in-memory comparison.

| | Source of truth | Normalizer | Predicates |
|---|---|---|---|
| Backend | [`core/roles.py`](../core/roles.py) | `normalize_role(raw)` | `is_dispatch_manager`, `is_role_admin_actor`, `is_admin_tier`, `is_technician` |
| Frontend | [`constants/roles.js`](../frontend/src/constants/roles.js) | `normalizeRole(raw)` | `isTechnician`, `isAdminTier`, `humanizeRole` |

`constants/roles.js` is a **mirror** of `core/roles.py` — keep the alias map and
canonical constants in sync (the tests `test_roles_canonical.py` /
`roles.spec.js` lock both ends).

### Do

```python
from gdx_dispatch.core import roles
if roles.is_technician(user_role): ...          # accepts 'tech' AND 'technician'
if roles.is_dispatch_manager(user): ...         # owner/admin/dispatcher/manager/superadmin
```
```js
import { isTechnician } from '@/constants/roles';
if (isTechnician(auth.user?.role)) { ... }      // accepts both spellings
```

### Don't

```python
if role == "tech" or role == "technician": ...  # ✗ scatters variant handling
DISPATCH = {"dispatcher", "admin", "owner"}      # ✗ new ad-hoc role set — use core/roles.py
```

### SQL is the ONE exception

A query that filters a role COLUMN must use that column's **stored** form —
normalize in Python, never in the query:

| Column | Stored form | Example |
|---|---|---|
| `users.role` | SHORT | `WHERE u.role = 'tech'` |
| `tenant_roles.name` | LONG | `WHERE r.name IN ('dispatcher','admin','owner')` |

If you need to compare a role you read from the DB against a canonical constant,
normalize it in Python after the fetch.

---

## Part 2 — Nav module visibility

### Single source of truth = permissions

Nav visibility is **permission-driven**. There is no longer a hardcoded role
allowlist. (The old `FIELD_TECH_MODULES` / `OFFICE_MODULES` Sets in
`useModuleSections.js`, which decided visibility by role STRING in parallel to
permissions, were deleted — they conflicted with the RBAC catalog.)

A user sees a module iff they hold the module's `permission`. Modules with **no**
`permission` are *field tier* — visible to every role.

### The three tiers

Every module in [`constants/modules.js`](../frontend/src/constants/modules.js)
is one of:

| Tier | `permission:` on the module | Who sees it |
|---|---|---|
| **field** | _(none)_ | everyone (all roles) |
| **office** | `nav.office` *(or its own fine-grained perm)* | dispatcher, sales, accounting, viewer, admin, owner |
| **admin** | `nav.admin` *(or its own fine-grained perm)* | admin, owner |

`nav.office` / `nav.admin` are nav-visibility permissions in
[`core/permissions.py`](../core/permissions.py) (category `navigation`). They
gate the nav ONLY — no API route enforces them. Grants:

- `nav.office` → dispatcher, sales, accounting, viewer (admin/owner inherit it).
- `nav.admin` → admin (via `_all_except`) and owner (wildcard) only.

A module that already has a meaningful fine-grained permission (e.g. `billing` →
`invoices.read_all`, `users` → `users.read`) keeps it instead of a `nav.*` tier
perm. That means a role holding the fine permission sees the module even if it's
admin-tier — this is intentional (e.g. the `accounting` role sees Expenses /
Payroll / Budget; a read-only `viewer` sees Users / Settings).

### Adding a nav module

1. Pick the gating permission:
   - Has a natural fine-grained perm already in the catalog? Use it.
   - Otherwise choose the tier: `nav.office` (office-wide) or `nav.admin`
     (admins only). Field-tier (visible to everyone, incl. techs)? Omit
     `permission`.
2. That's it — visibility is now editable per role in the **Roles & Permissions**
   UI (`/role-permissions`) by granting/revoking the permission.

The migration guard `useModuleSections.spec.js` enforces "every module is
ungated (field) or carries a permission" and that no role loses access.

---

## Why both live together

Roles and nav-permissions are the two halves of "who sees what". Keeping their
naming conventions in one place is the point: **one canonical role spelling, one
permission-driven visibility model, both editable in the UI, no hardcoded
parallel lists.**
