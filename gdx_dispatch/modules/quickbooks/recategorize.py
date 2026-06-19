"""Recategorize miscategorized QBO transactions.

Sprint fix-in-quickbooks (2026-05-25). Two pieces:

1. **suggest_target_account**: heuristic-driven category proposer. Given a
   transaction's memo + vendor + amount + type, picks the most likely
   target account from the tenant's QB chart of accounts. Returns the
   suggestion plus a `reason` string so the UI can show the why.

2. **recategorize_transaction**: actually performs the QBO API update —
   GET the entity, find the right line item, swap the AccountRef, PUT
   back with the entity's SyncToken for optimistic-concurrency safety.

Per CLAUDE.md AI access safety (Yellow tier): GDX proposes, UI confirms,
THEN this module writes. No auto-apply.

Supports two QBO entity types in v1:
- **Deposit** — line-level `DepositLineDetail.AccountRef`
- **Purchase** — line-level `AccountBasedExpenseLineDetail.AccountRef`

Out of scope (returns a "open_in_qb" suggestion instead):
- Bank-to-bank transfers (need delete + create, too risky for automation)
- JournalEntry (multi-line paired debits/credits)
- SalesReceipt / Payment / RefundReceipt
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from gdx_dispatch.modules.quickbooks.client import QBClient

log = logging.getLogger(__name__)


# ---- Heuristic rule table ----------------------------------------------------
# Each rule: (regex pattern, target account_type fragment, reason).
# Patterns match against the COMBINED memo + vendor_name string, lowercased.
# Order matters — first match wins. Transfer patterns are checked first so a
# bank-to-bank entry isn't accidentally classified as Income.

_TRANSFER_PATTERNS = (
    re.compile(r"transfer\s+(from|to)\s+x?\d+"),
    re.compile(r"\bxfer\b"),
    re.compile(r"bank\s+transfer"),
    re.compile(r"intra[\- ]?bank"),
)

_KEYWORD_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # Customer payment batches posted to wrong account (the big -$59K case in GDX).
    (re.compile(r"deposit/credit|customer\s+payment|merchant\s+deposit|ach\s+credit"),
     "Income", "memo looks like a customer payment batch"),
    # Software subscriptions
    (re.compile(r"anthropic|claude\.ai|openai|github|hostinger|netlify|vercel|aws|google.*cloud|microsoft|adobe|figma|notion|slack|zoom"),
     "Subscriptions",
     "vendor matches a known software subscription"),
    # Office supplies
    (re.compile(r"staples|office\s*depot|amazon\s*business"),
     "Office Supplies",
     "vendor sells office supplies"),
    # General supplies — Walmart / Target are ambiguous; weak suggestion
    (re.compile(r"\bwalmart\b|\btarget\b|\bcostco\b|sam'?s\s*club"),
     "Supplies", "vendor is a general retailer"),
)


def _match_transfer(text: str) -> bool:
    low = text.lower()
    return any(p.search(low) for p in _TRANSFER_PATTERNS)


def suggest_target_account(
    *,
    txn_type: str,
    vendor_name: str,
    memo: str,
    amount: Decimal,
    accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pick a likely target account for a miscategorized transaction.

    Args:
        txn_type: QBO entity type ("Deposit", "Purchase", "Expense", etc.)
        vendor_name: from ColData[3] of the P&L Detail report
        memo: from ColData[4] — typically the bank-feed description
        amount: signed Decimal; sign affects "deposit-like" detection
        accounts: list of {qb_account_id, name, account_type} dicts from qb_accounts

    Returns:
        {
          action: "recategorize" | "open_in_qb" | "unknown",
          suggested_account_id: str | None,
          suggested_account_name: str | None,
          target_account_type: str | None,  # what category we're aiming for
          reason: str,
          confidence: float,  # 0..1
        }
    """
    haystack = f"{memo} {vendor_name}".strip()
    if not haystack:
        return {
            "action": "unknown",
            "suggested_account_id": None,
            "suggested_account_name": None,
            "target_account_type": None,
            "reason": "no memo or vendor — can't suggest",
            "confidence": 0.0,
        }

    # 1. Transfer detection — these need the QB UI for the delete+create
    #    conversion, so we surface an "Open in QB" action instead of a write.
    if _match_transfer(haystack):
        return {
            "action": "open_in_qb",
            "suggested_account_id": None,
            "suggested_account_name": None,
            "target_account_type": "Bank",  # informational only
            "reason": "looks like a bank-to-bank transfer — fix in QB UI (delete + recreate as Transfer)",
            "confidence": 0.9,
        }

    # 1.5 Auditor 2026-05-25: bank Deposit aggregating customer payments is
    # STRUCTURALLY not a "wrong account" problem — it's a missing
    # ReceivePayment workflow. Auto-recategorizing to Income severs the
    # A/R linkage to invoices. Always defer Deposits to QB UI in v1 so
    # the bookkeeper can match them to the correct ReceivePayment entries.
    if txn_type.lower() == "deposit":
        return {
            "action": "open_in_qb",
            "suggested_account_id": None,
            "suggested_account_name": None,
            "target_account_type": None,
            "reason": ("Deposits should be matched to ReceivePayment entries against the "
                       "original invoices in QB — automatic recategorize would sever A/R linkage. "
                       "Open in QB and use the Match flow."),
            "confidence": 0.95,
        }

    # 2. Keyword-based suggestion.
    for pattern, target_type_fragment, reason in _KEYWORD_RULES:
        if not pattern.search(haystack.lower()):
            continue
        # Find the best-matching account in the tenant's chart.
        # Match by sub-name first, then by account_type containing the fragment.
        target = _find_account_for_type(accounts, target_type_fragment)
        if target is None:
            return {
                "action": "unknown",
                "suggested_account_id": None,
                "suggested_account_name": None,
                "target_account_type": target_type_fragment,
                "reason": (f"pattern matched ({reason}) but no '{target_type_fragment}' "
                           f"account exists in QB — create one or pick manually"),
                "confidence": 0.4,
            }
        return {
            "action": "recategorize",
            "suggested_account_id": target["qb_account_id"],
            "suggested_account_name": target["name"],
            "target_account_type": target.get("account_type"),
            "reason": reason,
            "confidence": 0.75,
        }

    # (Deposit fallback removed — handled above by always deferring to QB UI.)

    return {
        "action": "unknown",
        "suggested_account_id": None,
        "suggested_account_name": None,
        "target_account_type": None,
        "reason": "no rule matched — pick manually",
        "confidence": 0.0,
    }


