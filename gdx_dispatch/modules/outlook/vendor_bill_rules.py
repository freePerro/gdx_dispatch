"""The light half of the Outlook → vendor-bills bridge: the sender allowlist,
the candidate pre-filter, the counters and the broken-LLM sentinel.

This module imports nothing from the app, on purpose. Importing
``gdx_dispatch.models`` runs ``core.celery_app``, which imports
``modules.outlook.tasks`` at the bottom of the file to register its tasks, and
``tasks`` needs these names at import time. When they lived in
``vendor_bill_ingest`` (whose own imports reach the models), importing that
module first in a fresh interpreter found them undefined, and
``core/webhooks/__init__.py`` swallowed the ImportError and left
``emit_webhook`` as ``None`` for the life of the process (GDXA-587).
``vendor_bill_ingest`` re-exports every name here, so its importers keep
working.
"""
from __future__ import annotations

from typing import Any

FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"

# Sentinel for "the tenant HAS an LLM key but the client couldn't be built"
# (Fernet rotation incident, SDK failure). Distinct from None (= no key = rung
# deliberately off): a broken rung counts parser-rejected PDFs as retryable
# ERRORS so their messages stay un-checkpointed until the incident is fixed —
# never as unparseable, which would stamp them permanently skipped.
LLM_BROKEN = object()


def normalize_allowlist(allowlist: list[str] | None) -> list[str]:
    return [str(a).strip().lower() for a in (allowlist or []) if str(a).strip()]


def sender_allowed(from_address: str | None, allowlist: list[str]) -> bool:
    """True if ``from_address`` matches an allowlist entry — either the full
    address or its domain (an entry with no ``@`` is treated as a domain)."""
    if not from_address:
        return False
    addr = from_address.strip().lower()
    if not addr:
        return False
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    for entry in allowlist:
        if entry == addr:
            return True
        if "@" not in entry and domain and (domain == entry or domain.endswith("." + entry)):
            return True
    return False


def is_pdf_attachment(att: dict[str, Any]) -> bool:
    if att.get("@odata.type") != FILE_ATTACHMENT:
        return False
    ctype = (att.get("contentType") or "").lower()
    name = (att.get("name") or "").lower()
    return ctype == "application/pdf" or name.endswith(".pdf")


def from_address(message: dict[str, Any]) -> str | None:
    return ((message.get("from") or {}).get("emailAddress") or {}).get("address")


def is_candidate(message: dict[str, Any], allowlist: list[str]) -> bool:
    """Cheap pre-filter (no Graph call): an allowlisted sender WITH attachments.
    Used by the sync to collect candidates, which are then ingested in a
    SEPARATE transaction — never inside the folder-sync transaction, because
    ``upload_midwest_invoice`` owns flush/rollback/disk-I/O and would otherwise
    reach through and discard the sync's un-committed message mirror while the
    delta token still advances (silent gap)."""
    if not allowlist or not message.get("hasAttachments"):
        return False
    return sender_allowed(from_address(message), allowlist)


def new_totals() -> dict[str, int]:
    """The zero counters every ingest call/aggregate uses. ``capped`` counts
    messages whose PDF set was cut short by ``max_downloads``; ``llm_capped``
    counts messages with parser-unreadable PDFs left unprocessed because the
    per-run LLM ceiling was reached. The ``statement*`` keys count rung 1b
    (vendor statements of account) separately from bills — a statement is not
    a payable and must never inflate the ``ingested`` count the office reads as
    "new bills to review", and neither must an order confirmation, which is a
    commitment rather than a debt. The ``*_unparseable`` keys are the ones to
    watch: each means a document the supplier's own letterhead and title
    identify as ours was dropped because the parser couldn't read it."""
    return {
        "ingested": 0, "duplicate": 0, "unparseable": 0, "errors": 0,
        "downloads": 0, "capped": 0,
        "llm_extractions": 0, "ingested_llm": 0, "llm_capped": 0,
        "statements": 0, "statement_duplicate": 0, "statement_unparseable": 0,
        "orders": 0, "order_duplicate": 0, "order_unparseable": 0,
    }
