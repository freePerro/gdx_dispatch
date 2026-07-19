"""Phase 4 / ACL chokepoint — verify per-rule visibility decisions."""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from gdx_dispatch.modules.outlook.models import OutlookMessage
from gdx_dispatch.modules.outlook.visibility import can_view, filter_visible


def _msg(**overrides):
    m = OutlookMessage()
    m.account_id = overrides.get("account_id", uuid4())
    m.is_personal = overrides.get("is_personal", False)
    m.linked_customer_id = overrides.get("linked_customer_id")
    m.linked_job_id = overrides.get("linked_job_id")
    m.direction = overrides.get("direction", "inbound")
    m.from_address = overrides.get("from_address")
    m.to_addresses = overrides.get("to_addresses", [])
    m.cc_addresses = overrides.get("cc_addresses", [])
    m.bcc_addresses = overrides.get("bcc_addresses", [])
    return m


def _tdb_with_account(owner_user_id, role="technician"):
    """Build a tenant_db mock that resolves message.account_id → user_id +
    user.role for visibility queries."""
    tdb = MagicMock()
    account = MagicMock(); account.user_id = owner_user_id
    user = MagicMock(); user.role = role
    # `tenant_db.get(OutlookAccount, account_id)` → account
    # `tenant_db.get(User, user_id)` → user
    tdb.get.side_effect = lambda model, pk: (
        account if model.__name__ == "OutlookAccount" else user
    )
    # OutlookSettings query returns None → defaults apply
    tdb.query.return_value.filter.return_value.first.return_value = None
    return tdb


# ── personal override ──────────────────────────────────────────────────


def test_personal_message_visible_only_to_sender():
    sender = uuid4()
    other = uuid4()
    msg = _msg(is_personal=True)
    tdb = _tdb_with_account(sender)
    assert can_view(msg, sender, "technician", tdb) is True
    assert can_view(msg, other, "admin", tdb) is False
    assert can_view(msg, other, "owner", tdb) is False


def test_personal_message_visible_to_owner_when_account_user_id_is_str():
    """REGRESSION: OutlookAccount.user_id is String(36) on tenant plane;
    viewer_user_id from JWT may arrive as UUID. `UUID == str` is False in
    Python — visibility must coerce both sides to str before comparing.
    Without the coerce, owners could not see their own personal messages."""
    sender_uuid = uuid4()
    sender_str = str(sender_uuid)
    msg = _msg(is_personal=True)
    # Simulate production: OutlookAccount.user_id stored as STRING
    tdb = _tdb_with_account(sender_str)  # str on the account
    # Viewer arrives as UUID (the JWT path)
    assert can_view(msg, sender_uuid, "technician", tdb) is True
    # Viewer arrives as str (also valid)
    assert can_view(msg, sender_str, "technician", tdb) is True


def test_self_sees_own_mailbox_when_user_id_is_str_on_account():
    """REGRESSION: same equality concern on the is_self fast path."""
    sender_uuid = uuid4()
    sender_str = str(sender_uuid)
    msg = _msg()  # untagged, inbound
    tdb = _tdb_with_account(sender_str)
    assert can_view(msg, sender_uuid, "technician", tdb) is True


# ── self always sees own mailbox ────────────────────────────────────────


def test_self_always_sees_own_mailbox():
    sender = uuid4()
    msg = _msg()  # untagged, inbound, no special flags
    tdb = _tdb_with_account(sender)
    assert can_view(msg, sender, "technician", tdb) is True


# ── tagged visibility — default (tech_plus_one) ─────────────────────────


def test_tagged_email_visible_to_admin_above_tech():
    sender = uuid4()
    other = uuid4()
    msg = _msg(linked_customer_id=uuid4())
    tdb = _tdb_with_account(sender)
    assert can_view(msg, other, "admin", tdb) is True
    assert can_view(msg, other, "owner", tdb) is True
    assert can_view(msg, other, "dispatcher", tdb) is True


def test_tagged_email_hidden_from_other_tech_when_no_recipient_match():
    sender = uuid4()
    other_tech = uuid4()
    msg = _msg(linked_customer_id=uuid4(), to_addresses=["customer@x.com"])
    tdb = _tdb_with_account(sender)
    assert can_view(msg, other_tech, "technician", tdb) is False


def test_tagged_email_visible_to_other_tech_when_a_tech_is_in_to():
    sender = uuid4()
    other_tech = uuid4()
    msg = _msg(linked_customer_id=uuid4(), to_addresses=["doug@gdx"])
    tdb = _tdb_with_account(sender)
    tech_emails = {"doug@gdx"}
    assert can_view(msg, other_tech, "technician", tdb, tech_emails=tech_emails) is True


# ── untagged email — default (only_owner) ───────────────────────────────


def test_untagged_email_hidden_from_everyone_else():
    sender = uuid4()
    other = uuid4()
    msg = _msg()  # untagged, inbound
    tdb = _tdb_with_account(sender)
    assert can_view(msg, other, "admin", tdb) is False
    assert can_view(msg, other, "technician", tdb) is False


def test_untagged_outbound_from_tech_default_only_sender():
    sender = uuid4()
    other_tech = uuid4()
    msg = _msg(direction="outbound")
    tdb = _tdb_with_account(sender, role="technician")
    assert can_view(msg, sender, "technician", tdb) is True
    assert can_view(msg, other_tech, "technician", tdb) is False
    assert can_view(msg, other_tech, "admin", tdb) is False


