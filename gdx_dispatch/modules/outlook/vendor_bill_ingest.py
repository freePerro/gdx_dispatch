"""Outlook → vendor-bills bridge (Phase 2, delta ingest).

When the Outlook delta sync sees a new message from an allowlisted supplier
sender that has attachments, this downloads each PDF and feeds it to the
vendor-invoice pipeline (``upload_midwest_invoice``), so the bill lands in the
review queue automatically instead of by manual upload.

Safety posture:
- **Opt-in, default off.** Nothing ingests unless the tenant lists sender
  addresses/domains in ``OutlookSettings.vendor_bill_sender_allowlist``.
- **Allowlist gates the LLM boundary too** (design [AUDIT-R3]): only allowlisted
  senders' PDFs reach the pipeline; a stranger's attachment is never processed.
- **Idempotent by content hash.** Re-seeing a message re-runs the pipeline,
  which dedups on the document hash + (vendor, invoice_number) — no duplicate
  records. The ``OutlookMessage.vendor_bills_ingested_at`` checkpoint (stamped
  by the callers in tasks.py) additionally makes re-runs cost-idempotent: a
  fully-processed message is never re-DOWNLOADED by the history sweep.
- **Bounded downloads.** ``max_downloads`` caps attachment downloads per call
  so the history sweep (``sweep_vendor_bill_history``) can enforce a per-run
  download budget against Graph throttling / runaway cost.

NOT here (still gated / a later increment): the Claude-vision extraction rung
with a per-run cost ceiling.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import MidwestInvoiceParseError
from gdx_dispatch.modules.vendor_invoices.service import upload_midwest_invoice

log = logging.getLogger("gdx_dispatch.modules.outlook.vendor_bill_ingest")

_FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"


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
    if att.get("@odata.type") != _FILE_ATTACHMENT:
        return False
    ctype = (att.get("contentType") or "").lower()
    name = (att.get("name") or "").lower()
    return ctype == "application/pdf" or name.endswith(".pdf")


def _from_address(message: dict[str, Any]) -> str | None:
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
    return sender_allowed(_from_address(message), allowlist)


def new_totals() -> dict[str, int]:
    """The zero counters every ingest call/aggregate uses. ``capped`` counts
    messages whose PDF set was cut short by ``max_downloads``."""
    return {"ingested": 0, "duplicate": 0, "unparseable": 0, "errors": 0, "downloads": 0, "capped": 0}


def ingest_message_attachments(
    tdb: Session,
    gc,
    message: dict[str, Any],
    allowlist: list[str],
    *,
    uploaded_by: str = "outlook",
    max_downloads: int | None = None,
) -> dict[str, int]:
    """Ingest the PDF attachments of one message if its sender is allowlisted.

    Returns ``new_totals()``-shaped counts. Never raises for a single bad
    attachment — the sync must continue.

    ``max_downloads`` (None = unlimited) bounds attachment downloads for this
    call; a failed download attempt still consumes budget (the Graph call was
    spent). When the budget cuts a message short, ``capped`` is 1 and the
    remaining PDFs were not processed — the caller must NOT checkpoint the
    message as done.
    """
    result = new_totals()
    if not allowlist or not message.get("hasAttachments"):
        return result
    if not sender_allowed(_from_address(message), allowlist):
        return result

    graph_id = message.get("id")
    if not graph_id:
        return result

    try:
        attachments = gc.list_attachments(graph_id)
    except OutlookGraphAPIError as exc:
        log.warning("vendor_bill_ingest: list_attachments failed for %s: %s", graph_id, exc)
        result["errors"] += 1
        return result

    for att in attachments:
        if not is_pdf_attachment(att):
            continue
        if max_downloads is not None and result["downloads"] >= max_downloads:
            result["capped"] = 1
            break
        result["downloads"] += 1
        try:
            data = gc.download_attachment(graph_id, att["id"])
        except OutlookGraphAPIError as exc:
            log.warning("vendor_bill_ingest: download failed for %s/%s: %s", graph_id, att.get("id"), exc)
            result["errors"] += 1
            continue
        try:
            res = upload_midwest_invoice(
                tdb,
                pdf_bytes=data,
                original_filename=att.get("name") or "bill.pdf",
                content_type="application/pdf",
                uploaded_by=uploaded_by,
                source="email",
            )
            if res.created:
                result["ingested"] += 1
            else:
                result["duplicate"] += 1
        except MidwestInvoiceParseError:
            # Not a recognized vendor invoice (or a scan) — the LLM rung + manual
            # queue will handle these in a later increment. Skip for now.
            result["unparseable"] += 1
        except Exception:  # noqa: BLE001
            log.exception("vendor_bill_ingest: pipeline failed for %s", graph_id)
            result["errors"] += 1

    return result


def ingest_messages(
    tdb: Session,
    gc,
    messages: list[dict[str, Any]],
    allowlist: list[str],
    *,
    uploaded_by: str = "outlook",
) -> dict[str, int]:
    """Ingest a page of messages. Aggregates per-message counts."""
    totals = new_totals()
    if not allowlist:
        return totals
    for m in messages:
        r = ingest_message_attachments(tdb, gc, m, allowlist, uploaded_by=uploaded_by)
        for k in totals:
            totals[k] += r[k]
    return totals
