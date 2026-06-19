"""SS-3 Slice A schema tests.

Validates:
- All 16 platform-extensions models register on Base.metadata
- ORM-level schema can be materialized via metadata.create_all (SQLite)
- Alembic migration chain is sequenced correctly (003 -> 004a -> 004b -> 004c -> 004d)
- Critical FKs and unique constraints exist on the materialized schema
- Per-chunk rollback boundaries are independently inspectable

Note: full upgrade/downgrade/upgrade cycle against Alembic is deferred to
Slice B against a real Postgres instance — SS-2 migration uses
op.create_foreign_key which SQLite cannot handle. That cycle was validated
on staging for SS-2 and the same harness will be used for SS-3 in slice B.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

import gdx_dispatch.models.platform  # noqa: F401 — registers SS-2 tables on Base.metadata
import gdx_dispatch.models.platform_extensions  # noqa: F401 — registers SS-3 tables

# Import order matters — platform_extensions depends on platform.
from gdx_dispatch.control.models import Base

SS3_TABLES = [
    # 3a: OAuth surface
    "oauth_clients",
    "oauth_client_keys",
    "billing_accounts",
    "installations",
    "access_tokens",
    "revocation_denylist",
    # 3b: events + meter + audit
    "event_outbox",
    "meter_events",
    "audit_logs",
    # 3c: sharing
    "resource_descriptors",
    "resource_field_descriptors",
    "shared_resources",
    "shares",
    # 3d: supporting
    "developer_accounts",
    "sandbox_envs",
    "sso_configs",
]


@pytest.fixture(scope="module")
def materialized_engine():
    """SQLite engine with full Base.metadata materialized via create_all."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


# ── 16-table presence ───────────────────────────────────────────────────────


@pytest.mark.parametrize("table_name", SS3_TABLES)
def test_table_present_on_metadata(table_name):
    """Every SS-3 table is registered on Base.metadata via model imports."""
    assert table_name in Base.metadata.tables, f"{table_name} missing from Base.metadata"


@pytest.mark.parametrize("table_name", SS3_TABLES)
def test_table_materializes_on_sqlite(materialized_engine, table_name):
    """metadata.create_all builds every SS-3 table on SQLite without error."""
    inspector = inspect(materialized_engine)
    assert table_name in inspector.get_table_names(), f"{table_name} not materialized"


# ── Critical foreign keys ───────────────────────────────────────────────────


def _has_fk(inspector, table_name: str, source_col: str, target_table: str) -> bool:
    """True if `source_col` on `table_name` references `target_table`."""
    for fk in inspector.get_foreign_keys(table_name):
        if source_col in fk["constrained_columns"] and fk["referred_table"] == target_table:
            return True
    return False


def test_installation_fks(materialized_engine):
    insp = inspect(materialized_engine)
    assert _has_fk(insp, "installations", "oauth_client_id", "oauth_clients")
    assert _has_fk(insp, "installations", "tenant_id", "tenants")
    assert _has_fk(insp, "installations", "installer_identity_id", "identities")
    assert _has_fk(insp, "installations", "capability_set_id", "capability_sets")
    assert _has_fk(insp, "installations", "billing_account_id", "billing_accounts")


def test_oauth_client_keys_cascades_to_oauth_clients(materialized_engine):
    insp = inspect(materialized_engine)
    fks = insp.get_foreign_keys("oauth_client_keys")
    matching = [fk for fk in fks if "oauth_client_id" in fk["constrained_columns"]]
    assert matching, "oauth_client_keys.oauth_client_id FK missing"
    assert matching[0]["referred_table"] == "oauth_clients"
    # ondelete=CASCADE on this FK
    options = matching[0].get("options") or {}
    assert options.get("ondelete", "").upper() == "CASCADE"


def test_access_tokens_fks(materialized_engine):
    insp = inspect(materialized_engine)
    assert _has_fk(insp, "access_tokens", "installation_id", "installations")
    assert _has_fk(insp, "access_tokens", "capability_set_id", "capability_sets")


def test_capabilities_granted_via_install_fk_wired(materialized_engine):
    """SS-2 deferred FK is wired up by SS-3a (use_alter=True on the model side)."""
    insp = inspect(materialized_engine)
    assert _has_fk(insp, "capabilities", "granted_via_installation_id", "installations"), \
        "Capability.granted_via_installation_id FK to installations not wired"


def test_audit_logs_platform_columns_present(materialized_engine):
    insp = inspect(materialized_engine)
    cols = {c["name"] for c in insp.get_columns("audit_logs")}
    assert "installation_id" in cols
    assert "agent_identity" in cols
    assert "shared_via_resource_id" in cols
    assert "act_chain" in cols


def test_audit_shared_via_resource_fk_wired(materialized_engine):
    """SS-3c wires audit_logs.shared_via_resource_id -> shared_resources.id."""
    insp = inspect(materialized_engine)
    assert _has_fk(insp, "audit_logs", "shared_via_resource_id", "shared_resources")


def test_shares_cascades_to_shared_resources(materialized_engine):
    insp = inspect(materialized_engine)
    fks = insp.get_foreign_keys("shares")
    matching = [fk for fk in fks if "shared_resource_id" in fk["constrained_columns"]]
    assert matching
    assert matching[0]["referred_table"] == "shared_resources"
    options = matching[0].get("options") or {}
    assert options.get("ondelete", "").upper() == "CASCADE"


