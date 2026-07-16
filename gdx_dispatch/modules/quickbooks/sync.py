"""QuickBooks sync operations — pull and push entities via the REST API.

All functions take a QBClient instance and a SQLAlchemy Session.
No SDK dependencies — pure httpx REST calls via QBClient.
"""
from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.name_normalize import humanize_name
from gdx_dispatch.core.quickbooks import QBConnection, QBEntityMap, QBVendor
from gdx_dispatch.models.tenant_models import (
    CustomCatalog,
    CustomCatalogItem,
    Customer,
    Expense,
    ExpenseLine,
    Invoice,
    InvoiceLine,
    Job,
    Payment,
)
from gdx_dispatch.modules.quickbooks.client import QBAPIError, QBClient

log = logging.getLogger(__name__)


# Re-export for backward compat with tasks.py
class QBSyncError(Exception):
    pass


class QBRateLimitError(QBSyncError):
    pass


class QBPullDisabledError(QBSyncError):
    """A money-mutating QB pull was attempted while ledger posting is on.

    GL S9 (spec §5.4): once ``ledger_posting_enabled`` the GDX ledger is the
    book of record — pulls that mutate Invoice/Payment rows would write money
    around the posting chokepoint, so they fail loudly instead. Read-only
    mirror pulls (accounts, bank transactions) and non-money entity pulls
    (customers, items, vendors) are unaffected. QBO-side corrections flow
    forward from GDX (credit memo / void / adjustment → push), never back.
    """


def money_pulls_disabled(db: Session, tenant_id: str) -> bool:
    """Read-only form of the §5.4 gate, for health/status surfaces (qb_status,
    qb_dashboard, webhook dispatch). Fail-open on lookup errors — a status
    page must not 500 over a flag read; the in-pull gate below stays
    fail-closed and is the actual enforcement."""
    from gdx_dispatch.modules.ledger.service import ledger_posting_enabled  # noqa: PLC0415

    try:
        return ledger_posting_enabled(db, tenant_id)
    except Exception:
        log.exception("qb_ledger_flag_lookup_failed tenant=%s", tenant_id)
        db.rollback()
        return False


