from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, log_audit_event
from gdx_dispatch.models.tenant_models import (
    CustomCatalog,
    CustomCatalogItem,
    Customer,
    Expense,
    ExpenseLine,
    Invoice,
    InvoiceLine,
    Job,
)

log = logging.getLogger(__name__)


class QBError(Exception):
    pass


class QBAuthError(QBError):
    pass


class QBConnection(TenantBase):
    __tablename__ = "qb_connections"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    realm_id: Mapped[str] = mapped_column(String(100), nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    # Per-tenant override for the Slice 5 delete-detection flag. NULL means
    # "no preference — fall back to the global QB_DELETE_SYNC_ENABLED env var."
    # An admin flips this from the Reconciliation tab to pilot delete sync on
    # one tenant without touching the global env var. The env var remains a
    # global kill-switch — when set to 0 it overrides any True column value.
    delete_sync_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class QBEntityMap(TenantBase):
    __tablename__ = "qb_entity_maps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_type", "local_id", name="uq_qb_map_local"),
        UniqueConstraint("tenant_id", "entity_type", "qb_id", name="uq_qb_map_remote"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    local_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    qb_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class QBVendor(TenantBase):
    __tablename__ = "qb_vendors"
    __table_args__ = (UniqueConstraint("tenant_id", "qb_vendor_id", name="uq_qb_vendor_tenant"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    qb_vendor_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


def _run_audit(
    db: Session,
    *,
    event_type: str,
    entity_id: str,
    payload: dict[str, Any],
    actor_id: str = "system",
) -> None:
    try:
        asyncio.run(log_audit_event(db, event_type, actor_id, "quickbooks", entity_id, payload))
    except RuntimeError:
        logging.getLogger(__name__).exception("_run_audit caught exception")
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(log_audit_event(db, event_type, actor_id, "quickbooks", entity_id, payload))
        finally:
            loop.close()


def _touch_sync_success(tenant_id: str, db: Session) -> None:
    conn = db.execute(select(QBConnection).where(QBConnection.tenant_id == tenant_id)).scalar_one_or_none()
    if conn is None:
        return
    conn.last_sync_at = datetime.now(UTC)
    conn.last_error = None
    conn.updated_at = datetime.now(UTC)
    db.commit()


def _touch_sync_error(tenant_id: str, db: Session, exc: Exception) -> None:
    conn = db.execute(select(QBConnection).where(QBConnection.tenant_id == tenant_id)).scalar_one_or_none()
    if conn is None:
        return
    conn.error_count = int(conn.error_count or 0) + 1
    conn.last_error = str(exc)
    conn.updated_at = datetime.now(UTC)
    db.commit()


def _extract_qb_id(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        obj = payload.get(key)
        if isinstance(obj, dict) and obj.get("Id"):
            return str(obj["Id"])
    if payload.get("Id"):
        return str(payload["Id"])
    return ""


def _query_qb_entities(qb_client: Any, entity_name: str) -> list[dict[str, Any]]:
    entity_key = entity_name.lower()
    try:
        if entity_key == "customer":
            from quickbooks.objects.customer import Customer as QBModel
        elif entity_key == "invoice":
            from quickbooks.objects.invoice import Invoice as QBModel
        elif entity_key == "item":
            from quickbooks.objects.item import Item as QBModel
        elif entity_key == "vendor":
            from quickbooks.objects.vendor import Vendor as QBModel
        elif entity_key == "payment":
            from quickbooks.objects.payment import Payment as QBModel
        else:
            return []
        rows = QBModel.filter(max_results=1000, qb=qb_client)
    except Exception:
        logging.getLogger(__name__).exception("_query_qb_entities caught exception")
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
            continue
        # SDK to_json() returns a JSON *string* (not a dict).
        # json.loads() converts it to a plain dict with all nested objects serialized.
        if hasattr(row, "to_json"):
            try:
                import json as _json
                serialized = row.to_json()
                if isinstance(serialized, str):
                    out.append(_json.loads(serialized))
                elif isinstance(serialized, dict):
                    out.append(serialized)
                else:
                    out.append(dict(getattr(row, "__dict__", {})))
                continue
            except Exception:
                logging.getLogger(__name__).exception("_query_qb_entities caught exception")
                pass
        out.append(dict(getattr(row, "__dict__", {})))
    return out


def _qb_create(
    qb_client: Any,
    *,
    entity_name: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    entity_key = entity_name.lower()
    try:
        if hasattr(qb_client, "session") and qb_client.session is not None:
            qb_client.session.headers.update({"Request-Id": idempotency_key})
    except Exception:
        logging.getLogger(__name__).exception("_qb_create caught exception")
        pass

    try:
        if entity_key == "customer":
            from quickbooks.objects.customer import Customer as QBModel
        elif entity_key == "invoice":
            from quickbooks.objects.invoice import Invoice as QBModel
        elif entity_key == "purchase":
            from quickbooks.objects.purchase import Purchase as QBModel
        else:
            return {}

        obj = QBModel()
        for key, value in payload.items():
            setattr(obj, key, value)
        saved = obj.save(qb=qb_client)
    except Exception:
        logging.getLogger(__name__).exception("_qb_create caught exception")
        return {}

    if isinstance(saved, dict):
        return saved
    if hasattr(saved, "to_json"):
        data = saved.to_json()
        if isinstance(data, dict):
            return data
    return dict(getattr(saved, "__dict__", {}))


def store_oauth_tokens(
    tenant_id: str,
    *,
    realm_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_expires_in: int,
    db: Session,
) -> QBConnection:
    now = datetime.now(UTC)
    row = db.execute(select(QBConnection).where(QBConnection.tenant_id == tenant_id)).scalar_one_or_none()
    if row is None:
        row = QBConnection(tenant_id=tenant_id, realm_id=realm_id, access_token="", refresh_token="", access_token_expires_at=now, refresh_token_expires_at=now)
        db.add(row)

    row.realm_id = realm_id
    row.access_token = access_token
    row.refresh_token = refresh_token
    row.access_token_expires_at = now + timedelta(seconds=int(expires_in or 3600))
    row.refresh_token_expires_at = now + timedelta(seconds=int(refresh_expires_in or 8726400))
    row.updated_at = now
    db.commit()

    _run_audit(
        db,
        event_type="qb_oauth_callback",
        entity_id=tenant_id,
        payload={"tenant_id": tenant_id, "realm_id": realm_id},
    )
    db.commit()
    return row


def get_qb_client(tenant_id: str, db: Session) -> Any:
    row = db.execute(select(QBConnection).where(QBConnection.tenant_id == tenant_id)).scalar_one_or_none()
    if row is None:
        raise QBAuthError("QuickBooks connection not found for tenant")

    now = datetime.now(UTC)
    if row.refresh_token_expires_at and row.refresh_token_expires_at <= now:
        raise QBAuthError("QuickBooks refresh token expired")

    try:
        from intuitlib.client import AuthClient
        from quickbooks import QuickBooks

        auth_client = AuthClient(
            client_id=os.getenv("QB_CLIENT_ID", ""),
            client_secret=os.getenv("QB_CLIENT_SECRET", ""),
            redirect_uri=os.getenv("QB_REDIRECT_URI", ""),
            environment=os.getenv("QB_ENVIRONMENT", "production"),
            access_token=row.access_token,
            refresh_token=row.refresh_token,
            realm_id=row.realm_id,
        )

        # Auto-refresh access token if expired (refresh token is still valid per check above)
        if row.access_token_expires_at and row.access_token_expires_at <= now:
            _log = logging.getLogger(__name__)
            _log.info("qb_access_token_expired tenant=%s, refreshing", tenant_id)
            auth_client.refresh()
            # Persist the new tokens
            row.access_token = auth_client.access_token
            row.refresh_token = auth_client.refresh_token
            row.access_token_expires_at = now + timedelta(seconds=3600)
            row.updated_at = now
            db.commit()
            _log.info("qb_access_token_refreshed tenant=%s", tenant_id)

        client = QuickBooks(auth_client=auth_client, company_id=row.realm_id)
        client.realm_id = row.realm_id
        return client
    except ImportError:
        # Lightweight fallback object used in tests when SDK is not importable.
        logging.getLogger(__name__).exception("get_qb_client caught exception")
        return type("QBClient", (), {"realm_id": row.realm_id, "access_token": row.access_token, "refresh_token": row.refresh_token})()


def _upsert_map(tenant_id: str, entity_type: str, local_id: str, qb_id: str, db: Session) -> QBEntityMap:
    row = db.execute(
        select(QBEntityMap).where(
            QBEntityMap.tenant_id == tenant_id,
            QBEntityMap.entity_type == entity_type,
            QBEntityMap.local_id == local_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = QBEntityMap(tenant_id=tenant_id, entity_type=entity_type, local_id=local_id, qb_id=qb_id)
        db.add(row)
    row.qb_id = qb_id
    row.synced_at = datetime.now(UTC)
    return row


def pull_customers(tenant_id: str, db: Session) -> dict[str, int]:
    created = 0
    updated = 0
    try:
        qb_client = get_qb_client(tenant_id, db)
        rows = _query_qb_entities(qb_client, "Customer")

        for raw in rows:
            qb_id = str(raw.get("Id") or "")
            name = str(raw.get("DisplayName") or "").strip()
            if not qb_id or not name:
                continue
            email = ((raw.get("PrimaryEmailAddr") or {}).get("Address") or "").strip() or None
            phone = ((raw.get("PrimaryPhone") or {}).get("FreeFormNumber") or "").strip() or None

            mapping = db.execute(
                select(QBEntityMap).where(
                    QBEntityMap.tenant_id == tenant_id,
                    QBEntityMap.entity_type == "customer",
                    QBEntityMap.qb_id == qb_id,
                )
            ).scalar_one_or_none()

            if mapping is not None:
                customer = db.get(Customer, UUID(mapping.local_id))
                if customer is None:
                    continue
                customer.name = name
                customer.email = email
                customer.phone = phone
                mapping.synced_at = datetime.now(UTC)
                updated += 1
                continue

            customer = Customer(name=name, email=email, phone=phone, source="quickbooks", company_id=tenant_id)
            db.add(customer)
            db.flush()
            _upsert_map(tenant_id, "customer", str(customer.id), qb_id, db)
            created += 1

        db.commit()
        _touch_sync_success(tenant_id, db)
        _run_audit(
            db,
            event_type="qb_pull_customers",
            entity_id=tenant_id,
            payload={"tenant_id": tenant_id, "created": created, "updated": updated},
        )
        db.commit()
        return {"created": created, "updated": updated}
    except Exception as exc:
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


# GL S9 (spec §5 row 8): the legacy pull_invoices/pull_payments that lived
# here were dead raw Invoice.status/Payment writers (the live sync is
# modules/quickbooks/sync.py, gated by ledger_posting_enabled). Deleted so
# nobody resurrects a pull path that bypasses the ledger chokepoint.


def _get_or_create_qb_catalog(db: Session) -> CustomCatalog:
    catalog = db.execute(select(CustomCatalog).where(CustomCatalog.source_system == "qb", CustomCatalog.deleted_at.is_(None))).scalar_one_or_none()
    if catalog is not None:
        return catalog
    catalog = CustomCatalog(name="QuickBooks Catalog", source_system="qb")
    db.add(catalog)
    db.flush()
    return catalog


def pull_items(tenant_id: str, db: Session) -> dict[str, int]:
    created = 0
    updated = 0
    try:
        qb_client = get_qb_client(tenant_id, db)
        rows = _query_qb_entities(qb_client, "Item")
        catalog = _get_or_create_qb_catalog(db)

        for raw in rows:
            qb_id = str(raw.get("Id") or "")
            name = str(raw.get("Name") or "").strip()
            if not qb_id or not name:
                continue
            price = Decimal(str(raw.get("UnitPrice") or 0))
            description = str(raw.get("Description") or "").strip() or None
            category = str(raw.get("Type") or "").strip() or None
            active = bool(raw.get("Active", True))

            item = db.execute(
                select(CustomCatalogItem).where(
                    CustomCatalogItem.qb_item_id == qb_id,
                    CustomCatalogItem.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if item is None:
                item = CustomCatalogItem(
                    catalog_id=catalog.id,
                    sku=qb_id,
                    name=name,
                    description=description,
                    cost=price,
                    price=price,
                    category=category,
                    active=active,
                    qb_item_id=qb_id,
                )
                db.add(item)
                created += 1
            else:
                item.name = name
                item.description = description
                item.price = price
                item.active = active
                item.category = category
                updated += 1

        db.commit()
        _touch_sync_success(tenant_id, db)
        _run_audit(
            db,
            event_type="qb_pull_items",
            entity_id=tenant_id,
            payload={"tenant_id": tenant_id, "created": created, "updated": updated},
        )
        db.commit()
        return {"created": created, "updated": updated}
    except Exception as exc:
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


def pull_vendors(tenant_id: str, db: Session) -> dict[str, int]:
    created = 0
    updated = 0
    try:
        qb_client = get_qb_client(tenant_id, db)
        rows = _query_qb_entities(qb_client, "Vendor")

        for raw in rows:
            qb_vendor_id = str(raw.get("Id") or "")
            name = str(raw.get("DisplayName") or raw.get("CompanyName") or "").strip()
            if not qb_vendor_id or not name:
                continue
            email = ((raw.get("PrimaryEmailAddr") or {}).get("Address") or "").strip() or None
            phone = ((raw.get("PrimaryPhone") or {}).get("FreeFormNumber") or "").strip() or None

            row = db.execute(
                select(QBVendor).where(QBVendor.tenant_id == tenant_id, QBVendor.qb_vendor_id == qb_vendor_id)
            ).scalar_one_or_none()
            if row is None:
                row = QBVendor(tenant_id=tenant_id, qb_vendor_id=qb_vendor_id, name=name, email=email, phone=phone)
                db.add(row)
                created += 1
            else:
                row.name = name
                row.email = email
                row.phone = phone
                row.updated_at = datetime.now(UTC)
                updated += 1

        db.commit()
        _touch_sync_success(tenant_id, db)
        _run_audit(
            db,
            event_type="qb_pull_vendors",
            entity_id=tenant_id,
            payload={"tenant_id": tenant_id, "created": created, "updated": updated},
        )
        db.commit()
        return {"created": created, "updated": updated}
    except Exception as exc:
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


def push_customer(tenant_id: str, customer_id: str, db: Session) -> dict[str, str]:
    customer = db.get(Customer, UUID(customer_id))
    if customer is None:
        raise QBError("Customer not found")

    qb_client = get_qb_client(tenant_id, db)
    payload: dict[str, Any] = {"DisplayName": customer.name}
    if customer.email:
        payload["PrimaryEmailAddr"] = {"Address": customer.email}
    if customer.phone:
        payload["PrimaryPhone"] = {"FreeFormNumber": customer.phone}

    idempotency_key = f"gdx-customer-{customer_id}"
    resp = _qb_create(qb_client, entity_name="Customer", payload=payload, idempotency_key=idempotency_key)
    qb_customer_id = _extract_qb_id(resp, ("Customer",))
    if qb_customer_id:
        _upsert_map(tenant_id, "customer", customer_id, qb_customer_id, db)
    db.commit()

    _run_audit(
        db,
        event_type="qb_push_customer",
        entity_id=customer_id,
        payload={"tenant_id": tenant_id, "qb_customer_id": qb_customer_id, "idempotency_key": idempotency_key},
    )
    db.commit()
    return {"customer_id": customer_id, "qb_customer_id": qb_customer_id}


def push_invoice(tenant_id: str, invoice_id: str, db: Session) -> dict[str, str]:
    invoice = db.get(Invoice, UUID(invoice_id))
    if invoice is None:
        raise QBError("Invoice not found")

    # Counter-sale invoices have no job; resolve customer from the invoice
    # itself (NOT NULL since 2026-05-11). Job-attached invoices keep using
    # job.customer_id for back-compat with QB-imported synthetic-job rows.
    customer_local_id: str | None = None
    if invoice.job_id is not None:
        job = db.get(Job, invoice.job_id)
        if job and job.customer_id:
            customer_local_id = str(job.customer_id)
    if customer_local_id is None and invoice.customer_id is not None:
        customer_local_id = str(invoice.customer_id)

    customer_ref: dict[str, str] | None = None
    if customer_local_id is not None:
        customer_map = db.execute(
            select(QBEntityMap).where(
                QBEntityMap.tenant_id == tenant_id,
                QBEntityMap.entity_type == "customer",
                QBEntityMap.local_id == customer_local_id,
            )
        ).scalar_one_or_none()
        if customer_map is not None:
            customer_ref = {"value": customer_map.qb_id}

    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order.asc())
    ).scalars().all()
    qb_lines = []
    for line in lines:
        qb_lines.append(
            {
                "Amount": float(line.line_total),
                "DetailType": "SalesItemLineDetail",
                "Description": line.description,
                "SalesItemLineDetail": {
                    "Qty": float(line.quantity),
                    "UnitPrice": float(line.unit_price),
                },
            }
        )

    payload: dict[str, Any] = {
        "DocNumber": invoice.invoice_number,
        "Line": qb_lines,
    }
    if customer_ref is not None:
        payload["CustomerRef"] = customer_ref

    qb_client = get_qb_client(tenant_id, db)
    idempotency_key = f"gdx-invoice-{invoice_id}"
    resp = _qb_create(qb_client, entity_name="Invoice", payload=payload, idempotency_key=idempotency_key)
    qb_invoice_id = _extract_qb_id(resp, ("Invoice",))
    if qb_invoice_id:
        _upsert_map(tenant_id, "invoice", invoice_id, qb_invoice_id, db)
    db.commit()

    _run_audit(
        db,
        event_type="qb_push_invoice",
        entity_id=invoice_id,
        payload={"tenant_id": tenant_id, "qb_invoice_id": qb_invoice_id, "idempotency_key": idempotency_key},
    )
    db.commit()
    return {"invoice_id": invoice_id, "qb_invoice_id": qb_invoice_id}


def pull_accounts(tenant_id: str, db: Session) -> dict[str, int]:
    """Pull Chart of Accounts from QBO into qb_accounts table."""
    qb_client = get_qb_client(tenant_id, db)

    # D104 (an earlier session): gen_random_uuid() is PG-only — Build Rule #1 violation
    # because the SQLite test fixture has no equivalent. Drop the column default
    # and supply str(uuid4()) at INSERT time.
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS qb_accounts (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            qb_account_id VARCHAR(120) NOT NULL,
            name VARCHAR(300) NOT NULL,
            account_type VARCHAR(100),
            account_sub_type VARCHAR(100),
            classification VARCHAR(100),
            current_balance NUMERIC(14,2),
            active BOOLEAN DEFAULT true,
            synced_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(tenant_id, qb_account_id)
        )
    """))
    db.commit()

    try:
        from quickbooks.objects.account import Account as QBAccount
        rows = QBAccount.filter(max_results=500, qb=qb_client)
    except Exception:
        log.exception("qb_pull_accounts_query_failed")
        rows = []

    created, updated = 0, 0
    for row in rows:
        qb_id = str(getattr(row, "Id", ""))
        if not qb_id:
            continue
        name = getattr(row, "Name", "") or ""
        acct_type = getattr(row, "AccountType", "") or ""
        sub_type = getattr(row, "AccountSubType", "") or ""
        classification = getattr(row, "Classification", "") or ""
        balance = float(getattr(row, "CurrentBalance", 0) or 0)
        active = bool(getattr(row, "Active", True))

        existing = db.execute(text(
            "SELECT id FROM qb_accounts WHERE tenant_id = :tid AND qb_account_id = :qid"
        ), {"tid": tenant_id, "qid": qb_id}).scalar()

        if existing:
            db.execute(text("""
                UPDATE qb_accounts SET name = :name, account_type = :at, account_sub_type = :ast,
                    classification = :cls, current_balance = :bal, active = :act, synced_at = now()
                WHERE tenant_id = :tid AND qb_account_id = :qid
            """), {"name": name, "at": acct_type, "ast": sub_type, "cls": classification,
                   "bal": balance, "act": active, "tid": tenant_id, "qid": qb_id})
            updated += 1
        else:
            db.execute(text("""
                INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, account_sub_type,
                    classification, current_balance, active)
                VALUES (:id, :tid, :qid, :name, :at, :ast, :cls, :bal, :act)
            """), {"id": str(uuid4()), "tid": tenant_id, "qid": qb_id, "name": name, "at": acct_type, "ast": sub_type,
                   "cls": classification, "bal": balance, "act": active})
            created += 1

    db.commit()
    _touch_sync_success(tenant_id, db)
    _run_audit(db, event_type="qb_pull_accounts", entity_id=tenant_id,
               payload={"created": created, "updated": updated})
    return {"created": created, "updated": updated, "total": created + updated}


def pull_bank_transactions(tenant_id: str, db: Session, start_date: str = "", end_date: str = "") -> dict[str, int]:
    """Pull bank/credit card transactions from QBO."""
    qb_client = get_qb_client(tenant_id, db)

    # D104 (an earlier session): same as qb_accounts — no PG-only DEFAULT.
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS qb_bank_transactions (
            id UUID PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            qb_txn_id VARCHAR(120) NOT NULL,
            txn_date DATE,
            txn_type VARCHAR(50),
            account_name VARCHAR(300),
            payee VARCHAR(300),
            amount NUMERIC(14,2),
            memo TEXT,
            category VARCHAR(300),
            status VARCHAR(50),
            synced_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(tenant_id, qb_txn_id)
        )
    """))
    db.commit()

    # Query purchases (expenses/checks)
    try:
        from quickbooks.objects.purchase import Purchase as QBPurchase
        query = "SELECT * FROM Purchase"
        if start_date:
            query += f" WHERE TxnDate >= '{start_date}'"
            if end_date:
                query += f" AND TxnDate <= '{end_date}'"
        elif end_date:
            query += f" WHERE TxnDate <= '{end_date}'"
        query += " MAXRESULTS 500"
        rows = QBPurchase.query(query, qb=qb_client)
    except Exception:
        log.exception("qb_pull_bank_transactions_failed")
        rows = []

    created, updated = 0, 0
    for row in rows:
        qb_id = str(getattr(row, "Id", ""))
        if not qb_id:
            continue
        txn_date = getattr(row, "TxnDate", None)
        total = float(getattr(row, "TotalAmt", 0) or 0)
        payment_type = getattr(row, "PaymentType", "") or ""
        memo = getattr(row, "PrivateNote", "") or ""

        # Get payee name
        entity_ref = getattr(row, "EntityRef", None)
        payee = getattr(entity_ref, "name", "") if entity_ref else ""

        # Get account name
        account_ref = getattr(row, "AccountRef", None)
        account_name = getattr(account_ref, "name", "") if account_ref else ""

        existing = db.execute(text(
            "SELECT id FROM qb_bank_transactions WHERE tenant_id = :tid AND qb_txn_id = :qid"
        ), {"tid": tenant_id, "qid": qb_id}).scalar()

        if existing:
            db.execute(text("""
                UPDATE qb_bank_transactions SET txn_date = :dt, amount = :amt, payee = :payee,
                    account_name = :acct, memo = :memo, txn_type = :tt, synced_at = now()
                WHERE tenant_id = :tid AND qb_txn_id = :qid
            """), {"dt": txn_date, "amt": total, "payee": payee, "acct": account_name,
                   "memo": memo, "tt": payment_type, "tid": tenant_id, "qid": qb_id})
            updated += 1
        else:
            db.execute(text("""
                INSERT INTO qb_bank_transactions (id, tenant_id, qb_txn_id, txn_date, txn_type,
                    account_name, payee, amount, memo)
                VALUES (:id, :tid, :qid, :dt, :tt, :acct, :payee, :amt, :memo)
            """), {"id": str(uuid4()), "tid": tenant_id, "qid": qb_id, "dt": txn_date, "tt": payment_type,
                   "acct": account_name, "payee": payee, "amt": total, "memo": memo})
            created += 1

    db.commit()
    _touch_sync_success(tenant_id, db)
    _run_audit(db, event_type="qb_pull_bank_transactions", entity_id=tenant_id,
               payload={"created": created, "updated": updated})
    return {"created": created, "updated": updated, "total": created + updated}


def push_expense(tenant_id: str, expense_id: str, db: Session) -> dict[str, str]:
    expense = db.get(Expense, UUID(expense_id))
    if expense is None:
        raise QBError("Expense not found")

    expense_lines = db.execute(select(ExpenseLine).where(ExpenseLine.expense_id == expense.id)).scalars().all()
    line_payload = []
    if expense_lines:
        for line in expense_lines:
            line_payload.append(
                {
                    "Amount": float(line.amount),
                    "Description": line.description or expense.description or "Expense",
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "1"}},
                }
            )
    else:
        line_payload.append(
            {
                "Amount": float(expense.amount),
                "Description": expense.description or "Expense",
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "1"}},
            }
        )

    payload = {
        "TotalAmt": float(expense.amount),
        "PaymentType": "Cash",
        "Line": line_payload,
    }

    qb_client = get_qb_client(tenant_id, db)
    idempotency_key = f"gdx-expense-{expense_id}"
    resp = _qb_create(qb_client, entity_name="Purchase", payload=payload, idempotency_key=idempotency_key)
    qb_purchase_id = _extract_qb_id(resp, ("Purchase",))
    if qb_purchase_id:
        _upsert_map(tenant_id, "expense", expense_id, qb_purchase_id, db)
    db.commit()

    _run_audit(
        db,
        event_type="qb_push_expense",
        entity_id=expense_id,
        payload={"tenant_id": tenant_id, "qb_purchase_id": qb_purchase_id, "idempotency_key": idempotency_key},
    )
    db.commit()
    return {"expense_id": expense_id, "qb_purchase_id": qb_purchase_id}
