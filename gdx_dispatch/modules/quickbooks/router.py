"""QuickBooks router — all QB endpoints in one place.

Uses httpx via QBClient (no python-quickbooks SDK).
Tokens stored encrypted via QBTokenStore.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request as FastAPIRequest
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.quickbooks import QBConnection, QBEntityMap
from gdx_dispatch.modules.quickbooks import sync
from gdx_dispatch.modules.quickbooks.client import QBAuthError
from gdx_dispatch.modules.quickbooks.oauth import (
    QBTokenStore,
    exchange_code_for_tokens,
    get_qb_client,
    save_tokens,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/qb",
    tags=["quickbooks"],
    dependencies=[Depends(require_module("quickbooks"))],
)


def _tenant_id(request: FastAPIRequest) -> str:
    state_tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(state_tenant.get("id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tenant_id


def _delete_sync_source(conn: QBConnection | None) -> str:
    """Tell the UI whether the effective delete-sync flag is sourced from the
    per-tenant column or the global env-var fallback. Used to render the
    correct help text under the Reconciliation banner toggle."""
    if conn is not None and conn.delete_sync_enabled is not None:
        return "tenant"
    return "env"


def _audit(db: Session, request: FastAPIRequest, user: dict, action: str, entity_type: str = "quickbooks") -> None:
    tenant_id = _tenant_id(request)
    user_id = str(user.get("sub") or user.get("user_id") or "system")
    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id=user_id,
        action=action, entity_type=entity_type, entity_id="",
        details={}, request=request,
    )
    db.commit()


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

@router.get("/connect")
@router.post("/connect")
def qb_connect(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, str]:
    """Return the Intuit OAuth2 authorization URL.

    Both GET and POST are accepted — the frontend uses POST to match its
    action-handler pattern, and GET is kept for CLI/testing.
    """
    tenant_id = _tenant_id(request)
    client_id = os.getenv("QB_CLIENT_ID", "")
    redirect_uri = os.getenv("QB_REDIRECT_URI", "")
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=500, detail="QuickBooks OAuth env vars not configured")

    q = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "state": tenant_id,
    })
    return {"redirect_url": f"https://appcenter.intuit.com/connect/oauth2?{q}"}


def _callback_html(status: str, realm_id: str = "", message: str = "") -> str:
    """Render the popup OAuth result page.

    Uses html.escape() for display text and json.dumps() for the postMessage
    payload so untrusted values (realm_id, exception text) can't break out
    of their context and inject scripts. Target origin is scoped to the
    deploy's own origin, not '*'.
    """
    import html
    import json as _json

    icon = "✓" if status == "connected" else "✗"
    title = "QuickBooks Connected" if status == "connected" else "Connection Failed"
    default_msg = (
        "Your QuickBooks account is now linked."
        if status == "connected"
        else "Something went wrong — please try again."
    )
    display_msg = message or default_msg

    # HTML-safe JSON: Python's json.dumps does not escape `<`, `>`, `&`,
    # which means an attacker-controlled realm_id could contain `</script>`
    # and break out of the <script> tag. Apply the standard Django /
    # Flask-Markup escaping to make the JSON safe inside a script context.
    payload_json = _json.dumps({
        "type": "qb_oauth_result",
        "status": status,
        "realm_id": realm_id,
    }).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    # Target origin for postMessage. Prefer env-configured base URL; fall
    # back to window.location.origin at runtime. Never '*' — any listener
    # in another tab on the same browser could pick up tokens otherwise.
    gdx_base = os.getenv("GDX_BASE_URL", "").rstrip("/")
    target_origin_js = _json.dumps(gdx_base) if gdx_base else "window.location.origin"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
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
    <div class="icon {'ok' if status == 'connected' else 'err'}">{html.escape(icon)}</div>
    <h2>{html.escape(title)}</h2>
    <p>{html.escape(display_msg)}</p>
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
        // Fallback for non-popup contexts (direct navigation, blocked popups)
        if (!window.opener || window.opener.closed) {{
          window.location.href = '/settings?qb=' + encodeURIComponent(payload.status);
        }}
      }}, 1500);
    }})();
  </script>
</body>
</html>
"""


