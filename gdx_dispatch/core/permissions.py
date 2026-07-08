"""
Permission catalog — single source of truth for RBAC.

Shape: ``resource.action`` keys. The frontend reads this list to render the
role-permissions UI; the backend enforces against it via
``require_permission()``; audit logs reference the same keys.

Owner/admin escape hatch is enforced in the dependency, not here: a misclick
must NEVER lock the company out of their own tenant.

Wildcard: ``"*"`` granted to a role bypasses all checks (owner default).
"""
from __future__ import annotations

from typing import Final

from gdx_dispatch.core import roles as _roles

WILDCARD: Final = "*"


# (key, human_label, category) — category drives the checkbox grid grouping.
PERMISSIONS: Final[list[tuple[str, str, str]]] = [
    # Jobs
    ("jobs.read_own", "View own jobs", "jobs"),
    ("jobs.read_all", "View all jobs", "jobs"),
    ("jobs.write", "Create / edit jobs", "jobs"),
    ("jobs.delete", "Delete jobs", "jobs"),

    # Scheduling
    ("scheduling.read_own", "View own schedule", "scheduling"),
    ("scheduling.read_all", "View everyone's schedule", "scheduling"),
    ("scheduling.write", "Create / edit schedule entries", "scheduling"),

    # Customers
    ("customers.read_own", "View own customers", "customers"),
    ("customers.read_all", "View all customers", "customers"),
    ("customers.write", "Create / edit customers", "customers"),
    ("customers.delete", "Delete customers", "customers"),

    # Leads (sales pipeline — landing leads + leads; Sprint D-leads-authz-sweep)
    ("leads.read", "View the leads / landing-leads pipeline", "leads"),
    ("leads.write", "Create / edit / convert / advance leads", "leads"),
    ("leads.delete", "Delete leads and landing leads", "leads"),

    # Estimates
    ("estimates.read_own", "View own estimates", "estimates"),
    ("estimates.read_all", "View all estimates", "estimates"),
    ("estimates.write", "Create / edit estimates", "estimates"),
    ("estimates.send", "Send estimates to customers", "estimates"),

    # Invoices
    ("invoices.read_own", "View own invoices", "invoices"),
    ("invoices.read_all", "View all invoices", "invoices"),
    ("invoices.write", "Create / edit invoices", "invoices"),
    ("invoices.send", "Send invoices to customers", "invoices"),
    ("invoices.refund", "Issue refunds", "invoices"),

    # Payments
    ("payments.read", "View payments", "payments"),
    ("payments.process", "Process payments", "payments"),

    # Inventory
    ("inventory.read", "View inventory", "inventory"),
    ("inventory.write", "Adjust inventory", "inventory"),

    # Reports
    ("reports.read", "View reports", "reports"),
    ("reports.export", "Export reports", "reports"),

    # Dispatch (dispatch board + tech-efficiency report)
    ("dispatch.read", "View dispatch board and tech-efficiency report", "dispatch"),

    # Payroll
    ("payroll.read", "View payroll", "payroll"),
    ("payroll.write", "Edit payroll", "payroll"),
    ("payroll.export", "Export payroll", "payroll"),

    # Accounting
    ("accounting.read", "View accounting", "accounting"),
    ("accounting.write", "Edit accounting", "accounting"),
    ("accounting.export", "Export accounting", "accounting"),

    # Settings
    ("settings.read", "View settings", "settings"),
    ("settings.write", "Edit settings", "settings"),

    # Pricing — labor matrix (Sprint S97)
    ("pricing.labor_matrix.read", "View labor pricing matrix", "pricing"),
    ("pricing.labor_matrix.write", "Edit labor pricing matrix", "pricing"),

    # Vendor statements (Sprint vendor-statement-recon)
    ("vendor_statements.read", "View vendor statements", "vendor_statements"),
    ("vendor_statements.write", "Upload and manage vendor statements", "vendor_statements"),

    # Vendor invoices — A/P bill intake (Sprint vendor-invoice-intake)
    ("vendor_invoices.read", "View vendor bills", "vendor_invoices"),
    ("vendor_invoices.write", "Upload and confirm vendor bills", "vendor_invoices"),

    # Users
    ("users.read", "View users", "users"),
    ("users.write", "Invite / edit users", "users"),

    # Billing
    ("billing.read", "View billing", "billing"),
    ("billing.write", "Edit billing", "billing"),

    # Webhooks
    ("webhooks.manage", "Manage webhooks", "webhooks"),

    # Mobile (technician field app)
    ("mobile.use", "Use the mobile app", "mobile"),
    # Phase 4 Polish (Sprint tech_mobile)
    ("mobile.dispatch_view", "Use the mobile dispatch surface (/mobile/dispatch)", "mobile"),
    ("mobile.chat", "Send/receive per-job chat messages", "mobile"),

    # Navigation tiers — nav-visibility ONLY (no API route enforces these).
    # They replace the old hardcoded FIELD_TECH_MODULES / OFFICE_MODULES Sets in
    # the frontend: a module with no fine-grained permission is gated by its nav
    # tier instead, so module visibility is now a single permission-driven source
    # of truth that admins can edit per role in the Roles & Permissions UI.
    #   field tier  → ungated (every role; no permission needed)
    #   office tier → nav.office
    #   admin tier  → nav.admin
    ("nav.office", "See office-tier navigation modules", "navigation"),
    ("nav.admin", "See admin-tier navigation modules", "navigation"),
]


