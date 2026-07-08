"""Midwest Wholesale Doors retail-sale INVOICE parser (v1).

Sibling of ``parsers/midwest.py`` (which parses the monthly *statement of
account*). This parses the per-order *invoice* PDF — the thing Doug gets
emailed when he's billed for parts.

Layout (pypdf ``extraction_mode="layout"``)::

    ...                                          Retail Sale
    ...                                             INVOICE
    ...                                            <invoice #>
    ...                              Invoice Date: MM/DD/YYYY
    Sold To:                              Deliver To:
        <our company>                         <customer name>
                                          PO#: <customer / job ref>
    Units / Coverage   Inventory Item Description   Package   Unit Price      Price
        <qty>          Garage Door Material          <pkg>   $<unit>       $<total>
        Notes: <the REAL product description, may wrap to a second line>
        ...
                                      Sales Tax                    $<tax>
                                      Shipping & Handling          $<ship>
                                      Total                        $<total>
                                      Credits Pending              $<credits>
                                      Amount Due                   $<due>
    TERMS: Net 30. ...

Two things make this layout tricky and matter downstream:

1. The generic "Garage Door Material" item label is useless; the real,
   matchable product description lives in the ``Notes:`` row underneath each
   item and can wrap across a second physical line.
2. The vendor's *name* ("Midwest Wholesale Doors") is in the logo IMAGE and is
   NOT in the text layer — so we identify the format by its column-header
   signature, not by a vendor-name string (unlike the statement parser).

The parser is pure-data: it never raises on a failing arithmetic invariant
(sum(lines)+tax+shipping == total). It returns the parsed result and exposes
``invariant_discrepancy()`` so the *service* can route a mismatch to the
manual-entry queue (fail loudly, but let a human fix rather than losing the
document). It DOES raise ``MidwestInvoiceParseError`` when the document isn't
this format or is missing the structural anchors (invoice number, any line
item, the Total) — there's nothing to route in that case.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

log = logging.getLogger(__name__)

PARSER_NAME = "midwest_invoice_v1"
PARSER_VERSION = 1

# The vendor this parser is for. The name is NOT in the invoice text layer
# (it's in the logo image), so the service stamps it from here.
VENDOR_NAME = "Midwest Wholesale Doors"

# The item label the vendor prints for every door line. The real description
# is in the Notes row; we keep this only as a fallback.
_GENERIC_ITEM_LABEL = "Garage Door Material"


class MidwestInvoiceParseError(ValueError):
    """Raised when a PDF is not a parseable Midwest retail-sale invoice."""


_MONEY = r"[\d,]+\.\d{2,4}"

# One item row:  <qty>  <item label>  <package>  $<unit price>  $<line total>
_ITEM_ROW = re.compile(
    rf"""^\s*
        (?P<qty>\d+(?:\.\d+)?)\s+
        (?P<item>\S.*?)\s+
        (?P<package>\d+(?:\.\d+)?)\s+
        \$(?P<unit>{_MONEY})\s+
        \$(?P<total>{_MONEY})\s*$
    """,
    re.VERBOSE,
)

_INVOICE_NO = re.compile(r"INVOICE\s+(\d{4,})", re.IGNORECASE | re.DOTALL)
_INVOICE_DATE = re.compile(r"Invoice\s+Date:\s*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_PO = re.compile(r"PO#:\s*(.+)")
_TERMS = re.compile(r"TERMS:\s*([^.]+)\.", re.IGNORECASE)
_NET_DAYS = re.compile(r"Net\s+(\d+)", re.IGNORECASE)

_SALES_TAX = re.compile(rf"Sales\s+Tax\s+\$({_MONEY})", re.IGNORECASE)
_SHIPPING = re.compile(rf"Shipping\s*&?\s*Handling\s+\$({_MONEY})", re.IGNORECASE)
_TOTAL = re.compile(rf"(?<![A-Za-z])Total\s+\$({_MONEY})")
_CREDITS = re.compile(rf"Credits\s+Pending\s+\$({_MONEY})", re.IGNORECASE)
_AMOUNT_DUE = re.compile(rf"Amount\s+Due\s+\$({_MONEY})", re.IGNORECASE)

# Format signature — both column headers must be present.
_HEADER_SIGNATURE = ("Units / Coverage", "Inventory Item Description")


@dataclass
class ParsedInvoiceLine:
    line_no: int
    item_label: str
    description: str  # the Notes text (real product), or item_label as fallback
    quantity: Decimal
    package: Decimal | None
    unit_price: Decimal
    line_total: Decimal

    def line_math_discrepancy(self) -> Decimal:
        """abs(qty * unit_price - line_total). ~0 when the row is internally
        consistent. Non-trivial values flag a misread."""
        return abs((self.quantity * self.unit_price) - self.line_total)


@dataclass
class ParsedInvoice:
    invoice_number: str
    invoice_date: date | None
    po_reference: str | None
    terms: str | None
    net_days: int | None
    due_date: date | None
    tax: Decimal
    shipping: Decimal
    total: Decimal
    credits_pending: Decimal
    amount_due: Decimal | None
    lines: list[ParsedInvoiceLine] = field(default_factory=list)

    @property
    def subtotal(self) -> Decimal:
        return sum((ln.line_total for ln in self.lines), Decimal("0.00"))

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def invariant_discrepancy(self) -> Decimal:
        """abs(subtotal + tax + shipping - total).

        ~0 when the extracted numbers are structurally consistent with the
        printed Total. The service routes a discrepancy above tolerance to the
        manual-entry queue instead of trusting the parse.
        """
        return abs((self.subtotal + self.tax + self.shipping) - self.total)


def _money(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace("$", "").replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:  # noqa: PERF203
        raise MidwestInvoiceParseError(f"could not parse money value {raw!r}") from exc


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _is_boundary(line: str) -> bool:
    """A line that ends the item table (a totals row, terms, or a re-print of
    the column header)."""
    return bool(
        _SALES_TAX.search(line)
        or _SHIPPING.search(line)
        or _TOTAL.search(line)
        or _CREDITS.search(line)
        or _AMOUNT_DUE.search(line)
        or re.search(r"TERMS:", line, re.IGNORECASE)
        or _HEADER_SIGNATURE[1] in line
    )


def parse_invoice_text(text: str) -> ParsedInvoice:
    """Parse already-extracted layout text into a :class:`ParsedInvoice`.

    Pure function — the unit tests drive it with synthetic layout strings so
    no real invoice PDF is needed (or committed).
    """
    if not text or not text.strip():
        raise MidwestInvoiceParseError("empty invoice text")

    if not all(sig in text for sig in _HEADER_SIGNATURE):
        raise MidwestInvoiceParseError(
            "does not look like a Midwest retail-sale invoice "
            "(missing the 'Units / Coverage' / 'Inventory Item Description' header)"
        )

    invoice_number = _first(_INVOICE_NO, text)
    if not invoice_number:
        raise MidwestInvoiceParseError("could not find invoice number")

    invoice_date = None
    if raw := _first(_INVOICE_DATE, text):
        try:
            invoice_date = datetime.strptime(raw, "%m/%d/%Y").date()
        except ValueError:
            invoice_date = None

    po_reference = _first(_PO, text)

    terms = _first(_TERMS, text)
    net_days = None
    due_date = None
    if terms and (m := _NET_DAYS.search(terms)):
        net_days = int(m.group(1))
        if invoice_date is not None:
            due_date = invoice_date + timedelta(days=net_days)

    tax = _money(_first(_SALES_TAX, text) or "0.00")
    shipping = _money(_first(_SHIPPING, text) or "0.00")
    credits_pending = _money(_first(_CREDITS, text) or "0.00")

    total_raw = _first(_TOTAL, text)
    if total_raw is None:
        raise MidwestInvoiceParseError("could not find invoice Total")
    total = _money(total_raw)

    amount_due = None
    if raw := _first(_AMOUNT_DUE, text):
        amount_due = _money(raw)

    lines = _parse_line_items(text)
    if not lines:
        raise MidwestInvoiceParseError("no line items found in invoice")

    return ParsedInvoice(
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        po_reference=po_reference,
        terms=terms,
        net_days=net_days,
        due_date=due_date,
        tax=tax,
        shipping=shipping,
        total=total,
        credits_pending=credits_pending,
        amount_due=amount_due,
        lines=lines,
    )


def _parse_line_items(text: str) -> list[ParsedInvoiceLine]:
    physical = text.splitlines()
    # Start scanning after the column header row.
    start = 0
    for idx, line in enumerate(physical):
        if _HEADER_SIGNATURE[1] in line:
            start = idx + 1
            break

    lines: list[ParsedInvoiceLine] = []
    line_no = 0
    i = start
    n = len(physical)
    while i < n:
        raw = physical[i]
        if _is_boundary(raw):
            break
        m = _ITEM_ROW.match(raw)
        if not m:
            i += 1
            continue

        # Gather the Notes / continuation lines that follow, up to the next
        # item row or a boundary.
        note_parts: list[str] = []
        j = i + 1
        while j < n:
            nxt = physical[j]
            if _is_boundary(nxt) or _ITEM_ROW.match(nxt):
                break
            stripped = nxt.strip()
            if stripped:
                note_parts.append(re.sub(r"^Notes:\s*", "", stripped, flags=re.IGNORECASE))
            j += 1

        item_label = m.group("item").strip()
        description = " ".join(note_parts).strip() or item_label
        try:
            parsed = ParsedInvoiceLine(
                line_no=line_no,
                item_label=item_label,
                description=description,
                quantity=Decimal(m.group("qty")),
                package=Decimal(m.group("package")) if m.group("package") else None,
                unit_price=_money(m.group("unit")),
                line_total=_money(m.group("total")),
            )
        except (InvalidOperation, MidwestInvoiceParseError) as exc:
            raise MidwestInvoiceParseError(
                f"failed to parse line item at physical line {i}: {raw!r} ({exc})"
            ) from exc

        lines.append(parsed)
        line_no += 1
        i = j

    return lines


def parse_midwest_invoice(pdf_bytes: bytes) -> ParsedInvoice:
    """Extract layout text from the PDF and parse it.

    Raises ``MidwestInvoiceParseError`` if the PDF can't be read or isn't a
    Midwest retail-sale invoice.
    """
    if not pdf_bytes:
        raise MidwestInvoiceParseError("empty PDF bytes")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        raise MidwestInvoiceParseError(f"could not read PDF: {exc}") from exc

    text = ""
    for page in reader.pages:
        try:
            text += page.extract_text(extraction_mode="layout") + "\n"
        except Exception as exc:  # noqa: BLE001
            raise MidwestInvoiceParseError(f"layout extraction failed: {exc}") from exc

    return parse_invoice_text(text)
