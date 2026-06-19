"""QuickBooks Online RecurringTransaction → tenant DB mirror.

QBO endpoint:
    GET /v3/company/{realm_id}/query?query=SELECT * FROM RecurringTransaction&minorversion=75

The RecurringTransaction entity is a wrapper around child entity types
(Invoice / Bill / JournalEntry / SalesReceipt / Estimate / Purchase).
Each child has a RecurringInfo block with ScheduleInfo (NextDate,
IntervalType, NumInterval, DaysOfWeek, Active).

We pull the raw JSON and upsert one row per child entity. Auth uses the
canonical qb_token_store written by gdx_dispatch/modules/quickbooks/oauth.py's
save_tokens() — see test_quickbooks_token_storage.py for the contract.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.quickbooks import QBAuthError, QBError
from gdx_dispatch.modules.forecasting.models import QBRecurringTransaction

log = logging.getLogger(__name__)


def _qbo_base_url() -> str:
    env = os.getenv("QB_ENVIRONMENT", "production").lower()
    if env in ("sandbox", "dev", "development"):
        return "https://sandbox-quickbooks.api.intuit.com"
    return "https://quickbooks.api.intuit.com"


def _fetch_recurring(realm_id: str, access_token: str) -> list[dict[str, Any]]:
    """Hit the QBO query endpoint and return the raw RecurringTransaction list.

    Raises QBAuthError on 401; QBError on any other non-200 (so the
    router can surface it to the user). Silent `[]` on rate-limit /
    server error would render as "Recurring: 0" — a confidently wrong
    number, the worst failure mode for a finance dashboard.
    """
    query = "SELECT * FROM RecurringTransaction"
    url = f"{_qbo_base_url()}/v3/company/{realm_id}/query?query={quote(query)}&minorversion=75"
    req = Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == 401:
            raise QBAuthError("QuickBooks access token rejected") from exc
        body = ""
        try:
            body = exc.read().decode("utf-8")[:500]  # type: ignore[attr-defined]
        except Exception:
            pass
        log.error("qb_recurring_fetch_failed code=%s body=%s", code, body)
        raise QBError(f"QuickBooks recurring fetch failed (HTTP {code})") from exc
    parsed = json.loads(raw or "{}")
    qr = parsed.get("QueryResponse") or {}
    return list(qr.get("RecurringTransaction") or [])


def _flatten_child(wrapper: dict[str, Any]) -> dict[str, Any] | None:
    """A RecurringTransaction wrapper contains exactly one child entity.

    Returns the flattened child dict with a synthetic `_txn_type` key, or
    None if we can't identify the child shape.
    """
    for txn_type in ("Invoice", "Bill", "JournalEntry", "SalesReceipt", "Estimate", "Purchase", "CreditMemo"):
        child = wrapper.get(txn_type)
        if isinstance(child, dict):
            out = dict(child)
            out["_txn_type"] = txn_type
            return out
    return None


def _parse_next_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _amount_from(child: dict[str, Any]) -> float:
    # Invoice/Bill/SalesReceipt/etc. all use TotalAmt at the top level
    # when present; fall back to summing Line items if not.
    total = child.get("TotalAmt")
    if isinstance(total, (int, float)):
        return float(total)
    lines = child.get("Line") or []
    if isinstance(lines, list):
        s = 0.0
        for ln in lines:
            amt = (ln or {}).get("Amount")
            if isinstance(amt, (int, float)):
                s += float(amt)
        return s
    return 0.0


def upsert_recurring(db: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    created = 0
    updated = 0
    for wrapper in rows:
        child = _flatten_child(wrapper)
        if not child:
            continue
        qb_id = str(child.get("Id") or "").strip()
        if not qb_id:
            continue
        recurring_info = (child.get("RecurringInfo") or {})
        schedule = (recurring_info.get("ScheduleInfo") or {})
        customer_ref = child.get("CustomerRef") or {}

        row = db.execute(select(QBRecurringTransaction).where(QBRecurringTransaction.qb_id == qb_id)).scalar_one_or_none()
        if row is None:
            row = QBRecurringTransaction(qb_id=qb_id, txn_type=child["_txn_type"])
            db.add(row)
            created += 1
        else:
            updated += 1

        row.txn_type = child["_txn_type"]
        row.name = (recurring_info.get("Name") or child.get("DocNumber") or None)
        row.customer_qb_id = str(customer_ref.get("value") or "") or None
        row.customer_name = customer_ref.get("name") or None
        row.amount = _amount_from(child)
        row.next_date = _parse_next_date(schedule.get("NextDate"))
        row.interval_type = schedule.get("IntervalType") or None
        ni = schedule.get("NumInterval")
        row.num_interval = int(ni) if isinstance(ni, (int, float)) else None
        dow = schedule.get("DaysOfWeek")
        row.days_of_week = ",".join(dow) if isinstance(dow, list) else (dow or None)
        row.active = bool(recurring_info.get("Active", True))
        row.raw_json = wrapper
        row.last_synced_at = datetime.now(UTC)
    db.commit()
    return {"created": created, "updated": updated, "total": created + updated}


def sync_recurring_for_tenant(tenant_id: str, db: Session) -> dict[str, int]:
    """Pull RecurringTransaction list from QBO and upsert to tenant DB.

    Reads tokens from the canonical qb_token_store written by
    gdx_dispatch/modules/quickbooks/oauth.py::save_tokens (the function the
    /api/qb/oauth/callback handler calls). Tenants who never connected
    QuickBooks get a QBAuthError (handled by the router as 400).
    """
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from gdx_dispatch.modules.quickbooks.oauth import QBTokenStore, _decrypt

    try:
        row = db.execute(
            select(QBTokenStore).where(QBTokenStore.tenant_id == tenant_id)
            .order_by(QBTokenStore.updated_at.desc())
        ).scalars().first()
    except (ProgrammingError, OperationalError) as exc:
        # Tenant DB predates qb_token_store (never paved or migrated).
        # Postgres raises ProgrammingError on UndefinedTable; SQLite
        # raises OperationalError on no-such-table. Either way: loud,
        # attributable failure beats a 500 with a poisoned session.
        db.rollback()
        raise QBAuthError(
            f"qb_token_store missing on tenant {tenant_id} — "
            "run gdx_dispatch/tools/create_qb_token_store.py"
        ) from exc

    if row is None:
        raise QBAuthError(f"QuickBooks not connected for tenant {tenant_id}")

    access_token = _decrypt(row.access_token_enc)
    if not row.realm_id or not access_token:
        raise QBAuthError("QuickBooks credentials unavailable for tenant")

    rows = _fetch_recurring(str(row.realm_id), str(access_token))
    return upsert_recurring(db, rows)