AVAILABLE_PERMISSIONS: Final[list[str]] = [k for (k, _l, _c) in PERMISSIONS]
PERMISSION_CATEGORIES: Final[list[str]] = list(dict.fromkeys(c for (_k, _l, c) in PERMISSIONS))


def _all_except(*prefixes: str) -> list[str]:
    return [k for k in AVAILABLE_PERMISSIONS if not any(k.startswith(p) for p in prefixes)]


def _by_prefix(*prefixes: str) -> list[str]:
    return [k for k in AVAILABLE_PERMISSIONS if any(k.startswith(p) for p in prefixes)]


# Builtin role names match existing TenantRole rows — do NOT rename.
# Tenants can edit these via /role-permissions; "Reset to default" reloads from here.
BUILTIN_ROLES: Final[dict[str, list[str]]] = {
    "owner": [WILDCARD],
    "admin": _all_except("billing.write"),
    "dispatcher": [
        "jobs.read_all", "jobs.write",
        "scheduling.read_all", "scheduling.write",
        "dispatch.read",
        "customers.read_all", "customers.write",
        "leads.read", "leads.write", "leads.delete",
        "estimates.read_all", "estimates.write",
        "invoices.read_all",
        "payments.read",
        "pricing.labor_matrix.read", "pricing.labor_matrix.write",
        "vendor_statements.read", "vendor_statements.write",
        "vendor_invoices.read", "vendor_invoices.write",
        "mobile.use", "mobile.dispatch_view", "mobile.chat",
        "nav.office",
    ],
    "technician": [
        "jobs.read_own", "jobs.write",
        "scheduling.read_own",
        "customers.read_own",
        "estimates.read_own",
        "inventory.read", "inventory.write",
        "pricing.labor_matrix.read",
        "mobile.use", "mobile.chat",
        # No nav.office / nav.admin — technicians are the field tier.
    ],
    "sales": [
        "customers.read_all", "customers.write",
        "leads.read", "leads.write", "leads.delete",
        "estimates.read_all", "estimates.write", "estimates.send",
        "invoices.read_all",
        "jobs.read_all",
        "pricing.labor_matrix.read", "pricing.labor_matrix.write",
        "vendor_statements.read",
        "vendor_invoices.read",
        "nav.office",
    ],
    "accounting": [
        "leads.read",
        "invoices.read_all", "invoices.write", "invoices.send", "invoices.refund",
        "payments.read", "payments.process",
        "payroll.read", "payroll.write", "payroll.export",
        "accounting.read", "accounting.write", "accounting.export",
        "reports.read", "reports.export",
        "billing.read",
        "pricing.labor_matrix.read", "pricing.labor_matrix.write",
        "vendor_statements.read", "vendor_statements.write",
        "vendor_invoices.read", "vendor_invoices.write",
        "nav.office",
    ],
    # viewer = read-only auditor across every module, plus office-tier nav so the
    # ungated office modules (which carry no .read permission) stay visible.
    "viewer": [k for k in AVAILABLE_PERMISSIONS if ".read" in k] + ["nav.office"],
}


