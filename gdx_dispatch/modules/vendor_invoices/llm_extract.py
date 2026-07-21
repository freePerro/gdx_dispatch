"""LLM extraction rung (rung 2) for vendor-bill PDFs.

When the deterministic vendor parser can't read a PDF
(``MidwestInvoiceParseError``), this rung sends the document to the tenant's
configured Anthropic model and maps the forced tool output onto the SAME
``ParsedInvoice`` contract the deterministic parser produces — so everything
downstream (dedup layers, invariant checks, line materialization, the review
queue) is identical for both extraction paths.

Posture (design [AUDIT-R3]):
- The sender ALLOWLIST gates what reaches this rung — callers only invoke it
  for allowlisted senders' PDFs — and the per-run cost ceiling lives in the
  caller (``LLM_MAX_EXTRACTIONS_PER_RUN`` in the outlook tasks).
- Content-hash dedup runs BEFORE extraction in the service, so a re-seen PDF
  never re-spends tokens.
- LLM output is data, not truth: the service stamps ``extraction_method='llm'``
  plus a notes marker, and the structural invariant check routes any
  non-balancing extraction to manual review exactly like a parser misread.
"""
from __future__ import annotations

import base64
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from gdx_dispatch.modules.vendor_invoices.parsers.midwest_invoice import (
    ParsedInvoice,
    ParsedInvoiceLine,
)

LLM_EXTRACTION_MODEL = "claude-haiku-4-5"
# Anthropic's request limit is 32MB; stay well under it. Supplier bills are
# typically well below 1MB — anything larger is a scan dump we route to the
# manual queue rather than pay to process.
MAX_LLM_PDF_BYTES = 16 * 1024 * 1024
_MAX_TOKENS = 4096

_CENT = Decimal("0.01")


class LLMExtractionError(ValueError):
    """The model could not produce a usable extraction for this PDF.

    Deterministic-failure semantics: callers treat this like an unparseable
    document (manual queue), NOT like a transient API error (retry)."""


_EXTRACTION_TOOL = {
    "name": "record_invoice",
    "description": (
        "Record the extracted fields of a supplier invoice/bill PDF. "
        "If the document is NOT an invoice or bill, call this with "
        "invoice_number set to an empty string."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": ["invoice", "statement", "quote", "order_acknowledgment",
                         "packing_slip", "receipt", "other"],
                "description": (
                    "What this document actually is. A monthly STATEMENT listing "
                    "multiple invoices is NOT an invoice. Only 'invoice' documents "
                    "are recorded as bills."
                ),
            },
            "vendor_name": {
                "type": "string",
                "description": "Supplier/vendor company name as printed (letterhead or logo).",
            },
            "invoice_number": {
                "type": "string",
                "description": "The invoice/bill number. Empty string if this is not an invoice.",
            },
            "invoice_date": {
                "type": ["string", "null"],
                "description": "Invoice date as YYYY-MM-DD, null if absent.",
            },
            "po_reference": {"type": ["string", "null"]},
            "terms": {"type": ["string", "null"], "description": "Payment terms as printed, e.g. 'Net 30'."},
            "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, null if absent."},
            "tax": {
                "type": ["string", "number", "null"],
                "description": "Total sales tax as a decimal string, no currency symbol. Null/0 if none.",
            },
            "shipping": {
                "type": ["string", "number", "null"],
                "description": "Freight/shipping & handling total. Do NOT also list it as a line.",
            },
            "total": {
                "type": ["string", "number"],
                "description": "The printed invoice total (grand total).",
            },
            "lines": {
                "type": "array",
                "description": (
                    "Every product/charge line EXCEPT freight and sales tax "
                    "(those go in shipping/tax)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "item_label": {"type": ["string", "null"], "description": "Item/SKU label if printed."},
                        "description": {"type": "string"},
                        "quantity": {"type": ["string", "number", "null"], "description": "Default 1 if absent."},
                        "unit_price": {"type": ["string", "number", "null"]},
                        "line_total": {"type": ["string", "number"]},
                    },
                    "required": ["description", "line_total"],
                },
            },
        },
        "required": ["document_type", "vendor_name", "invoice_number", "total", "lines"],
    },
}

_PROMPT = (
    "First classify the document: a single supplier INVOICE/BILL is the only "
    "type recorded — a monthly statement, quote, order acknowledgment, packing "
    "slip, or receipt is NOT an invoice (set document_type accordingly). "
    "For an invoice, extract it exactly as printed — do not infer, round, or "
    "correct any value. Amounts as decimal strings without currency symbols; "
    "dates as YYYY-MM-DD. Report freight/shipping and sales tax ONLY in their "
    "dedicated fields, never as lines. Then record the result with the "
    "record_invoice tool."
)


