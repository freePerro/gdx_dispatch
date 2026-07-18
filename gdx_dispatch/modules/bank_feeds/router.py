"""Bank Feeds API router — /api/bank-feeds/*.

Module gating is PER-ENDPOINT (Outlook style) rather than router-level so
the OAuth callback — an unauthenticated top-level browser navigation whose
proof is the signed single-use state — can check the grant itself and
render a friendly popup page instead of a JSON 403.

The documents download endpoint guards ``storage_path`` with
``os.path.normpath`` + ``startswith`` — the CodeQL-recognized
path-injection guard (``Path.is_relative_to`` is an invisible-safe FP).
"""
from __future__ import annotations

import html as _html
import json as _json
import logging
import os
from datetime import date, datetime, timezone
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request as FastAPIRequest
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import is_module_enabled, require_module, require_permission
from gdx_dispatch.modules.bank_feeds import oauth, service
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    BankFeedAccount,
    BankFeedDocument,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bank-feeds", tags=["bank_feeds"])

_MODULE = Depends(require_module("bank_feeds"))


def _tenant_id(request: FastAPIRequest) -> str:
    state_tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(state_tenant.get("id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tenant_id


def _audit(db: Session, request: FastAPIRequest, user: dict, action: str, entity_id: str = "") -> None:
    log_audit_event_sync(
        db,
        tenant_id=_tenant_id(request),
        user_id=str(user.get("sub") or user.get("user_id") or "system"),
        action=action,
        entity_type="bank_feeds",
        entity_id=entity_id,
        details={},
        request=request,
    )
    db.commit()


def _build_redirect_uri() -> str:
    explicit = os.getenv("BANK_FEEDS_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = os.getenv("GDX_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=500,
            detail="BANK_FEEDS_REDIRECT_URI or GDX_BASE_URL must be configured",
        )
    return f"{base}/api/bank-feeds/oauth/callback"


# ── institutions ───────────────────────────────────────────────────────


class InstitutionIn(BaseModel):
    fi_host: str
    display_label: str = ""
    client_id: str = ""
    client_secret: str = ""


class InstitutionPatch(BaseModel):
    fi_host: str | None = None
    display_label: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    enabled: bool | None = None


def _institution_out(inst: BannoInstitution) -> dict:
    return {
        "id": str(inst.id),
        "fi_host": inst.fi_host,
        "display_label": inst.display_label,
        "client_id": inst.client_id or "",
        "secret_set": bool(inst.client_secret_enc),
        "secret_set_at": inst.secret_set_at.isoformat() if inst.secret_set_at else None,
        "enabled": inst.enabled,
    }


def _load_institution(db: Session, institution_id: str) -> BannoInstitution:
    try:
        inst_uuid = UUID(str(institution_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Institution not found") from None
    inst = db.get(BannoInstitution, inst_uuid)
    if inst is None:
        raise HTTPException(status_code=404, detail="Institution not found")
    return inst


@router.get("/institutions", dependencies=[_MODULE])
def list_institutions(
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(BannoInstitution).order_by(BannoInstitution.created_at.asc())
    ).scalars().all()
    return {"institutions": [_institution_out(i) for i in rows]}


@router.post("/institutions", dependencies=[_MODULE])
def create_institution(
    body: InstitutionIn,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    try:
        fi_host = oauth.validate_fi_host(body.fi_host)
    except Exception as exc:  # noqa: BLE001 — ValueError or OutboundURLBlocked
        raise HTTPException(status_code=422, detail=str(exc)) from None
    existing = db.execute(
        select(BannoInstitution).where(BannoInstitution.fi_host == fi_host)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Institution host already exists")

    inst = BannoInstitution(
        fi_host=fi_host,
        display_label=(body.display_label or fi_host)[:120],
        client_id=(body.client_id or "").strip()[:128] or None,
    )
    if body.client_secret:
        inst.client_secret_enc = oauth._encrypt(body.client_secret)
        inst.secret_set_at = datetime.now(timezone.utc)
    db.add(inst)
    db.commit()
    db.refresh(inst)
    _audit(db, request, current_user, "bank_feeds_institution_created", str(inst.id))
    return _institution_out(inst)


@router.patch("/institutions/{institution_id}", dependencies=[_MODULE])
def patch_institution(
    institution_id: str,
    body: InstitutionPatch,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    inst = _load_institution(db, institution_id)
    host_changed = False
    if body.fi_host is not None:
        try:
            new_host = oauth.validate_fi_host(body.fi_host)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if new_host != inst.fi_host:
            inst.fi_host = new_host
            host_changed = True
    if body.display_label is not None:
        inst.display_label = body.display_label[:120] or inst.fi_host
    if body.client_id is not None:
        inst.client_id = body.client_id.strip()[:128] or None
    if body.client_secret is not None:
        if body.client_secret:
            inst.client_secret_enc = oauth._encrypt(body.client_secret)
            inst.secret_set_at = datetime.now(timezone.utc)
        else:
            inst.client_secret_enc = None
            inst.secret_set_at = None
    if body.enabled is not None:
        inst.enabled = body.enabled
    inst.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(inst)
    _audit(db, request, current_user, "bank_feeds_institution_updated", str(inst.id))
    out = _institution_out(inst)
    if host_changed:
        out["warning"] = "Institution host changed — existing connections must reconnect."
    return out


class InstitutionDeleteIn(BaseModel):
    purge: bool = False


@router.delete("/institutions/{institution_id}", dependencies=[_MODULE])
def delete_institution(
    institution_id: str,
    request: FastAPIRequest,
    body: InstitutionDeleteIn | None = None,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    """Refuses (409) while synced data exists unless ``{"purge": true}`` —
    "disconnect keeps data; delete purges". Purge deletes children in FK
    order in the service layer (no reliance on DB cascade)."""
    inst = _load_institution(db, institution_id)
    connection_ids = [
        row[0]
        for row in db.execute(
            select(BannoConnection.id).where(BannoConnection.institution_id == inst.id)
        )
    ]
    account_count = 0
    if connection_ids:
        account_count = db.execute(
            select(func.count(BankFeedAccount.id)).where(
                BankFeedAccount.connection_id.in_(connection_ids)
            )
        ).scalar_one()
    purge = bool(body and body.purge)
    if account_count and not purge:
        raise HTTPException(
            status_code=409,
            detail=(
                "Institution has synced data. Disconnect keeps data; "
                "pass {\"purge\": true} to delete everything."
            ),
        )

    if connection_ids:
        account_ids = [
            row[0]
            for row in db.execute(
                select(BankFeedAccount.id).where(BankFeedAccount.connection_id.in_(connection_ids))
            )
        ]
        if account_ids:
            db.execute(
                BankFeedTransaction.__table__.delete().where(
                    BankFeedTransaction.account_id.in_(account_ids)
                )
            )
            db.execute(
                BankFeedAccount.__table__.delete().where(BankFeedAccount.id.in_(account_ids))
            )
        db.execute(
            BankFeedDocument.__table__.delete().where(
                BankFeedDocument.connection_id.in_(connection_ids)
            )
        )
        db.execute(
            BannoConnection.__table__.delete().where(BannoConnection.id.in_(connection_ids))
        )
    db.delete(inst)
    db.commit()
    _audit(db, request, current_user, "bank_feeds_institution_deleted", str(inst.id))
    return {"deleted": True, "purged_accounts": account_count}


# ── status ─────────────────────────────────────────────────────────────


@router.get("/status", dependencies=[_MODULE])
def bank_feeds_status(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
) -> dict:
    from gdx_dispatch.modules.bank_feeds.tasks import breaker_state

    institutions = db.execute(
        select(BannoInstitution).order_by(BannoInstitution.created_at.asc())
    ).scalars().all()
    out = []
    for inst in institutions:
        connection = db.execute(
            select(BannoConnection)
            .where(
                BannoConnection.institution_id == inst.id,
                BannoConnection.auth_state != AUTH_DISCONNECTED,
            )
            .order_by(BannoConnection.updated_at.desc())
        ).scalars().first()
        account_count = 0
        last_synced_at = None
        if connection is not None:
            account_count = db.execute(
                select(func.count(BankFeedAccount.id)).where(
                    BankFeedAccount.connection_id == connection.id
                )
            ).scalar_one()
            last_synced_at = db.execute(
                select(func.max(BankFeedAccount.last_synced_at)).where(
                    BankFeedAccount.connection_id == connection.id
                )
            ).scalar_one()
        out.append({
            "id": str(inst.id),
            "label": inst.display_label,
            "fi_host": inst.fi_host,
            "enabled": inst.enabled,
            "configured": bool(inst.client_id and inst.client_secret_enc),
            "connected": connection is not None,
            "auth_state": connection.auth_state if connection else None,
            "account_count": account_count,
            "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
            "documents_available": connection.documents_available if connection else None,
            "breaker_state": breaker_state(str(inst.id)),
        })
    schedule = service.get_or_create_schedule(db)
    return {"institutions": out, "schedule": service.schedule_dict(schedule)}


# ── OAuth connect + callback ───────────────────────────────────────────


class ConnectIn(BaseModel):
    institution_id: str


@router.post("/connect", dependencies=[_MODULE])
def connect(
    body: ConnectIn,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    inst = _load_institution(db, body.institution_id)
    client_secret = oauth._decrypt(inst.client_secret_enc or "")
    if not inst.client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="Institution credentials incomplete — enter client_id and client_secret first.",
        )
    try:
        discovery = oauth.discover_oidc(inst.fi_host)
    except oauth.BankFeedsAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    state, nonce = oauth.make_state(
        user_id=str(current_user.get("sub") or current_user.get("user_id") or ""),
        tenant_id=_tenant_id(request),
        institution_id=str(inst.id),
    )
    params = urlencode({
        "client_id": inst.client_id,
        "redirect_uri": _build_redirect_uri(),
        "response_type": "code",
        "scope": oauth.OAUTH_SCOPES,
        "state": state,
        "nonce": nonce,
    })
    authorize_endpoint = str(discovery["authorization_endpoint"])
    _audit(db, request, current_user, "bank_feeds_connect_started", str(inst.id))
    return {"redirect_url": f"{authorize_endpoint}?{params}"}


def _callback_html(status: str, message: str = "") -> str:
    """QB `_callback_html` mechanics: html.escape for display text,
    HTML-safe JSON for the postMessage payload, target origin scoped to
    GDX_BASE_URL (never '*'), fallback redirect for non-popup contexts."""
    title = "Bank Connected" if status == "connected" else "Connection Failed"
    icon = "✓" if status == "connected" else "✗"
    default_msg = (
        "Your bank account is now linked. Transactions will sync shortly."
        if status == "connected"
        else "Something went wrong — please try again."
    )
    display_msg = message or default_msg
    payload_json = _json.dumps({
        "type": "bank_feeds_oauth_result",
        "status": status,
    }).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    gdx_base = os.getenv("GDX_BASE_URL", "").rstrip("/")
    target_origin_js = _json.dumps(gdx_base) if gdx_base else "window.location.origin"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{_html.escape(title)}</title>
  <style>
    body {{ font-family: -apple-system, system-ui, sans-serif; padding: 2rem; text-align: center; background: #f8fafc; }}
    .card {{ max-width: 420px; margin: 2rem auto; padding: 2rem; background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .icon {{ font-size: 3rem; margin-bottom: 0.5rem; }}
    .ok {{ color: #16a34a; }}
    .err {{ color: #dc2626; }}
    h2 {{ margin: 0.5rem 0; color: #0f172a; }}
    p {{ color: #475569; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon {'ok' if status == 'connected' else 'err'}">{_html.escape(icon)}</div>
    <h2>{_html.escape(title)}</h2>
    <p>{_html.escape(display_msg)}</p>
    <p style="font-size: 0.9em; color: #94a3b8;">This window will close automatically.</p>
  </div>
  <script>
    (function() {{
      var payload = {payload_json};
      var targetOrigin = {target_origin_js};
      try {{
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage(payload, targetOrigin);
        }}
      }} catch (e) {{}}
      setTimeout(function() {{
        try {{ window.close(); }} catch (e) {{}}
        if (!window.opener || window.opener.closed) {{
          window.location.href = '/bank-feeds?connected=' + encodeURIComponent(payload.status);
        }}
      }}, 1500);
    }})();
  </script>
</body>
</html>
"""


@router.get("/oauth/callback")
def oauth_callback(
    request: FastAPIRequest,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """No auth dep — the signed single-use state IS the proof (Outlook
    rationale). Every failure path renders friendly popup HTML, never JSON."""
    if error:
        log.warning("bank_feeds_callback_error_param error=%s", str(error)[:60])
        return HTMLResponse(_callback_html("error", "The bank declined the connection."))
    if not code or not state:
        return HTMLResponse(_callback_html("error", "Missing code or state."), status_code=400)

    try:
        payload = oauth.load_state(state)
    except oauth.BankFeedsAuthError:
        log.warning("bank_feeds_callback_bad_state", exc_info=True)
        return HTMLResponse(_callback_html("error", "Sign-in link expired — try again."), status_code=400)

    if not oauth.consume_nonce(str(payload["nonce"])):
        log.warning("bank_feeds_callback_state_replayed")
        return HTMLResponse(_callback_html("error", "This sign-in link was already used."), status_code=400)

    # Module grant re-check (audited plan S12): revoked mid-flow → write
    # nothing. The tenant context for this unauthenticated navigation comes
    # from the VERIFIED signed state (signed by us) when the middleware
    # didn't populate it. A failure to EVALUATE the grant is not treated
    # as revocation.
    try:
        if not (getattr(request.state, "tenant", None) or {}).get("id"):
            request.state.tenant = {"id": str(payload.get("tenant_id") or "")}
        if not is_module_enabled("bank_feeds", request, db):
            return HTMLResponse(
                _callback_html("error", "Bank Feeds module is not enabled."), status_code=403
            )
    except Exception:  # noqa: BLE001
        pass

    inst = db.get(BannoInstitution, UUID(str(payload["institution_id"])))
    if inst is None:
        return HTMLResponse(_callback_html("error", "Institution no longer exists."), status_code=404)
    client_secret = oauth._decrypt(inst.client_secret_enc or "")
    if not inst.client_id or not client_secret:
        return HTMLResponse(_callback_html("error", "Institution credentials incomplete."), status_code=400)

    try:
        token_data = oauth.exchange_code_for_tokens(
            inst.fi_host, inst.client_id, client_secret,
            code=code, redirect_uri=_build_redirect_uri(),
        )
    except oauth.BankFeedsAuthError:
        log.exception("bank_feeds_token_exchange_failed institution=%s", inst.id)
        return HTMLResponse(
            _callback_html("error", "Token exchange with the bank failed. Please try again."),
            status_code=502,
        )

    try:
        discovery = oauth.discover_oidc(inst.fi_host)
        claims = oauth.verify_id_token(
            str(token_data.get("id_token") or ""),
            fi_host=inst.fi_host,
            client_id=inst.client_id,
            nonce=str(payload["nonce"]),
            discovery=discovery,
        )
    except oauth.BankFeedsAuthError:
        log.exception("bank_feeds_id_token_verification_failed institution=%s", inst.id)
        return HTMLResponse(
            _callback_html("error", "Identity verification failed. Please try again."),
            status_code=502,
        )

    try:
        connection = oauth.upsert_connection(
            db, inst,
            banno_user_id=str(claims["sub"]),
            token_data=token_data,
            connected_by=str(payload.get("user_id") or "") or None,
        )
    except oauth.BankFeedsAuthError as exc:
        db.rollback()
        return HTMLResponse(_callback_html("error", str(exc)), status_code=409)

    log_audit_event_sync(
        db,
        tenant_id=str(payload.get("tenant_id") or ""),
        user_id=str(payload.get("user_id") or "system"),
        action="bank_feeds_connected",
        entity_type="bank_feeds",
        entity_id=str(inst.id),
        details={"fi_host": inst.fi_host},
    )
    db.commit()

    try:
        from gdx_dispatch.modules.bank_feeds.tasks import bank_feeds_sync_task

        bank_feeds_sync_task.delay(
            str(payload.get("tenant_id") or ""), False, str(inst.id)
        )
    except Exception:  # noqa: BLE001 — broker down must not fail the connect
        log.exception("bank_feeds_initial_sync_enqueue_failed")

    _ = connection
    return HTMLResponse(_callback_html("connected"))


# ── disconnect + sync ──────────────────────────────────────────────────


class InstitutionRefIn(BaseModel):
    institution_id: str


@router.post("/disconnect", dependencies=[_MODULE])
def disconnect(
    body: InstitutionRefIn,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    inst = _load_institution(db, body.institution_id)
    count = oauth.soft_disconnect(db, inst.id)
    _audit(db, request, current_user, "bank_feeds_disconnected", str(inst.id))
    return {"disconnected": count}


class SyncIn(BaseModel):
    institution_id: str | None = None


@router.post("/sync", dependencies=[_MODULE])
def sync_now(
    request: FastAPIRequest,
    body: SyncIn | None = None,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    from gdx_dispatch.modules.bank_feeds.tasks import bank_feeds_sync_task

    institution_id = body.institution_id if body else None
    if institution_id:
        _load_institution(db, institution_id)  # 404 before queueing
    bank_feeds_sync_task.delay(_tenant_id(request), True, institution_id)
    return {"queued": True}


# ── accounts ───────────────────────────────────────────────────────────


class AccountPatch(BaseModel):
    sync_enabled: bool


def _institution_map(db: Session) -> dict:
    """connection_id → (institution_id, label)."""
    rows = db.execute(
        select(BannoConnection.id, BannoInstitution.id, BannoInstitution.display_label)
        .join(BannoInstitution, BannoInstitution.id == BannoConnection.institution_id)
    ).all()
    return {r[0]: (str(r[1]), r[2]) for r in rows}


@router.get("/accounts", dependencies=[_MODULE])
def list_accounts(
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
) -> dict:
    inst_map = _institution_map(db)
    rows = db.execute(
        select(BankFeedAccount).order_by(BankFeedAccount.created_at.asc())
    ).scalars().all()
    items = []
    for a in rows:
        inst = inst_map.get(a.connection_id, ("", ""))
        items.append({
            "id": str(a.id),
            "institution_id": inst[0],
            "institution_label": inst[1],
            "name": a.name,
            "account_number_masked": a.account_number_masked,
            "account_type": a.account_type,
            "account_subtype": a.account_subtype,
            "balance": str(a.balance) if a.balance is not None else None,
            "available_balance": str(a.available_balance) if a.available_balance is not None else None,
            "balance_as_of": a.balance_as_of.isoformat() if a.balance_as_of else None,
            "sync_enabled": a.sync_enabled,
            "is_inactive": a.is_inactive,
            "initial_backfill_done": a.initial_backfill_done,
            "last_synced_at": a.last_synced_at.isoformat() if a.last_synced_at else None,
        })
    return {"accounts": items}


@router.patch("/accounts/{account_id}", dependencies=[_MODULE])
def patch_account(
    account_id: str,
    body: AccountPatch,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    try:
        acct = db.get(BankFeedAccount, UUID(account_id))
    except ValueError:
        acct = None
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    acct.sync_enabled = body.sync_enabled
    acct.updated_at = datetime.now(timezone.utc)
    db.commit()
    _audit(db, request, current_user, "bank_feeds_account_toggled", str(acct.id))
    return {"id": str(acct.id), "sync_enabled": acct.sync_enabled}


# ── transactions ───────────────────────────────────────────────────────


@router.get("/transactions", dependencies=[_MODULE])
def list_transactions(
    institution_id: str | None = None,
    account_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    include_pending: bool = True,
    limit: int = 50,
    offset: int = 0,
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    query = (
        select(BankFeedTransaction, BankFeedAccount)
        .join(BankFeedAccount, BankFeedAccount.id == BankFeedTransaction.account_id)
        .join(BannoConnection, BannoConnection.id == BankFeedAccount.connection_id)
        .where(BankFeedTransaction.deleted_at.is_(None))
    )
    if institution_id:
        try:
            query = query.where(BannoConnection.institution_id == UUID(institution_id))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid institution_id") from None
    if account_id:
        try:
            query = query.where(BankFeedTransaction.account_id == UUID(account_id))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid account_id") from None

    # Date filters apply to posted_date; pendings (NULL posted_date) are
    # included alongside when include_pending — a date filter must not
    # silently drop them (audited plan S9).
    pending_clause = BankFeedTransaction.pending.is_(True)
    if date_from is not None:
        clause = BankFeedTransaction.posted_date >= date_from
        query = query.where((clause | pending_clause) if include_pending else clause)
    if date_to is not None:
        clause = BankFeedTransaction.posted_date <= date_to
        query = query.where((clause | pending_clause) if include_pending else clause)
    if not include_pending:
        query = query.where(BankFeedTransaction.pending.is_(False))
    if q:
        needle = f"%{q.strip()}%"
        query = query.where(
            BankFeedTransaction.payee.ilike(needle)
            | BankFeedTransaction.memo.ilike(needle)
            | BankFeedTransaction.filtered_memo.ilike(needle)
            | BankFeedTransaction.merchant_name.ilike(needle)
        )

    total = db.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar_one()

    rows = db.execute(
        query.order_by(
            BankFeedTransaction.posted_date.desc().nullsfirst(),
            BankFeedTransaction.created_at.desc(),
        ).limit(limit).offset(offset)
    ).all()

    inst_map = _institution_map(db)
    items = []
    for txn, acct in rows:
        inst = inst_map.get(acct.connection_id, ("", ""))
        items.append({
            "id": str(txn.id),
            "account_id": str(txn.account_id),
            "account_name": acct.name,
            "institution_id": inst[0],
            "institution_label": inst[1],
            "amount_cents": txn.amount_cents,
            "pending": txn.pending,
            "posted_date": txn.posted_date.isoformat() if txn.posted_date else None,
            "payee": txn.payee,
            "memo": txn.filtered_memo or txn.memo,
            "check_number": txn.check_number,
            "category": txn.category,
            "merchant_name": txn.merchant_name,
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ── documents ──────────────────────────────────────────────────────────


@router.get("/documents", dependencies=[_MODULE])
def list_documents(
    institution_id: str | None = None,
    document_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
) -> dict:
    limit = max(1, min(limit, 200))
    query = (
        select(BankFeedDocument, BannoConnection)
        .join(BannoConnection, BannoConnection.id == BankFeedDocument.connection_id)
    )
    if institution_id:
        try:
            query = query.where(BannoConnection.institution_id == UUID(institution_id))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid institution_id") from None
    if document_type:
        query = query.where(BankFeedDocument.document_type == document_type)
    if date_from is not None:
        query = query.where(BankFeedDocument.document_date >= date_from)
    if date_to is not None:
        query = query.where(BankFeedDocument.document_date <= date_to)

    total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = db.execute(
        query.order_by(BankFeedDocument.document_date.desc().nullslast())
        .limit(limit).offset(max(0, offset))
    ).all()

    inst_labels = {
        str(i.id): i.display_label
        for i in db.execute(select(BannoInstitution)).scalars()
    }
    items = []
    for doc, conn in rows:
        items.append({
            "id": str(doc.id),
            "institution_id": str(conn.institution_id),
            "institution_label": inst_labels.get(str(conn.institution_id), ""),
            "document_type": doc.document_type,
            "title": doc.title,
            "document_date": doc.document_date.isoformat() if doc.document_date else None,
            "account_ids": doc.account_ids or [],
            "fetched": doc.fetched_at is not None,
            "size_bytes": doc.size_bytes,
        })
    return {"items": items, "total": total, "limit": limit, "offset": max(0, offset)}


@router.get("/documents/{document_id}/download", dependencies=[_MODULE])
def download_document(
    document_id: str,
    _perm: None = Depends(require_permission("bank_feeds.read")),
    db: Session = Depends(get_db),
):
    try:
        doc = db.get(BankFeedDocument, UUID(document_id))
    except ValueError:
        doc = None
    if doc is None or not doc.storage_path or doc.fetched_at is None:
        raise HTTPException(status_code=404, detail="Document not available")

    # CodeQL-recognized path-injection guard: normpath + startswith.
    upload_root = os.path.normpath(os.getenv("UPLOAD_DIR", "/app/uploads/"))
    resolved = os.path.normpath(doc.storage_path)
    if not resolved.startswith(upload_root + os.sep):
        log.error("bank_feeds_document_path_outside_uploads doc=%s", document_id)
        raise HTTPException(status_code=404, detail="Document not available")
    if not os.path.exists(resolved):
        raise HTTPException(status_code=404, detail="Document file missing")

    download_name = doc.filename or f"{doc.document_type}-{doc.document_date or 'document'}.pdf"
    return FileResponse(
        resolved,
        media_type=doc.content_type or "application/pdf",
        filename=download_name,
    )


# ── schedule ───────────────────────────────────────────────────────────


class SchedulePut(BaseModel):
    frequency: str
    backfill_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/schedule", dependencies=[_MODULE])
def get_schedule(
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    return service.schedule_dict(service.get_or_create_schedule(db))


@router.put("/schedule", dependencies=[_MODULE])
def put_schedule(
    body: SchedulePut,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    _perm: None = Depends(require_permission("bank_feeds.manage")),
    db: Session = Depends(get_db),
) -> dict:
    try:
        row = service.update_schedule(db, body.frequency, backfill_days=body.backfill_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _audit(db, request, current_user, "bank_feeds_schedule_updated")
    return service.schedule_dict(row)