@router.get("/oauth/callback")
@router.get("/callback")
async def qb_callback(
    code: str,
    state: str,
    realmId: str,
    db: Session = Depends(get_db),
):
    """Handle the OAuth2 callback from Intuit.

    Returns an HTML page that postMessages the parent window (for popup
    OAuth flows) and then closes itself. Falls back to a /settings redirect
    if window.close() fails (cross-origin or non-popup contexts).
    """
    from starlette.responses import HTMLResponse

    tenant_id = str(state).strip()
    if not tenant_id:
        return HTMLResponse(_callback_html("error", message="Missing tenant state"), status_code=400)

    try:
        token_data = await exchange_code_for_tokens(code)
    except QBAuthError:
        # Don't leak exception text into the HTML — log server-side only.
        log.exception("quickbooks_token_exchange_failed tenant=%s", tenant_id)
        return HTMLResponse(
            _callback_html("error", message="Token exchange with Intuit failed. Please try again."),
            status_code=502,
        )

    save_tokens(
        tenant_id=tenant_id,
        realm_id=str(realmId),
        access_token=str(token_data.get("access_token") or ""),
        refresh_token=str(token_data.get("refresh_token") or ""),
        expires_in=int(token_data.get("expires_in") or 3600),
        refresh_expires_in=int(token_data.get("x_refresh_token_expires_in") or 8726400),
        db=db,
    )

    return HTMLResponse(_callback_html("connected", realm_id=str(realmId)))


