"""Outlook → vendor-bills bridge (Phase 2, delta ingest).

When the Outlook delta sync sees a new message from an allowlisted supplier
sender that has attachments, this downloads each PDF and walks it up a rung
ladder until something claims it, so supplier paperwork lands in GDX
automatically instead of by manual upload:

    rung 1   ``upload_midwest_invoice``    deterministic bill parser
    rung 1b  ``ingest_midwest_statement``  deterministic statement parser
    rung 1c  ``ingest_midwest_order``      deterministic order-confirmation parser
    rung 2   ``upload_invoice_via_llm``    Claude-vision bill extraction

Rung 1b exists because a statement of account is not a bill and the two must
not be conflated — recording a statement as a payable would shadow the real
invoice through ``(vendor, invoice_number)`` dedup. It sits BEFORE the LLM
because it is deterministic and free; see ``_statement_rung``.

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
from gdx_dispatch.modules.vendor_orders.parsers.midwest_order import (
    MidwestOrderParseError,
    MidwestOrderStructureError,
)
from gdx_dispatch.modules.vendor_orders.service import ingest_midwest_order
from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    MidwestParseError as MidwestStatementParseError,
)
from gdx_dispatch.modules.vendor_statements.parsers.midwest import (
    MidwestStatementStructureError,
)
from gdx_dispatch.modules.vendor_statements.service import ingest_midwest_statement

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
            # Rung 1b: not a parseable Midwest invoice — is it a statement of
            # account? Deterministic and free, so it runs BEFORE the LLM.
            handled = _statement_rung(
                tdb, result,
                pdf_bytes=data,
                original_filename=att.get("name") or "statement.pdf",
                uploaded_by=uploaded_by,
                graph_id=graph_id,
            )
            if handled:
                continue
            # Rung 1c: an order confirmation? Also deterministic and free, and
            # it arrives BEFORE any bill — this is the only view of committed
            # spend that isn't yet a payable.
            handled = _order_rung(
                tdb, result,
                pdf_bytes=data,
                original_filename=att.get("name") or "order.pdf",
                uploaded_by=uploaded_by,
                graph_id=graph_id,
            )
            if handled:
                continue
            # Rung 2: none of the deterministic parsers claimed it — try LLM
            # extraction if the tenant configured a key and the run's cost
            # ceiling allows.
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


def _statement_rung(
    tdb: Session,
    result: dict[str, int],
    *,
    pdf_bytes: bytes,
    original_filename: str,
    uploaded_by: str,
    graph_id: str,
) -> bool:
    """Rung 1b: record a vendor STATEMENT of account. Mutates ``result``.

    Returns True when this PDF is settled (recorded, already known, or errored)
    and must not continue to the LLM rung; False when it simply isn't a
    statement we can parse, which is the caller's signal to keep climbing.

    Ordered before the LLM deliberately. The Midwest statement parser is
    strongly self-identifying — it requires the vendor's name in the extracted
    text, an aging row carrying exactly eight currency columns, and the branch
    code anchor on the paired detail line — so it cannot mistake an invoice for
    a statement, and it costs nothing. Statements from vendors with no parser
    still fall through to rung 2, where the LLM classifier recognizes them and
    (correctly) declines to record one as a bill.
    """
    try:
        res = ingest_midwest_statement(
            tdb,
            pdf_bytes=pdf_bytes,
            original_filename=original_filename,
            content_type="application/pdf",
            uploaded_by=uploaded_by,
            source="email",
        )
    except MidwestStatementStructureError as exc:
        # The letterhead says this IS a Midwest statement and we still couldn't
        # read it. Three things must NOT happen here. It must not fall through
        # to rung 2 — handing a statement to a bill extractor is how one gets
        # booked as a payable. It must not be filed under the generic
        # `unparseable` count, where a lost statement is indistinguishable from
        # a junk attachment. And it must not be counted an error, which would
        # block the checkpoint and make a parser gap re-download the same PDF
        # every single run forever.
        #
        # So: claim it, count it under its own name, and shout. A non-zero
        # `statement_unparseable` in a sweep report means the parser has drifted
        # from what the vendor is sending and a real document was dropped —
        # visible, attributable, and fixable, which the old behavior was not.
        result["statement_unparseable"] += 1
        log.error(
            "vendor_bill_ingest: %s from message %s IS a Midwest statement but "
            "the parser could not read it (%s) — NOT recorded. The parser needs "
            "to be updated to this statement's layout, then this message's "
            "vendor_bills_ingested_at cleared to re-ingest it.",
            original_filename, graph_id, exc,
        )
        return True
    except MidwestStatementParseError:
        return False  # not a statement at all — climb to rung 2
    except Exception:  # noqa: BLE001
        # Retryable (DB/disk). Counting it an error keeps the message
        # un-checkpointed, and claims the PDF so a failure to STORE a statement
        # can't be laundered into an LLM bill-extraction attempt.
        log.exception("vendor_bill_ingest: statement rung failed for %s", graph_id)
        result["errors"] += 1
        return True

    if res.created:
        result["statements"] += 1
        log.info(
            "vendor_bill_ingest: recorded vendor statement %s (%s, %d lines) from %s",
            res.statement.id, res.statement.statement_date, res.statement.line_count, graph_id,
        )
    else:
        result["statement_duplicate"] += 1
    return True


def _order_rung(
    tdb: Session,
    result: dict[str, int],
    *,
    pdf_bytes: bytes,
    original_filename: str,
    uploaded_by: str,
    graph_id: str,
) -> bool:
    """Rung 1c: record a supplier ORDER CONFIRMATION. Mutates ``result``.

    Returns True when the PDF is settled and must not climb further; False when
    it simply isn't an order confirmation.

    An order is a commitment, not a debt — its totals are the supplier's own
    estimate, explicitly a 30-day quote with shipping and tax provisional — so
    it gets its own counters and never touches the bill counts. What it buys is
    the front of the chain: the supplier's order number becomes their invoice
    number, so an order recorded here threads to the bill and the statement line
    that follow it, and an order with no invoice yet is committed spend that is
    otherwise invisible.
    """
    try:
        res = ingest_midwest_order(
            tdb,
            pdf_bytes=pdf_bytes,
            original_filename=original_filename,
            content_type="application/pdf",
            uploaded_by=uploaded_by,
            source="email",
        )
    except MidwestOrderStructureError as exc:
        # Titled ORDER CONFIRMATION and still unreadable. Same posture as the
        # statement rung: claim it (never hand a non-bill to the bill
        # extractor), count it under its own name so the loss is attributable
        # rather than buried with junk attachments, and do NOT count it an
        # error — that would block the checkpoint and re-download the same PDF
        # every run forever.
        result["order_unparseable"] += 1
        log.error(
            "vendor_bill_ingest: %s from message %s IS an order confirmation but "
            "the parser could not read it (%s) — NOT recorded. Update the parser "
            "to this layout, then clear this message's vendor_bills_ingested_at "
            "to re-ingest it.",
            original_filename, graph_id, exc,
        )
        return True
    except MidwestOrderParseError:
        return False  # not an order confirmation — climb to rung 2
    except Exception:  # noqa: BLE001
        # Retryable (DB/disk). Keeps the message un-checkpointed, and claims the
        # PDF so a storage failure can't be laundered into a bill extraction.
        log.exception("vendor_bill_ingest: order rung failed for %s", graph_id)
        result["errors"] += 1
        return True

    if res.created:
        result["orders"] += 1
        log.info(
            "vendor_bill_ingest: recorded vendor order %s (%s, %d lines, est %s) from %s",
            res.order.order_number, res.order.order_date, res.order.line_count,
            res.order.estimated_total, graph_id,
        )
    else:
        result["order_duplicate"] += 1
    return True


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
