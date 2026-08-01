"""Community-bank statement PDF parser (Fiserv-style layout).

The real institution's name is deliberately NOT in this public repo; the
neutral ``INSTITUTION`` label below is the account-registry key, and the
display name on a ``bank_accounts`` row is the operator's to set. Extraction is pypdf ``layout`` mode —
NOT pdftotext, whose output differs (the audited pypdf quirks: the
overdraft/returned-fees block can be emitted several times per page from
overlapping draws, and column offsets shift between the two tools). The
grammar is therefore strictly per-line regex; nothing keys on column
offsets, and furniture is skipped statelessly so repetition is harmless.

Section state machine. Section headers repeat on every page and re-affirm
state rather than reset it — a transaction description can WRAP across a
page break, resuming after the repeated page header + section header +
column header, and must glue back onto its anchor row.

Amount grammar: ``M/DD  DESCRIPTION  AMOUNT[-|-SC]``. Trailing ``-`` =
debit, ``-SC`` = the service-charge row (which the summary counts OUTSIDE
the deposits/debits counts, as it does interest). Amounts may lack a
leading zero (``.48``). Dates lack a year — resolved from the statement
period (month earlier than the period-start month ⇒ period-end year, for
Dec→Jan spans).

Traps this grammar was audited against (docs/design/
bank-statement-import-plan.md §11):
- ``ACCT ENDING nnnn`` appears INSIDE loan-autopay descriptions — account
  resolution anchors on the one summary line that also carries
  ``Statement Dates``.
- The check-image caption page (``Check: N Amount: $X Date: D``) images
  deposit slips as ``Check: 0`` and must never parse as transactions.
- The bank's own debits column header is misspelled ``DESCRIPTIION``.
- The daily-balance table may open with a period-start seed row on a day
  with no transactions (equal to the beginning balance).

Error flavors follow the Midwest supplier-statement precedent
(``vendor_statements/parsers/midwest.py``): ``StatementParseError`` means
"not this bank's statement layout — not mine"; its subclass
``StatementStructureError`` means "it IS one and the structure defeated
the parser", which is a defect and must stay loud.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from pypdf import PdfReader

log = logging.getLogger(__name__)

PARSER_NAME = "community_bank_v1"

INSTITUTION = "Primary Bank"

# Line-level evidence sections (mirrors statement_models.VALID_SECTIONS).
SECTION_DEPOSIT = "deposit"
SECTION_DEBIT = "debit"
SECTION_CHECK = "check"
SECTION_SERVICE_CHARGE = "service_charge"
SECTION_INTEREST = "interest"


class StatementParseError(ValueError):
    """Not a parseable statement in this bank's layout — routine 'not mine'."""


class StatementStructureError(StatementParseError):
    """IS a statement in this bank's layout, and the structure defeated the parser.

    Loud on purpose — a caller must never file this next to junk uploads.
    """


@dataclass
class CaptionEntry:
    """One check-image caption from the trailing images page(s):
    ``Check: N Amount: $X Date: M/D/YYYY``. ``check_no`` "0" = a deposit
    ticket (the bank's virtual BUSINESS DEPOSIT document); nonzero = a
    written check. Captions carry FULL dates (unlike transaction rows)."""

    check_no: str
    amount_cents: int
    caption_date: date


@dataclass
class ParsedTxn:
    txn_date: date
    amount_cents: int          # signed; negative = money out
    anchor: str                # the first physical line's description, verbatim
    description: str           # anchor + glued wrap lines
    section: str
    check_number: str | None = None