def _find_account_for_type(
    accounts: list[dict[str, Any]], type_fragment: str,
) -> dict[str, Any] | None:
    """Find an active account whose type or name contains the fragment.

    Heuristic: prefer name match over type match. Income → 'Service Income'
    over a generic 'Other Income'. Subscriptions → 'Subscriptions' or 'Software'.
    """
    frag = type_fragment.lower()
    name_matches = [a for a in accounts
                    if a.get("active") is not False and frag in (a.get("name") or "").lower()]
    if name_matches:
        return name_matches[0]
    type_matches = [a for a in accounts
                    if a.get("active") is not False and frag in (a.get("account_type") or "").lower()]
    return type_matches[0] if type_matches else None


# ---- QBO API write -----------------------------------------------------------


# V1 supports Purchase / Expense recategorization only. Deposits go through
# the "Open in QB" path so the bookkeeper can match them to the correct
# ReceivePayment entries (auditor 2026-05-25 — auto-recat would sever A/R).
SUPPORTED_TYPES = frozenset({"Purchase", "Expense"})


class RecategorizeError(Exception):
    """Wraps the user-visible error reason."""


async def recategorize_transaction(
    qb: QBClient,
    *,
    txn_type: str,
    txn_id: str,
    new_account_id: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """GET the entity, swap the line's AccountRef, PUT it back.

    Returns:
        {
          txn_id, txn_type,
          before_account_id, after_account_id,
          before_account_name, after_account_name,
          synctoken_before, synctoken_after,
        }

    Raises RecategorizeError with a user-friendly message on:
        - unsupported txn_type
        - entity not found in QB
        - no account-bearing line found (e.g. multi-line entry beyond v1 scope)
        - QBO API error (rate limit / auth / 400 from invalid account ref)
    """
    if txn_type not in SUPPORTED_TYPES:
        raise RecategorizeError(
            f"recategorize: {txn_type} is not supported in v1 — fix in QB UI"
        )
    # V1: Purchase / Expense only. Deposit was removed after the 2026-05-25
    # audit (A/R-linkage risk on customer-payment deposits).
    entity_name = "purchase"
    entity_key = "Purchase"
    line_detail_key = "AccountBasedExpenseLineDetail"

    # 1. GET the entity to read its current state + SyncToken.
    read_url = f"/v3/company/{qb.realm_id}/{entity_name}/{txn_id}?minorversion={qb.minor_version}"
    resp = await qb._client.get(read_url)  # noqa: SLF001
    qb._raise_for_status(resp)  # noqa: SLF001
    body = resp.json()
    entity = body.get(entity_key)
    if not entity:
        raise RecategorizeError(f"{txn_type} {txn_id} not found in QuickBooks")

    sync_token_before = entity.get("SyncToken")
    lines = entity.get("Line") or []
    if not lines:
        raise RecategorizeError(f"{txn_type} {txn_id} has no line items to recategorize")

    # 2. Capture per-line before-state for FULL reversibility (auditor
    #    2026-05-25: the prior single before_account_id couldn't undo a
    #    multi-line entry). Then swap AccountRef on every account-based line.
    before_lines: list[dict[str, Any]] = []
    item_based_lines_skipped = 0
    for idx, line in enumerate(lines):
        detail = line.get(line_detail_key)
        if not detail:
            # Item-based or other line type — record skip so the user knows
            # only part of the entry was recategorized.
            if line.get("ItemBasedExpenseLineDetail"):
                item_based_lines_skipped += 1
            continue
        acct_ref = detail.get("AccountRef") or {}
        before_lines.append({
            "line_index": idx,
            "account_id": str(acct_ref.get("value") or ""),
            "account_name": str(acct_ref.get("name") or ""),
            "amount": str(line.get("Amount") or "0"),
        })
        detail["AccountRef"] = {"value": new_account_id}

    if not before_lines:
        raise RecategorizeError(
            f"{txn_type} {txn_id} has no account-bearing lines (likely an item-based "
            f"line — fix in QB UI)"
        )

    if item_based_lines_skipped:
        # Refuse partial-update silent state. Either we recategorize the
        # whole entry or we don't touch it. Mixed item-based + account-based
        # entries need QB UI to handle properly.
        raise RecategorizeError(
            f"{txn_type} {txn_id} has both item-based and account-based lines — "
            f"partial recategorize would land the books in an inconsistent state. "
            f"Fix in QB UI."
        )

    # 3. Build a MINIMAL PUT body. Auditor 2026-05-25: spreading the GET
    #    response back drags server-computed fields (MetaData, TotalTax,
    #    LinkedTxn, ExchangeRate, domain) that QBO either rejects or
    #    silently recomputes. Send only the fields we mean to change.
    update_payload = {
        "Id": txn_id,
        "SyncToken": sync_token_before,
        "sparse": True,
        "Line": lines,  # full Line array — sparse does not apply to arrays
    }
    # Preserve a few entity-level fields QBO requires on Purchase update.
    # Per Intuit docs, AccountRef (the BANK side) and PaymentType are
    # required on Purchase update even with sparse=true; CurrencyRef on
    # multi-currency books. Pull only those from the GET response.
    for required_field in ("AccountRef", "PaymentType", "CurrencyRef"):
        if required_field in entity:
            update_payload[required_field] = entity[required_field]

    write_url = f"/v3/company/{qb.realm_id}/{entity_name}?operation=update&minorversion={qb.minor_version}"
    if idempotency_key:
        from urllib.parse import quote
        write_url += f"&requestid={quote(idempotency_key, safe='')}"
    resp2 = await qb._client.post(write_url, json=update_payload)  # noqa: SLF001
    qb._raise_for_status(resp2)  # noqa: SLF001
    updated = (resp2.json() or {}).get(entity_key, {})

    # Pull the after-state account name from the first line that now points
    # to the new account.
    after_account_name = None
    for line in updated.get("Line", []):
        detail = line.get(line_detail_key)
        if detail and detail.get("AccountRef", {}).get("value") == new_account_id:
            after_account_name = detail["AccountRef"].get("name")
            break

    log.info(
        "qb_recategorized txn=%s type=%s lines=%d before=%s after=%s",
        txn_id, txn_type, len(before_lines),
        [b["account_id"] for b in before_lines], new_account_id,
    )
    return {
        "txn_id": txn_id,
        "txn_type": txn_type,
        "before_lines": before_lines,  # full reversibility — per-line prior state
        "after_account_id": new_account_id,
        "after_account_name": after_account_name,
        "synctoken_before": sync_token_before,
        "synctoken_after": updated.get("SyncToken"),
    }
