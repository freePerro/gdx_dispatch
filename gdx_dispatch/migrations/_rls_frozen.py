"""Frozen RLS target lists + policy-SQL generators for historical migrations.

RELOCATED VERBATIM (public-release strip, 2026-06-15) from the former
``gdx_dispatch/tools/{control_plane_rls_targets,commerce_plane_rls_targets,
rls_remaining_tables}.py``. These feed alembic migrations 007/024/025/027/031/033
ONLY. Frozen — do not edit; do not import from application code. The live DB is
already past these revisions, so changes here affect fresh-install upgrades only.
"""
from __future__ import annotations


# ===== from control_plane_rls_targets.py =====
"""Canonical list of control-plane tables covered by tenant-isolation RLS.

Source of truth shared between:
  * ``gdx_dispatch/migrations/versions/024_control_plane_rls.py`` — the migration
    that ENABLEs+FORCEs RLS and installs the policy.
  * ``gdx_dispatch/migrations/versions/031_d97_swap_uuid_columns.py`` — the D97
    swap that re-renders every policy after column types flip to UUID.
  * ``gdx_dispatch/tests/test_control_plane_rls.py`` — the PG-gate integration
    tests that verify the policy surface + mechanics.

Adding a new control-plane tenant-scoped table:
  1. Add it to the appropriate tuple below.
  2. Write a new alembic migration that calls ``policy_sql(table, column, cast)``
     from this module (or inlines the same pattern).
  3. The test suite picks it up automatically.

Post-D97 (an earlier session): every tenant-id column on every control-plane
table is ``uuid`` (the slug→UUID swap landed in migration 031). The
GUC ``app.tenant_id`` is text, so all policies use ``::text`` cast on
the column to compare. ``TEXT_TENANT_TABLES`` is empty by design.

Deferred tables (nullable tenant column — NULL means "platform-scoped",
needs a separate is_platform context decision):
    audit_logs, event_outbox, game_definitions, game_state,
    platform_feature_flags, resource_field_descriptors, resource_type,
    ss21_authorization_codes, ss21_oauth_tokens

Commerce-plane tables (Phase A3, bidirectional predicates):
    cross_tenant_share, cross_tenant_share_acceptance, and any model with
    supplier_tenant_id + dealer_tenant_id twin columns.
"""


# Reserved for any future control-plane table that legitimately stores
# its tenant key as text (none today; D97 eliminated the last ones).
TEXT_TENANT_TABLES: tuple[str, ...] = ()

# tenant_id : uuid, NOT NULL — predicate casts column to text.
UUID_TENANT_TABLES: tuple[str, ...] = (
    "audit_retention_policy",
    "billing_overage_event",
    "billing_plan",
    "cutover_schedule",
    "deprecated_table_record",
    "installations",
    "mcp_execution_log",
    "mcp_tool_execution_audit",
    "memberships",
    "metering_checkpoint",
    "metering_usage",
    "resource_instance",
    "sandbox_envs",
    "shadow_migration_checkpoint",
    "shadow_migration_drift",
    "shadow_migration_state",
    "ss21_admin_consent_grants",
    "ss21_webhook_subscriptions",
    "ss31_federation_provider",
    "sso_configs",
    "platform_consumer_audit",
    "tenant_module_grants",
    "tenant_settings",
)

# owner_tenant_id : uuid, NOT NULL — same ``::text`` cast pattern.
OWNER_TENANT_TABLES: tuple[str, ...] = ("shared_resources",)


def policy_sql(table: str, column: str, cast: str = "") -> str:
    """ENABLE+FORCE RLS and install ``<table>_tenant_isolation`` (FOR ALL).

    Single predicate (``USING``+``WITH CHECK`` same) keeps read and write
    in lock-step. ``DROP POLICY IF EXISTS`` makes re-apply idempotent.
    """
    expr = f"{column}{cast} = current_setting('app.tenant_id', true)"
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};
CREATE POLICY {table}_tenant_isolation
    ON {table}
    FOR ALL
    USING ({expr})
    WITH CHECK ({expr});