@dataclass
class ParsedStatement:
    last4: str
    account_title: str
    kind: str                  # checking | savings | other
    period_start: date
    period_end: date
    beginning_cents: int
    ending_cents: int
    deposits_count: int | None     # None when the summary prints no count
    deposits_total_cents: int
    debits_count: int | None
    debits_total_cents: int
    service_charge_cents: int
    interest_cents: int
    lines: list[ParsedTxn] = field(default_factory=list)
    daily_balances: list[tuple[date, int]] = field(default_factory=list)
    # Image captions in reading order (see CaptionEntry). NOT transactions —
    # they duplicate deposits/checks already listed — and they never enter
    # the tie-out arithmetic; they exist to pair extracted check/slip
    # images to evidence lines.
    captions: list[CaptionEntry] = field(default_factory=list)
    # ACCOUNT SERVICE CHARGE detail rows (fee breakdown; NOT evidence lines —
    # the -SC row in the debits section is the charge; this only cross-checks).
    service_charge_detail_cents: int = 0
    has_service_charge_detail: bool = False


# ── anchors & row grammars ─────────────────────────────────────────────

# THE account-resolution anchor: the only line trusted for last4 + period.
_ACCOUNT_LINE = re.compile(
    r"ACCOUNT NUMBER\s+ACCT ENDING\s+(?P<last4>\d{4})\s+"
    r"Statement Dates\s+(?P<start>\d{1,2}/\d{2}/\d{2})\s+thru\s+(?P<end>\d{1,2}/\d{2}/\d{2})"
)

_AMOUNT = r"[\d,]*\.\d{2}"

# Summary-of-accounts title row: "ACCT ENDING 9204   BUSINESS CHECKING   1,450.00"
_TITLE_ROW = re.compile(rf"^\s*ACCT ENDING\s+\d{{4}}\s+(?P<title>.+?)\s+{_AMOUNT}\s*$")

_BEGINNING = re.compile(rf"BEGINNING BALANCE\s+(?P<amt>{_AMOUNT})")
_ENDING = re.compile(rf"ENDING BALANCE\s+(?P<amt>{_AMOUNT})")
_DEPOSITS = re.compile(rf"^\s*(?P<count>\d*)\s*DEPOSITS/CREDITS\s+(?P<amt>{_AMOUNT})")
_DEBITS = re.compile(rf"^\s*(?P<count>\d*)\s*CHECKS/DEBITS\s+(?P<amt>{_AMOUNT})")
_SERVICE_CHARGE = re.compile(rf"^\s*SERVICE CHARGE\s+(?P<amt>{_AMOUNT})")
_INTEREST = re.compile(rf"^\s*INTEREST PAID\s+(?P<amt>{_AMOUNT})")

# Transaction row: date, description, amount, optional -/-SC suffix. No
# leading-indent bound — pypdf's column offsets differ from pdftotext's,
# and inside a transaction section every date-led amount row IS a row.
_TXN_ROW = re.compile(
    rf"^\s*(?P<date>\d{{1,2}}/\d{{2}})\s+(?P<desc>.+?)\s+(?P<amt>{_AMOUNT})(?P<suffix>-SC|-)?\s*$"
)

# CHECKS IN NUMBER ORDER: two (date, check#, amount) tuples per line;
# '*' after the number marks a break in sequence, not part of the number.
_CHECK_TUPLE = re.compile(rf"(\d{{1,2}}/\d{{2}})\s+(\d{{3,8}})\*?\s+({_AMOUNT})")

# DAILY BALANCE INFORMATION: up to three (date, balance) pairs per line.
_DAILY_PAIR = re.compile(rf"(\d{{1,2}}/\d{{2}})\s+({_AMOUNT})")

# ── section headers (repeat per page; re-affirm state) ─────────────────
_SECTIONS = (
    (re.compile(r"^\s*ACCOUNT SERVICE CHARGE\s*$"), "sc_detail"),
    (re.compile(r"^\s*DEPOSITS AND OTHER CREDITS\s*$"), "deposits"),
    (re.compile(r"^\s*CHECKS AND OTHER DEBITS\s*$"), "debits"),
    (re.compile(r"^\s*-*\s*CHECKS IN NUMBER ORDER\s*-*\s*$"), "checks"),
    (re.compile(r"^\s*DAILY BALANCE INFORMATION\s*$"), "daily"),
    (re.compile(r"^\s*INTEREST RATE SUMMARY\s*$"), "rate_summary"),
)

