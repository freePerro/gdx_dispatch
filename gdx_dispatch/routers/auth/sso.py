"""SSO (Single Sign-On) router for Google and Microsoft OAuth login.

Provides:
  GET  /auth/sso/google       — redirect to Google OAuth consent screen
  GET  /auth/sso/google/callback — handle Google OAuth callback
  GET  /auth/sso/microsoft    — redirect to Microsoft OAuth consent screen
  GET  /auth/sso/microsoft/callback — handle Microsoft OAuth callback
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/sso", tags=["sso"])


class SSOConfig(BaseModel):
    client_id: str
    client_secret: str
    redirect_uri: str


def _google_config() -> SSOConfig | None:
    client_id = os.getenv("GOOGLE_SSO_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_SSO_CLIENT_SECRET", "")
    redirect_uri = os.getenv("GOOGLE_SSO_REDIRECT_URI", "")
    if not client_id or not client_secret:
        return None
    return SSOConfig(client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri)


def _microsoft_config() -> SSOConfig | None:
    client_id = os.getenv("MICROSOFT_SSO_CLIENT_ID", "")
    client_secret = os.getenv("MICROSOFT_SSO_CLIENT_SECRET", "")
    redirect_uri = os.getenv("MICROSOFT_SSO_REDIRECT_URI", "")
    if not client_id or not client_secret:
        return None
    return SSOConfig(client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri)


# ---------------------------------------------------------------------------
# Google SSO
# ---------------------------------------------------------------------------

@router.get("/google")
def google_sso_redirect(request: Request) -> RedirectResponse:
    """Redirect to Google OAuth consent screen."""
    config = _google_config()
    if not config:
        raise HTTPException(status_code=503, detail="Google SSO not configured")

    state = secrets.token_urlsafe(32)
    # Store state in session for CSRF protection
    request.session["sso_state"] = state if hasattr(request, "session") else None

    params = (
        f"client_id={config.client_id}"
        f"&redirect_uri={config.redirect_uri or request.url_for('google_sso_callback')}"
        f"&response_type=code"
        f"&scope=openid+email+profile"
        f"&state={state}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/google/callback")
async def google_sso_callback(request: Request, code: str = "", state: str = "") -> dict[str, Any]:
    """Handle Google OAuth callback — exchange code for tokens and create/find user."""
    config = _google_config()
    if not config:
        raise HTTPException(status_code=503, detail="Google SSO not configured")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "redirect_uri": config.redirect_uri or str(request.url_for("google_sso_callback")),
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to exchange code for token")

            tokens = token_resp.json()
            access_token = tokens.get("access_token", "")

            # Get user info
            userinfo_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to get user info")

            userinfo = userinfo_resp.json()

        log.info("google_sso_login", extra={"email": userinfo.get("email"), "name": userinfo.get("name")})

        return {
            "provider": "google",
            "email": userinfo.get("email"),
            "name": userinfo.get("name"),
            "picture": userinfo.get("picture"),
            "google_id": userinfo.get("id"),
        }

    except HTTPException:
        raise
    except Exception:
        log.exception("google_sso_callback_failed")
        raise HTTPException(status_code=500, detail="SSO callback failed") from None


# ---------------------------------------------------------------------------
# Microsoft SSO
# ---------------------------------------------------------------------------

@router.get("/microsoft")
def microsoft_sso_redirect(request: Request) -> RedirectResponse:
    """Redirect to Microsoft OAuth consent screen."""
    config = _microsoft_config()
    if not config:
        raise HTTPException(status_code=503, detail="Microsoft SSO not configured")

    state = secrets.token_urlsafe(32)
    tenant = os.getenv("MICROSOFT_SSO_TENANT", "common")

    params = (
        f"client_id={config.client_id}"
        f"&redirect_uri={config.redirect_uri or request.url_for('microsoft_sso_callback')}"
        f"&response_type=code"
        f"&scope=openid+email+profile+User.Read"
        f"&state={state}"
        f"&response_mode=query"
    )
    return RedirectResponse(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{params}"
    )


@router.get("/microsoft/callback")
async def microsoft_sso_callback(request: Request, code: str = "", state: str = "") -> dict[str, Any]:
    """Handle Microsoft OAuth callback."""
    config = _microsoft_config()
    if not config:
        raise HTTPException(status_code=503, detail="Microsoft SSO not configured")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    tenant = os.getenv("MICROSOFT_SSO_TENANT", "common")

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={
                    "code": code,
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "redirect_uri": config.redirect_uri or str(request.url_for("microsoft_sso_callback")),
                    "grant_type": "authorization_code",
                    "scope": "openid email profile User.Read",
                },
            )
            if token_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to exchange code for token")

            tokens = token_resp.json()
            access_token = tokens.get("access_token", "")

            graph_resp = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if graph_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to get user info")

            userinfo = graph_resp.json()

        log.info("microsoft_sso_login", extra={"email": userinfo.get("mail"), "name": userinfo.get("displayName")})

        return {
            "provider": "microsoft",
            "email": userinfo.get("mail") or userinfo.get("userPrincipalName"),
            "name": userinfo.get("displayName"),
            "microsoft_id": userinfo.get("id"),
        }

    except HTTPException:
        raise
    except Exception:
        log.exception("microsoft_sso_callback_failed")
        raise HTTPException(status_code=500, detail="SSO callback failed") from None