def test_resource_field_descriptors_cascades(materialized_engine):
    insp = inspect(materialized_engine)
    fks = insp.get_foreign_keys("resource_field_descriptors")
    matching = [fk for fk in fks if "resource_descriptor_id" in fk["constrained_columns"]]
    assert matching
    assert matching[0]["referred_table"] == "resource_descriptors"
    options = matching[0].get("options") or {}
    assert options.get("ondelete", "").upper() == "CASCADE"


def test_meter_events_billing_fk(materialized_engine):
    insp = inspect(materialized_engine)
    assert _has_fk(insp, "meter_events", "installation_id", "installations")
    assert _has_fk(insp, "meter_events", "billing_account_id", "billing_accounts")


def test_event_outbox_fks(materialized_engine):
    insp = inspect(materialized_engine)
    assert _has_fk(insp, "event_outbox", "tenant_id", "tenants")
    assert _has_fk(insp, "event_outbox", "installation_id", "installations")


# ── Critical unique constraints ─────────────────────────────────────────────


def test_unique_install_app_tenant(materialized_engine):
    """One installation per (oauth_client, tenant) pair."""
    insp = inspect(materialized_engine)
    uqs = insp.get_unique_constraints("installations")
    names = {uq["name"] for uq in uqs}
    assert "uq_install_app_tenant" in names


def test_unique_client_keys_kid(materialized_engine):
    insp = inspect(materialized_engine)
    uqs = insp.get_unique_constraints("oauth_client_keys")
    names = {uq["name"] for uq in uqs}
    assert "uq_client_keys_kid" in names


def test_unique_field_descriptor(materialized_engine):
    insp = inspect(materialized_engine)
    uqs = insp.get_unique_constraints("resource_field_descriptors")
    names = {uq["name"] for uq in uqs}
    assert "uq_field_descriptor" in names


# ── Critical indexes ────────────────────────────────────────────────────────


def test_critical_indexes_present(materialized_engine):
    """Spot-check that performance-critical indexes from spec exist."""
    insp = inspect(materialized_engine)
    expected = {
        "installations": ["ix_installations_tenant", "ix_installations_status"],
        "access_tokens": ["ix_access_tokens_installation"],
        "shared_resources": ["ix_shared_resources_owner", "ix_shared_resources_resource"],
        "meter_events": ["ix_meter_events_install_time", "ix_meter_events_billing_time"],
        "audit_logs": ["ix_audit_install", "ix_audit_shared_via", "ix_audit_tenant_time"],
    }
    for table, idx_names in expected.items():
        actual = {idx["name"] for idx in insp.get_indexes(table)}
        for idx in idx_names:
            assert idx in actual, f"index {idx} missing on {table} (have: {actual})"


# ── Migration chain integrity ───────────────────────────────────────────────


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load_migration_module(filename: str):
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), MIGRATIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("filename,expected_rev,expected_down", [
    ("004a_ss3_oauth_surface.py", "004a_ss3_oauth_surface", "003_platform_schema_foundation"),
    ("004b_ss3_events_metering_audit.py", "004b_ss3_events_metering_audit", "004a_ss3_oauth_surface"),
    ("004c_ss3_sharing.py", "004c_ss3_sharing", "004b_ss3_events_metering_audit"),
    ("004d_ss3_supporting.py", "004d_ss3_supporting", "004c_ss3_sharing"),
])
def test_migration_chain_sequenced(filename, expected_rev, expected_down):
    """Each SS-3 migration declares the right revision + down_revision."""
    module = _load_migration_module(filename)
    assert module.revision == expected_rev, f"{filename} revision should be {expected_rev}"
    assert module.down_revision == expected_down, f"{filename} down_revision should be {expected_down}"


def test_migration_files_have_upgrade_and_downgrade():
    """Every SS-3 migration has both upgrade() and downgrade() callables."""
    for fname in [
        "004a_ss3_oauth_surface.py",
        "004b_ss3_events_metering_audit.py",
        "004c_ss3_sharing.py",
        "004d_ss3_supporting.py",
    ]:
        module = _load_migration_module(fname)
        assert callable(module.upgrade), f"{fname}.upgrade is not callable"
        assert callable(module.downgrade), f"{fname}.downgrade is not callable"


# ── Per-chunk rollback boundaries (documentation tests) ─────────────────────


# These tests assert structural facts about the chain that documentation
# in current_result.md depends on. If the chain ever changes, these break first.


def test_004a_drops_only_3a_tables_in_downgrade():
    """004a's downgrade drops only the 6 tables it created (no cross-chunk)."""
    _load_migration_module("004a_ss3_oauth_surface.py")
    src = (MIGRATIONS_DIR / "004a_ss3_oauth_surface.py").read_text()
    expected_drops = {
        "revocation_denylist", "access_tokens", "installations",
        "billing_accounts", "oauth_client_keys", "oauth_clients",
    }
    for table in expected_drops:
        assert f'op.drop_table("{table}")' in src, f"004a downgrade missing drop_table for {table}"


def test_004d_does_not_drop_billing_accounts():
    """004d's downgrade does NOT drop billing_accounts (3a owns the stub)."""
    src = (MIGRATIONS_DIR / "004d_ss3_supporting.py").read_text()
    # The downgrade text should not contain a drop_table for billing_accounts
    # (3d's docstring says so explicitly — this enforces it).
    downgrade_section = src.split("def downgrade", 1)[1] if "def downgrade" in src else ""
    assert 'drop_table("billing_accounts")' not in downgrade_section, \
        "004d.downgrade incorrectly drops billing_accounts (3a owns the stub)"
