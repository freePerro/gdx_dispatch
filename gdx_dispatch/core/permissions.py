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
        "mobile.use", "mobile.dispatch_view", "mobile.chat",
    ],
    "technician": [
        "jobs.read_own", "jobs.write",
        "scheduling.read_own",
        "customers.read_own",
        "estimates.read_own",
        "inventory.read", "inventory.write",
        "pricing.labor_matrix.read",
        "mobile.use", "mobile.chat",
    ],
    "sales": [
        "customers.read_all", "customers.write",
        "leads.read", "leads.write", "leads.delete",
        "estimates.read_all", "estimates.write", "estimates.send",
        "invoices.read_all",
        "jobs.read_all",
        "pricing.labor_matrix.read", "pricing.labor_matrix.write",
        "vendor_statements.read",
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
    ],
    "viewer": [k for k in AVAILABLE_PERMISSIONS if ".read" in k],
}


# Platform-contract roles: their DB rows exist for UI display, but the
# permission resolver in gdx_dispatch/core/modules.py (S97 fix) reads from
# BUILTIN_ROLES at request time and ignores any drift in the DB row.
# Editing these would silently no-op, so the router 400s on the PATCH
# instead. Tenants customize by cloning to a new role.
# The other system roles (dispatcher, technician, sales, accounting,
# viewer) ARE tenant-editable — the resolver honors their DB row.
PLATFORM_LOCKED_ROLES: Final[frozenset[str]] = frozenset({"owner", "admin"})


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
