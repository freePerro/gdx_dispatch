"""Midwest Wholesale Doors statement-of-account parser (v1).

The PDF Midwest sends GDX is a layout-formatted table. We extract text via
pypdf's `layout` extraction_mode, which preserves spatial alignment, then
walk the lines pairwise:

    line A:  <invoice#> <rep> MM/DD/YYYY $amount $balance $0/29 $30/59 $60/89 $90/119 $120+ $retainage
    line B:  <job#> <po_text> / 58 <description>

The constant `58` between the slash and description is Midwest's branch code
and appears on every row in this dataset; we use it as the join anchor.

Parser is deliberately strict: any line that looks like an invoice row but
fails to parse raises `MidwestParseError`. Slice-1 has no recovery; we'd
rather fail loudly and have Doug see the broken statement than silently lose
line items.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pypdf import PdfReader

log = logging.getLogger(__name__)

PARSER_NAME = "midwest_v1"
PARSER_VERSION = 1


class MidwestParseError(ValueError):
    """Raised when a Midwest statement PDF cannot be parsed."""


_MONEY = r"\$[\d,]+\.\d{2}"

# Line A: invoice#, rep, date, amount, balance, 6 aging buckets
_LINE_A = re.compile(
    rf"""^\s*
        (?P<invoice>\d{{6,}})\s+
        (?P<rep>\S+)\s+
        (?P<date>\d{{2}}/\d{{2}}/\d{{4}})\s+
        (?P<amount>{_MONEY})\s+
        (?P<balance>{_MONEY})\s+
        (?P<a0_29>{_MONEY})\s+
        (?P<a30_59>{_MONEY})\s+
        (?P<a60_89>{_MONEY})\s+
        (?P<a90_119>{_MONEY})\s+
        (?P<a120>{_MONEY})\s+
        (?P<retainage>{_MONEY})\s*$
    """,
    re.VERBOSE,
)

# Line B: job#, po_text, /, 58, description
_LINE_B = re.compile(
    r"""^\s*
        (?P<job>\d{6,})\s+
        (?P<po>.+?)\s+/\s+58\s+
        (?P<description>.+?)\s*$
    """,
    re.VERBOSE,
)

_STATEMENT_DATE = re.compile(r"STATEMENT\s+DATE:\s*([\d/]+)", re.IGNORECASE)
_CUSTOMER_CODE = re.compile(r"CUSTOMER\s+CODE:\s*(\S+)", re.IGNORECASE)


@dataclass
class MidwestParsedLine:
    line_no: int
    invoice_no: str
    job_no: str
    rep: str
    line_date: date
    amount: Decimal
    balance: Decimal
    aging_0_29: Decimal
    aging_30_59: Decimal
    aging_60_89: Decimal
    aging_90_119: Decimal
    aging_120_plus: Decimal
    retainage: Decimal
    po_ref: str
    description: str
    raw_text: str

    @property
    def aging_bucket(self) -> str:
        """Pick the bucket where the unpaid balance lives.

        Uses the largest non-zero aging value. If everything's zero, returns
        'current'. If two buckets tie (rare), returns the older one.
        """
        buckets = [
            ("retainage", self.retainage),
            ("120+", self.aging_120_plus),
            ("90-119", self.aging_90_119),
            ("60-89", self.aging_60_89),
            ("30-59", self.aging_30_59),
            ("0-29", self.aging_0_29),
        ]
        for name, value in buckets:
            if value > 0:
                return name
        return "current"


@dataclass
class MidwestParseResult:
    statement_date: Optional[date]
    customer_code: Optional[str]
    raw_total: Decimal
    lines: list[MidwestParsedLine] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.lines)


def _money(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", ""))


def _looks_like_line_a(line: str) -> bool:
    """Cheap pre-filter — line starts with an invoice-shaped number AND has 8 currency tokens."""
    stripped = line.strip()
    if not stripped or not stripped[0].isdigit():
        return False
    return len(re.findall(_MONEY, stripped)) == 8


def parse_midwest_statement(pdf_bytes: bytes) -> MidwestParseResult:
    """Parse a Midwest statement PDF into a structured result.

    Raises `MidwestParseError` if the file isn't a parseable Midwest statement.
    """
    if not pdf_bytes:
        raise MidwestParseError("empty PDF bytes")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        raise MidwestParseError(f"could not read PDF: {exc}") from exc

    text = ""
    for page in reader.pages:
        try:
            text += page.extract_text(extraction_mode="layout") + "\n"
        except Exception as exc:  # noqa: BLE001
            raise MidwestParseError(f"layout extraction failed: {exc}") from exc

    if "Midwest Wholesale Doors" not in text:
        raise MidwestParseError("PDF does not appear to be a Midwest Wholesale Doors statement")

    statement_date = None
    if m := _STATEMENT_DATE.search(text):
        try:
            statement_date = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError:
            statement_date = None

    customer_code = None
    if m := _CUSTOMER_CODE.search(text):
        customer_code = m.group(1).strip()

    lines = text.splitlines()
    parsed: list[MidwestParsedLine] = []
    raw_total = Decimal("0.00")
    line_no = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _looks_like_line_a(line):
            i += 1
            continue

        m_a = _LINE_A.match(line)
        if not m_a:
            i += 1
            continue

        # Find next non-blank line for line B
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            raise MidwestParseError(f"line A at index {i} has no following line B")

        line_b = lines[j]
        m_b = _LINE_B.match(line_b)
        if not m_b:
            raise MidwestParseError(
                f"could not parse line B for invoice {m_a.group('invoice')}: {line_b!r}"
            )

        try:
            parsed_line = MidwestParsedLine(
                line_no=line_no,
                invoice_no=m_a.group("invoice"),
                job_no=m_b.group("job"),
                rep=m_a.group("rep"),
                line_date=datetime.strptime(m_a.group("date"), "%m/%d/%Y").date(),
                amount=_money(m_a.group("amount")),
                balance=_money(m_a.group("balance")),
                aging_0_29=_money(m_a.group("a0_29")),
                aging_30_59=_money(m_a.group("a30_59")),
                aging_60_89=_money(m_a.group("a60_89")),
                aging_90_119=_money(m_a.group("a90_119")),
                aging_120_plus=_money(m_a.group("a120")),
                retainage=_money(m_a.group("retainage")),
                po_ref=m_b.group("po").strip(),
                description=m_b.group("description").strip(),
                raw_text=line.strip() + "\n" + line_b.strip(),
            )
        except Exception as exc:  # noqa: BLE001
            raise MidwestParseError(
                f"failed to build parsed line for invoice {m_a.group('invoice')}: {exc}"
            ) from exc

        parsed.append(parsed_line)
        raw_total += parsed_line.balance
        line_no += 1
        i = j + 1

    if not parsed:
        raise MidwestParseError("no line items found in PDF")

    return MidwestParseResult(
        statement_date=statement_date,
        customer_code=customer_code,
        raw_total=raw_total,
        lines=parsed,
    )
