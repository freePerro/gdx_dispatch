from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.customer_portal.models import CustomerUser


def send_magic_link(email: str, customer_id, db: Session) -> str:
    user = db.execute(select(CustomerUser).where(CustomerUser.email == email, CustomerUser.customer_id == customer_id, CustomerUser.is_active.is_(True))).scalar_one_or_none()
    if not user: return ""  # noqa: E701,E702
    token = secrets.token_urlsafe(32)
    user.portal_token, user.portal_token_expires_at = token, datetime.now(UTC) + timedelta(hours=1)
    db.commit()
    base = os.getenv("CUSTOMER_PORTAL_BASE_URL", "")
    return f"{base.rstrip('/')}/portal/auth/verify/{token}" if base else f"/portal/auth/verify/{token}"


def verify_magic_link(token: str, db: Session) -> CustomerUser | None:
    now = datetime.now(UTC)
    user = db.execute(select(CustomerUser).where(CustomerUser.portal_token == token, CustomerUser.is_active.is_(True))).scalar_one_or_none()
    if not user: return None  # noqa: E701,E702
    exp = user.portal_token_expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if not exp or exp <= now: return None  # noqa: E701,E702
    user.portal_token, user.portal_token_expires_at, user.last_login_at = None, None, now
    db.commit()
    return user
