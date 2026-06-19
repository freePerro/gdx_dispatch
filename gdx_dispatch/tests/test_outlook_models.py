"""Slice outlook-s1 — verify the 5 new tenant-plane tables construct cleanly,
defaults match Doug's spec, and the three-plane invariant holds."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.outlook.models import (
    OutlookAccount,
    OutlookAttachment,
    OutlookMessage,
    OutlookSettings,
    OutlookSubscription,
)


def _fresh_engine():
    eng = create_engine("sqlite:///:memory:")
    TenantBase.metadata.create_all(eng)
    return eng


def test_create_all_emits_outlook_tables():
    eng = _fresh_engine()
    tables = set(inspect(eng).get_table_names())
    for name in (
        "outlook_accounts",
        "outlook_messages",
        "outlook_attachments",
        "outlook_subscriptions",
        "outlook_settings",
    ):
        assert name in tables, f"{name} not created"


def test_outlook_settings_defaults_apply_on_insert():
    """Column-level defaults fire at INSERT, not at __init__ — verify post-flush."""
    eng = _fresh_engine()
    Session = sessionmaker(bind=eng)
    with Session() as s:
        row = OutlookSettings()
        s.add(row)
        s.flush()
        s.refresh(row)
        rules = row.visibility_rules
        assert rules["tagged_visibility_above_role"] == "tech_plus_one"
        assert rules["tech_recipient_visible_to_all_techs"] is True
        assert rules["tech_outbound_no_tag_visibility"] == "only_sender"
        assert rules["tech_to_tech_internal_visibility"] == "only_participants"
        assert rules["above_tech_scope"] == "all_tagged"
        assert rules["untagged_visibility"] == "only_owner"
        assert row.backfill_days == 90
        assert row.tag_strategy_order == ["auto_match", "job_thread", "ai"]
        assert row.tag_strategy_enabled == {"auto_match": True, "job_thread": True, "ai": True}
        assert row.ai_tag_threshold == Decimal("0.85")
        assert row.auto_email_triggers == {}


def test_no_tenant_id_columns_on_outlook_models():
    """Three-plane invariant: tenant-plane models do NOT carry tenant_id."""
    for cls in (OutlookAccount, OutlookMessage, OutlookAttachment, OutlookSubscription, OutlookSettings):
        cols = {c.name for c in cls.__table__.columns}
        assert "tenant_id" not in cols, f"{cls.__name__} must not carry tenant_id"
        assert "company_id" not in cols, f"{cls.__name__} must not carry company_id"


def test_outlook_message_unique_on_account_and_graph_id():
    found = any(
        getattr(c, "name", None) == "uq_email_account_graph_id"
        for c in OutlookMessage.__table__.constraints
    )
    assert found, "OutlookMessage must have UniqueConstraint(account_id, graph_message_id)"


def test_outlook_account_provider_default_is_outlook():
    """When we add Gmail later, the discriminator column means existing
    Outlook rows don't get mistakenly served as Gmail accounts."""
    eng = _fresh_engine()
    # Need a parent users row for the FK; sqlite tolerates dangling FKs by default.
    Session = sessionmaker(bind=eng)
    with Session() as s:
        row = OutlookAccount()
        from uuid import uuid4
        row.user_id = str(uuid4())
        s.add(row)
        s.flush()
        s.refresh(row)
        assert row.provider == "outlook"