# Platform-contract roles: their DB rows exist for UI display, but the
# permission resolver in gdx_dispatch/core/modules.py (S97 fix) reads from
# BUILTIN_ROLES at request time and ignores any drift in the DB row.
# Editing these would silently no-op, so the router 400s on the PATCH
# instead. Tenants customize by cloning to a new role.
# The other system roles (dispatcher, technician, sales, accounting,
# viewer) ARE tenant-editable — the resolver honors their DB row.
PLATFORM_LOCKED_ROLES: Final[frozenset[str]] = frozenset({"owner", "admin"})


# Roles whose *assignment* to a user is owner-exclusive. admin == owner for
# operations, but only an owner (or superadmin) may grant, change, or remove the
# admin/owner role on a user — that's the single owner-only privilege.
_OWNER_ASSIGNABLE_ROLES: Final[frozenset[str]] = frozenset({"owner", "admin"})

# Role groups now live in core/roles.py (single source of truth, variant-aware
# via normalize_role). Re-exported here for any legacy importer; both contain
# ONLY canonical forms — compare via the helpers / normalize_role, not raw `in`.
_ROLE_ADMIN_ACTORS: Final[frozenset[str]] = _roles.ROLE_ADMIN_ACTORS
DISPATCH_ROLES: Final[frozenset[str]] = _roles.DISPATCH_MANAGER_ROLES


def _actor_role(actor: object) -> str:
    if isinstance(actor, dict):
        return str(actor.get("role") or "").strip().lower()
    return str(getattr(actor, "role", "") or "").strip().lower()


def is_dispatch_manager(actor: object) -> bool:
    """True if the actor may act on other users' records (dispatch/admin tier).

    Delegates to core/roles.py, which normalizes legacy spellings (dispatch →
    dispatcher, etc.) before the tier check.
    """
    return _roles.is_dispatch_manager(_actor_role(actor))


def assert_can_assign_role(actor: object, target_role: str | None, current_role: str | None = None) -> None:
    """Raise 403 unless the actor may assign/change this role.

    Guards both directions: granting admin/owner to a user, AND changing the
    role of a user who is currently admin/owner (demote/edit). Only owner /
    superadmin pass. Everyone else (incl. admin) is blocked from touching the
    admin tier.
    """
    target = (target_role or "").strip().lower()
    current = (current_role or "").strip().lower()
    if target not in _OWNER_ASSIGNABLE_ROLES and current not in _OWNER_ASSIGNABLE_ROLES:
        return  # non-privileged role change — admins may do it
    if isinstance(actor, dict):
        actor_role = str(actor.get("role") or "")
    else:
        actor_role = str(getattr(actor, "role", "") or "")
    if _roles.is_role_admin_actor(actor_role):
        return
    from fastapi import HTTPException

    raise HTTPException(
        status_code=403,
        detail={
            "error_type": "insufficient_role",
            "detail": "only an owner may grant or change the admin/owner role",
            "required_roles": ["owner"],
        },
    )


BUILTIN_DESCRIPTIONS: Final[dict[str, str]] = {
    "owner": "Full access to everything including billing and tenant settings",
    "admin": "Administrative access (cannot edit billing)",
    "dispatcher": "Manage jobs, scheduling, customers, and estimates across the team",
    "technician": "Field technician — own jobs, own schedule, mobile app",
    "sales": "Quote and close — customers, estimates, invoice visibility",
    "accounting": "Invoicing, payments, payroll, accounting, financial reports",
    "viewer": "Read-only access across all modules (auditor / bookkeeper)",
}


def builtin_role_names() -> list[str]:
    return list(BUILTIN_ROLES.keys())


def is_known_permission(key: str) -> bool:
    return key == WILDCARD or key in AVAILABLE_PERMISSIONS