@router.get("/status")
def qb_status(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Check QuickBooks connection status."""
    # 2026-04-29: when QBTokenStore is present we still query QBConnection for
    # last_sync_at — the previous code hardcoded None, so the /quickbooks page
    # showed "Last Sync: —" while the sync_log table had real recent rows.
    tenant_id_lookahead = _tenant_id(request)
    legacy_last_sync_at = None
    try:
        legacy = db.execute(
            select(QBConnection).where(QBConnection.tenant_id == tenant_id_lookahead)
        ).scalar_one_or_none()
        if legacy and legacy.last_sync_at:
            legacy_last_sync_at = legacy.last_sync_at.isoformat()
    except Exception:
        log.exception("qb_status_legacy_lookahead_failed")
        db.rollback()

    # Check new token store first (S122-4: tenant-scoped per N1).
    # The table may not exist yet on tenant DBs that haven't been migrated —
    # fall through to legacy QBConnection if so.
    tenant_id = _tenant_id(request)
    try:
        token_row = db.execute(
            select(QBTokenStore).where(QBTokenStore.tenant_id == tenant_id)
            .order_by(QBTokenStore.updated_at.desc())
        ).scalar_one_or_none()
        if token_row:
            # S122-13: surface auth_state so the frontend can render a
            # "Reconnect QuickBooks" CTA when the refresh token is dead
            # instead of silently failing every sync attempt.
            auth_state = getattr(token_row, "auth_state", "healthy") or "healthy"
            return {
                "connected": True,
                "realm_id": token_row.realm_id,
                "environment": token_row.environment,
                "expires_at": token_row.access_token_expires_at.isoformat(),
                "last_sync_at": legacy_last_sync_at,
                "error_count": 0,
                "last_error": None,
                "auth_state": auth_state,
                "needs_reconnect": auth_state == "needs_reconnect",
            }
    except Exception:
        log.exception("qb_token_store_query_failed — falling back to QBConnection")
        db.rollback()

    # Fall back to legacy QBConnection
    row = db.execute(
        select(QBConnection).where(QBConnection.tenant_id == tenant_id)
    ).scalar_one_or_none()
    return {
        "connected": bool(row),
        "realm_id": row.realm_id if row else None,
        "last_sync_at": row.last_sync_at.isoformat() if row and row.last_sync_at else None,
        "error_count": int(row.error_count or 0) if row else 0,
        "last_error": row.last_error if row else None,
    }


@router.post("/disconnect")
def qb_disconnect(
    request: FastAPIRequest,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    """Disconnect QuickBooks — removes tokens from BOTH stores.

    QBTokenStore (new, encrypted) and QBConnection (legacy, plaintext) can
    both exist on a tenant that was connected before/after the 2026-04-12
    rewrite. Clearing just one leaves zombie tokens that could be re-used.
    """
    if user.get("role") not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Insufficient role")

    tenant_id = _tenant_id(request)
    removed = []

    # New encrypted token store (S122-4: tenant-scoped per N1)
    try:
        token_rows = db.execute(
            select(QBTokenStore).where(QBTokenStore.tenant_id == tenant_id)
        ).scalars().all()
        for token_row in token_rows:
            db.delete(token_row)
        if token_rows:
            removed.append("qb_token_store")
    except Exception:
        log.exception("qb_disconnect_token_store_failed")
        db.rollback()

    # Legacy plaintext connection (may not exist post-migration but stays
    # until the old table is dropped).
    try:
        conn_row = db.execute(
            select(QBConnection).where(QBConnection.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if conn_row:
            db.delete(conn_row)
            removed.append("qb_connection")
    except Exception:
        log.exception("qb_disconnect_connection_failed")
        db.rollback()

    db.commit()
    _audit(db, request, user, "qb_disconnect")
    return {"disconnected": True, "cleared": removed}


# ---------------------------------------------------------------------------
# Events (sync log for the UI)
# ---------------------------------------------------------------------------

@router.get("/events")
def qb_events(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
    action: str = "",
) -> dict[str, list[dict[str, object]]]:
    """Return recent QB sync events from the audit log.

    Powers the sync log DataTable on /quickbooks. Reads from AuditLog where
    entity_type = 'quickbooks'.

    When ``action`` is provided (e.g. ``qb_delete_sync``), filters at SQL
    level and returns the raw rows with the qb_id surfaced in ``entity_id``
    plus the audit details payload. The default sync-log presentation
    (count + prefix stripping) is skipped — used by the Reconciliation tab.
    """
    from gdx_dispatch.core.audit import AuditLog
    _tenant_id(request)

    query = (
        select(AuditLog)
        .where(AuditLog.entity_type == "quickbooks")
        .order_by(AuditLog.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if action:
        # AuditLog rows may store the action string in either column
        # depending on the writer. log_audit_event_sync writes both.
        query = query.where(
            (AuditLog.action == action) | (AuditLog.event_type == action)
        )

    rows = db.execute(query).scalars().all()

    if action:
        # Raw shape — caller filters explicitly, surface full payload.
        return {
            "events": [
                {
                    "timestamp": row.created_at.isoformat() if row.created_at else None,
                    "action": row.action or row.event_type,
                    "entity_id": row.entity_id,
                    "details": row.details or {},
                }
                for row in rows
            ]
        }

    events = []
    for row in rows:
        details = row.details or {}
        created = int(details.get("created", 0))
        updated = int(details.get("updated", 0))
        # Event type: strip qb_pull_ / qb_push_ prefix for display
        event_name = (row.event_type or row.action or "unknown")
        for prefix in ("qb_pull_", "qb_push_"):
            if event_name.startswith(prefix):
                event_name = event_name[len(prefix):]
                break
        # Filter to just sync events (skip oauth_callback, disconnect, etc.)
        if event_name in ("oauth_callback", "disconnect", "connect", "tokens_saved", "webhook_received"):
            continue
        # Reconciliation deletes belong on the Reconciliation tab only —
        # exclude from the default sync log so admins don't see a
        # confusing per-row delete entry alongside aggregate sync rows.
        if event_name == "qb_delete_sync":
            continue
        events.append({
            "timestamp": row.created_at.isoformat() if row.created_at else None,
            "type": event_name,
            "count": created + updated,
            "status": "success",  # Failures raise before the audit entry is written
            "details": f"{created} created, {updated} updated" if (created or updated) else None,
        })
    return {"events": events}


# ---------------------------------------------------------------------------
# Sync endpoints
# ---------------------------------------------------------------------------

@router.post("/sync/customers")
async def sync_customers(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _audit(db, request, current_user, "sync_customers", "customer")
    async with await get_qb_client(tenant_id, db) as qb:
        return await sync.pull_customers(tenant_id, db, qb)


@router.post("/sync/invoices")
async def sync_invoices(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _audit(db, request, current_user, "sync_invoices", "invoice")
    async with await get_qb_client(tenant_id, db) as qb:
        return await sync.pull_invoices(tenant_id, db, qb)


@router.post("/sync/items")
async def sync_items(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    _audit(db, request, current_user, "sync_items", "item")
    async with await get_qb_client(tenant_id, db) as qb:
        return await sync.pull_items(tenant_id, db, qb)


@router.post("/sync/accounts")
async def sync_accounts(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Pull Chart of Accounts from QB into qb_accounts."""
    tenant_id = _tenant_id(request)
    _audit(db, request, current_user, "sync_accounts", "account")
    async with await get_qb_client(tenant_id, db) as qb:
        return await sync.pull_accounts(tenant_id, db, qb)


@router.post("/sync/bank-transactions")
async def sync_bank_transactions(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """Pull bank/credit-card transactions (Purchase entity) from QB."""
    tenant_id = _tenant_id(request)
    _audit(db, request, current_user, "sync_bank_transactions", "bank_transaction")
    async with await get_qb_client(tenant_id, db) as qb:
        return await sync.pull_bank_transactions(tenant_id, db, qb, start_date, end_date)


@router.get("/accounts")
def list_accounts(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    """List synced Chart of Accounts from local qb_accounts."""
    from sqlalchemy import text as _text
    tenant_id = _tenant_id(request)
    try:
        rows = db.execute(_text("""
            SELECT qb_account_id, name, account_type, account_sub_type, classification,
                   current_balance, active, synced_at
            FROM qb_accounts WHERE tenant_id = :tid
            ORDER BY classification, account_type, name
        """), {"tid": tenant_id}).mappings().all()
    except Exception:
        log.exception("qb_list_accounts_failed tenant=%s", tenant_id)
        db.rollback()
        rows = []
    return {"items": [dict(r) for r in rows]}


@router.get("/bank-transactions")
def list_bank_transactions(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start_date: str = "",
    end_date: str = "",
    limit: int = 100,
) -> dict[str, list[dict[str, Any]]]:
    """List synced bank transactions from local qb_bank_transactions.

    Tenant isolation is the DB connection itself (per CLAUDE.md tenant
    plane rule) — no tenant_id filter. Filters `deleted_at IS NULL` so
    tombstoned Purchases drop out, with a fallback for tenants whose
    legacy schema hasn't yet absorbed the deleted_at additive migration.
    """
    from sqlalchemy import text as _text
    tenant_id = _tenant_id(request)
    where = ["1=1"]
    params: dict[str, Any] = {"lim": max(1, min(limit, 500))}
    if start_date:
        where.append("txn_date >= :sd")
        params["sd"] = start_date
    if end_date:
        where.append("txn_date <= :ed")
        params["ed"] = end_date
    try:
        # Strict form: filter deleted_at. If the column doesn't exist on
        # this tenant's legacy schema, fall through to the unfiltered read.
        try:
            rows = db.execute(_text(f"""
                SELECT qb_txn_id, txn_date, txn_type, account_name, payee, amount, memo, synced_at
                FROM qb_bank_transactions WHERE deleted_at IS NULL AND {' AND '.join(where)}
                ORDER BY txn_date DESC LIMIT :lim
            """), params).mappings().all()
        except Exception:
            db.rollback()
            rows = db.execute(_text(f"""
                SELECT qb_txn_id, txn_date, txn_type, account_name, payee, amount, memo, synced_at
                FROM qb_bank_transactions WHERE {' AND '.join(where)}
                ORDER BY txn_date DESC LIMIT :lim
            """), params).mappings().all()
    except Exception:
        log.exception("qb_list_bank_transactions_failed tenant=%s", tenant_id)
        db.rollback()
        rows = []
    return {"items": [dict(r) for r in rows]}


@router.post("/sync")
@router.post("/sync/full")
async def sync_full(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, dict[str, Any]]:
    """Full sync — pull all entities from QB. POST /api/qb/sync (frontend) or
    POST /api/qb/sync/full (CLI/admin).

    Return type is dict[str, Any] (not dict[str, int]) because each entity's
    result carries a list of per-row errors alongside the integer counts —
    see 2026-04-13 bug where FastAPI ResponseValidationError crashed the
    endpoint with 500 even though the actual sync had succeeded against QB.
    """
    tenant_id = _tenant_id(request)
    _audit(db, request, current_user, "sync_full", "full")
    async with await get_qb_client(tenant_id, db) as qb:
        # Accounts + bank transactions live in qb_accounts / qb_bank_transactions
        # (Slice 3B), separate from qb_entity_maps. Folded into the full sync so
        # "Sync Now" actually populates the Overview counts and the COA / Bank
        # tabs — without these, those tabs stay empty after a generic sync.
        return {
            "customers": await sync.pull_customers(tenant_id, db, qb),
            "invoices": await sync.pull_invoices(tenant_id, db, qb),
            "items": await sync.pull_items(tenant_id, db, qb),
            "vendors": await sync.pull_vendors(tenant_id, db, qb),
            "payments": await sync.pull_payments(tenant_id, db, qb),
            "accounts": await sync.pull_accounts(tenant_id, db, qb),
            "bank_transactions": await sync.pull_bank_transactions(tenant_id, db, qb),
        }


# ---------------------------------------------------------------------------
# Settings — tenant-level toggles (S103)
# ---------------------------------------------------------------------------

@router.post("/settings/delete-sync")
def set_delete_sync(
    request: FastAPIRequest,
    payload: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Flip the per-tenant delete-sync flag.

    Body: ``{"enabled": true | false | null}``. NULL clears the override and
    falls back to the QB_DELETE_SYNC_ENABLED env var. Admin/owner only —
    flipping this enables destructive deletes against this tenant's data.
    """
    if current_user.get("role") not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Insufficient role")

    raw = payload.get("enabled") if isinstance(payload, dict) else None
    if raw is None:
        new_value: bool | None = None
    elif isinstance(raw, bool):
        new_value = raw
    else:
        raise HTTPException(status_code=400, detail="enabled must be a boolean or null")

    tenant_id = _tenant_id(request)
    conn = db.execute(
        select(QBConnection).where(QBConnection.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if conn is None:
        # No row yet — flag has nowhere to live until QB is connected. The
        # alternative would be to lazily create a stub QBConnection, but that
        # confuses /status (treats existence as "connected").
        raise HTTPException(status_code=409, detail="QuickBooks is not connected for this tenant")

    previous = conn.delete_sync_enabled
    conn.delete_sync_enabled = new_value
    db.commit()
    _audit(db, request, current_user, "set_delete_sync", "settings")
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=str(current_user.get("user_id") or current_user.get("sub") or "unknown"),
        action="qb_set_delete_sync",
        entity_type="quickbooks",
        entity_id="settings",
        details={"previous": previous, "new": new_value},
    )

    return {
        "delete_sync_enabled": sync._delete_sync_enabled(tenant_id, db),
        "delete_sync_source": _delete_sync_source(conn),
        "column_value": new_value,
    }


# ---------------------------------------------------------------------------
# Push endpoints
# ---------------------------------------------------------------------------

@router.post("/push/invoice/{id}")
async def push_invoice_endpoint(
    id: str,
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    tenant_id = _tenant_id(request)
    _audit(db, request, current_user, "push_invoice", "invoice")
    async with await get_qb_client(tenant_id, db) as qb:
        return await sync.push_invoice(tenant_id, id, db, qb)


@router.post("/push/invoice-auto")
async def auto_push_invoice(
    request: FastAPIRequest,
    invoice_id: str = "",
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Push an invoice to QuickBooks automatically."""
    if not invoice_id:
        raise HTTPException(status_code=400, detail="invoice_id required")
    tenant_id = _tenant_id(request)
    _audit(db, request, user, "push_invoice_auto", "invoice")
    async with await get_qb_client(tenant_id, db) as qb:
        result = await sync.push_invoice(tenant_id, invoice_id, db, qb)
    return {"status": "ok", **result}


# ---------------------------------------------------------------------------
# Dashboard + list endpoints
# ---------------------------------------------------------------------------

@router.get("/dashboard")
def qb_dashboard(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    from sqlalchemy import func, text as _text
    tenant_id = _tenant_id(request)
    conn = db.execute(
        select(QBConnection).where(QBConnection.tenant_id == tenant_id)
    ).scalar_one_or_none()

    # Connected status: true if EITHER the legacy QBConnection row exists OR
    # the new encrypted QBTokenStore has a row for this tenant. The
    # disconnect endpoint clears both, so the OR matches reality.
    token_present = False
    try:
        # S122-4 (N1): filter by tenant; previously .limit(1) returned the
        # whichever-tenant global row, leaking dashboard connected-status
        # across tenants.
        token_present = db.execute(
            select(QBTokenStore).where(QBTokenStore.tenant_id == tenant_id)
        ).scalar_one_or_none() is not None
    except Exception:
        log.exception("qb_dashboard_token_store_query_failed")
        db.rollback()

    entity_counts = {
        row[0]: int(row[1])
        for row in db.execute(
            select(QBEntityMap.entity_type, func.count())
            .where(QBEntityMap.tenant_id == tenant_id)
            .group_by(QBEntityMap.entity_type)
        ).all()
    }
    # Slice 3B tables (qb_accounts, qb_bank_transactions) live on the
    # tenant plane — isolation is the DB connection itself, so no
    # tenant_id filter (per the 2026-04-22 tenant-plane invariant). The
    # qb_bank_transactions count excludes tombstoned rows; tenants whose
    # legacy schema hasn't gained deleted_at fall back to the raw count.
    for table_name, key in (("qb_accounts", "account"), ("qb_bank_transactions", "bank_transaction")):
        try:
            if table_name == "qb_bank_transactions":
                try:
                    count = db.execute(_text(
                        f"SELECT COUNT(*) FROM {table_name} WHERE deleted_at IS NULL"
                    )).scalar() or 0
                except Exception:
                    db.rollback()
                    count = db.execute(_text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
            else:
                count = db.execute(_text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
            if count or key not in entity_counts:
                entity_counts[key] = int(count)
        except Exception:
            # Table may not exist yet on a tenant that has never synced
            # accounts/bank-tx. Treat as zero, don't fail the dashboard.
            db.rollback()

    # 2026-05-20 banking expansion: the Overview "Banking" row should
    # reflect the FULL unified feed (Purchases + Deposits + Transfers +
    # the 5 qb_banking_entries types), not just qb_bank_transactions.
    # Add the extra source tables to bank_transaction's running total.
    # Tenant isolation here is the DB connection itself, so these
    # tables hold only this tenant's rows — no tenant_id filter needed.
    extra_banking_total = 0
    for tbl in ("qb_deposits", "qb_transfers", "qb_banking_entries"):
        try:
            n = db.execute(_text(
                f"SELECT COUNT(*) FROM {tbl} WHERE deleted_at IS NULL"
            )).scalar() or 0
            extra_banking_total += int(n)
        except Exception:
            db.rollback()
    if extra_banking_total:
        entity_counts["bank_transaction"] = int(entity_counts.get("bank_transaction", 0)) + extra_banking_total

    return {
        "connected": bool(conn) or token_present,
        "realm_id": conn.realm_id if conn else None,
        "last_sync_at": conn.last_sync_at.isoformat() if conn and conn.last_sync_at else None,
        "error_count": int(conn.error_count or 0) if conn else 0,
        "last_error": conn.last_error if conn else None,
        "entity_counts": entity_counts,
        "delete_sync_enabled": sync._delete_sync_enabled(tenant_id, db),
        "delete_sync_source": _delete_sync_source(conn),
    }


# ---------------------------------------------------------------------------
# Banking (Purchases + Deposits + Transfers) + per-tenant sync schedule
# ---------------------------------------------------------------------------

from gdx_dispatch.modules.quickbooks import banking as _banking
from pydantic import BaseModel as _BaseModel, Field as _Field


class _ScheduleIn(_BaseModel):
    frequency: str = _Field(..., description="manual | hourly | every_4h | daily | weekly")


@router.post("/sync/deposits")
async def sync_deposits(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    try:
        async with await get_qb_client(tenant_id, db) as qb:
            result = await _banking.pull_deposits(tenant_id, db, qb, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log_audit_event_sync(
        db, tenant_id=tenant_id, actor_id=str(current_user.get("sub") or ""),
        action="qb.sync_deposits", entity_type="qb_deposits", entity_id="*", metadata=result,
    )
    return result


@router.post("/sync/transfers")
async def sync_transfers(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    try:
        async with await get_qb_client(tenant_id, db) as qb:
            result = await _banking.pull_transfers(tenant_id, db, qb, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log_audit_event_sync(
        db, tenant_id=tenant_id, actor_id=str(current_user.get("sub") or ""),
        action="qb.sync_transfers", entity_type="qb_transfers", entity_id="*", metadata=result,
    )
    return result


@router.get("/banking/balances")
def banking_balances(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_id(request)
    return {"items": _banking.bank_balances(db)}


@router.get("/banking/transactions")
def banking_transactions(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    kind: str = "",          # comma-separated kinds, or "all"/empty
    search: str = "",        # case-insensitive substring across account/counterparty/memo/qb_txn_id/txn_type
    account: str = "",       # exact account_name match
    start_date: str = "",    # YYYY-MM-DD
    end_date: str = "",      # YYYY-MM-DD
    order_by: str = "txn_date",  # whitelisted: txn_date/amount/account/counterparty/kind/txn_type
    order_dir: str = "desc",
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    """Paginated, filtered banking feed.

    Returns {items, total, page, page_size}. Pushdown of date/kind/account
    happens inside per-source SELECTs so the per-source cap is taken AFTER
    predicates — fixes the bug where filtering 'Transfers' could appear
    empty because Purchases dominated the unfiltered pre-cap.
    """
    _tenant_id(request)
    if page < 1:
        raise HTTPException(status_code=400, detail="page must be >= 1")
    if page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="page_size must be 1..200")
    result = _banking.unified_banking_transactions(
        db,
        kind=kind or None,
        search=search,
        account=account,
        start_date=start_date,
        end_date=end_date,
        order_by=order_by,
        order_dir=order_dir,
        page=page,
        page_size=page_size,
        paginated=True,
    )
    # unified_banking_transactions returns dict in paginated mode (our path).
    return result if isinstance(result, dict) else {"items": result, "total": len(result), "page": 1, "page_size": page_size}


@router.post("/banking/sync")
async def banking_sync(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """Run all four pulls under one date window: accounts (for balances) +
    purchases + deposits + transfers."""
    tenant_id = _tenant_id(request)
    out: dict[str, Any] = {}

    async def _try_pull(key: str, coro_factory) -> None:
        """Run one entity's pull, capture the error if it fails so the
        other entities still land. One bad entity (QBO schema mismatch,
        rate limit on that specific endpoint, etc.) shouldn't 500 the
        whole sync. Errors surface in the response per entity."""
        try:
            out[key] = await coro_factory()
        except Exception as exc:  # noqa: BLE001 — intentionally broad: isolate per-pull failures
            log.exception("qb_banking_sync_pull_failed entity=%s tenant=%s", key, tenant_id)
            out[key] = {"created": 0, "updated": 0, "deleted": 0, "errors": [
                {"qb_id": "*", "error": f"{type(exc).__name__}: {str(exc)[:240]}"},
            ]}

    try:
        async with await get_qb_client(tenant_id, db) as qb:
            await _try_pull("accounts",        lambda: sync.pull_accounts(tenant_id, db, qb))
            await _try_pull("purchases",       lambda: sync.pull_bank_transactions(tenant_id, db, qb, start_date, end_date))
            await _try_pull("deposits",        lambda: _banking.pull_deposits(tenant_id, db, qb, start_date, end_date))
            await _try_pull("transfers",       lambda: _banking.pull_transfers(tenant_id, db, qb, start_date, end_date))
            await _try_pull("bill_payments",   lambda: _banking.pull_bill_payments(tenant_id, db, qb, start_date, end_date))
            await _try_pull("sales_receipts",  lambda: _banking.pull_sales_receipts(tenant_id, db, qb, start_date, end_date))
            await _try_pull("refund_receipts", lambda: _banking.pull_refund_receipts(tenant_id, db, qb, start_date, end_date))
            await _try_pull("journal_entries", lambda: _banking.pull_journal_entries(tenant_id, db, qb, start_date, end_date))
            await _try_pull("customer_payments", lambda: _banking.pull_customer_payments(tenant_id, db, qb, start_date, end_date))
            await _try_pull("vendor_credits", lambda: _banking.pull_vendor_credits(tenant_id, db, qb, start_date, end_date))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    def _audit_slice(v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            return {}
        return {
            "created": v.get("created", 0),
            "updated": v.get("updated", 0),
            "deleted": v.get("deleted", 0),
            "errors": len(v.get("errors") or []),
        }

    log_audit_event_sync(
        db, tenant_id=tenant_id, actor_id=str(current_user.get("sub") or ""),
        action="qb.banking_sync", entity_type="qb_banking", entity_id="*",
        metadata={k: _audit_slice(v) for k, v in out.items()},
    )
    return out


@router.get("/schedule")
def get_qb_schedule(
    request: FastAPIRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tenant_id(request)
    s = _banking.get_or_create_schedule(db)
    return _banking.schedule_dict(s)


@router.put("/schedule")
def put_qb_schedule(
    request: FastAPIRequest,
    payload: _ScheduleIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    try:
        s = _banking.update_schedule(db, payload.frequency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log_audit_event_sync(
        db, tenant_id=tenant_id, actor_id=str(current_user.get("sub") or ""),
        action="qb.schedule.update", entity_type="qb_sync_schedule", entity_id=str(s.id),
        metadata={"frequency": s.frequency},
    )
    return _banking.schedule_dict(s)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@router.post("/webhooks")
async def qb_webhooks(
    request: FastAPIRequest,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Receive and verify QuickBooks webhooks."""
    import base64
    import hashlib
    import hmac
    import json

    verifier = os.getenv("QB_WEBHOOK_VERIFIER_TOKEN", "")
    raw = await request.body()

    if verifier:
        sig_header = request.headers.get("intuit-signature", "")
        if not sig_header:
            raise HTTPException(status_code=403, detail="Missing QuickBooks signature")
        expected = base64.b64encode(
            hmac.new(verifier.encode("utf-8"), raw, hashlib.sha256).digest()
        ).decode("utf-8")
        if not hmac.compare_digest(expected, sig_header):
            raise HTTPException(status_code=403, detail="Invalid QuickBooks signature")

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.exception("quickbooks_webhook_payload_invalid_json")
        payload = {}

    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
    log.info("quickbooks_webhook_processed tenant=%s verified=True", tenant_id)

    log_audit_event_sync(
        db, tenant_id=tenant_id, user_id="system",
        action="qb_webhook_received", entity_type="qb_webhook",
        entity_id="", details={}, request=request,
    )
    db.commit()

    event_count = len(payload.get("eventNotifications", [])) if isinstance(payload, dict) else 0
    return {"ok": True, "verified": True, "event_count": event_count}