# ── stateless furniture (safe to see any number of times, any state) ───
_SKIP = (
    re.compile(r"^\s*DATE\s+DESCRIPTI?ION\s+AMOUNT\s*$"),          # bank's typo included
    re.compile(r"^\s*Date\s+Check No\b"),
    re.compile(r"^\s*(?:Date|DATE)\s+(?:Balance|BALANCE)\b"),
    re.compile(r"^\s*Notice: See reverse side"),
    re.compile(r"Member FDIC\s*$"),
    re.compile(r"^\s*Page\s+\d+\s+of\s+\d+\s*$"),
    re.compile(r"^\s*\* Denotes missing check numbers"),
    # Fees block — pypdf may emit it repeatedly (overlapping draws).
    re.compile(r"^\s*Total For\b|^\s*This Period\b"),
    re.compile(r"TOTAL (?:OVERDRAFT|RETURNED ITEM) FEES"),
    re.compile(r"^\s*(?:Overdraft|Return) item fees year to date"),
    re.compile(r"^\s*DATE\s+RATE\s*$"),                            # rate-summary header
)

# Page-header block opener: "…  Date 6/30/26  Page 2". Everything until the
# next blank line is name/address furniture (which would otherwise look like
# wrap text).
_PAGE_HEADER = re.compile(r"Date\s+\d{1,2}/\d{1,2}/\d{2}\s+Page\s+\d+")

_INTEREST_DEPOSIT_DESC = re.compile(r"^interest deposit$", re.IGNORECASE)

# Image-page caption: "Check: N Amount: $X Date: M/D/YYYY", up to three per
# line. Full dates, unlike transaction rows. Parsed (not skipped) so the
# extracted check/slip images can be paired to evidence lines; NEVER counted
# as transactions.
_CAPTION = re.compile(
    r"Check:\s*(?P<check_no>\d+)\s+Amount:\s*\$(?P<amt>[\d,]+\.\d{2})\s+"
    r"Date:\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})"
)


def _cents(s: str) -> int:
    return int(Decimal(s.replace(",", "")) * 100)


def _short_date(s: str, period_start: date, period_end: date) -> date:
    """Resolve M/DD (no year) against the statement period.

    Raises StatementStructureError (never a bare ValueError) on an
    impossible date — by the time this runs the document is a confirmed
    statement, and a bare ValueError would escape the import service's
    except-arms as a 500 (found by the pre-commit adversarial audit).
    """
    month_s, day_s = s.split("/")
    month, day = int(month_s), int(day_s)
    year = period_start.year if month >= period_start.month else period_end.year
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise StatementStructureError(f"impossible transaction date {s!r}: {exc}") from exc


def _summary_field(pattern: re.Pattern, lines: list[str], label: str) -> re.Match:
    for line in lines:
        if m := pattern.search(line):
            return m
    raise StatementStructureError(f"summary field not found: {label}")


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """pypdf layout-mode extraction — the production extractor (see module
    docstring for why fixtures must mimic ITS output, not pdftotext's)."""
    if not pdf_bytes:
        raise StatementParseError("empty PDF bytes")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        raise StatementParseError(f"could not read PDF: {exc}") from exc
    text = ""
    for page in reader.pages:
        try:
            text += page.extract_text(extraction_mode="layout") + "\n"
        except Exception as exc:  # noqa: BLE001
            raise StatementParseError(f"layout extraction failed: {exc}") from exc
    return text


def parse_statement_pdf(pdf_bytes: bytes) -> ParsedStatement:
    return parse_statement_text(extract_pdf_text(pdf_bytes))


# Empirically verified against the real corpus: caption pages carry one
# scanned document per caption (deposit tickets ~1312×600, written checks
# ~1200×550) plus blank filler pages (~1020×1320, pure white) and the small
# repeated bank logo (<500px wide). Blank + logo are furniture.
_MIN_IMAGE_WIDTH = 500