"""

# ===== from commerce_plane_rls_targets.py =====
"""Canonical list of commerce-plane tables with tenant-isolation RLS.

Commerce plane = shared-DB tables where a row is legitimately visible to
MORE THAN ONE tenant (e.g., both parties of a cross-tenant resource share).
RLS predicates are compound — a tenant qualifies as a party if its id
appears in any of the party columns on the row.

Source of truth shared between:
  * ``gdx_dispatch/migrations/versions/025_commerce_plane_rls.py`` — the migration.
  * ``gdx_dispatch/tests/test_commerce_plane_rls.py`` — policy-mechanics tests.

Targets (2026-04-24):
  * ``cross_tenant_share`` — two parties: sharer_tenant_id + sharee_tenant_id.
    Both can read; only the sharer can write (can't forge a share FROM
    another tenant).
  * ``cross_tenant_share_acceptance`` — single party: accepted_by_tenant_id
    (the accepter). Narrow first: only the accepter sees their own row.
    The sharer can discover acceptances via a server-side helper that
    JOINs through cross_tenant_share; pushing that into RLS would require
    a subquery per row (workable but more complex — deferred until there
    is a concrete read-path that needs it).

Non-targets (explicitly out of scope for A3):
  * ``tenant_relationships`` + ``cross_tier_module_grants`` (gdx_dispatch/control/
    relationships.py) — models exist but tables are NOT present on lab
    control-db. No migration has created them. Re-classify when /  if
    they land.
  * dealer_orders / wholesale.CatalogItem / wholesale.PricingTier /
    wholesale.ChannelAnalytic / distributor.DealerOrder /
    distributor.DistributorAnalytic — live in per-tenant DBs (tenant
    plane). Under the three-plane model, tenant plane doesn't need RLS
    (connection is the isolation). The sprint plan's framing of these
    as commerce-plane was pre-audit; the actual schema places them on
    the tenant plane.
"""


def cross_tenant_share_policy_sql() -> str:
    """cross_tenant_share: both parties can read, only sharer can write.

    USING: either the sharer or the sharee can see the row.
    WITH CHECK: only the sharer can INSERT/UPDATE the row — prevents a
    tenant from unilaterally creating a share FROM another tenant, or
    from moving a row to a new sharer.
    """
    # Post-D97 (031): party columns are uuid; cast to text so the comparison
    # against the text-form ``current_setting('app.tenant_id', true)`` GUC
    # is type-aligned.
    read_expr = (
        "current_setting('app.tenant_id', true) IN "
        "(sharer_tenant_id::text, sharee_tenant_id::text)"
    )
    write_expr = "sharer_tenant_id::text = current_setting('app.tenant_id', true)"
    return f"""
ALTER TABLE cross_tenant_share ENABLE ROW LEVEL SECURITY;
ALTER TABLE cross_tenant_share FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS cross_tenant_share_parties_read ON cross_tenant_share;
DROP POLICY IF EXISTS cross_tenant_share_sharer_write ON cross_tenant_share;
CREATE POLICY cross_tenant_share_parties_read
    ON cross_tenant_share
    FOR SELECT
    USING ({read_expr});
CREATE POLICY cross_tenant_share_sharer_write
    ON cross_tenant_share
    FOR ALL
    USING ({write_expr})
    WITH CHECK ({write_expr});
"""


def cross_tenant_share_acceptance_policy_sql() -> str:
    """cross_tenant_share_acceptance: only the accepter sees/writes their own row."""
    # Post-D97 (031): accepted_by_tenant_id is uuid; ::text cast aligns
    # with the text-form GUC.
    expr = "accepted_by_tenant_id::text = current_setting('app.tenant_id', true)"
    return f"""
