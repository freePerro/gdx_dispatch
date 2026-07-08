"""Slice outlook-s12 — verify Graph subscription lifecycle helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.subscriptions import (
    DEFAULT_LIFETIME_HOURS,
    SubscriptionError,
    create_subscription,
    delete_subscription,
    renew_subscription,
)


TID, UID = uuid4(), uuid4()


def _mock_account_and_tenant(tdb, cdb):
    account = MagicMock()
    account.id = uuid4()
    account.user_id = UID
    tdb.query.return_value.filter.return_value.one_or_none.return_value = account
    tenant = MagicMock()
    tenant.slug = "gdx"
    cdb.get.return_value = tenant
    return account, tenant


def test_create_subscription_posts_to_graph_and_persists(monkeypatch):
    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)  # exercise the legacy fallback
    monkeypatch.setenv("TENANT_BASE_DOMAIN", "example.com")
    cdb, tdb = MagicMock(), MagicMock()
    account, _ = _mock_account_and_tenant(tdb, cdb)
    # 2nd call to filter().one_or_none() is for OutlookSubscription row → None means new
    tdb.query.return_value.filter.return_value.one_or_none.side_effect = [account, None]

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"id": "graph-sub-id-123"}
    fake_gc = MagicMock()
    fake_gc._request.return_value = fake_resp

    with patch("gdx_dispatch.modules.outlook.subscriptions.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        sub = create_subscription(control_db=cdb, tenant_db=tdb, tenant_id=TID, user_id=UID)

    fake_gc._request.assert_called_once()
    method, path = fake_gc._request.call_args.args
    assert method == "POST"
    assert path == "/subscriptions"
    body = fake_gc._request.call_args.kwargs["json"]
    assert body["changeType"] == "created,updated"
    assert body["resource"] == "/me/messages"
    assert "gdx.example.com" in body["notificationUrl"]
    assert len(body["clientState"]) == 64
    assert sub.graph_subscription_id == "graph-sub-id-123"


def test_create_subscription_raises_when_no_account():
    cdb, tdb = MagicMock(), MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    with pytest.raises(SubscriptionError, match="no OutlookAccount"):
        create_subscription(control_db=cdb, tenant_db=tdb, tenant_id=TID, user_id=UID)


def test_renew_subscription_skips_when_far_from_expiry():
    cdb, tdb = MagicMock(), MagicMock()
    sub = MagicMock()
    sub.expiration_at = datetime.now(timezone.utc) + timedelta(hours=24)  # > threshold
    sub.id = uuid4()
    with patch("gdx_dispatch.modules.outlook.subscriptions.with_outlook_client") as ctx:
        out = renew_subscription(control_db=cdb, tenant_db=tdb, tenant_id=TID, subscription=sub)
    assert out is sub
    ctx.assert_not_called()


def test_renew_subscription_patches_when_near_expiry():
    cdb, tdb = MagicMock(), MagicMock()
    sub = MagicMock()
    sub.expiration_at = datetime.now(timezone.utc) + timedelta(hours=2)  # < threshold
    sub.graph_subscription_id = "graph-id"
    sub.id = uuid4()
    sub.account_id = uuid4()
    account = MagicMock()
    account.user_id = UID
    tdb.get.return_value = account

    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.subscriptions.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        renew_subscription(control_db=cdb, tenant_db=tdb, tenant_id=TID, subscription=sub)

    fake_gc._request.assert_called_once()
    assert fake_gc._request.call_args.args[0] == "PATCH"
    assert "/subscriptions/graph-id" in fake_gc._request.call_args.args[1]
    new_exp = sub.expiration_at
    delta_hours = (new_exp - datetime.now(timezone.utc)).total_seconds() / 3600
    assert DEFAULT_LIFETIME_HOURS - 1 < delta_hours < DEFAULT_LIFETIME_HOURS + 1


def test_renew_subscription_writes_last_error_on_failure():
    cdb, tdb = MagicMock(), MagicMock()
    sub = MagicMock()
    sub.expiration_at = datetime.now(timezone.utc) + timedelta(hours=2)
    sub.graph_subscription_id = "graph-id"
    sub.id = uuid4()
    sub.account_id = uuid4()
    account = MagicMock()
    account.user_id = UID
    tdb.get.return_value = account

    fake_gc = MagicMock()
    fake_gc._request.side_effect = OutlookGraphAPIError(404, "subscription not found")
    with patch("gdx_dispatch.modules.outlook.subscriptions.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        with pytest.raises(SubscriptionError):
            renew_subscription(control_db=cdb, tenant_db=tdb, tenant_id=TID, subscription=sub)
    assert sub.last_error and "404" in sub.last_error


def test_delete_subscription_removes_row():
    cdb, tdb = MagicMock(), MagicMock()
    account = MagicMock()
    account.id = uuid4()
    sub = MagicMock()
    sub.id = uuid4()
    sub.graph_subscription_id = "g-id"
    tdb.query.return_value.filter.return_value.one_or_none.side_effect = [account, sub]
    fake_gc = MagicMock()
    with patch("gdx_dispatch.modules.outlook.subscriptions.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        delete_subscription(control_db=cdb, tenant_db=tdb, tenant_id=TID, user_id=UID)
    fake_gc._request.assert_called_once()
    assert fake_gc._request.call_args.args[0] == "DELETE"
    tdb.delete.assert_called_once_with(sub)


def test_delete_subscription_swallows_graph_failure():
    """Disconnect path must not block on Graph cleanup failure."""
    cdb, tdb = MagicMock(), MagicMock()
    account = MagicMock()
    account.id = uuid4()
    sub = MagicMock()
    sub.graph_subscription_id = "g-id"
    tdb.query.return_value.filter.return_value.one_or_none.side_effect = [account, sub]
    fake_gc = MagicMock()
    fake_gc._request.side_effect = OutlookGraphAPIError(404, "already gone")
    with patch("gdx_dispatch.modules.outlook.subscriptions.with_outlook_client") as ctx:
        ctx.return_value.__enter__.return_value = fake_gc
        delete_subscription(control_db=cdb, tenant_db=tdb, tenant_id=TID, user_id=UID)
    tdb.delete.assert_called_once_with(sub)


# ── _build_notification_url (2026-07-07 audit) ─────────────────────────
# The old builder unconditionally used {slug}.{TENANT_BASE_DOMAIN} with an
# example.com default — on single-tenant prod (env var unset) Graph's
# endpoint validation failed and outlook_subscriptions stayed empty.


def test_notification_url_prefers_public_base_url(monkeypatch):
    from gdx_dispatch.modules.outlook.subscriptions import _build_notification_url

    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://dispatch.example.com/")
    monkeypatch.setenv("TENANT_BASE_DOMAIN", "example.com")  # must lose
    url = _build_notification_url("gdx", "c" * 64)
    assert url == f"https://dispatch.example.com/api/webhooks/outlook/gdx/{'c' * 64}"


def test_notification_url_falls_back_to_tenant_domain(monkeypatch):
    from gdx_dispatch.modules.outlook.subscriptions import _build_notification_url

    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("TENANT_BASE_DOMAIN", "gdx-hosting.com")
    url = _build_notification_url("acme", "s" * 64)
    assert url == f"https://acme.gdx-hosting.com/api/webhooks/outlook/acme/{'s' * 64}"


def test_notification_url_raises_when_unconfigured(monkeypatch):
    from gdx_dispatch.modules.outlook.subscriptions import SubscriptionError, _build_notification_url

    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TENANT_BASE_DOMAIN", raising=False)
    with pytest.raises(SubscriptionError):
        _build_notification_url("gdx", "x" * 64)
