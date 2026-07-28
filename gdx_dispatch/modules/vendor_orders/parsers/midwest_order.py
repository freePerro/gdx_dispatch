"""Midwest Wholesale Doors ORDER CONFIRMATION parser (v1).

The third document type this supplier sends. It arrives when an order is
placed, before anything is billed, and it matters because of one fact verified
against real mail: **the order number becomes the invoice number.** Eight of ten
sampled confirmations already appear as invoice numbers on a statement or a
captured bill; the two that don't are the newest — ordered, not yet billed.
That threads the whole lifecycle on a single key:

    order confirmation  →  invoice  →  statement line

Which also means an order confirmation is NOT a bill. Its totals are explicitly
provisional — the document says "Quoted pricing valid for 30 days. Shipping and
tax are estimated" — so every amount here is named ``estimated_*`` and nothing
downstream may treat one as money owed.

The layout is a fixed single-page form, read with pypdf's ``layout`` mode:

        ORDER CONFIRMATION
    <vendor> (438)              ORDER NUMBER:  20635854
    <address>                     ORDER DATE:  07/23/2026
    <city>                       SALES PERSON:  037458
                                 CUSTOMER NO:  GARA01
     BILL TO:                SHIP TO:
     <us>                    <jobsite>
                             Lot No.:
                             CALLED IN BY:
      CUSTOMER P.O.          TERMS
      <po text>              <terms>
      ITEM DESCRIPTION          QTY ORDERED  UNIT  UNIT COST      COST
      <description>                       2    EA  $1,853.8700  $3,707.74
        Notes: CHI 4283 12x10 Black Long Panel, ...
                                ESTIMATED ORDER TOTAL          $3,707.74

Two columns share a line in several places (BILL TO/SHIP TO, CUSTOMER P.O./
TERMS). Those are read by finding the right-hand header's column offset and
slicing subsequent lines at it, rather than by guessing at whitespace runs —
a jobsite name with two spaces in it would defeat the latter.

``Lot No.`` and ``CALLED IN BY`` are captured even though they are empty on
every confirmation seen so far. They are the free fields this supplier already
offers, and they are where a GDX job number or CHI quote number would go if the
office starts putting one there — capturing them now means that link works the
day it starts being filled in rather than needing a parser change.

Failure kinds mirror the statement parser, for the same reason:

    MidwestOrderParseError      not an order confirmation — keep looking
    MidwestOrderStructureError  IS one, and we lost it — raise the alarm

The letterhead alone identifies a document as this supplier's, not as an order
confirmation — it is on their statements and invoices too. The
``ORDER CONFIRMATION`` title plus an ``ORDER NUMBER`` is what distinguishes one.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

from gdx_dispatch.modules.vendor_statements.parsers.midwest import VENDOR_LETTERHEAD

log = logging.getLogger(__name__)

PARSER_NAME = "midwest_order_v1"
PARSER_VERSION = 1

DOCUMENT_TITLE = "ORDER CONFIRMATION"

# Unit costs print to 4dp, so a few cents of rounding across several rows is
# expected; anything larger means a row was missed.
TOTALS_TOLERANCE = Decimal("0.05")


class MidwestOrderParseError(ValueError):
    """Not a Midwest order confirmation — the caller should keep looking."""


class MidwestOrderStructureError(MidwestOrderParseError):
    """It IS an order confirmation and the parser could not read it.

    Distinct from its parent so a caller walking a ladder of parsers can tell
    "not mine" (routine) from "mine, and I lost it" (a defect worth shouting
    about). Collapsing the two buries a real document among junk attachments.
    """


# Horizontal whitespace only. Plain `\s*` also matches the NEWLINE, so a label
# whose value is blank silently swallows the next line — the first run against
# real documents returned lot_no="CALLED IN BY:", which is the label underneath
# it, not a lot number. An empty field must read as empty.
_H = r"[^\S\r\n]*"
_ORDER_NUMBER = re.compile(rf"ORDER{_H}NUMBER:{_H}([^\r\n\s]+)", re.IGNORECASE)
_ORDER_DATE = re.compile(rf"ORDER{_H}DATE:{_H}([\d/]+)", re.IGNORECASE)
_SALESPERSON = re.compile(rf"SALES{_H}PERSON:{_H}([^\r\n\s]+)", re.IGNORECASE)
_CUSTOMER_NO = re.compile(rf"CUSTOMER{_H}NO:{_H}([^\r\n\s]+)", re.IGNORECASE)
_LOT_NO = re.compile(rf"Lot{_H}No\.?:{_H}([^\r\n]*)", re.IGNORECASE)
_CALLED_IN_BY = re.compile(rf"CALLED{_H}IN{_H}BY:{_H}([^\r\n]*)", re.IGNORECASE)

_MONEY = r"\$?-?[\d,]+\.\d{2,4}"
_SUBTOTAL = re.compile(rf"ESTIMATED\s+SUBTOTAL\s+({_MONEY})", re.IGNORECASE)
_SHIPPING = re.compile(rf"ESTIMATED\s+SHIPPING\s+({_MONEY})", re.IGNORECASE)
_TAX = re.compile(rf"ESTIMATED\s+TAX\s+({_MONEY})", re.IGNORECASE)
_TOTAL = re.compile(rf"ESTIMATED\s+ORDER\s+TOTAL\s+({_MONEY})", re.IGNORECASE)

# A detail row ends with: qty, unit, unit cost, extended cost.
_ITEM_ROW = re.compile(
    rf"""^\s*(?P<description>.+?)\s{{2,}}
         (?P<qty>[\d,]+(?:\.\d+)?)\s+
         (?P<unit>[A-Za-z]{{1,6}})\s+
         (?P<unit_cost>{_MONEY})\s+
         (?P<line_total>{_MONEY})\s*$
    """,
    re.VERBOSE,
)
_NOTES_ROW = re.compile(r"^\s*Notes:\s*(.+?)\s*$", re.IGNORECASE)


@dataclass
class ParsedOrderLine:
    line_no: int
    description: str
    notes: str | None
    quantity: Decimal
    unit: str | None
    unit_cost: Decimal
    line_total: Decimal


@dataclass
class ParsedOrder:
    order_number: str
    order_date: date | None
    customer_code: str | None
    salesperson: str | None
    ship_to: str | None
    customer_po: str | None
    terms: str | None
    lot_no: str | None
    called_in_by: str | None
    estimated_subtotal: Decimal
    estimated_shipping: Decimal
    estimated_tax: Decimal
    estimated_total: Decimal
    lines: list[ParsedOrderLine] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.lines)


def _money(raw: str | None, default: Decimal | None = None) -> Decimal:
    if raw is None:
        if default is None:
            raise MidwestOrderStructureError("missing amount")
        return default
    try:
        return Decimal(raw.replace("$", "").replace(",", "").strip())
    except InvalidOperation as exc:
        raise MidwestOrderStructureError(f"unreadable amount {raw!r}") from exc


# Section headings that live below the two-column rows. Scanning forward for a
# non-empty slice would happily return one of these when the field above is
# BLANK — an empty CUSTOMER P.O. returned "ITEM DESCRIPTION", the table header
# underneath it. A blank field must read as blank.
_NOT_A_VALUE = (
    "ITEM DESCRIPTION", "ESTIMATED", "NOTES:", "QTY ORDERED",
    "CUSTOMER P.O.", "TERMS", "LOT NO", "CALLED IN BY", "SHIP TO", "BILL TO",
)


def _looks_like_a_heading(value: str) -> bool:
    upper = value.upper()
    return any(marker in upper for marker in _NOT_A_VALUE)


def _column_value(lines: list[str], header_idx: int, col: int, *, left: bool) -> str | None:
    """First non-blank value beneath a column, or None if the field is empty.

    Stops at the first non-blank line rather than scanning on: on this form the
    value sits directly under its header, so anything further down belongs to
    the next section. Whatever it finds is rejected if it reads as a heading.
    """
    for probe in lines[header_idx + 1: header_idx + 3]:
        raw = probe[:col] if left else (probe[col:] if len(probe) > col else "")
        value = raw.strip()
        if not value:
            if probe.strip():
                # Line has content, just not in this column — the field is empty.
                return None
            continue  # wholly blank line, keep looking
        return None if _looks_like_a_heading(value) else value
    return None


def _right_column(lines: list[str], header_idx: int, marker: str) -> str | None:
    """Value under a right-hand column header.

    Two-column rows share a line, so a value belongs to whichever header it sits
    beneath. Slicing at the header's own column offset survives values that
    contain runs of spaces; splitting on whitespace does not.
    """
    col = lines[header_idx].upper().find(marker.upper())
    if col < 0:
        return None
    return _column_value(lines, header_idx, col, left=False)


def _left_column(lines: list[str], header_idx: int, right_marker: str) -> str | None:
    """Value under the LEFT header of a two-column row — everything to the left
    of where the right-hand header starts."""
    col = lines[header_idx].upper().find(right_marker.upper())
    if col < 0:
        return None
    return _column_value(lines, header_idx, col, left=True)


def parse_midwest_order(pdf_bytes: bytes) -> ParsedOrder:
    """Parse a Midwest order-confirmation PDF.

    Raises ``MidwestOrderParseError`` when the document isn't one (the caller's
    signal to try the next parser) and ``MidwestOrderStructureError`` when it is
    one but couldn't be read (a defect worth surfacing loudly).
    """
    if not pdf_bytes:
        raise MidwestOrderParseError("empty PDF bytes")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        raise MidwestOrderParseError(f"could not read PDF: {exc}") from exc

    text = ""
    for page in reader.pages:
        try:
            text += page.extract_text(extraction_mode="layout") + "\n"
        except Exception as exc:  # noqa: BLE001
            raise MidwestOrderParseError(f"layout extraction failed: {exc}") from exc

    if VENDOR_LETTERHEAD not in text:
        raise MidwestOrderParseError("PDF does not appear to be from this supplier")
    if DOCUMENT_TITLE not in text.upper():
        # Their letterhead is on statements and invoices too.
        raise MidwestOrderParseError("supplier letterhead but not an order confirmation")

    # Past this point the document has identified itself: every remaining
    # failure is structural.
    m_order = _ORDER_NUMBER.search(text)
    if not m_order:
        raise MidwestOrderStructureError("order confirmation with no ORDER NUMBER")
    order_number = m_order.group(1).strip()

    order_date = None
    if m := _ORDER_DATE.search(text):
        try:
            order_date = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError:
            order_date = None  # present but unreadable — still a real order

    lines = text.splitlines()

    ship_to = customer_po = terms = None
    for i, line in enumerate(lines):
        upper = line.upper()
        if ship_to is None and "SHIP TO:" in upper:
            ship_to = _right_column(lines, i, "SHIP TO:")
        if customer_po is None and "CUSTOMER P.O." in upper:
            # The row is `CUSTOMER P.O.   TERMS`; slice at TERMS.
            customer_po = _left_column(lines, i, "TERMS")
            terms = _right_column(lines, i, "TERMS")

    lot_no = called_in_by = None
    if m := _LOT_NO.search(text):
        lot_no = m.group(1).strip() or None
    if m := _CALLED_IN_BY.search(text):
        called_in_by = m.group(1).strip() or None

    parsed_lines: list[ParsedOrderLine] = []
    for raw in lines:
        if "ESTIMATED SUBTOTAL" in raw.upper():
            break
        m_item = _ITEM_ROW.match(raw)
        if not m_item:
            if parsed_lines and (m_note := _NOTES_ROW.match(raw)):
                # The Notes row carries the actual door spec (model, size,
                # colour, panel, glass, hardware) and belongs to the row above.
                parsed_lines[-1].notes = m_note.group(1).strip()
            continue
        description = m_item.group("description").strip()
        if description.upper().startswith("ITEM DESCRIPTION"):
            continue  # the column header itself
        parsed_lines.append(ParsedOrderLine(
            line_no=len(parsed_lines),
            description=description,
            notes=None,
            quantity=_money(m_item.group("qty")),
            unit=(m_item.group("unit") or "").strip() or None,
            unit_cost=_money(m_item.group("unit_cost")),
            line_total=_money(m_item.group("line_total")),
        ))

    m_total = _TOTAL.search(text)
    if m_total is None and not parsed_lines:
        raise MidwestOrderStructureError(
            "order confirmation with neither line items nor an order total"
        )

    m_subtotal = _SUBTOTAL.search(text)
    subtotal = _money(m_subtotal.group(1) if m_subtotal else None, Decimal("0.00"))
    shipping = _money(m.group(1) if (m := _SHIPPING.search(text)) else None, Decimal("0.00"))
    tax = _money(m.group(1) if (m := _TAX.search(text)) else None, Decimal("0.00"))
    total = _money(
        m_total.group(1) if m_total else None,
        sum((pl.line_total for pl in parsed_lines), Decimal("0.00")),
    )

    # Did we read every ROW? Compare what we parsed against what the supplier
    # printed as the subtotal.
    #
    # An earlier version compared printed subtotal+shipping+tax against the
    # printed total. That is the vendor checking their own arithmetic — every
    # term came from the document, so it could not detect a row the pattern
    # missed, which is the only failure that matters here. It also hard-failed a
    # perfectly good document that printed a fourth component such as a discount.
    #
    # The real risk is understatement: a missed freight or handling row makes a
    # commitment look smaller than it is. Only the line sum can catch that.
    lines_sum = sum((pl.line_total for pl in parsed_lines), Decimal("0.00"))
    if parsed_lines and m_subtotal is not None:
        drift = abs(lines_sum - subtotal)
        if drift > TOTALS_TOLERANCE:
            raise MidwestOrderStructureError(
                f"order {order_number}: parsed {len(parsed_lines)} line(s) summing "
                f"{lines_sum}, but the printed subtotal is {subtotal} "
                f"(off by {drift}) — a row was missed"
            )

    return ParsedOrder(
        order_number=order_number,
        order_date=order_date,
        customer_code=(m.group(1).strip() if (m := _CUSTOMER_NO.search(text)) else None),
        salesperson=(m.group(1).strip() if (m := _SALESPERSON.search(text)) else None),
        ship_to=ship_to,
        customer_po=customer_po,
        terms=terms,
        lot_no=lot_no,
        called_in_by=called_in_by,
        estimated_subtotal=subtotal,
        estimated_shipping=shipping,
        estimated_tax=tax,
        estimated_total=total,
        lines=parsed_lines,
    )