ALTER TABLE cross_tenant_share_acceptance ENABLE ROW LEVEL SECURITY;
ALTER TABLE cross_tenant_share_acceptance FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS cross_tenant_share_acceptance_accepter_only
    ON cross_tenant_share_acceptance;
CREATE POLICY cross_tenant_share_acceptance_accepter_only
    ON cross_tenant_share_acceptance
    FOR ALL
    USING ({expr})
    WITH CHECK ({expr});
"""


TARGET_TABLES: tuple[str, ...] = (
    "cross_tenant_share",
    "cross_tenant_share_acceptance",
)

# ===== from rls_remaining_tables.py =====
"""SS-17 slice A — enumeration of REMAINING tenant-scoped tables beyond SS-16's 4 criticals.

DEPRECATED UNDER THREE-PLANE (2026-04-24, Phase A4)
---------------------------------------------------
Targets the TENANT plane (per-tenant DBs). Under the three-plane model
(ARCHITECTURAL_STATE.md), tenant-plane isolation is the connection
itself — RLS here is an architectural no-op. Kept for SS-17 migration
history and the test_rls_remaining_integration.py mechanics tests.

For CURRENT RLS work:
  * Control-plane → gdx_dispatch/tools/control_plane_rls_targets.py + migration 024.
  * Commerce-plane → gdx_dispatch/tools/commerce_plane_rls_targets.py + migration 025.

Do NOT add tenant-plane tables here for new RLS work.
───────────────────────────────────────────────────

SS-16 applied RLS to the 4 highest-traffic critical tables (jobs / customers /
invoices / leads) via :data:`gdx_dispatch.tools.rls_render.CRITICAL_TABLES`. SS-17
extends the exact same rendering pattern to the rest of the tenant-scoped
surface so RLS coverage is not "theater" (see SS-17 plan "Why this SS exists"
and the 'ORM Was Lying' session lesson).

This module is a *sibling* to ``rls_render.py`` — per the SS-17 scope, we do
NOT edit ``rls_render.py`` or its template. We import
:func:`~gdx_dispatch.tools.rls_render.render_policies` and feed it a new table list.

Access-pattern classification
-----------------------------
Every remaining tenant-scoped table gets the same SQL shape as SS-16:

* a ``<table>_select_policy`` that requires ``company_id = app.tenant_id``
  AND (role is owner/admin OR ``${select_extra_predicate}``)
* a ``<table>_write_policy`` that restricts INSERT/UPDATE/DELETE to
  owner/admin within the same tenant (matches SS-16 exactly)

Per-table knobs
---------------
* ``select_extra_predicate`` — additional predicate that lets non-admin roles
  (e.g. ``tech``) read a subset. For the remaining-tables set, there are no
  tech-assignment columns today — every entry uses ``"FALSE"`` (admin/owner
  only). Tech-visibility RLS is deferred to D73 (requires
  ``assigned_to_identity_id`` column to be built + backfilled first).
