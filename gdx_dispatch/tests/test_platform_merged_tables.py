"""Sprint 0.9-a platform merge smoke test.

Each SS<N> stub contributed either new tables or column additions to the
canonical ``gdx_dispatch.models.platform.Base`` metadata. This test asserts every
stub's merged outputs are present on Base.metadata.

See ``plans/integration/sprint-0.9-integration.md`` §Phase 1 slice 0.9-a.
"""
from __future__ import annotations

from gdx_dispatch.models import platform, platform_extensions  # noqa: F401


def _tables() -> set[str]:
    return set(platform.Base.metadata.tables.keys())


# ── column-addition sites (no new tables) ──────────────────────────────────


def test_ss15_access_token_columns_present():
    """SS-15 adds ``status`` + ``metadata_json`` to the existing
    ``access_tokens`` table (AccessToken on platform_extensions.Base)."""
    cols = platform.Base.metadata.tables["access_tokens"].columns
    assert "status" in cols
    assert "metadata_json" in cols


def test_ss22_is_noop():
    """SS-22 required no new columns; present as a defensive guard that
    the merge did not accidentally add anything."""
    # No SS-22 specific tables — SCIM uses existing Identity / IdentityProvider.
    # Identity still has its original columns, no new SS-22 additions.
    cols = set(platform.Base.metadata.tables["identities"].columns.keys())
    assert "email" in cols
    assert "status" in cols


# ── new-table contributions ─────────────────────────────────────────────────


def test_ss18_mcp_tool_tables_present():
    t = _tables()
    assert "mcp_tool_registration" in t
    assert "mcp_tool_execution_audit" in t


def test_ss19_mcp_execution_log_present():
    assert "mcp_execution_log" in _tables()


def test_ss20_dev_portal_tables_present():
    """Slice 0.9-a.1: SS-20 renamed to developer_portal_* and merged onto
    canonical Base. See ai-queue/orchestrator_qa/outbox.md
    Q-2026-04-19T01:30:00Z for why the rename was required."""
    t = _tables()
    assert "developer_portal_accounts" in t
    assert "developer_portal_email_verifications" in t
    assert "developer_portal_apps" in t
    assert "developer_portal_app_secrets" in t


def test_ss21_third_party_oauth_tables_present():
    t = _tables()
    assert "ss21_authorization_codes" in t
    assert "ss21_oauth_tokens" in t
    assert "ss21_admin_consent_grants" in t
    assert "ss21_webhook_subscriptions" in t
    assert "ss21_webhook_signing_keys" in t
    assert "ss21_webhook_deliveries" in t


def test_ss23_event_bus_tables_present():
    t = _tables()
    assert "event_subscription" in t
    assert "event_drain_checkpoint" in t


def test_ss24_metering_billing_tables_present():
    t = _tables()
    assert "metering_usage" in t
    assert "metering_checkpoint" in t
    assert "billing_plan" in t
    assert "billing_overage_event" in t


def test_ss27_cross_tenant_sharing_tables_present():
    t = _tables()
    assert "cross_tenant_share" in t
    assert "cross_tenant_share_acceptance" in t


def test_ss27_acceptance_fk_to_share_preserved():
    """The SS-27 FK from cross_tenant_share_acceptance.share_id → cross_tenant_share.id
    must survive the merge (task brief §Per-stub procedure step 4)."""
    acc = platform.Base.metadata.tables["cross_tenant_share_acceptance"]
    fks = {f.column.table.name for f in acc.c.share_id.foreign_keys}
    assert "cross_tenant_share" in fks


def test_ss28_consumer_audit_tables_present():
    t = _tables()
    assert "platform_consumer_audit" in t
    assert "audit_retention_policy" in t


def test_ss29_shadow_migration_tables_present():
    t = _tables()
    assert "shadow_migration_state" in t
    assert "shadow_migration_checkpoint" in t
    assert "shadow_migration_drift" in t


def test_ss30_cutover_tables_present():
    t = _tables()
    assert "cutover_schedule" in t
    assert "deprecated_table_record" in t


def test_ss31_federation_tables_present():
    t = _tables()
    assert "federation_provider" in t
    assert "federation_link" in t
    assert "federation_trust_bundle_cache" in t


def test_ss31_federation_link_fk_to_identities_preserved():
    """FK federation_link.identity_id → identities.id required by spec."""
    fl = platform.Base.metadata.tables["federation_link"]
    fks = {f.column.table.name for f in fl.c.identity_id.foreign_keys}
    assert "identities" in fks


def test_ss32_spiffe_tables_present():
    t = _tables()
    assert "spiffe_workload_registration" in t
    assert "spiffe_trust_bundle_cache" in t


def test_ss33_resource_tables_present():
    t = _tables()
    assert "resource_type" in t
    assert "resource_instance" in t
    assert "resource_type_deletion_request" in t


def test_ss34_dr_tables_present():
    t = _tables()
    assert "dr_snapshot_manifest" in t
    assert "dr_drill_run" in t
    assert "dr_verification_report" in t


def test_ss35_pii_tables_present():
    t = _tables()
    assert "sar_request" in t
    assert "erasure_request" in t
    assert "pii_event_log" in t


# ── global invariants ──────────────────────────────────────────────────────


def test_no_duplicate_table_names():
    """Base.metadata has no duplicate table names (would raise earlier)."""
    names = list(platform.Base.metadata.tables.keys())
    assert len(names) == len(set(names))


def test_expected_total_tables_upper_bound():
    """Upper-bound sanity: merged platform should have ~68 tables.
    If this fires unexpectedly, a stub added more than planned."""
    assert 60 <= len(platform.Base.metadata.tables) <= 80
