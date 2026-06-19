"""Sprint Outlook Integration — Microsoft Graph webhook subscription helpers.

Three lifecycle operations on /subscriptions:
  - create_subscription(): POST /subscriptions on connect
  - renew_subscription(): PATCH /subscriptions/{id} every 24h before 70h expiry
  - delete_subscription(): DELETE /subscriptions/{id} on disconnect

Each subscription targets a single user's /me/messages and notifies our
public webhook URL. Renewal is idempotent — calling twice is a no-op
unless the row's expiration_at is within the renewal threshold.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookSubscription
from gdx_dispatch.modules.outlook.token_refresh import with_outlook_client


log = logging.getLogger("gdx_dispatch.modules.outlook.subscriptions")

DEFAULT_LIFETIME_HOURS = 60          # MS Graph max is 70h for /messages
RENEWAL_THRESHOLD_HOURS = 12         # renew when expiration_at is within this window


class SubscriptionError(RuntimeError):
    """Raised when Graph subscription lifecycle fails."""


def _build_notification_url(tenant_slug: str, client_state: str) -> str:
    base = os.environ.get("TENANT_BASE_DOMAIN", "example.com").strip("/")
    return f"https://{tenant_slug}.{base}/api/webhooks/outlook/{tenant_slug}/{client_state}"


def create_subscription(
    *,
    control_db: Session,
    tenant_db: Session,
    tenant_id: UUID,
    user_id: UUID,
) -> OutlookSubscription:
    """Create a Graph subscription for this user's /me/messages and persist
    the subscription_id + expiration. One row per OutlookAccount (UNIQUE on
    account_id)."""
    account = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id), OutlookAccount.provider == "outlook")
        .one_or_none()
    )
    if account is None:
        raise SubscriptionError(f"no OutlookAccount for user {user_id}")
    tenant = control_db.get(Tenant, tenant_id)
    if tenant is None or not tenant.slug:
        raise SubscriptionError(f"tenant {tenant_id} has no slug")

    client_state = secrets.token_hex(32)
    notification_url = _build_notification_url(tenant.slug, client_state)
    expiration = datetime.now(timezone.utc) + timedelta(hours=DEFAULT_LIFETIME_HOURS)

    body = {
        "changeType": "created,updated",
        "notificationUrl": notification_url,
        "resource": "/me/messages",
        "expirationDateTime": expiration.isoformat().replace("+00:00", "Z"),
        "clientState": client_state,
    }
    try:
        with with_outlook_client(control_db, tenant_db, user_id, tenant_id) as gc:
            resp = gc._request("POST", "/subscriptions", json=body)
        graph_body = resp.json()
    except OutlookGraphAPIError as exc:
        raise SubscriptionError(f"create_subscription failed: {exc}") from exc

    sub = (
        tenant_db.query(OutlookSubscription)
        .filter(OutlookSubscription.account_id == account.id)
        .one_or_none()
    )
    if sub is None:
        sub = OutlookSubscription()
        sub.account_id = account.id
        tenant_db.add(sub)
    sub.graph_subscription_id = graph_body["id"]
    sub.notification_url = notification_url
    sub.client_state = client_state
    sub.expiration_at = expiration
    sub.last_renewed_at = datetime.now(timezone.utc)
    sub.last_error = None
    tenant_db.commit()
    return sub


def renew_subscription(
    *,
    control_db: Session,
    tenant_db: Session,
    tenant_id: UUID,
    subscription: OutlookSubscription,
) -> OutlookSubscription:
    """PATCH /subscriptions/{id} with a new expirationDateTime. Idempotent —
    if expiration_at is more than RENEWAL_THRESHOLD_HOURS away, no-op."""
    now = datetime.now(timezone.utc)
    if (subscription.expiration_at - now).total_seconds() > RENEWAL_THRESHOLD_HOURS * 3600:
        return subscription

    account = tenant_db.get(OutlookAccount, subscription.account_id)
    if account is None:
        raise SubscriptionError(f"no OutlookAccount for subscription {subscription.id}")

    new_expiration = now + timedelta(hours=DEFAULT_LIFETIME_HOURS)
    try:
        with with_outlook_client(control_db, tenant_db, account.user_id, tenant_id) as gc:
            gc._request(
                "PATCH",
                f"/subscriptions/{subscription.graph_subscription_id}",
                json={"expirationDateTime": new_expiration.isoformat().replace("+00:00", "Z")},
            )
    except OutlookGraphAPIError as exc:
        subscription.last_error = str(exc)[:500]
        # Commit BEFORE re-raising so renew_all_outlook_subscriptions's
        # `last_error.is_(None)` filter can skip this sub on the next pass.
        try:
            tenant_db.commit()
        except Exception:  # noqa: BLE001
            log.exception(
                "renew_subscription: commit of last_error failed for sub %s — "
                "next renewal pass may retry in tight loop",
                subscription.id,
            )
            tenant_db.rollback()
        raise SubscriptionError(f"renew_subscription failed: {exc}") from exc

    subscription.expiration_at = new_expiration
    subscription.last_renewed_at = now
    subscription.last_error = None
    tenant_db.commit()
    return subscription


def delete_subscription(
    *,
    control_db: Session,
    tenant_db: Session,
    tenant_id: UUID,
    user_id: UUID,
) -> None:
    """Best-effort delete on Graph + remove the row. Failure to delete on
    Graph (already-expired sub, etc.) is logged but does not raise."""
    account = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id), OutlookAccount.provider == "outlook")
        .one_or_none()
    )
    if account is None:
        return
    sub = (
        tenant_db.query(OutlookSubscription)
        .filter(OutlookSubscription.account_id == account.id)
        .one_or_none()
    )
    if sub is None:
        return
    try:
        with with_outlook_client(control_db, tenant_db, user_id, tenant_id) as gc:
            gc._request("DELETE", f"/subscriptions/{sub.graph_subscription_id}")
    except OutlookGraphAPIError as exc:
        log.warning("delete_subscription Graph DELETE failed for %s: %s", sub.id, exc)
    tenant_db.delete(sub)
    tenant_db.commit()