* ``soft_delete_column`` — column name whose IS NULL presence must hold for
  the row to be visible (red-team Pattern 3 closure). Populated for every
  table that carries a ``deleted_at`` column in ``gdx_dispatch/models/*``; ``None``
  for tables without one.

2026-04-21 (an earlier session) restructure
-----------------------------------
The previous version of this file listed ~114 tables. The 0.9-r PG coverage
test revealed that 49 of those tables DO NOT carry a ``company_id`` column
in ``TenantBase.metadata`` — the rendered policy references ``company_id``
directly, so the migration silently skipped every one. The list was
"advertising" RLS coverage that did not exist.

The 49 fall into recognizable patterns:

* **Views (9)** — ``*_router`` compatibility views. PostgreSQL can't
  ENABLE ROW LEVEL SECURITY on views directly.
* **Join-scoped tables** — e.g., ``invoice_lines`` (tenant-scoped via
  parent ``invoices.company_id``). RLS by join requires either a column
  backfill or a different policy shape (subquery against parent).
* **Historical/log tables** — e.g., ``equipment_asset_history``.
* **Singleton per-tenant tables** — e.g., ``app_settings`` (one row per
  tenant, isolation via DB-level connection or single-row constraint).

They've been split into :data:`TABLES_PENDING_RLS_AUDIT` so the canonical
:data:`REMAINING_TABLES` list only contains entries that actually receive
RLS protection today. D74 tracks the per-table audit.

DO NOT touch ``rls_render.py`` or the 4 CRITICAL_TABLES from this module —
they are load-bearing for SS-16 and explicitly out-of-scope for SS-17
(see SS-17 plan "DO NOT TOUCH").
"""

# ``REMAINING_TABLES`` is the canonical list of tables that get RLS policies
# applied by migration 007_ss17_rls_remaining.py. Every entry here must
# carry a ``company_id`` column on its live schema — enforced by the
# coverage test ``test_rls_production_coverage_pg.py``.
#
# Shape: ``(table_name, select_extra_predicate, soft_delete_column)``.
REMAINING_TABLES: list[tuple[str, str, str | None]] = [
    # core CRM / ops
    ("appointments", "FALSE", "deleted_at"),
    ("customer_locations", "FALSE", "deleted_at"),
    ("customer_reviews", "FALSE", "deleted_at"),
    ("document_signatures", "FALSE", "deleted_at"),
    ("follow_ups", "FALSE", "deleted_at"),
    ("holding_areas", "FALSE", "deleted_at"),
    ("inbound_emails", "FALSE", None),
    ("inbound_sms", "FALSE", None),
    ("internal_tasks", "FALSE", "deleted_at"),
    ("job_notes", "FALSE", "deleted_at"),
    ("job_photos", "FALSE", "deleted_at"),
    ("job_parts_needed", "FALSE", None),
    ("landing_leads", "FALSE", "deleted_at"),
    ("message_threads", "FALSE", None),
    ("mobile_sync_actions", "FALSE", None),
    ("planner_tasks", "FALSE", None),
    ("plans", "FALSE", None),
    ("proposals", "FALSE", "deleted_at"),
    ("resources", "FALSE", "deleted_at"),
    ("safety_checklists", "FALSE", "deleted_at"),
    ("sticky_notes", "FALSE", "deleted_at"),
    ("survey_responses", "FALSE", None),
    ("survey_sends", "FALSE", None),
    ("survey_templates", "FALSE", "deleted_at"),
    ("tags", "FALSE", "deleted_at"),
    ("tag_assignments", "FALSE", None),
    ("team_messages", "FALSE", "deleted_at"),
    ("team_message_recipients", "FALSE", None),
    ("tech_commission_rates", "FALSE", "deleted_at"),
    ("tech_unavailability", "FALSE", "deleted_at"),
    ("technicians", "FALSE", "deleted_at"),
    ("users", "FALSE", "deleted_at"),
    ("warranty_claims", "FALSE", "deleted_at"),
    # financial (money-moving — admin/owner-only everywhere)
    ("commission_entries", "FALSE", None),
    ("commission_rules", "FALSE", None),
    ("estimates", "FALSE", "deleted_at"),
    ("estimate_lines", "FALSE", None),
    ("expenses", "FALSE", "deleted_at"),
    ("invoice_lines", "FALSE", None),
    ("markup_rules", "FALSE", "deleted_at"),
    ("payments", "FALSE", None),
    ("reminder_settings", "FALSE", None),
    ("saas_subscriptions", "FALSE", None),
    ("tax_jurisdictions", "FALSE", "deleted_at"),
    ("time_entries", "FALSE", "deleted_at"),
    ("timeclocks", "FALSE", None),
    # loyalty / marketing
    ("estimate_nurture_rules", "FALSE", None),
    ("loyalty_referrals", "FALSE", "deleted_at"),
    ("marketing_campaigns", "FALSE", "deleted_at"),
    ("winback_campaigns", "FALSE", "deleted_at"),
    ("winback_sends", "FALSE", None),
    # automations / catalog / inventory
    ("maintenance_plans", "FALSE", "deleted_at"),
    ("pdf_templates", "FALSE", None),
    ("plan_enrollments", "FALSE", "deleted_at"),
    ("po_requests", "FALSE", "deleted_at"),
    ("service_agreements", "FALSE", "deleted_at"),
    ("service_agreement_templates", "FALSE", "deleted_at"),
    ("service_triggers", "FALSE", "deleted_at"),
    ("supplier_catalog", "FALSE", None),
    ("supplier_orders", "FALSE", None),
    ("van_inventory", "FALSE", "deleted_at"),
    # notifications / settings / permissions
    ("bug_reports", "FALSE", None),
    ("client_errors", "FALSE", None),
    ("company_module_grants", "FALSE", None),
    ("custom_field_definitions", "FALSE", "deleted_at"),
    ("custom_field_values", "FALSE", None),
    ("email_settings", "FALSE", None),
    ("feature_flags", "FALSE", "deleted_at"),
    ("onboarding_state", "FALSE", None),
    ("tenant_roles", "FALSE", "deleted_at"),
    ("technician_locations", "FALSE", None),
    ("user_role_assignments", "FALSE", None),
    ("webhook_deliveries", "FALSE", None),
    ("webhook_delivery_logs", "FALSE", None),
    ("webhook_subscriptions", "FALSE", "deleted_at"),
]


# Tables that were previously listed as "RLS-covered" but DON'T carry a
# ``company_id`` column. Migration silently skipped each. These need per-table
# investigation — see D74. Categories:
#   - views (9 _router entries): cannot RLS-protect directly, inherit from
#     base tables
#   - join-scoped (*_lines, *_steps, *_history): tenant-scoped via parent
#     FK; need either company_id backfill or subquery-based policy
#   - singleton per-tenant (app_settings): one row per tenant, isolation
#     via DB connection / constraint not policy
#   - plain gaps: probably need company_id added
#
# Format: ``table_name`` with no predicate/soft-delete tuple (since they're
# not rendered). Kept as a plain list so D74 can sweep.
TABLES_PENDING_RLS_AUDIT: list[str] = [
    # Views (cannot RLS-protect directly)
    "booking_jobs_router",
    "booking_requests_router",
    "checklist_items_router",
    "checklist_templates_router",
    "checklists_router",
    "fleet_vehicles_router",
    "fleet_vehicle_service_logs_router",
    "timeclock_breaks_router",
    "timeclock_entries_router",
    # Singleton per-tenant (isolation via DB/row-count, not policy)
    "app_settings",
    # Join-scoped through parent FK — needs subquery policy or column backfill
    "change_order_lines",
    "expense_lines",
    "po_request_lines",
    "supplier_order_lines",
    "job_dependencies",
    "job_templates",
    "plan_steps",
    "automation_enrollments",
    "automation_sequences",
    "automation_steps",
    "document_folders",
    "documents",
    "equipment_assets",
    "equipment_asset_history",
    "inventory_items",
    "stock_adjustments",
    "van_inventory_log",
    "custom_catalogs",
    "custom_catalog_items",
    "recurring_job_schedules",
    "warranties",
    "payment_reminders",
    "estimate_nurture_log",
    "loyalty_points",
    "loyalty_tiers",
    "review_requests",
    "segments",
    "messages",
    "portal_booking_requests",
    "portal_messages",
    "supplier_accounts",
    "supplier_invitations",
    "supplier_tenant_links",
    "vendors",
    # Notifications / permissions
    "notifications",
    "notifications_settings",
    "notification_sent_history",
    "notification_templates",
    "role_permissions",
]


def remaining_table_names() -> list[str]:
    """Return just the table names (for logging / drift-check reports)."""
    return [entry[0] for entry in REMAINING_TABLES]