def _dec(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise LLMExtractionError(f"unreadable amount: {value!r}") from exc


def _iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _build_lines(raw_lines: list[dict[str, Any]]) -> list[ParsedInvoiceLine]:
    lines: list[ParsedInvoiceLine] = []
    for i, raw in enumerate(raw_lines):
        description = str(raw.get("description") or "").strip()
        line_total = _dec(raw.get("line_total"))
        if not description or line_total is None:
            raise LLMExtractionError(f"line {i + 1} missing description or line_total")
        quantity = _dec(raw.get("quantity"), default=Decimal("1.00"))
        if quantity is None or quantity <= 0:
            quantity = Decimal("1.00")
        unit_price = _dec(raw.get("unit_price"))
        if unit_price is None:
            # Derive so qty*unit == total holds for the line-math invariant.
            unit_price = (line_total / quantity).quantize(_CENT, rounding=ROUND_HALF_UP)
        item_label = str(raw.get("item_label") or "").strip() or description[:100]
        lines.append(
            ParsedInvoiceLine(
                line_no=i + 1,
                item_label=item_label,
                description=description,
                quantity=quantity,
                package=None,
                unit_price=unit_price,
                line_total=line_total,
            )
        )
    return lines


def extract_invoice_via_llm(client, pdf_bytes: bytes) -> tuple[str, ParsedInvoice]:
    """Extract ``(vendor_name_raw, ParsedInvoice)`` from a PDF via the tenant's
    Anthropic client. Raises ``LLMExtractionError`` when the document can't be
    read as an invoice; lets transport/API exceptions propagate (the caller
    treats those as retryable, not as unparseable)."""
    if not pdf_bytes:
        raise LLMExtractionError("empty file")
    if len(pdf_bytes) > MAX_LLM_PDF_BYTES:
        raise LLMExtractionError(
            f"pdf too large for LLM extraction ({len(pdf_bytes)} > {MAX_LLM_PDF_BYTES} bytes)"
        )

    msg = client.messages.create(
        model=LLM_EXTRACTION_MODEL,
        max_tokens=_MAX_TOKENS,
        tools=[_EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )

    if getattr(msg, "stop_reason", None) == "max_tokens":
        # A truncated tool call must not masquerade as a partial extraction.
        raise LLMExtractionError("extraction truncated (max_tokens) — document too long")
    block = next((b for b in msg.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None:
        raise LLMExtractionError("model returned no structured extraction")
    data: dict[str, Any] = block.input or {}

    doc_type = str(data.get("document_type") or "").strip().lower()
    if doc_type and doc_type != "invoice":
        # Allowlisted suppliers also send statements/quotes/acknowledgments.
        # Recording a STATEMENT as a bill could even shadow the real invoice
        # via (vendor, invoice_number) dedup — reject anything not an invoice.
        raise LLMExtractionError(f"document is a {doc_type}, not an invoice")

    vendor_name = str(data.get("vendor_name") or "").strip()
    invoice_number = str(data.get("invoice_number") or "").strip()
    if not vendor_name:
        raise LLMExtractionError("no vendor name extracted")
    if not invoice_number:
        raise LLMExtractionError("not an invoice (no invoice number extracted)")

    total = _dec(data.get("total"))
    if total is None or total <= 0:
        raise LLMExtractionError(f"no positive total extracted ({total!r})")
    raw_lines = data.get("lines") or []
    if not isinstance(raw_lines, list) or not raw_lines:
        raise LLMExtractionError("no lines extracted")

    parsed = ParsedInvoice(
        invoice_number=invoice_number,
        invoice_date=_iso_date(data.get("invoice_date")),
        po_reference=(str(data.get("po_reference")).strip() or None) if data.get("po_reference") else None,
        terms=(str(data.get("terms")).strip() or None) if data.get("terms") else None,
        net_days=None,
        due_date=_iso_date(data.get("due_date")),
        tax=_dec(data.get("tax"), default=Decimal("0.00")),
        shipping=_dec(data.get("shipping"), default=Decimal("0.00")),
        total=total,
        credits_pending=Decimal("0.00"),
        amount_due=None,
        lines=_build_lines(raw_lines),
    )
    return vendor_name, parsed