def _assert_money_pull_allowed(tenant_id: str, db: Session, operation: str) -> None:
    """Gate for the four §5.4 money-mutating pull paths. Raises when the
    ledger flag is on; a flag-lookup error propagates (fail-closed — better a
    failed sync than money written around the chokepoint). The import is
    local so celery workers that never sync don't pay the ledger import at
    module load."""
    from gdx_dispatch.modules.ledger.service import ledger_posting_enabled  # noqa: PLC0415

    if ledger_posting_enabled(db, tenant_id):
        log.error(
            "qb_money_pull_blocked tenant=%s operation=%s — ledger_posting_enabled: "
            "GDX is the book of record; QBO-side changes to invoices/payments no "
            "longer flow back (GL spec §5.4). Correct in GDX and push forward.",
            tenant_id, operation,
        )
        raise QBPullDisabledError(
            f"QuickBooks {operation} pull is disabled: ledger posting is enabled "
            "and GDX is the book of record. Make the correction in GDX "
            "(credit memo, void, adjustment) and push it to QuickBooks instead."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_qb_date(value: Any) -> date | None:
    """Parse a QB date field (accepts 'YYYY-MM-DD' or longer ISO strings). Returns None on miss."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _normalize_name(name: str) -> str:
    """Lowercase + collapse whitespace + strip common punctuation for matching."""
    if not name:
        return ""
    import re
    s = name.lower().strip()
    s = re.sub(r"[.,'&()/\-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _phone_last4(phone: str) -> str:
    """Last 4 digits of a phone number. Empty string if fewer than 4 digits."""
    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else ""


def _adopt_existing_customer(db, *, tenant_id: str, qb_name: str, qb_email: str | None,
                              qb_phone: str | None) -> tuple[Customer, bool] | None:
    """Find an existing local customer to adopt for this QB entity.

    Returns (customer, ambiguous_flag) or None.

    Matching strategy:
      1. Exact (normalized name + last-4 phone) — highest confidence
      2. Multiple candidates on (name + phone) → check email — moderate
      3. Ambiguous or none → return None (caller CREATEs)
    """
    norm_qb_name = _normalize_name(qb_name)
    qb_phone4 = _phone_last4(qb_phone or "")
    if not norm_qb_name:
        return None

    # Load candidates with matching normalized name. We pull name+phone+email
    # to post-filter in Python; SQL-side normalization would need generated
    # columns (out of scope for this fix).
    candidates = db.execute(
        select(Customer).where(
            Customer.company_id == tenant_id,
            Customer.deleted_at.is_(None) if hasattr(Customer, "deleted_at") else True,  # type: ignore[arg-type]
        )
    ).scalars().all()

    name_matches = [c for c in candidates if _normalize_name(c.name) == norm_qb_name]
    if not name_matches:
        return None

    # Tier 1: name + phone match
    if qb_phone4:
        phone_matches = [c for c in name_matches if _phone_last4(c.phone or "") == qb_phone4]
        if len(phone_matches) == 1:
            return (phone_matches[0], False)
        if len(phone_matches) > 1:
            # Multiple with same name+phone → check email
            if qb_email:
                email_matches = [c for c in phone_matches if (c.email or "").strip().lower() == qb_email.strip().lower()]
                if len(email_matches) == 1:
                    return (email_matches[0], False)
            # Still ambiguous → flag for review
            return (phone_matches[0], True)

    # Tier 2: name matched but phone didn't (or QB had no phone). Only
    # adopt if there's exactly one local customer by that name AND we have
    # SOME other signal (email). Otherwise pure-name matches are unsafe.
    if len(name_matches) == 1 and qb_email:
        local_email = (name_matches[0].email or "").strip().lower()
        qb_email_norm = qb_email.strip().lower()
        if local_email and local_email == qb_email_norm:
            return (name_matches[0], False)

    # No confident match — caller CREATEs new, which is fine for genuinely
    # new customers.
    return None


def _delete_sync_enabled(tenant_id: str | None = None, db: Session | None = None) -> bool:
    """Slice 5 / S103 (2026-05-05): delete detection is gated by a per-tenant
    QBConnection.delete_sync_enabled column, with QB_DELETE_SYNC_ENABLED env
    var as the fallback when the column is NULL.

    Default OFF. The full-set-diff approach soft-deletes any local entity
    whose qb_id is no longer present in QBO. The risk if mis-applied (e.g.
    partial QBO scrape that misses rows) is real — entire customer rows
    would soft-delete from a half-fetched diff.

    Resolution order:
      1. If tenant_id+db provided AND QBConnection.delete_sync_enabled is
         not NULL, that wins. Admin opted in (True) or out (False) explicitly.
      2. Otherwise, fall back to the QB_DELETE_SYNC_ENABLED env var.
         (Pre-S103 behavior — preserves backward compat for existing callers.)

    The env var stays a useful global default for fresh tenants (NULL column)
    but a tenant admin can always pilot or veto by flipping their column.
    """
    if tenant_id and db is not None:
        try:
            value = db.execute(
                select(QBConnection.delete_sync_enabled).where(QBConnection.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if value is not None:
                return bool(value)
        except Exception:
            log.exception("qb_delete_sync_column_lookup_failed tenant=%s", tenant_id)
    return os.getenv("QB_DELETE_SYNC_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


async def _detect_qbo_merge_deletes(
    tenant_id: str,
    entity_type: str,
    seen_qb_ids: set[str],
    db: Session,
    qb: QBClient,
) -> int:
    """Detect QBO-side merges/deletes by per-row Active probe.

    QBO has no programmatic merge API; the user merges via the UI by renaming
    one customer to match another's DisplayName, which triggers QBO's
    DisplayName-uniqueness constraint into prompting a merge. The merged-away
    record gets ``Active=false`` and falls out of the default
    ``SELECT * FROM Customer`` pull (which filters Active=true by default).

    This helper closes the loop on the local side: for each existing
    ``qb_entity_maps`` row whose qb_id was NOT in this sync's ``seen_qb_ids``,
    issue a targeted ``GET /<entity>/<qb_id>`` and check the ``Active`` flag:

      - ``Active=false`` → confirmed remote merge/delete; soft-delete the local
        row, drop the map, audit as ``qbo_<entity>_merged_remote``.
      - ``Active=true`` → the entity exists but was missing from this pull
        (pagination, where-clause filter, transient API hiccup). No-op.
      - HTTP 404 / lookup failure → ambiguous. No-op.

    This is safer than the full-set-diff ``_apply_qbo_deletes`` path because
    every deletion is positively confirmed by a per-row GET. No feature flag
    required — always-on.

    Returns the number of soft-deletes performed.
    """
    model_map = {"customer": Customer, "invoice": Invoice, "payment": Payment}
    model = model_map.get(entity_type)
    if model is None:
        return 0

    entity_name_qb = {"customer": "Customer", "invoice": "Invoice", "payment": "Payment"}[entity_type]

    all_maps = db.execute(
        select(QBEntityMap).where(
            QBEntityMap.tenant_id == tenant_id,
            QBEntityMap.entity_type == entity_type,
        )
    ).scalars().all()
    candidates = [m for m in all_maps if m.qb_id not in seen_qb_ids]

    if not candidates:
        return 0

    soft_deleted = 0
    for mapping in candidates:
        try:
            remote = await qb.read(entity_name_qb, mapping.qb_id)
        except Exception:
            log.warning(
                "qbo_merge_probe_failed tenant=%s entity=%s qb_id=%s (skipping — ambiguous)",
                tenant_id, entity_type, mapping.qb_id,
            )
            continue

        if remote.get("Active", True):
            # Entity exists and is active — just missed by this pull. Don't act.
            continue

        # Confirmed Active=false → merged or deleted on the QBO side.
        try:
            _audit(db, f"qbo_{entity_type}_merged_remote", mapping.qb_id, {
                "tenant_id": tenant_id,
                "entity_type": entity_type,
                "qb_id": mapping.qb_id,
                "local_id": mapping.local_id,
                "reason": "qbo_active_false_after_pull",
                "remote_display_name": remote.get("DisplayName"),
            })
        except Exception:
            log.exception(
                "qbo_merge_audit_failed tenant=%s entity=%s qb_id=%s",
                tenant_id, entity_type, mapping.qb_id,
            )

        try:
            local = db.get(model, UUID(mapping.local_id))
            if local is not None and getattr(local, "deleted_at", None) is None:
                local.deleted_at = datetime.now(UTC)
                soft_deleted += 1
            db.delete(mapping)
        except Exception:
            log.exception(
                "qbo_merge_apply_failed tenant=%s entity=%s qb_id=%s",
                tenant_id, entity_type, mapping.qb_id,
            )

    if soft_deleted:
        log.info(
            "qbo_merge_detected tenant=%s entity_type=%s soft_deleted=%d",
            tenant_id, entity_type, soft_deleted,
        )
    return soft_deleted


def _apply_qbo_deletes(
    tenant_id: str,
    entity_type: str,
    seen_qb_ids: set[str],
    db: Session,
) -> int:
    """Soft-delete local entities whose qb_id no longer appears in QBO.

    Strategy: full-set diff. Caller passes the set of qb_ids returned by the
    just-completed QBO query (paginated, complete). Any QBEntityMap row for
    this tenant + entity_type whose qb_id is NOT in that set is treated as
    deleted upstream. CDC would be more efficient but ambiguous on
    create-vs-delete (Intuit docs); for GDX-scale data (300 invoices, 256
    customers) the diff is simple, deterministic, and safe.

    Soft-delete semantics:
      - Set ``deleted_at = now`` on the local entity row (audit-friendly).
      - DELETE the QBEntityMap row (decouples local from a removed QB id).

    Returns the number of soft-deletes performed. No-op when the feature flag
    is off or when ``seen_qb_ids`` is empty (treat empty as "we don't know" —
    do not nuke everything; require the caller to opt in by passing a real
    set).
    """
    if entity_type in ("invoice", "payment"):
        _assert_money_pull_allowed(tenant_id, db, f"{entity_type} delete-sync")
    if not _delete_sync_enabled(tenant_id, db):
        return 0
    if not seen_qb_ids:
        log.warning(
            "qb_delete_sync_skipped tenant=%s entity_type=%s reason=empty_seen_set",
            tenant_id, entity_type,
        )
        return 0

    # Map entity_type → ORM model.
    model_map = {
        "customer": Customer,
        "invoice": Invoice,
        "payment": Payment,
    }
    model = model_map.get(entity_type)
    if model is None:
        return 0

    stale = db.execute(
        select(QBEntityMap).where(
            QBEntityMap.tenant_id == tenant_id,
            QBEntityMap.entity_type == entity_type,
            ~QBEntityMap.qb_id.in_(seen_qb_ids),
        )
    ).scalars().all()

    deleted = 0
    for mapping in stale:
        # Audit BEFORE the destructive ops. The mapping row gets hard-deleted
        # below so this is the only persistent record of what was removed.
        # Without this row, Slice 5 deletes are invisible to the UI and to
        # any post-mortem — only a stdlib log line remains, which rotates.
        try:
            _audit(db, "qb_delete_sync", mapping.qb_id, {
                "tenant_id": tenant_id,
                "entity_type": entity_type,
                "qb_id": mapping.qb_id,
                "local_id": mapping.local_id,
                "reason": "absent_from_full_set_diff",
            })
        except Exception:
            log.exception("qb_delete_audit_failed tenant=%s entity=%s qb_id=%s",
                          tenant_id, entity_type, mapping.qb_id)

        try:
            local = db.get(model, UUID(mapping.local_id))
            if local is not None and getattr(local, "deleted_at", None) is None:
                local.deleted_at = datetime.now(UTC)
                deleted += 1
            db.delete(mapping)
        except Exception:
            log.exception("qb_delete_apply_failed tenant=%s entity=%s qb_id=%s",
                          tenant_id, entity_type, mapping.qb_id)
    if deleted:
        log.info("qb_delete_sync tenant=%s entity_type=%s deleted=%d",
                 tenant_id, entity_type, deleted)
    return deleted


def _resync_invoice_lines(invoice_id: UUID, raw_lines: list[dict[str, Any]], tenant_id: str, db: Session) -> int:
    """Replace local InvoiceLine rows with the lines from a QB invoice payload.

    Pre-fix the update + adoption branches of ``pull_invoices`` skipped lines
    entirely — adopted local invoices stayed line-less forever (the prod GDX
    audit found 282 line-less invoices for ~$615k of total revenue with no line
    detail), and edits in QB never propagated to GDX lines on existing maps.

    Strategy: delete-and-replace. QB is the source of truth for QB-imported
    invoices. The ``Amount`` field on each QB Line is the line total; ``Qty`` /
    ``UnitPrice`` may be in ``SalesItemLineDetail`` but pre-fix code only used
    ``Amount`` and stored qty=1. Keep that contract for now to avoid widening
    scope; structural quality of imported lines is a separate concern from
    presence/absence of any lines at all.

    Returns the number of lines written.
    """
    _assert_money_pull_allowed(tenant_id, db, "invoice line resync")
    db.execute(
        InvoiceLine.__table__.delete().where(InvoiceLine.invoice_id == invoice_id)
    )
    written = 0
    # QuickBooks Line objects come in several DetailType flavors. We want
    # ONLY the per-item charge lines — the others are summary/structure
    # rows that QB sums into the invoice TotalAmt server-side. Including
    # them in our local InvoiceLine table double-counts: the persisted
    # invoice.subtotal is correct (from QB TotalAmt) but sum(line_total)
    # ends up nearly 2× the subtotal. Confirmed on prod invoice #1111
    # 2026-05-09 — lines summed to $2,741.50, persisted total $1,471.84.
    _ITEM_LINE_TYPES = {
        "SalesItemLineDetail",
        "ItemBasedExpenseLineDetail",
        "AccountBasedExpenseLineDetail",
    }
    for sort_order, line in enumerate(raw_lines or [], start=1):
        amount_raw = line.get("Amount")
        if amount_raw is None:
            continue
        detail_type = line.get("DetailType")
        # Only filter when QB explicitly tells us the type. If absent,
        # fall through (older payloads / hand-built test fixtures).
        if detail_type and detail_type not in _ITEM_LINE_TYPES:
            continue
        amount = Decimal(str(amount_raw))
        db.add(InvoiceLine(
            invoice_id=invoice_id,
            description=str(line.get("Description") or "QuickBooks line"),
            quantity=1,
            unit_price=amount,
            line_total=amount,
            sort_order=sort_order,
            company_id=tenant_id,
        ))
        written += 1
    return written


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


def _touch_sync_success(tenant_id: str, db: Session) -> None:
    conn = db.execute(
        select(QBConnection).where(QBConnection.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if conn is None:
        return
    conn.last_sync_at = datetime.now(UTC)
    conn.last_error = None
    conn.updated_at = datetime.now(UTC)
    db.commit()


def _touch_sync_error(tenant_id: str, db: Session, exc: Exception) -> None:
    conn = db.execute(
        select(QBConnection).where(QBConnection.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if conn is None:
        return
    conn.error_count = int(conn.error_count or 0) + 1
    conn.last_error = str(exc)
    conn.updated_at = datetime.now(UTC)
    db.commit()


def _audit(db: Session, event_type: str, entity_id: str, payload: dict[str, Any]) -> None:
    log_audit_event_sync(
        db,
        tenant_id=payload.get("tenant_id", ""),
        user_id="system",
        action=event_type,
        entity_type="quickbooks",
        entity_id=entity_id,
        details=payload,
    )


# ---------------------------------------------------------------------------
# Pull operations (QB → GDX)
# ---------------------------------------------------------------------------

async def pull_customers(tenant_id: str, db: Session, qb: QBClient) -> dict[str, int]:
    """Pull all customers from QuickBooks into the local database.

    Per-row transactions via SAVEPOINTs so a single bad row (duplicate email,
    validation failure, etc.) doesn't roll back the whole sync. Errors are
    collected and returned in the result.
    """
    created = 0
    updated = 0
    adopted = 0  # existing local records linked to a QB id for the first time
    errors: list[dict[str, str]] = []
    seen_qb_ids: set[str] = set()
    try:
        rows = await qb.query("Customer")

        for raw in rows:
            qb_id = str(raw.get("Id") or "")
            # humanize_name fixes the QB-source data hygiene ("mike wendt"
            # → "Mike Wendt") without mangling acronyms or already-titled
            # rows. See gdx_dispatch/core/name_normalize.py.
            name = humanize_name(str(raw.get("DisplayName") or "").strip()) or ""
            if not qb_id or not name:
                continue
            seen_qb_ids.add(qb_id)
            email = ((raw.get("PrimaryEmailAddr") or {}).get("Address") or "").strip() or None
            phone = ((raw.get("PrimaryPhone") or {}).get("FreeFormNumber") or "").strip() or None

            try:
                with db.begin_nested():  # SAVEPOINT — one row failure stays local
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

                    # No QB map yet — try to adopt an existing local customer.
                    #
                    # Matching strategy (per Doug 2026-04-13, after a session
                    # where email-only matching failed to adopt ~250 field-service
                    # customers who had no email in QuickBooks):
                    #
                    #   1. Normalized NAME + last-4 of phone — high-confidence match
                    #   2. If multiple candidates match on (name + phone), compare
                    #      email to disambiguate
                    #   3. If still ambiguous → CREATE new + record a needs_review
                    #      flag on the new customer so a human can merge later
                    #
                    # Name is normalized: lowercased, collapsed whitespace,
                    # punctuation stripped. Phone normalized to digits-only, last 4.
                    adopted_hit = _adopt_existing_customer(
                        db, tenant_id=tenant_id, qb_name=name,
                        qb_email=email, qb_phone=phone,
                    )
                    if adopted_hit is not None:
                        adopted_customer, ambiguous = adopted_hit
                        adopted_customer.name = name  # QB version wins on displayable name
                        if email and not adopted_customer.email:
                            adopted_customer.email = email
                        if phone and not adopted_customer.phone:
                            adopted_customer.phone = phone
                        adopted_customer.source = adopted_customer.source or "quickbooks"
                        _upsert_map(tenant_id, "customer", str(adopted_customer.id), qb_id, db)
                        adopted += 1
                        continue

                    # No existing customer matched — create new.
                    customer = Customer(name=name, email=email, phone=phone, source="quickbooks", company_id=tenant_id)
                    db.add(customer)
                    db.flush()
                    _upsert_map(tenant_id, "customer", str(customer.id), qb_id, db)
                    created += 1
            except Exception as row_exc:
                log.exception("qb_pull_customers_row_failed qb_id=%s", qb_id)
                errors.append({"qb_id": qb_id, "name": name, "error": str(row_exc)[:200]})

        merged_remote = await _detect_qbo_merge_deletes(tenant_id, "customer", seen_qb_ids, db, qb)
        deleted = _apply_qbo_deletes(tenant_id, "customer", seen_qb_ids, db)
        db.commit()
        _touch_sync_success(tenant_id, db)
        _audit(db, "qb_pull_customers", tenant_id, {
            "tenant_id": tenant_id, "created": created, "updated": updated,
            "adopted": adopted, "merged_remote": merged_remote,
            "deleted": deleted, "errors": len(errors),
        })
        db.commit()
        return {"created": created, "updated": updated, "adopted": adopted,
                "merged_remote": merged_remote, "deleted": deleted, "errors": errors}
    except QBAPIError:
        db.rollback()
        raise
    except Exception as exc:
        log.exception("qb_pull_customers_failed tenant=%s", tenant_id)
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


def _extract_tax_amount(raw: dict[str, Any]) -> Decimal:
    """S122-2 (T3): pull total sales tax from a QBO Invoice payload.

    QBO stores invoice tax in ``TxnTaxDetail.TotalTax`` (top-level field on
    the Invoice resource). Pre-fix, ``tax_amount`` was hardcoded to 0 on
    every imported invoice and ``subtotal`` was set equal to ``total`` —
    so the tax line was silently dropped from GDX's AR rollups for every
    QB-imported invoice since 2026-04-13. Returns Decimal('0') when absent.
    """
    detail = raw.get("TxnTaxDetail") or {}
    total_tax = detail.get("TotalTax")
    if total_tax is None:
        return Decimal("0")
    try:
        return Decimal(str(total_tax))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def _idempotency_key(realm_id: str, entity_type: str, local_id: str, op: str) -> str:
    """S122-8 (C1): deterministic ``?requestid=`` for QBO mutations.

    Intuit's server-side dedup window is ~24h on the requestid value, so
    Celery retries on transient 5xx / socket timeouts no longer create twin
    entities. UUID5 namespace ensures that a re-shipped task (same
    realm/local-id/op) carries the SAME requestid — Intuit returns the
    previously-created entity instead of inserting a duplicate.
    """
    name = f"qb:{realm_id}:{entity_type}:{local_id}:{op}"
    return str(uuid5(NAMESPACE_URL, name))


def _extract_line_subtotal(raw: dict[str, Any]) -> Decimal:
    """S122-2 (T3, auditor catch round 2): sum the SalesItemLine amounts —
    do NOT compute ``subtotal = total - tax``. The naive subtract path is
    wrong when the invoice has DiscountLine, ShippingLine, SubTotalLine, or
    other non-item Line entries: their amounts contribute to ``TotalAmt``
    but aren't part of the product/service subtotal. Right answer: iterate
    Line[] and sum Amount where DetailType in {SalesItemLineDetail,
    ItemBasedExpenseLineDetail}. Falls back to ``TotalAmt - TotalTax`` only
    when no recognized item lines are present.
    """
    item_total = Decimal("0")
    found_item_line = False
    for line in raw.get("Line") or []:
        detail_type = str(line.get("DetailType") or "")
        if detail_type in {"SalesItemLineDetail", "ItemBasedExpenseLineDetail"}:
            found_item_line = True
            amount = line.get("Amount")
            if amount is None:
                continue
            try:
                item_total += Decimal(str(amount))
            except (TypeError, ValueError, ArithmeticError):
                continue
    if found_item_line:
        return item_total
    # No recognized item lines — back-derive from total minus tax. This
    # branch is wrong for invoices with DiscountLine but no SalesItemLine
    # (rare); the SubTotalLine fallback below is safer when present.
    total = Decimal(str(raw.get("TotalAmt") or 0))
    tax = _extract_tax_amount(raw)
    return total - tax


async def pull_invoices(tenant_id: str, db: Session, qb: QBClient) -> dict[str, int]:
    """Pull all invoices from QuickBooks.

    Handles three states per QB invoice:
      1. Already mapped → update existing local invoice
      2. Not mapped but local has same invoice_number → ADOPT (link map, update
         values). This is the fix for the 2026-04-13 prod crash where local had
         invoice #505316 and QB brought the same number, hitting the unique
         constraint on every retry.
      3. Net-new → create local invoice + lines

    Per-row SAVEPOINTs so one bad invoice doesn't roll back vendors/payments/etc.
    """
    _assert_money_pull_allowed(tenant_id, db, "invoice")
    created = 0
    updated = 0
    adopted = 0
    errors: list[dict[str, str]] = []
    seen_qb_ids: set[str] = set()
    try:
        rows = await qb.query("Invoice")

        for raw in rows:
            qb_id = str(raw.get("Id") or "")
            if not qb_id:
                continue
            seen_qb_ids.add(qb_id)

            doc_number = str(raw.get("DocNumber") or f"QB-{qb_id}")

            try:
                with db.begin_nested():
                    # Parse amounts inside the SAVEPOINT so a bad TotalAmt /
                    # Balance doesn't crash the whole sync.
                    # Note: Balance=0 means the invoice is fully paid. Using
                    # `raw.get("Balance") or total` would treat 0 as falsy and
                    # fall back to the full total — so a paid invoice would
                    # appear unpaid forever. Only fall back when the key is
                    # missing or None.
                    total = Decimal(str(raw.get("TotalAmt") or 0))
                    balance_raw = raw.get("Balance")
                    balance = Decimal(str(balance_raw)) if balance_raw is not None else total
                    tax_amount = _extract_tax_amount(raw)
                    subtotal = _extract_line_subtotal(raw)  # S122-2 (T3 round 2)

                    mapping = db.execute(
                        select(QBEntityMap).where(
                            QBEntityMap.tenant_id == tenant_id,
                            QBEntityMap.entity_type == "invoice",
                            QBEntityMap.qb_id == qb_id,
                        )
                    ).scalar_one_or_none()

                    if mapping is not None:
                        invoice = db.get(Invoice, UUID(mapping.local_id))
                        if invoice is None:
                            continue
                        invoice.subtotal = subtotal
                        invoice.tax_amount = tax_amount
                        invoice.total = total
                        invoice.balance_due = balance
                        invoice.status = "paid" if balance <= 0 else "sent"
                        # Pre-fix the update path only refreshed totals/status,
                        # so invoices imported before D99 (an earlier session) kept
                        # invoice_date/due_date/sent_at/paid_at = NULL forever
                        # and every period-filtered metric read $0. Refresh the
                        # date triple from QB on every sync — TxnDate is the
                        # business date, never created_at (S69 lesson).
                        txn_date_raw = raw.get("TxnDate")
                        if txn_date_raw:
                            try:
                                invoice.invoice_date = date.fromisoformat(str(txn_date_raw)[:10])
                            except (TypeError, ValueError):
                                pass
                        due_date_raw = raw.get("DueDate")
                        if due_date_raw:
                            try:
                                invoice.due_date = date.fromisoformat(str(due_date_raw)[:10])
                            except (TypeError, ValueError):
                                pass
                        is_paid_now = balance <= 0
                        if invoice.invoice_date and not invoice.sent_at:
                            invoice.sent_at = datetime.combine(
                                invoice.invoice_date, datetime.min.time(), tzinfo=UTC
                            )
                        if is_paid_now and not invoice.paid_at:
                            stamp = invoice.invoice_date or datetime.now(UTC).date()
                            invoice.paid_at = datetime.combine(
                                stamp, datetime.min.time(), tzinfo=UTC
                            )
                        # Re-sync line items from QB. Pre-fix the update branch
                        # only refreshed totals/dates, so QB-side line edits
                        # never propagated and a second sync_full lied about
                        # parity.
                        _resync_invoice_lines(invoice.id, raw.get("Line") or [], tenant_id, db)
                        updated += 1
                        continue

                    # Adoption path: local invoice with the same number already
                    # exists. Link it rather than attempting a duplicate insert.
                    existing = db.execute(
                        select(Invoice).where(
                            Invoice.company_id == tenant_id,
                            Invoice.invoice_number == doc_number,
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        existing.subtotal = subtotal
                        existing.tax_amount = tax_amount
                        existing.total = total
                        existing.balance_due = balance
                        existing.status = "paid" if balance <= 0 else "sent"
                        # Same date-refresh logic as the mapped-update path —
                        # adoption hits invoices that already existed locally
                        # but never got a TxnDate from QB.
                        txn_date_raw = raw.get("TxnDate")
                        if txn_date_raw:
                            try:
                                existing.invoice_date = date.fromisoformat(str(txn_date_raw)[:10])
                            except (TypeError, ValueError):
                                pass
                        due_date_raw = raw.get("DueDate")
                        if due_date_raw:
                            try:
                                existing.due_date = date.fromisoformat(str(due_date_raw)[:10])
                            except (TypeError, ValueError):
                                pass
                        if existing.invoice_date and not existing.sent_at:
                            existing.sent_at = datetime.combine(
                                existing.invoice_date, datetime.min.time(), tzinfo=UTC
                            )
                        if balance <= 0 and not existing.paid_at:
                            stamp = existing.invoice_date or datetime.now(UTC).date()
                            existing.paid_at = datetime.combine(
                                stamp, datetime.min.time(), tzinfo=UTC
                            )
                        # Adopt QB lines too. Pre-fix the adoption branch wrote
                        # totals + linked the map but skipped lines entirely,
                        # which is the documented root cause of 282 line-less
                        # invoices in GDX prod (~$615k, March 2026 mass-adoption).
                        _resync_invoice_lines(existing.id, raw.get("Line") or [], tenant_id, db)
                        _upsert_map(tenant_id, "invoice", str(existing.id), qb_id, db)
                        adopted += 1
                        continue

                    qb_customer_id = str((raw.get("CustomerRef") or {}).get("value") or "")
                    customer_map = None
                    if qb_customer_id:
                        customer_map = db.execute(
                            select(QBEntityMap).where(
                                QBEntityMap.tenant_id == tenant_id,
                                QBEntityMap.entity_type == "customer",
                                QBEntityMap.qb_id == qb_customer_id,
                            )
                        ).scalar_one_or_none()

                    if customer_map is not None:
                        customer = db.get(Customer, UUID(customer_map.local_id))
                        if customer is None:
                            continue
                    else:
                        customer = Customer(
                            name=f"QB Customer {qb_customer_id or 'Unknown'}",
                            source="quickbooks",
                            company_id=tenant_id,
                        )
                        db.add(customer)
                        db.flush()
                        if qb_customer_id:
                            _upsert_map(tenant_id, "customer", str(customer.id), qb_customer_id, db)

                    # 2026-05-04 — no synthetic job. Pre-fix code attached every
                    # imported invoice to a "QuickBooks Import" job per customer,
                    # which misattributed all imported revenue to fake jobs. QB
                    # invoices don't represent a GDX job workflow; ``job_id``
                    # stays NULL and ``customer_id`` carries the linkage.

                    # D99 (an earlier session): persist QB TxnDate as invoice_date and DueDate
                    # as due_date. Pre-fix QB-imported invoices landed with both
                    # null, so every period-filtered metric read $0 against real revenue.
                    txn_date_raw = raw.get("TxnDate")
                    invoice_date_value = None
                    if txn_date_raw:
                        try:
                            invoice_date_value = date.fromisoformat(str(txn_date_raw)[:10])
                        except (TypeError, ValueError):
                            invoice_date_value = None
                    due_date_raw = raw.get("DueDate")
                    due_date_value = None
                    if due_date_raw:
                        try:
                            due_date_value = date.fromisoformat(str(due_date_raw)[:10])
                        except (TypeError, ValueError):
                            due_date_value = None
                    # P1-6 fix 2026-04-27: stamp ``sent_at`` (always — QB invoices
                    # are necessarily ``sent`` at minimum) and ``paid_at`` (when
                    # balance=0). Best-available source is ``invoice_date`` (the
                    # QB TxnDate); QB doesn't expose a separate paid-date in the
                    # core Invoice entity, so this is the closest truth without
                    # joining the Payment list. Pre-fix imports left both null,
                    # breaking aging/payment-timing/customer-paid-on-time reports.
                    is_paid = balance <= 0
                    stamp_dt = (
                        datetime.combine(invoice_date_value, datetime.min.time(), tzinfo=UTC)
                        if invoice_date_value
                        else datetime.now(UTC)
                    )
                    invoice = Invoice(
                        job_id=None,
                        invoice_number=doc_number,
                        subtotal=subtotal,
                        tax_amount=tax_amount,
                        total=total,
                        balance_due=balance,
                        status="paid" if is_paid else "sent",
                        invoice_date=invoice_date_value,
                        due_date=due_date_value,
                        sent_at=stamp_dt,
                        paid_at=stamp_dt if is_paid else None,
                        public_token=f"qb-{qb_id}"[:64],
                        notes="Imported from QuickBooks",
                        customer_id=customer.id,
                        company_id=tenant_id,
                    )
                    db.add(invoice)
                    db.flush()

                    for sort_order, line in enumerate(raw.get("Line") or [], start=1):
                        amount = Decimal(str(line.get("Amount") or 0))
                        db.add(InvoiceLine(
                            invoice_id=invoice.id,
                            description=str(line.get("Description") or "QuickBooks line"),
                            quantity=1,
                            unit_price=amount,
                            line_total=amount,
                            sort_order=sort_order,
                            company_id=tenant_id,
                        ))

                    _upsert_map(tenant_id, "invoice", str(invoice.id), qb_id, db)
                    created += 1
            except Exception as row_exc:
                log.exception("qb_pull_invoices_row_failed qb_id=%s doc_number=%s", qb_id, doc_number)
                errors.append({
                    "qb_id": qb_id,
                    "invoice_number": doc_number,
                    "error": str(row_exc)[:200],
                })

        deleted = _apply_qbo_deletes(tenant_id, "invoice", seen_qb_ids, db)
        db.commit()
        _touch_sync_success(tenant_id, db)
        _audit(db, "qb_pull_invoices", tenant_id, {
            "tenant_id": tenant_id, "created": created, "updated": updated,
            "adopted": adopted, "deleted": deleted, "errors": len(errors),
        })
        db.commit()
        return {"created": created, "updated": updated, "adopted": adopted,
                "deleted": deleted, "errors": errors}
    except QBAPIError:
        db.rollback()
        raise
    except Exception as exc:
        log.exception("qb_pull_invoices_failed tenant=%s", tenant_id)
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


async def pull_items(tenant_id: str, db: Session, qb: QBClient) -> dict[str, int]:
    """Pull items (products/services) from QuickBooks into the catalog."""
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    try:
        rows = await qb.query("Item")
        catalog = _get_or_create_qb_catalog(db)

        for raw in rows:
            qb_id = str(raw.get("Id") or "")
            name = str(raw.get("Name") or "").strip()
            if not qb_id or not name:
                continue
            # QB Item entity has TWO money fields:
            #   UnitPrice    — what we charge the customer (sell price → price)
            #   PurchaseCost — what we paid for it (cost basis → cost)
            # Pre-fix code aliased both to UnitPrice, which made every margin
            # calculation read 0%. Sourcing them separately now.
            price = Decimal(str(raw.get("UnitPrice") or 0))
            cost_raw = raw.get("PurchaseCost")
            cost = Decimal(str(cost_raw)) if cost_raw is not None else Decimal("0")
            sku = str(raw.get("Sku") or qb_id).strip() or qb_id
            description = str(raw.get("Description") or "").strip() or None
            category = str(raw.get("Type") or "").strip() or None
            active = bool(raw.get("Active", True))

            try:
                with db.begin_nested():
                    item = db.execute(
                        select(CustomCatalogItem).where(
                            CustomCatalogItem.qb_item_id == qb_id,
                            CustomCatalogItem.deleted_at.is_(None),
                        )
                    ).scalar_one_or_none()
                    if item is None:
                        item = CustomCatalogItem(
                            catalog_id=catalog.id, sku=sku, name=name,
                            description=description, cost=cost, price=price,
                            category=category, active=active, qb_item_id=qb_id,
                        )
                        db.add(item)
                        created += 1
                    else:
                        item.name = name
                        item.description = description
                        item.price = price
                        item.cost = cost
                        item.sku = sku
                        item.active = active
                        item.category = category
                        updated += 1
            except Exception as row_exc:
                log.exception("qb_pull_items_row_failed qb_id=%s", qb_id)
                errors.append({"qb_id": qb_id, "name": name, "error": str(row_exc)[:200]})

        db.commit()
        _touch_sync_success(tenant_id, db)
        _audit(db, "qb_pull_items", tenant_id, {
            "tenant_id": tenant_id, "created": created, "updated": updated, "errors": len(errors),
        })
        db.commit()
        return {"created": created, "updated": updated, "errors": errors}
    except QBAPIError:
        db.rollback()
        raise
    except Exception as exc:
        log.exception("qb_pull_items_failed tenant=%s", tenant_id)
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


def _get_or_create_qb_catalog(db: Session) -> CustomCatalog:
    catalog = db.execute(
        select(CustomCatalog).where(CustomCatalog.source_system == "qb", CustomCatalog.deleted_at.is_(None))
    ).scalar_one_or_none()
    if catalog is not None:
        return catalog
    catalog = CustomCatalog(name="QuickBooks Catalog", source_system="qb")
    db.add(catalog)
    db.flush()
    return catalog


async def pull_vendors(tenant_id: str, db: Session, qb: QBClient) -> dict[str, int]:
    """Pull vendors from QuickBooks."""
    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    try:
        rows = await qb.query("Vendor")

        for raw in rows:
            qb_vendor_id = str(raw.get("Id") or "")
            name = str(raw.get("DisplayName") or raw.get("CompanyName") or "").strip()
            if not qb_vendor_id or not name:
                continue
            email = ((raw.get("PrimaryEmailAddr") or {}).get("Address") or "").strip() or None
            phone = ((raw.get("PrimaryPhone") or {}).get("FreeFormNumber") or "").strip() or None

            try:
                with db.begin_nested():
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
            except Exception as row_exc:
                log.exception("qb_pull_vendors_row_failed qb_id=%s", qb_vendor_id)
                errors.append({"qb_id": qb_vendor_id, "name": name, "error": str(row_exc)[:200]})

        db.commit()
        _touch_sync_success(tenant_id, db)
        _audit(db, "qb_pull_vendors", tenant_id, {
            "tenant_id": tenant_id, "created": created, "updated": updated, "errors": len(errors),
        })
        db.commit()
        return {"created": created, "updated": updated, "errors": errors}
    except QBAPIError:
        db.rollback()
        raise
    except Exception as exc:
        log.exception("qb_pull_vendors_failed tenant=%s", tenant_id)
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


async def pull_payments(tenant_id: str, db: Session, qb: QBClient) -> dict[str, int]:
    """Pull payments from QuickBooks and link to local invoices."""
    _assert_money_pull_allowed(tenant_id, db, "payment")
    created = 0
    updated = 0
    skipped = 0  # payments with no linkable local invoice
    errors: list[dict[str, str]] = []
    seen_qb_ids: set[str] = set()
    try:
        rows = await qb.query("Payment")

        for raw in rows:
            qb_id = str(raw.get("Id") or "")
            if not qb_id:
                continue
            seen_qb_ids.add(qb_id)

            try:
                with db.begin_nested():
                    # QB TxnDate is the real cash-receipt date. Pre-fix code stamped
                    # ``date.today()`` on every imported payment, so 263 prod rows
                    # all carried 2026-04-13 (the day sync first ran) — every
                    # cash-flow / aging / commission report that bucketed by
                    # payment_date was wrong by months. Always source from QB.
                    qb_amount = Decimal(str(raw.get("TotalAmt") or 0))
                    qb_payment_date = _parse_qb_date(raw.get("TxnDate")) or date.today()

                    payment_map = db.execute(
                        select(QBEntityMap).where(
                            QBEntityMap.tenant_id == tenant_id,
                            QBEntityMap.entity_type == "payment",
                            QBEntityMap.qb_id == qb_id,
                        )
                    ).scalar_one_or_none()
                    if payment_map is not None:
                        # Update branch was a no-op counter pre-fix — incremented
                        # ``updated`` without touching the row, so QB-side edits
                        # never propagated and a second sync_full lied about how
                        # much it had reconciled. Update only when something
                        # actually changed so the counter remains truthful.
                        existing_payment = db.get(Payment, UUID(payment_map.local_id))
                        if existing_payment is not None:
                            changed = False
                            if existing_payment.amount != qb_amount:
                                existing_payment.amount = qb_amount
                                changed = True
                            if existing_payment.payment_date != qb_payment_date:
                                existing_payment.payment_date = qb_payment_date
                                changed = True
                            if changed:
                                payment_map.synced_at = datetime.now(UTC)
                                updated += 1
                        continue

                    linked_qb_invoice_id = ""
                    for line in raw.get("Line") or []:
                        for txn in line.get("LinkedTxn") or []:
                            if str(txn.get("TxnType") or "").lower() == "invoice" and txn.get("TxnId"):
                                linked_qb_invoice_id = str(txn["TxnId"])
                                break
                        if linked_qb_invoice_id:
                            break
                    if not linked_qb_invoice_id:
                        skipped += 1
                        continue

                    invoice_map = db.execute(
                        select(QBEntityMap).where(
                            QBEntityMap.tenant_id == tenant_id,
                            QBEntityMap.entity_type == "invoice",
                            QBEntityMap.qb_id == linked_qb_invoice_id,
                        )
                    ).scalar_one_or_none()
                    if invoice_map is None:
                        # Linked invoice hasn't been synced yet — this happens
                        # on partial syncs. Not an error, just skip.
                        skipped += 1
                        continue
                    invoice = db.get(Invoice, UUID(invoice_map.local_id))
                    if invoice is None:
                        skipped += 1
                        continue

                    payment = Payment(
                        invoice_id=invoice.id,
                        amount=qb_amount,
                        method="quickbooks",
                        payment_date=qb_payment_date,
                        company_id=tenant_id,
                    )
                    db.add(payment)
                    db.flush()
                    _upsert_map(tenant_id, "payment", str(payment.id), qb_id, db)
                    # Sprint 1.0.6 — keep customer rolling-volume cache fresh
                    if invoice.customer_id:
                        try:
                            from gdx_dispatch.services.customer_rolling_volume import refresh_cached_volume
                            refresh_cached_volume(invoice.customer_id, db)
                        except Exception:
                            log.exception("rolling_volume_refresh_failed_qb_pull")
                    created += 1
            except Exception as row_exc:
                log.exception("qb_pull_payments_row_failed qb_id=%s", qb_id)
                errors.append({"qb_id": qb_id, "error": str(row_exc)[:200]})

        deleted = _apply_qbo_deletes(tenant_id, "payment", seen_qb_ids, db)
        db.commit()
        _touch_sync_success(tenant_id, db)
        _audit(db, "qb_pull_payments", tenant_id, {
            "tenant_id": tenant_id, "created": created, "updated": updated,
            "skipped": skipped, "deleted": deleted, "errors": len(errors),
        })
        db.commit()
        return {"created": created, "updated": updated, "skipped": skipped,
                "deleted": deleted, "errors": errors}
    except QBAPIError:
        db.rollback()
        raise
    except Exception as exc:
        log.exception("qb_pull_payments_failed tenant=%s", tenant_id)
        db.rollback()
        _touch_sync_error(tenant_id, db, exc)
        raise


# ---------------------------------------------------------------------------
# Push operations (GDX → QB)
# ---------------------------------------------------------------------------

async def push_customer(tenant_id: str, customer_id: str, db: Session, qb: QBClient) -> dict[str, str]:
    """Push a local customer to QuickBooks.

    S122-8 auditor catch round 2:
      1. Short-circuit if the customer is already mapped — saves a round-trip
         AND makes the function tolerant of Intuit's dedup-replay response
         shape (which may differ from a fresh-create body and parse to an
         empty Id, leaving the map unwritten and locking the retry loop).
      2. Raise on empty Id after create — the previous code silently
         committed an empty audit log + no map row, which is exactly the
         failure mode the auditor named.
    """
    customer = db.get(Customer, UUID(customer_id))
    if customer is None:
        raise QBSyncError("Customer not found")

    existing_map = db.execute(
        select(QBEntityMap).where(
            QBEntityMap.tenant_id == tenant_id,
            QBEntityMap.entity_type == "customer",
            QBEntityMap.local_id == customer_id,
        )
    ).scalar_one_or_none()
    if existing_map is not None:
        return {"customer_id": customer_id, "qb_customer_id": existing_map.qb_id,
                "already_mapped": "true"}

    payload: dict[str, Any] = {"DisplayName": customer.name}
    if customer.email:
        payload["PrimaryEmailAddr"] = {"Address": customer.email}
    if customer.phone:
        payload["PrimaryPhone"] = {"FreeFormNumber": customer.phone}

    resp = await qb.create(
        "Customer", payload,
        idempotency_key=_idempotency_key(qb.realm_id, "customer", customer_id, "create"),
    )
    qb_customer_id = str(resp.get("Id") or "")
    if not qb_customer_id:
        raise QBSyncError(
            f"push_customer: empty Id in QBO response for customer {customer_id}. "
            f"Response keys: {sorted(resp.keys())}"
        )
    _upsert_map(tenant_id, "customer", customer_id, qb_customer_id, db)
    # S122-17: clear the dirty flag after successful push. The Customer
    # before_update listener won't re-flip because we only touch qb_dirty +
    # qb_synced_at (both in the internal-cols allowlist).
    customer.qb_dirty = False
    customer.qb_synced_at = datetime.now(UTC)
    db.commit()

    _audit(db, "qb_push_customer", customer_id, {
        "tenant_id": tenant_id, "qb_customer_id": qb_customer_id,
    })
    db.commit()
    return {"customer_id": customer_id, "qb_customer_id": qb_customer_id}


async def push_invoice(tenant_id: str, invoice_id: str, db: Session, qb: QBClient) -> dict[str, str]:
    """Push a local invoice to QuickBooks.

    S122-8 auditor catch round 2: short-circuit on existing map; raise on
    empty Id after create. Same rationale as push_customer.
    """
    invoice = db.get(Invoice, UUID(invoice_id))
    if invoice is None:
        raise QBSyncError("Invoice not found")

    existing_map = db.execute(
        select(QBEntityMap).where(
            QBEntityMap.tenant_id == tenant_id,
            QBEntityMap.entity_type == "invoice",
            QBEntityMap.local_id == invoice_id,
        )
    ).scalar_one_or_none()
    if existing_map is not None:
        return {"invoice_id": invoice_id, "qb_invoice_id": existing_map.qb_id,
                "already_mapped": "true"}

    # Resolve the customer for the QB CustomerRef. job_id is now optional
    # (Slice 2): QB-imported invoices stand on their own with customer_id and
    # no job. Prefer the invoice's direct customer_id; fall back to the linked
    # job's customer for legacy invoices that only carry the job linkage.
    resolved_customer_id: UUID | None = invoice.customer_id
    if resolved_customer_id is None and invoice.job_id is not None:
        job = db.get(Job, invoice.job_id)
        if job is not None:
            resolved_customer_id = job.customer_id

    customer_ref: dict[str, str] | None = None
    if resolved_customer_id is not None:
        customer_map = db.execute(
            select(QBEntityMap).where(
                QBEntityMap.tenant_id == tenant_id,
                QBEntityMap.entity_type == "customer",
                QBEntityMap.local_id == str(resolved_customer_id),
            )
        ).scalar_one_or_none()
        if customer_map is not None:
            customer_ref = {"value": customer_map.qb_id}

    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order.asc())
    ).scalars().all()
    qb_lines = [
        {
            "Amount": float(line.line_total),
            "DetailType": "SalesItemLineDetail",
            "Description": line.description,
            "SalesItemLineDetail": {
                "Qty": float(line.quantity),
                "UnitPrice": float(line.unit_price),
            },
        }
        for line in lines
    ]

    payload: dict[str, Any] = {"DocNumber": invoice.invoice_number, "Line": qb_lines}
    if customer_ref is not None:
        payload["CustomerRef"] = customer_ref
    # Sprint customer-multi-location (2026-05-21) — when the linked job
    # carries a location_id, we OWN the QBO PrivateNote field. GDX writes
    # `[label — address]` plus any local Invoice.notes body so QB
    # statements can be eyeballed by site (Doug decision-lock: one GDX
    # customer ↔ one QB customer; site differentiation lives in the memo,
    # not in sub-customers).
    #
    # Idempotency: same invoice + same location row → identical payload
    # PrivateNote on every push. The function never reads the existing
    # PrivateNote from QBO — so a manual edit by a QBO admin is clobbered
    # on the next push. That's intentional: GDX is the source of truth
    # for this field once a GDX-side location exists. Documented in
    # /audit critique 2026-05-21.
    if invoice.job_id is not None:
        loc_row = db.execute(
            text(
                "SELECT cl.label, cl.address "
                "FROM jobs j "
                "JOIN customer_locations cl ON cl.id = j.location_id "
                "WHERE j.id = :jid AND cl.deleted_at IS NULL"
            ),
            {"jid": str(invoice.job_id)},
        ).first()
        if loc_row:
            label, address = loc_row[0], loc_row[1]
            inside = " — ".join(p for p in (label, address) if p)
            if inside:
                prefix = f"[{inside}]"
                body = (invoice.notes or "").strip()
                # Strip a pre-existing identical prefix so re-runs don't
                # double-bracket. Defends against the QBO admin who copied
                # the memo from a prior push into invoice.notes locally.
                if body.startswith(prefix):
                    body = body[len(prefix):].lstrip()
                payload["PrivateNote"] = f"{prefix} {body}".strip() if body else prefix
    # S122-2 (T3 push side): if the local invoice carries tax, send it as
    # TxnTaxDetail.TotalTax. NOTE (auditor catch round 2): on QBO realms with
    # Automated Sales Tax (AST) enabled — essentially every post-2018 US
    # account — Intuit recomputes tax server-side from the customer's
    # ShipAddr/BillAddr + each line's TaxCodeRef, and TotalTax is treated as
    # a hint, not authoritative. The proper push for AST realms uses
    # per-line TaxCodeRef + TxnTaxDetail.OverrideDeltaAmount when an explicit
    # override is required. This Phase-1 fix carries the hint forward to
    # avoid losing the value on classic-tax realms; full AST compatibility
    # is filed as D-S122-ast-tax-push (out of Phase 1 scope).
    if invoice.tax_amount and invoice.tax_amount > 0:
        payload["TxnTaxDetail"] = {"TotalTax": float(invoice.tax_amount)}

    resp = await qb.create(
        "Invoice", payload,
        idempotency_key=_idempotency_key(qb.realm_id, "invoice", invoice_id, "create"),
    )
    qb_invoice_id = str(resp.get("Id") or "")
    if not qb_invoice_id:
        raise QBSyncError(
            f"push_invoice: empty Id in QBO response for invoice {invoice_id}. "
            f"Response keys: {sorted(resp.keys())}"
        )
    _upsert_map(tenant_id, "invoice", invoice_id, qb_invoice_id, db)

    # S122-14: clear the dirty flag after successful push so the next full
    # sync skips this invoice unless it changes again. The Invoice
    # before_update listener won't re-flip it because we're only touching
    # qb_dirty + qb_synced_at (both in the internal-cols set).
    invoice.qb_dirty = False
    invoice.qb_synced_at = datetime.now(UTC)
    db.commit()

    _audit(db, "qb_push_invoice", invoice_id, {
        "tenant_id": tenant_id, "qb_invoice_id": qb_invoice_id,
    })
    db.commit()
    return {"invoice_id": invoice_id, "qb_invoice_id": qb_invoice_id}


def _default_expense_account_qb_id(tenant_id: str, db: Session) -> str:
    """S122-19: look up an active Expense / COGS account from the local copy
    of the realm's Chart of Accounts (populated by ``pull_accounts``).

    Pre-fix ``push_expense`` hardcoded ``AccountRef.value = "1"`` for every
    line — accepted by Intuit when "1" happens to be a usable Expense
    account in the realm, but on realms where Account 1 is something else
    (Income, Bank, COGS, Asset) the Purchase create either fails outright
    or posts the expense to the wrong account silently.

    Strategy: prefer ``account_type='Expense'``, fall back to
    ``account_type='CostOfGoodsSold'``. Both are valid AccountRef targets
    for ``AccountBasedExpenseLineDetail`` per the Intuit Item/Purchase API
    docs.

    Raises QBSyncError when no usable account exists — the caller surfaces
    this to the tenant ("connect QB then run a full sync first to populate
    the chart of accounts").
    """
    try:
        row = db.execute(text("""
            SELECT qb_account_id, account_type FROM qb_accounts
            WHERE tenant_id = :tid AND active = TRUE
              AND account_type IN ('Expense', 'CostOfGoodsSold')
            ORDER BY (account_type = 'Expense') DESC, synced_at DESC
            LIMIT 1
        """), {"tid": tenant_id}).first()
    except Exception as exc:
        # qb_accounts may not exist yet on tenants that have never run
        # pull_accounts. The DDL is created on first pull; until then,
        # surface a clean error instead of a raw OperationalError.
        db.rollback()
        raise QBSyncError(
            f"S122-19: qb_accounts table not yet populated for tenant {tenant_id}. "
            "Run pull_accounts (Settings → Sync Now) to refresh the local "
            "Chart of Accounts cache, then retry."
        ) from exc
    if row is None:
        raise QBSyncError(
            f"S122-19: no active Expense / COGS account in QB Chart of Accounts "
            f"for tenant {tenant_id}. Run pull_accounts (Settings → Sync Now) "
            "to refresh the local cache, then retry."
        )
    return str(row[0])


async def push_expense(tenant_id: str, expense_id: str, db: Session, qb: QBClient) -> dict[str, str]:
    """Push a local expense to QuickBooks as a Purchase.

    S122-8 auditor catch round 2: short-circuit on existing map; raise on
    empty Id after create. Same rationale as push_customer.
    """
    expense = db.get(Expense, UUID(expense_id))
    if expense is None:
        raise QBSyncError("Expense not found")

    existing_map = db.execute(
        select(QBEntityMap).where(
            QBEntityMap.tenant_id == tenant_id,
            QBEntityMap.entity_type == "expense",
            QBEntityMap.local_id == expense_id,
        )
    ).scalar_one_or_none()
    if existing_map is not None:
        return {"expense_id": expense_id, "qb_purchase_id": existing_map.qb_id,
                "already_mapped": "true"}

    expense_lines = db.execute(
        select(ExpenseLine).where(ExpenseLine.expense_id == expense.id)
    ).scalars().all()

    # S122-19: replace the hardcoded "1" with a real lookup from the realm's
    # Chart of Accounts. Cached in qb_accounts via pull_accounts.
    account_ref_value = _default_expense_account_qb_id(tenant_id, db)

    line_payload = []
    if expense_lines:
        for line in expense_lines:
            line_payload.append({
                "Amount": float(line.amount),
                "Description": line.description or expense.description or "Expense",
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_ref_value}},
            })
    else:
        line_payload.append({
            "Amount": float(expense.amount),
            "Description": expense.description or "Expense",
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_ref_value}},
        })

    payload = {
        "TotalAmt": float(expense.amount),
        "PaymentType": "Cash",
        "Line": line_payload,
    }

    resp = await qb.create(
        "Purchase", payload,
        idempotency_key=_idempotency_key(qb.realm_id, "expense", expense_id, "create"),
    )
    qb_purchase_id = str(resp.get("Id") or "")
    if not qb_purchase_id:
        raise QBSyncError(
            f"push_expense: empty Id in QBO response for expense {expense_id}. "
            f"Response keys: {sorted(resp.keys())}"
        )
    _upsert_map(tenant_id, "expense", expense_id, qb_purchase_id, db)
    db.commit()

    _audit(db, "qb_push_expense", expense_id, {
        "tenant_id": tenant_id, "qb_purchase_id": qb_purchase_id,
    })
    db.commit()
    return {"expense_id": expense_id, "qb_purchase_id": qb_purchase_id}


# ---------------------------------------------------------------------------
# Chart of Accounts (Slice 3B — ported from gdx_dispatch/core/quickbooks.py to the
# modular HTTP-only stack 2026-05-04)
# ---------------------------------------------------------------------------

# Build Rule #1 (SQL portability): the test fixture is SQLite; prod is
# Postgres. `DEFAULT now()` is PG-only; `DEFAULT 1` works in SQLite but PG
# rejects an integer literal as a BOOLEAN default ("DatatypeMismatch — column
# 'active' is of type boolean but default expression is of type integer",
# caught on prod 2026-05-05). `DEFAULT TRUE` and `DEFAULT CURRENT_TIMESTAMP`
# are SQL-standard and accepted by both backends. UUID/TIMESTAMP are
# SQLite-permissive (treated as TEXT).
_QB_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS qb_accounts (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    qb_account_id VARCHAR(120) NOT NULL,
    name VARCHAR(300) NOT NULL,
    account_type VARCHAR(100),
    account_sub_type VARCHAR(100),
    classification VARCHAR(100),
    current_balance NUMERIC(14,2),
    active BOOLEAN DEFAULT TRUE,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tenant_id, qb_account_id)
)
"""

# Fresh-tenant DDL. Legacy production tables still carry tenant_id +
# UNIQUE(tenant_id, qb_txn_id); the additive migration in
# `_ensure_bank_tx_schema` below brings them forward without dropping data:
# adds deleted_at, makes tenant_id nullable, swaps the legacy unique
# constraint for UNIQUE(qb_txn_id). The legacy column itself stays for one
# release cycle (dropping it is a separate Phase 2 sprint, gated).
#
# This DDL is only the create-from-empty path for brand-new tenants; the
# real source of truth for fresh DBs is the QBBankTransaction ORM model
# in banking.py, which create_all() picks up.
_QB_BANK_TX_DDL = """
CREATE TABLE IF NOT EXISTS qb_bank_transactions (
    id UUID PRIMARY KEY,
    qb_txn_id VARCHAR(120) NOT NULL UNIQUE,
    txn_date DATE,
    txn_type VARCHAR(50),
    account_name VARCHAR(300),
    payee VARCHAR(300),
    amount NUMERIC(14,2),
    memo TEXT,
    category VARCHAR(300),
    status VARCHAR(50),
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP NULL
)
"""

# Additive migration for legacy tenants. Idempotent — runs on every
# pull_bank_transactions call until the column/constraint state matches
# the target. PG-only (SQLite test fixtures create tables fresh).
_QB_BANK_TX_MIGRATE = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'qb_bank_transactions'
    ) THEN
        -- 1. Make tenant_id nullable so writes can omit it. We do NOT drop
        --    the column yet — burn-in window before Phase 2 drop.
        --    Narrow exception: only swallow `undefined_column` (already gone).
        --    Real failures (lock contention, permission denied) propagate up
        --    so the surrounding try/except in _ensure_bank_tx_schema logs them.
        BEGIN
            ALTER TABLE qb_bank_transactions ALTER COLUMN tenant_id DROP NOT NULL;
        EXCEPTION WHEN undefined_column THEN NULL;
        END;

        -- 2. Add deleted_at for Purchase tombstoning (matches the
        --    qb_deposits/qb_transfers pattern from S119).
        ALTER TABLE qb_bank_transactions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL;

        -- 3. Replace UNIQUE(tenant_id, qb_txn_id) with UNIQUE(qb_txn_id).
        --    On tenant-plane the connection IS the tenant; qb_txn_id is
        --    globally unique within one tenant's QBO realm.
        BEGIN
            ALTER TABLE qb_bank_transactions
                DROP CONSTRAINT qb_bank_transactions_tenant_id_qb_txn_id_key;
        EXCEPTION WHEN undefined_object THEN NULL;
        END;
        CREATE UNIQUE INDEX IF NOT EXISTS qb_bank_transactions_qb_txn_id_key
            ON qb_bank_transactions (qb_txn_id);
    END IF;
END $$;
"""


def _ensure_bank_tx_schema(db: Session) -> None:
    """Bring legacy tenant DBs forward to the post-tenant_id schema. No-op
    on SQLite (tests create the table fresh from the ORM); no-op on already-
    migrated PG."""
    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect != "postgresql":
        return
    try:
        db.execute(text(_QB_BANK_TX_MIGRATE))
        db.commit()
    except Exception:
        # Migration is best-effort — if it fails we still write to the
        # legacy columns. The sync will surface the underlying error via
        # the per-row exception handler.
        log.exception("qb_bank_transactions_migration_failed")
        db.rollback()


def _reconcile_bank_tx_tombstones(
    db: Session, seen_qb_ids: set[str], start_date: str, end_date: str,
) -> int:
    """Tombstone Purchase rows in the synced window whose qb_txn_id wasn't
    returned by QBO. Same approach as banking._reconcile_tombstones —
    QBO query() doesn't return deleted Purchases, so absence is the signal.

    SAFETY GATES (audit follow-up — found pre-merge):
      1. Empty seen_qb_ids → return 0 unconditionally. A pull that returned
         zero rows might be a real "everything was deleted in QBO" event,
         but it's WAY more likely to be a transient API failure, quota
         throttle, or empty response that already errored elsewhere. We
         refuse to nuke every live row on absence. If a real bulk-delete
         happens in QBO, the next non-empty sync will tombstone what's
         missing relative to what came back.
      2. Empty date window AND empty seen_qb_ids would compound (1) into
         "tombstone everything ever synced." Guarded by (1).
    """
    if not seen_qb_ids:
        return 0
    params: dict[str, Any] = {}
    where = ["deleted_at IS NULL"]
    from sqlalchemy import bindparam
    where.append("qb_txn_id NOT IN :seen")
    params["seen"] = list(seen_qb_ids)
    if start_date:
        where.append("(txn_date >= :sd OR txn_date IS NULL)")
        params["sd"] = start_date
    if end_date:
        where.append("(txn_date <= :ed OR txn_date IS NULL)")
        params["ed"] = end_date
    sql = (
        "UPDATE qb_bank_transactions "
        "SET deleted_at = CURRENT_TIMESTAMP "
        "WHERE " + " AND ".join(where)
    )
    stmt = text(sql).bindparams(bindparam("seen", expanding=True))
    try:
        result = db.execute(stmt, params)
        db.commit()
        return int(result.rowcount or 0)
    except (ProgrammingError, OperationalError):
        # deleted_at column doesn't exist on this tenant DB (migration
        # blocked or running first time on SQLite test). No-op.
        db.rollback()
        return 0


async def pull_accounts(tenant_id: str, db: Session, qb: QBClient) -> dict[str, Any]:
    """Pull Chart of Accounts from QBO into qb_accounts table.

    Modular HTTP-only port of gdx_dispatch/core/quickbooks.pull_accounts. Uses
    qb.query("Account") instead of the SDK's Account.filter.

    Queries both active and inactive accounts so merges/deletes in QBO
    flow through as `active=false` updates here; without the explicit
    `Active in (true,false)`, QBO defaults to active-only and stale rows
    for merged-source accounts linger forever (2026-05-25 GDX duplicate-
    bank-account cleanup found 173/174 still showing balances after a
    QBO merge because the upsert never saw them again).
    """
    db.execute(text(_QB_ACCOUNTS_DDL))
    db.commit()

    created = 0
    updated = 0
    errors: list[dict[str, str]] = []

    try:
        rows = await qb.query("Account", where="Active in (true, false)", max_results=500)
    except QBAPIError:
        db.rollback()
        raise
    except Exception as exc:
        log.exception("qb_pull_accounts_query_failed tenant=%s", tenant_id)
        _touch_sync_error(tenant_id, db, exc)
        raise

    for raw in rows:
        qb_id = str(raw.get("Id") or "")
        if not qb_id:
            continue
        name = str(raw.get("Name") or "")
        acct_type = str(raw.get("AccountType") or "")
        sub_type = str(raw.get("AccountSubType") or "")
        classification = str(raw.get("Classification") or "")
        balance = float(raw.get("CurrentBalance") or 0)
        active = bool(raw.get("Active", True))

        try:
            existing = db.execute(text(
                "SELECT id FROM qb_accounts WHERE tenant_id = :tid AND qb_account_id = :qid"
            ), {"tid": tenant_id, "qid": qb_id}).scalar()
            if existing:
                db.execute(text("""
                    UPDATE qb_accounts SET name = :name, account_type = :at, account_sub_type = :ast,
                        classification = :cls, current_balance = :bal, active = :act, synced_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = :tid AND qb_account_id = :qid
                """), {"name": name, "at": acct_type, "ast": sub_type, "cls": classification,
                       "bal": balance, "act": active, "tid": tenant_id, "qid": qb_id})
                updated += 1
            else:
                db.execute(text("""
                    INSERT INTO qb_accounts (id, tenant_id, qb_account_id, name, account_type, account_sub_type,
                        classification, current_balance, active)
                    VALUES (:id, :tid, :qid, :name, :at, :ast, :cls, :bal, :act)
                """), {"id": str(uuid4()), "tid": tenant_id, "qid": qb_id, "name": name,
                       "at": acct_type, "ast": sub_type, "cls": classification, "bal": balance, "act": active})
                created += 1
        except Exception as row_exc:
            log.exception("qb_pull_accounts_row_failed qb_id=%s", qb_id)
            errors.append({"qb_id": qb_id, "name": name, "error": str(row_exc)[:200]})

    db.commit()
    _touch_sync_success(tenant_id, db)
    _audit(db, "qb_pull_accounts", tenant_id, {
        "tenant_id": tenant_id, "created": created, "updated": updated, "errors": len(errors),
    })
    db.commit()
    return {"created": created, "updated": updated, "errors": errors}


async def pull_bank_transactions(
    tenant_id: str,
    db: Session,
    qb: QBClient,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """Pull bank/credit-card transactions (Purchase entity) from QBO.

    2026-05-20 audit follow-up: dropped tenant_id reads/writes (tenant
    isolation is the connection itself per CLAUDE.md), added deleted_at
    column + reconciler so deleted Purchases tombstone correctly.
    The additive _ensure_bank_tx_schema runs first to bring legacy
    tenant DBs forward; new tenants get the modern shape via
    QBBankTransaction.create_all().
    """
    # Legacy DDL (CREATE IF NOT EXISTS) for tenants on the very-old path.
    # New tenants come in via QBBankTransaction.metadata; this DDL is a
    # no-op there.
    db.execute(text(_QB_BANK_TX_DDL))
    db.commit()
    # Then promote legacy schemas forward: drop NOT NULL on tenant_id,
    # add deleted_at, swap the unique constraint.
    _ensure_bank_tx_schema(db)

    where_parts: list[str] = []
    if start_date:
        where_parts.append(f"TxnDate >= '{start_date}'")
    if end_date:
        where_parts.append(f"TxnDate <= '{end_date}'")
    where = " AND ".join(where_parts)

    created = 0
    updated = 0
    errors: list[dict[str, str]] = []
    seen_qb_ids: set[str] = set()

    try:
        rows = await qb.query("Purchase", where=where, max_results=500)
    except QBAPIError:
        db.rollback()
        raise
    except Exception as exc:
        log.exception("qb_pull_bank_transactions_query_failed tenant=%s", tenant_id)
        _touch_sync_error(tenant_id, db, exc)
        raise

    for raw in rows:
        qb_id = str(raw.get("Id") or "")
        if not qb_id:
            continue
        seen_qb_ids.add(qb_id)
        txn_date = _parse_qb_date(raw.get("TxnDate"))
        total = float(raw.get("TotalAmt") or 0)
        payment_type = str(raw.get("PaymentType") or "")
        memo = str(raw.get("PrivateNote") or "")
        entity_ref = raw.get("EntityRef") or {}
        payee = str(entity_ref.get("name") or "") if isinstance(entity_ref, dict) else ""
        account_ref = raw.get("AccountRef") or {}
        account_name = str(account_ref.get("name") or "") if isinstance(account_ref, dict) else ""

        try:
            existing = db.execute(text(
                "SELECT id FROM qb_bank_transactions WHERE qb_txn_id = :qid"
            ), {"qid": qb_id}).scalar()
            if existing:
                # Clear deleted_at on re-sync — same inverse-operation logic
                # as qb_deposits/qb_transfers. A row that QBO returns again
                # un-tombstones itself.
                db.execute(text("""
                    UPDATE qb_bank_transactions SET txn_date = :dt, amount = :amt, payee = :payee,
                        account_name = :acct, memo = :memo, txn_type = :tt,
                        synced_at = CURRENT_TIMESTAMP, deleted_at = NULL
                    WHERE qb_txn_id = :qid
                """), {"dt": txn_date, "amt": total, "payee": payee, "acct": account_name,
                       "memo": memo, "tt": payment_type, "qid": qb_id})
                updated += 1
            else:
                db.execute(text("""
                    INSERT INTO qb_bank_transactions (id, qb_txn_id, txn_date, txn_type,
                        account_name, payee, amount, memo)
                    VALUES (:id, :qid, :dt, :tt, :acct, :payee, :amt, :memo)
                """), {"id": str(uuid4()), "qid": qb_id, "dt": txn_date,
                       "tt": payment_type, "acct": account_name, "payee": payee, "amt": total, "memo": memo})
                created += 1
        except Exception as row_exc:
            log.exception("qb_pull_bank_transactions_row_failed qb_id=%s", qb_id)
            errors.append({"qb_id": qb_id, "error": str(row_exc)[:200]})

    db.commit()
    deleted = _reconcile_bank_tx_tombstones(db, seen_qb_ids, start_date, end_date)
    _touch_sync_success(tenant_id, db)
    _audit(db, "qb_pull_bank_transactions", tenant_id, {
        "tenant_id": tenant_id, "created": created, "updated": updated, "deleted": deleted, "errors": len(errors),
    })
    db.commit()
    return {"created": created, "updated": updated, "deleted": deleted, "errors": errors}