def extract_caption_images(pdf_bytes: bytes) -> list:
    """Extract the check/deposit-ticket scans from the trailing images
    page(s), as PIL images in caption order. Pages qualify by containing
    caption text; furniture (logo, blank backs) is filtered by width and
    blankness. Returns [] when the statement has no images page (savings).

    NOTE: these scans show full account numbers and signatures — callers
    must store them under the private uploads tree only, never anywhere
    public.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001
        raise StatementParseError(f"could not read PDF: {exc}") from exc

    images = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            page_text = ""
        if not _CAPTION.search(page_text):
            continue
        for pdf_image in page.images:
            try:
                pil = pdf_image.image
            except Exception:  # noqa: BLE001 — undecodable codec (e.g. JBIG2)
                log.debug("skipping undecodable statement image", exc_info=True)
                continue
            if pil is None or pil.width < _MIN_IMAGE_WIDTH:
                continue
            # Alpha-flatten onto white BEFORE the blankness check: a blank
            # RGBA filler converts to L with black transparent pixels and
            # would sneak past a bare extrema test (PR-2 audit finding).
            if pil.mode in ("RGBA", "LA", "PA"):
                from PIL import Image as _Image

                flat = _Image.new("RGB", pil.size, (255, 255, 255))
                flat.paste(pil.convert("RGBA"), mask=pil.convert("RGBA").split()[-1])
                pil = flat
            lo, _hi = pil.convert("L").getextrema()
            if lo >= 250:  # pure-white filler page
                continue
            images.append(pil)
    return images


def parse_statement_text(text: str) -> ParsedStatement:  # noqa: C901
    lines = text.splitlines()

    # ── identification + account resolution (anchored — NEVER a global
    # "ACCT ENDING" search: loan descriptions contain that string too) ──
    account_m = None
    for line in lines:
        if m := _ACCOUNT_LINE.search(line):
            account_m = m
            break
    if account_m is None:
        raise StatementParseError(
            "no 'ACCOUNT NUMBER … ACCT ENDING nnnn Statement Dates …' anchor — "
            "not this bank's statement layout"
        )
    last4 = account_m.group("last4")
    try:
        period_start = datetime.strptime(account_m.group("start"), "%m/%d/%y").date()
        period_end = datetime.strptime(account_m.group("end"), "%m/%d/%y").date()
    except ValueError as exc:
        raise StatementStructureError(f"unparseable statement period: {exc}") from exc

    title = "BANK ACCOUNT"
    for line in lines:
        if m := _TITLE_ROW.match(line):
            title = m.group("title").strip()
            break
    kind = "checking" if "CHECKING" in title else "savings" if "SAVINGS" in title else "other"

    # ── summary block (first match wins; summary precedes all sections) ──
    beginning = _cents(_summary_field(_BEGINNING, lines, "BEGINNING BALANCE").group("amt"))
    ending = _cents(_summary_field(_ENDING, lines, "ENDING BALANCE").group("amt"))
    dep_m = _summary_field(_DEPOSITS, lines, "DEPOSITS/CREDITS")
    deb_m = _summary_field(_DEBITS, lines, "CHECKS/DEBITS")
    deposits_count = int(dep_m.group("count")) if dep_m.group("count") else None
    deposits_total = _cents(dep_m.group("amt"))
    debits_count = int(deb_m.group("count")) if deb_m.group("count") else None
    debits_total = _cents(deb_m.group("amt"))
    service_charge = _cents(_summary_field(_SERVICE_CHARGE, lines, "SERVICE CHARGE").group("amt"))
    interest = _cents(_summary_field(_INTEREST, lines, "INTEREST PAID").group("amt"))

    parsed = ParsedStatement(
        last4=last4,
        account_title=title,
        kind=kind,
        period_start=period_start,
        period_end=period_end,
        beginning_cents=beginning,
        ending_cents=ending,
        deposits_count=deposits_count,
        deposits_total_cents=deposits_total,
        debits_count=debits_count,
        debits_total_cents=debits_total,
        service_charge_cents=service_charge,
        interest_cents=interest,
    )

    # ── walk the sections ──────────────────────────────────────────────
    state: str | None = None
    open_txn: ParsedTxn | None = None
    in_page_header = False

    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()

        if in_page_header:
            if not stripped:
                in_page_header = False
            continue
        if _PAGE_HEADER.search(line):
            in_page_header = True
            continue
        if not stripped:
            continue
        if "Check:" in line and (caption_hits := _CAPTION.findall(line)):
            for check_no, amt, full_date in caption_hits:
                try:
                    cap_date = datetime.strptime(full_date, "%m/%d/%Y").date()
                except ValueError as exc:
                    raise StatementStructureError(f"bad caption date {full_date!r}") from exc
                parsed.captions.append(
                    CaptionEntry(check_no=check_no, amount_cents=_cents(amt), caption_date=cap_date)
                )
            continue
        if any(p.search(line) for p in _SKIP):
            continue

        matched_section = False
        for pattern, section_state in _SECTIONS:
            if pattern.match(line):
                if section_state != state:
                    # A genuine section CHANGE closes any open row; a
                    # re-affirmed header (page break, same section) keeps it
                    # open so cross-page wraps glue back correctly.
                    open_txn = None
                state = section_state
                matched_section = True
                break
        if matched_section:
            continue

        if state is None or state == "rate_summary":
            # Letterhead, summary block, fees block, rate table — not rows.
            continue

        if state == "daily":
            pairs = _DAILY_PAIR.findall(line)
            if not pairs:
                raise StatementStructureError(f"unrecognized line in daily-balance table: {line!r}")
            for d, amt in pairs:
                parsed.daily_balances.append((_short_date(d, period_start, period_end), _cents(amt)))
            continue

        if state == "checks":
            tuples = _CHECK_TUPLE.findall(line)
            if not tuples:
                raise StatementStructureError(f"unrecognized line in checks-in-number-order: {line!r}")
            for d, check_no, amt in tuples:
                txn_date = _short_date(d, period_start, period_end)
                desc = f"Check {check_no}"
                parsed.lines.append(
                    ParsedTxn(
                        txn_date=txn_date,
                        amount_cents=-_cents(amt),
                        anchor=desc,
                        description=desc,
                        section=SECTION_CHECK,
                        check_number=check_no,
                    )
                )
            continue

        # states: deposits / debits / sc_detail — txn rows + wrap lines
        m = _TXN_ROW.match(line)
        if m:
            txn_date = _short_date(m.group("date"), period_start, period_end)
            desc = m.group("desc").strip()
            amount = _cents(m.group("amt"))
            suffix = m.group("suffix")

            if state == "sc_detail":
                # Fee breakdown — cross-check only; the -SC row is the charge.
                parsed.service_charge_detail_cents += amount
                parsed.has_service_charge_detail = True
                open_txn = None
                continue

            if suffix == "-SC":
                section, signed = SECTION_SERVICE_CHARGE, -amount
            elif suffix == "-":
                section, signed = SECTION_DEBIT, -amount
            else:
                if state == "debits":
                    raise StatementStructureError(
                        f"debit-section row without '-' suffix: {line!r}"
                    )
                if _INTEREST_DEPOSIT_DESC.match(desc):
                    section, signed = SECTION_INTEREST, amount
                else:
                    section, signed = SECTION_DEPOSIT, amount

            open_txn = ParsedTxn(
                txn_date=txn_date,
                amount_cents=signed,
                anchor=desc,
                description=desc,
                section=section,
            )
            parsed.lines.append(open_txn)
            continue

        # Wrap line — glue to the open anchor row (survives page breaks
        # because section headers re-affirm rather than reset state).
        if state == "sc_detail":
            continue
        if open_txn is None:
            raise StatementStructureError(
                f"wrap text with no open transaction in section {state!r}: {line!r}"
            )
        open_txn.description += "\n" + stripped

    if not parsed.lines and (deposits_total or debits_total or service_charge or interest):
        raise StatementStructureError("summary reports activity but no transaction rows parsed")

    return parsed
