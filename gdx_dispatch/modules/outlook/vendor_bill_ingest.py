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
- **LLM rung 2, bounded.** When the deterministic parser can't read a PDF and
  the tenant has an Anthropic key configured, the PDF goes to Claude-vision
  extraction (``upload_invoice_via_llm``) — capped per run via
  ``max_llm_extractions`` (the cost ceiling from [AUDIT-R3]). No key = rung
  off = unparseables queue for manual entry, exactly as before.
"""
from __future__ import annotations

import logging
from typing import Any

import anthropic
from sqlalchemy.orm import Session

from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError
from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import MidwestInvoiceParseError
from gdx_dispatch.modules.vendor_invoices.service import (
    LLMExtractionError,
    upload_invoice_via_llm,
    upload_midwest_invoice,
)

log = logging.getLogger("gdx_dispatch.modules.outlook.vendor_bill_ingest")

_FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"

# Sentinel for "the tenant HAS an LLM key but the client couldn't be built"
# (Fernet rotation incident, SDK failure). Distinct from None (= no key = rung
# deliberately off): a broken rung counts parser-rejected PDFs as retryable
# ERRORS so their messages stay un-checkpointed until the incident is fixed —
# never as unparseable, which would stamp them permanently skipped.
LLM_BROKEN = object()

# Anthropic status codes that mean THIS DOCUMENT is unprocessable (encrypted,
# corrupt, >100 pages, oversized request) — deterministic, no point retrying.
# Everything else (401/403 key incident, 429 throttle, 5xx) is retryable.
_LLM_DETERMINISTIC_STATUS = frozenset({400, 404, 413, 422})


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
    messages whose PDF set was cut short by ``max_downloads``; ``llm_capped``
    counts messages with parser-unreadable PDFs left unprocessed because the
    per-run LLM ceiling was reached."""
    return {
        "ingested": 0, "duplicate": 0, "unparseable": 0, "errors": 0,
        "downloads": 0, "capped": 0,
        "llm_extractions": 0, "ingested_llm": 0, "llm_capped": 0,
    }


def ingest_message_attachments(
    tdb: Session,
    gc,
    message: dict[str, Any],
    allowlist: list[str],
    *,
    uploaded_by: str = "outlook",
    max_downloads: int | None = None,
    llm_client=None,
    max_llm_extractions: int | None = None,
) -> dict[str, int]:
    """Ingest the PDF attachments of one message if its sender is allowlisted.

    Returns ``new_totals()``-shaped counts. Never raises for a single bad
    attachment — the sync must continue.

    ``max_downloads`` (None = unlimited) bounds attachment downloads for this
    call; a failed download attempt still consumes budget (the Graph call was
    spent). When the budget cuts a message short, ``capped`` is 1 and the
    remaining PDFs were not processed — the caller must NOT checkpoint the
    message as done.

    ``llm_client`` (None = rung 2 off) enables Claude-vision extraction for
    PDFs the deterministic parser rejects, bounded by ``max_llm_extractions``
    (the per-run cost ceiling; None = unlimited). A PDF skipped because the
    ceiling was hit sets ``llm_capped`` — like ``capped``, it blocks the
    checkpoint so a later run retries. LLM API/transport failures count as
    ``errors`` (retryable); a model that can't read the document counts as
    ``unparseable`` (deterministic — manual queue).
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
            # Rung 2: not a parseable Midwest invoice — try LLM extraction if
            # the tenant configured a key and the run's cost ceiling allows.
            _llm_rung(
                tdb, result,
                pdf_bytes=data,
                original_filename=att.get("name") or "bill.pdf",
                uploaded_by=uploaded_by,
                graph_id=graph_id,
                llm_client=llm_client,
                max_llm_extractions=max_llm_extractions,
            )
        except Exception:  # noqa: BLE001
            log.exception("vendor_bill_ingest: pipeline failed for %s", graph_id)
            result["errors"] += 1

    return result


def _llm_rung(
    tdb: Session,
    result: dict[str, int],
    *,
    pdf_bytes: bytes,
    original_filename: str,
    uploaded_by: str,
    graph_id: str,
    llm_client,
    max_llm_extractions: int | None,
) -> None:
    """Rung 2 for one parser-rejected PDF. Mutates ``result`` counters."""
    if llm_client is None:
        # No key configured — the PDF waits for manual entry (no bill row is
        # created), exactly the pre-LLM behavior.
        result["unparseable"] += 1
        return
    if llm_client is LLM_BROKEN:
        # Key exists but the client couldn't be built (rotation incident).
        # Retryable: keep the message un-checkpointed until the key is fixed.
        result["errors"] += 1
        return
    if max_llm_extractions is not None and result["llm_extractions"] >= max_llm_extractions:
        result["llm_capped"] = 1  # cost ceiling — blocks checkpoint, retried later
        return
    result["llm_extractions"] += 1
    try:
        res = upload_invoice_via_llm(
            tdb,
            pdf_bytes=pdf_bytes,
            original_filename=original_filename,
            content_type="application/pdf",
            uploaded_by=uploaded_by,
            source="email",
            llm_client=llm_client,
        )
        if res.created:
            result["ingested"] += 1
            result["ingested_llm"] += 1
        else:
            result["duplicate"] += 1
    except LLMExtractionError as exc:
        log.info("vendor_bill_ingest: LLM couldn't read %s/%s: %s", graph_id, original_filename, exc)
        result["unparseable"] += 1
    except anthropic.APIStatusError as exc:
        if getattr(exc, "status_code", 0) in _LLM_DETERMINISTIC_STATUS:
            # The API deterministically rejects THIS document (encrypted,
            # corrupt, >100 pages). Retrying would burn budget on a guaranteed
            # failure every run — treat like unparseable (manual entry).
            log.warning(
                "vendor_bill_ingest: LLM rejected document %s/%s (HTTP %s) — not retryable",
                graph_id, original_filename, exc.status_code,
            )
            result["unparseable"] += 1
        else:
            # 401/403 (key incident), 429 (throttle), 5xx — retryable.
            log.warning(
                "vendor_bill_ingest: LLM API error for %s (HTTP %s) — will retry",
                graph_id, getattr(exc, "status_code", "?"),
            )
            result["errors"] += 1
    except Exception:  # noqa: BLE001
        # Transport failure / anything unclassified — retryable, must NOT
        # checkpoint the message.
        log.exception("vendor_bill_ingest: LLM rung failed for %s", graph_id)
        result["errors"] += 1


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
