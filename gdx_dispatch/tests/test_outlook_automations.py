"""Phase 7 / auto-email triggers — verify renderer + dispatch gating."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from gdx_dispatch.modules.outlook.automations import (
    _user_opt_in,
    dispatch_trigger,
    render_template,
)


# ── render_template ────────────────────────────────────────────────────


def test_render_template_substitutes_dict_paths():
    out = render_template(
        "Hi {{customer.name}}, your invoice {{invoice.total}} is ready.",
        {"customer": {"name": "Alice"}, "invoice": {"total": "$500.00"}},
    )
    assert out == "Hi Alice, your invoice $500.00 is ready."


def test_render_template_missing_keys_render_empty():
    out = render_template("Hi {{customer.name}}", {})
    assert out == "Hi "


def test_render_template_handles_object_attributes():
    obj = MagicMock(); obj.email = "alice@x.com"
    out = render_template("To: {{customer.email}}", {"customer": obj})
    assert out == "To: alice@x.com"


# ── _user_opt_in ───────────────────────────────────────────────────────


def test_user_opt_in_true_when_set():
    user = MagicMock()
    user.preferences = {"outlook_auto_email_opt_in": {"invoice.created": True}}
    assert _user_opt_in(user, "invoice.created") is True


def test_user_opt_in_false_when_missing():
    user = MagicMock()
    user.preferences = {}
    assert _user_opt_in(user, "invoice.created") is False


def test_user_opt_in_false_when_preferences_not_dict():
    user = MagicMock()
    user.preferences = "not a dict"
    assert _user_opt_in(user, "invoice.created") is False


# ── dispatch_trigger gating ────────────────────────────────────────────


def _settings_with_triggers(triggers):
    s = MagicMock()
    s.auto_email_triggers = triggers
    return s


def test_dispatch_skips_when_trigger_not_configured():
    tdb = MagicMock(); cdb = MagicMock()
    tdb.query.return_value.filter.return_value.first.return_value = _settings_with_triggers({})
    out = dispatch_trigger(
        "invoice.created", {}, user_id=uuid4(), tenant_id=uuid4(),
        tenant_db=tdb, control_db=cdb,
    )
    assert out["sent"] is False
    assert "trigger not configured" in out["skipped"]


def test_dispatch_skips_when_user_not_opted_in():
    tdb = MagicMock(); cdb = MagicMock()
    user = MagicMock(); user.preferences = {}
    tdb.query.return_value.filter.return_value.first.return_value = _settings_with_triggers({
        "invoice.created": {"template": "<p>hi</p>"},
    })
    tdb.get.return_value = user
    out = dispatch_trigger(
        "invoice.created", {"customer": {"email": "doug@gdx"}},
        user_id=uuid4(), tenant_id=uuid4(),
        tenant_db=tdb, control_db=cdb,
    )
    assert out["sent"] is False
    assert "not opted in" in out["skipped"]


def test_dispatch_skips_when_no_outlook_account():
    tdb = MagicMock(); cdb = MagicMock()
    user = MagicMock()
    user.preferences = {"outlook_auto_email_opt_in": {"invoice.created": True}}
    tdb.query.return_value.filter.return_value.first.return_value = _settings_with_triggers({
        "invoice.created": {"template": "<p>hi</p>"},
    })
    tdb.get.return_value = user
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    out = dispatch_trigger(
        "invoice.created", {"customer": {"email": "doug@gdx"}},
        user_id=uuid4(), tenant_id=uuid4(),
        tenant_db=tdb, control_db=cdb,
    )
    assert out["sent"] is False
    assert "not connected outlook" in out["skipped"]


def test_dispatch_skips_when_no_recipient_resolved():
    tdb = MagicMock(); cdb = MagicMock()
    user = MagicMock()
    user.preferences = {"outlook_auto_email_opt_in": {"invoice.created": True}}
    account = MagicMock(); account.access_token_enc = "fernet"
    tdb.query.return_value.filter.return_value.first.return_value = _settings_with_triggers({
        "invoice.created": {"template": "<p>hi</p>"},
    })
    tdb.get.return_value = user
    tdb.query.return_value.filter.return_value.one_or_none.return_value = account
    out = dispatch_trigger(
        "invoice.created", {"customer": {}},
        user_id=uuid4(), tenant_id=uuid4(),
        tenant_db=tdb, control_db=cdb,
    )
    assert out["sent"] is False
    assert "no recipient resolved" in out["skipped"]


def test_dispatch_happy_path_calls_graph_send():
    tdb = MagicMock(); cdb = MagicMock()
    user = MagicMock()
    user.preferences = {"outlook_auto_email_opt_in": {"invoice.created": True}}
    account = MagicMock(); account.access_token_enc = "fernet"
    tdb.query.return_value.filter.return_value.first.return_value = _settings_with_triggers({
        "invoice.created": {
            "subject": "Invoice {{invoice.number}}",
            "template": "<p>Hi {{customer.name}}</p>",
        },
    })
    tdb.get.return_value = user
    tdb.query.return_value.filter.return_value.one_or_none.return_value = account

    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.automations.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        out = dispatch_trigger(
            "invoice.created",
            {"customer": {"name": "Alice", "email": "alice@x.com"},
             "invoice": {"number": "INV-42"}},
            user_id=uuid4(), tenant_id=uuid4(),
            tenant_db=tdb, control_db=cdb,
        )

    assert out["sent"] is True
    fake_gc._request.assert_called_once()
    body = fake_gc._request.call_args.kwargs["json"]
    assert body["message"]["subject"] == "Invoice INV-42"
    assert "Alice" in body["message"]["body"]["content"]
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == "alice@x.com"