# ── tagged_visibility_above_role: admin_only ───────────────────────────


def test_admin_only_rule_blocks_dispatcher_from_tagged():
    sender = uuid4()
    other = uuid4()
    msg = _msg(linked_customer_id=uuid4())
    settings = MagicMock()
    settings.visibility_rules = {
        "tagged_visibility_above_role": "admin_only",
        "tech_recipient_visible_to_all_techs": True,
        "tech_outbound_no_tag_visibility": "only_sender",
        "tech_to_tech_internal_visibility": "only_participants",
        "above_tech_scope": "all_tagged",
        "untagged_visibility": "only_owner",
    }
    account = MagicMock(); account.user_id = sender
    user = MagicMock(); user.role = "technician"
    tdb = MagicMock()
    tdb.get.side_effect = lambda model, pk: account if model.__name__ == "OutlookAccount" else user
    tdb.query.return_value.filter.return_value.first.return_value = settings
    assert can_view(msg, other, "admin", tdb) is True
    assert can_view(msg, other, "owner", tdb) is True
    assert can_view(msg, other, "dispatcher", tdb) is False
    assert can_view(msg, other, "technician", tdb) is False


# ── filter_visible bulk ─────────────────────────────────────────────────


def test_filter_visible_personal_blocks_other_users():
    """When the personal flag is set, a user other than the sender sees nothing."""
    sender = uuid4()
    other_user = uuid4()
    msg_personal = _msg(is_personal=True)
    msg_tagged = _msg(linked_customer_id=uuid4())
    tdb = _tdb_with_account(sender)
    # other_user as a tech should see neither (personal blocks; tagged hidden from random tech)
    out = filter_visible([msg_personal, msg_tagged], other_user, "technician", tdb)
    assert len(out) == 0


def test_filter_visible_admin_sees_tagged():
    sender = uuid4()
    other = uuid4()
    msg_tagged_a = _msg(linked_customer_id=uuid4())
    msg_tagged_b = _msg(linked_job_id=uuid4())
    msg_untagged = _msg()
    tdb = _tdb_with_account(sender)
    out = filter_visible([msg_tagged_a, msg_tagged_b, msg_untagged], other, "admin", tdb)
    assert len(out) == 2


# ── tagged_visibility_above_role: owner_only ───────────────────────────


def _owner_only_settings():
    settings = MagicMock()
    settings.visibility_rules = {
        "tagged_visibility_above_role": "owner_only",
        "tech_recipient_visible_to_all_techs": True,
        "tech_outbound_no_tag_visibility": "only_sender",
        "tech_to_tech_internal_visibility": "only_participants",
        "above_tech_scope": "all_tagged",
        "untagged_visibility": "only_owner",
    }
    return settings


def test_owner_only_rule_hides_tagged_from_every_other_role():
    """owner_only = true single-person privacy: other admins/owners included."""
    sender = uuid4()
    other = uuid4()
    msg = _msg(linked_customer_id=uuid4())
    account = MagicMock(); account.user_id = sender
    user = MagicMock(); user.role = "technician"
    tdb = MagicMock()
    tdb.get.side_effect = lambda model, pk: account if model.__name__ == "OutlookAccount" else user
    tdb.query.return_value.filter.return_value.first.return_value = _owner_only_settings()
    for role in ("owner", "admin", "dispatcher", "csr", "technician", "viewer"):
        assert can_view(msg, other, role, tdb) is False, f"{role} must NOT see owner_only tagged mail"


def test_owner_only_rule_owner_still_sees_tagged():
    sender = uuid4()
    msg = _msg(linked_customer_id=uuid4())
    account = MagicMock(); account.user_id = sender
    user = MagicMock(); user.role = "admin"
    tdb = MagicMock()
    tdb.get.side_effect = lambda model, pk: account if model.__name__ == "OutlookAccount" else user
    tdb.query.return_value.filter.return_value.first.return_value = _owner_only_settings()
    assert can_view(msg, sender, "admin", tdb) is True


# ── visible_to_agent (machine-principal privacy gate) ──────────────────


def test_agent_gate_hides_personal_always():
    msg = _msg(is_personal=True)
    from gdx_dispatch.modules.outlook.visibility import visible_to_agent
    assert visible_to_agent(msg, MagicMock(), rules={}) is False


def test_agent_gate_hides_tagged_when_owner_only():
    msg = _msg(linked_customer_id=uuid4())
    from gdx_dispatch.modules.outlook.visibility import visible_to_agent
    rules = {"tagged_visibility_above_role": "owner_only"}
    assert visible_to_agent(msg, MagicMock(), rules=rules) is False


def test_agent_gate_passes_tagged_under_default_rules():
    msg = _msg(linked_customer_id=uuid4())
    from gdx_dispatch.modules.outlook.visibility import visible_to_agent
    rules = {"tagged_visibility_above_role": "tech_plus_one"}
    assert visible_to_agent(msg, MagicMock(), rules=rules) is True


def test_agent_gate_passes_untagged_non_personal():
    msg = _msg()
    from gdx_dispatch.modules.outlook.visibility import visible_to_agent
    assert visible_to_agent(msg, MagicMock(), rules={}) is True
