"""SS-35 slice D — /api/sar router (Subject Access Request).

Endpoints
---------

- ``POST /api/sar/request``               — file a SAR for self (or, if
                                              caller is super-admin, for
                                              another identity via body
                                              ``target_identity_id``).
- ``GET  /api/sar/{sar_id}/status``       — poll status.
- ``GET  /api/sar/{sar_id}/download``     — single-use signed download.

Single-use download discipline
------------------------------

- ``download_token`` issued once at completion.
- 24-hour expiry.
- Redeeming the token marks ``downloaded_at`` — any further GET returns
  410 Gone.

Auth rules
----------

- A SAR on self: any authenticated identity may file.
- A SAR on behalf of another identity: caller MUST be super-admin, and
  the admin's identity is recorded as ``requested_by_identity_id``.
- Tenant-admin is NOT sufficient for on-behalf — SARs cross tenant
  boundaries and the admin role is scoped to a single tenant.

Events
------

- ``gdx.sar.requested.v1``  — at POST (always).
- ``gdx.sar.completed.v1``  — when the export is built.

Event emission is best-effort: the request row is persisted first; if
the event-outbox insert fails, the router still returns 202.

TODO: not mounted in ``gdx_dispatch/app.py`` yet. TODO:
signed URL minting currently uses an HMAC token bound to the SAR id +
issued_at. When the platform's canonical signer lands (see
``gdx_dispatch/core/stripe_connect.py`` pattern), replace :func:`_mint_token`.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.sar_builder import build_sar_export

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sar", tags=["sar"])


SIGNED_URL_TTL_SECONDS = 24 * 3600

_SIGNING_KEY_ENV = "GDX_SAR_SIGNING_KEY"


# ───────────────────────── helpers ─────────────────────────


# Deps — overridden at app.py wiring time to the control-plane session
# factory (get_db). Mirrors the SS-21 oauth2 / SS-31 federation
# pattern. The sentinel raises if create_app() didn't wire the override
# so a missing wire fails loudly, not silently.
#
# Pre-2026-05-15 this router read `request.state.db`, which no prod
# middleware sets — every /api/sar/* call 500'd "no db session on
# request" (only schemathesis hit it; test_sar_router.py masked the gap
# by injecting request.state.db in test middleware). The
# download/status paths only touch `sar_request`, which has RLS OFF on
# prod, so get_db with no tenant GUC reads them correctly.
# request_sar's PII walk is a different story — see _sar_build_ready().
def get_db() -> Session:  # pragma: no cover — overridden
    raise RuntimeError(
        "get_db must be overridden. Wire in gdx_dispatch/app.py: "
        "app.dependency_overrides[sar.get_db] = get_db."
    )


def _sar_build_ready() -> bool:
    """SAR *filing* (request_sar) builds an export via the cross-tenant
    PII walk (build_sar_export → get_pii_for_identity). On prod that walk
    runs raw SELECTs across registered PII tables; `memberships` is
    RLS-ON keyed to `app.tenant_id`, so a plain control session with no
    tenant GUC silently returns ZERO membership rows — an *incomplete*
    GDPR Art. 15 export. Regulators treat incomplete access responses as
    enforceable violations, so we must NOT emit one silently.

    There is no general "reporting-role session" primitive yet (SS-17's
    security_definer is per-function allow-listed, not session-scoped),
    so until the dedicated SECURITY DEFINER gather function lands
    (D-ss35-sar-integration), filing is fenced off. Default OFF; the
    test suite sets the env so the create→download flow stays covered
    on its RLS-free SQLite harness (the controlled env where the
    incompleteness can't occur).
    """
    return os.environ.get("SAR_BUILD_PRODUCTION_READY", "").strip() == "1"


def _caller_identity(request: Request) -> tuple[str, Optional[str]]:
    """Return ``(identity_id, role)`` or raise 401."""
    state = getattr(request, "state", None)
    iid = getattr(state, "principal_identity_id", None) if state else None
    if not iid:
        principal = getattr(state, "principal", None)
        if principal is not None:
            iid = getattr(principal, "identity_id", None)
    if not iid:
        raise HTTPException(status_code=401, detail="no principal identity")
    role = getattr(state, "principal_role", None)
    return str(iid), role


def _is_super_admin(role: Optional[str]) -> bool:
    return role in ("super-admin", "super_admin", "platform-admin")


def _signing_key() -> bytes:
    key = os.environ.get(_SIGNING_KEY_ENV)
    if not key:
        # Deterministic test fallback — logged once at info so local
        # runs don't scream, but production MUST set the env.
        key = "ss35-dev-only-do-not-use-in-prod"
    return key.encode("utf-8")


def _mint_token(sar_id: UUID, issued_at_iso: str) -> str:
    """HMAC-SHA256 over ``sar_id|issued_at`` → hex digest + nonce."""
    nonce = secrets.token_hex(8)
    msg = f"{sar_id}|{issued_at_iso}|{nonce}".encode("utf-8")
    mac = hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()
    return f"{nonce}.{mac}"


def _verify_token(sar_id: UUID, issued_at_iso: str, token: str) -> bool:
    try:
        nonce, mac = token.split(".", 1)
    except ValueError:
        return False
    msg = f"{sar_id}|{issued_at_iso}|{nonce}".encode("utf-8")
    expected = hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, mac)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ───────────────────────── models ─────────────────────────


class SARRequestBody(BaseModel):
    target_identity_id: Optional[str] = Field(
        default=None,
        description="Only super-admins may set this. Self-SAR omits it.",
    )
    reason: Optional[str] = Field(default=None, max_length=1024)


# ───────────────────────── endpoints ─────────────────────────


@router.post("/request")
def request_sar(
    request: Request,
    body: SARRequestBody = Body(default=SARRequestBody()),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """File a new SAR.

    Returns ``{sar_id, status}`` with status = ``queued``. The building
    of the export is synchronous for now (tiny dataset) but the
    endpoint contract allows it to go async later.
    """
    from gdx_dispatch.models.platform_ss35_additions import SARRequest  # lazy

    # Auth first so an unauthenticated caller still gets 401, not 501.
    caller_id, role = _caller_identity(request)

    # Fence: filing builds a cross-tenant PII export that is silently
    # incomplete on prod (RLS-ON `memberships` drops to zero rows under
    # a plain session). Refuse loudly rather than emit a partial GDPR
    # Art. 15 response. Lifted once the SECURITY DEFINER gather lands.
    if not _sar_build_ready():
        raise HTTPException(
            status_code=501,
            detail=(
                "SAR filing is not production-ready: the cross-tenant "
                "PII gather is unimplemented (D-ss35-sar-integration). "
                "Filing is disabled to avoid emitting an incomplete "
                "GDPR Art. 15 export."
            ),
        )

    target_id = body.target_identity_id or caller_id
    if target_id != caller_id and not _is_super_admin(role):
        raise HTTPException(
            status_code=403,
            detail="on-behalf SAR requires super-admin",
        )

    now = _utcnow()
    row = SARRequest(
        id=uuid4(),
        target_identity_id=target_id,
        requested_by_identity_id=caller_id,
        reason=body.reason,
        status="queued",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    _emit_event(db, "gdx_dispatch.sar.requested.v1", {
        "sar_id": str(row.id),
        "target_identity_id": target_id,
        "requested_by_identity_id": caller_id,
        "created_at": now.isoformat(),
    })

    # Build synchronously — tiny data volumes. Async path future-work.
    try:
        export = build_sar_export(db, target_id)
        row.export_json = export
        row.status = "completed"
        issued_iso = _utcnow().isoformat()
        row.completed_at = _utcnow()
        row.download_token = _mint_token(row.id, issued_iso)
        row.download_issued_at = issued_iso
        row.updated_at = _utcnow()
        _emit_event(db, "gdx_dispatch.sar.completed.v1", {
            "sar_id": str(row.id),
            "target_identity_id": target_id,
            "field_count": export["field_count"],
            "completed_at": row.completed_at.isoformat(),
        })
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("SAR export build failed: %s", exc)
        row.status = "failed"
        row.updated_at = _utcnow()
        db.commit()
        raise HTTPException(status_code=500, detail="export failed") from exc

    db.commit()
    return {
        "sar_id": str(row.id),
        "status": row.status,
        "download_url": f"/api/sar/{row.id}/download?token={row.download_token}",
        "expires_at": (row.completed_at + timedelta(seconds=SIGNED_URL_TTL_SECONDS)).isoformat(),
    }


@router.get("/{sar_id}/status")
def sar_status(
    sar_id: UUID, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    from gdx_dispatch.models.platform_ss35_additions import SARRequest  # lazy

    caller_id, role = _caller_identity(request)

    row = db.get(SARRequest, sar_id)
    if row is None:
        raise HTTPException(status_code=404, detail="sar not found")

    if (str(row.target_identity_id) != caller_id
            and str(row.requested_by_identity_id) != caller_id
            and not _is_super_admin(role)):
        raise HTTPException(status_code=404, detail="sar not found")

    return {
        "sar_id": str(row.id),
        "status": row.status,
        "target_identity_id": str(row.target_identity_id),
        "requested_by_identity_id": str(row.requested_by_identity_id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "downloaded_at": row.downloaded_at.isoformat() if row.downloaded_at else None,
    }


@router.get("/{sar_id}/download")
def sar_download(
    sar_id: UUID,
    token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Redeem the single-use signed URL. Returns the export JSON body.

    After the first successful redemption, ``downloaded_at`` is
    written and subsequent GETs return 410.
    """
    from gdx_dispatch.models.platform_ss35_additions import SARRequest  # lazy

    row = db.get(SARRequest, sar_id)
    if row is None:
        raise HTTPException(status_code=404, detail="sar not found")
    if row.status != "completed":
        raise HTTPException(status_code=409, detail=f"status={row.status}")
    if row.downloaded_at is not None:
        raise HTTPException(status_code=410, detail="single-use token already redeemed")

    if not row.download_token or not row.download_issued_at:
        raise HTTPException(status_code=409, detail="no download token issued")
    if not _verify_token(row.id, row.download_issued_at, token):
        raise HTTPException(status_code=403, detail="bad token")

    issued = datetime.fromisoformat(row.download_issued_at)
    if _utcnow() - issued > timedelta(seconds=SIGNED_URL_TTL_SECONDS):
        raise HTTPException(status_code=410, detail="token expired")

    row.downloaded_at = _utcnow()
    row.updated_at = _utcnow()
    db.commit()

    return row.export_json or {}


# ───────────────────────── event helper ─────────────────────────


def _emit_event(db: Any, event_name: str, payload: dict) -> None:
    """Append an event to ``event_outbox``. Best-effort — any failure is
    logged but does not break the caller."""
    try:
        from gdx_dispatch.models.platform_extensions import EventOutbox
        # Use a SAVEPOINT so a failed flush (e.g. table not present in
        # a test fixture) does not poison the outer transaction.
        sp = db.begin_nested()
        try:
            db.add(EventOutbox(
                event_name=event_name,
                source_event_id=uuid4(),
                tenant_id=None,
                payload=payload,
            ))
            db.flush()
            sp.commit()
        except Exception as inner:
            sp.rollback()
            raise inner
    except Exception as exc:
        logger.warning("event emit failed for %s: %s", event_name, exc)
