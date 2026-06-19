"""Shared admin bearer-auth dependency for core/admin_modules + core/admin_flags."""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)

ADMIN_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")


def _require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    """Validate bearer token for admin endpoints."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")
    if credentials is None or credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access denied")
