"""Bank statement evidence layer — parser grammar, tie-out gate, overlap
integrity, import/void service, router surface.

Fixtures are SYNTHETIC (public-repo hygiene: real statements never enter the
repo) and mimic the **pypdf layout-mode** output shape — repeated page
headers, the quadruplicated fees block, the caption page, the bank's
``DESCRIPTIION`` typo — because that is the production extractor and its
output differs from pdftotext (plan §2.1). Coverage boundary, stated
honestly: the pypdf *extraction* step itself is exercised only by the
real-corpus acceptance run (plan §8); these tests exercise everything from
extracted text down. Service tests stub ``extract_pdf_text`` so "PDF bytes"
are fixture text.

The audited traps each have a dedicated test (plan §11): period-start
daily-balance seed row, period-aware restatement + continuity, wrap-glue
drift caught by overlap integrity, ``ACCT ENDING`` inside a loan
description, both transfer grammars, Dec→Jan year span, corruption cases
that MUST fail the gate.
"""
from __future__ import annotations

from datetime import date

import pytest
from starlette.requests import Request as StarletteRequest

from gdx_dispatch.modules.bank_feeds import statement_service
from gdx_dispatch.modules.bank_feeds.statement_models import (
    TIE_OUT_FAILED,
    TIE_OUT_PASSED,
    BankAccount,
    BankStatementImport,
    BankStatementLine,
    BankStatementLineSource,
)
from gdx_dispatch.modules.bank_feeds.statement_parsers import community_bank
from gdx_dispatch.modules.bank_feeds.statement_parsers.community_bank import (
    SECTION_CHECK,
    SECTION_DEBIT,
    SECTION_DEPOSIT,
    SECTION_INTEREST,
    SECTION_SERVICE_CHARGE,
    StatementParseError,
    StatementStructureError,
    parse_statement_text,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"sub": "tester", "tenant_id": COMPANY, "role": "admin"}

# ── synthetic fixtures (pypdf-layout shaped) ───────────────────────────

FEES_BLOCK = """
                                                                   Total For           Total
                                                                 This Period    Year-to-Date

   TOTAL OVERDRAFT FEES                                                 $.00             $.00

   TOTAL RETURNED ITEM FEES                                             $.00             $.00
"""

# pypdf emits the fees block repeatedly (overlapping draws) — bake the quirk in.
FEES_QUAD = FEES_BLOCK * 4

PAGE_HEADER = """                                            Date 6/30/26            Page     {page}
       SAMPLE FIXTURE CO                    PRIMARY ACCOUNT   ACCT ENDING {last4}
       123 EXAMPLE ROAD
       NOWHERE MN 00000

"""


def checking_text() -> str:
    """A full checking statement: wraps, page-break wrap continuation, the
    loan-description ACCT ENDING trap, both transfer grammars, -SC row,
    SC-detail section, checks-in-number-order, period-start daily seed row,
    caption page, quadruplicated fees block.

    Ledger (beginning 1,000.00):
      6/02 +500.00 deposit · 6/03 −100.00 card · 6/08 −50.00 loan autopay
      6/12 +250.00 transfer in · 6/13 −40.00 check 1083 · 6/15 −25.00 card
      6/27 −75.00 check 1062 · 6/30 −10.00 SC  → ending 1,450.00
    """
    return (
        "  PO BOX 1 Faketown, MN 00000\n"
        "\n"
        "           SAMPLE FIXTURE CO                                       Date 6/30/26             Page     1\n"
        "           SAMPLE OWNER                                            PRIMARY ACCOUNT    ACCT ENDING 9204\n"
        "           123 EXAMPLE ROAD\n"
        "           NOWHERE MN 00000\n"
        "\n"
        "  --------------------------------SUMMARY OF ACCOUNTS--------------------------------\n"
        "  ACCOUNT NUMBER ACCOUNT TITLE                             CURRENT BALANCE\n"
        "  ACCT ENDING 9204   BUSINESS CHECKING                          1,450.00\n"
        "  CHECKING ACCOUNT\n"
        "  BUSINESS CHECKING                                                                         0\n"
        "  ACCOUNT NUMBER                  ACCT ENDING 9204   Statement Dates   6/01/26 thru   6/30/26\n"
        "  BEGINNING BALANCE                       1,000.00   DAYS IN THE STATEMENT PERIOD          30\n"
        "      2 DEPOSITS/CREDITS                    750.00\n"
        "      5 CHECKS/DEBITS                       290.00\n"
        "  SERVICE CHARGE                             10.00\n"
        "  INTEREST PAID                                .00\n"
        "  ENDING BALANCE                          1,450.00\n"
        + FEES_QUAD +
        "  ACCOUNT SERVICE CHARGE\n"
        "  DATE     DESCRIPTION                                             AMOUNT\n"
        "   6/30     Monthly Fee in Service Charge                          10.00\n"
        "  DEPOSITS AND OTHER CREDITS\n"
        "  DATE      DESCRIPTION                                           AMOUNT\n"
        "   6/02     Deposit/Credit                                        500.00\n"
        "   6/12     Trnsfr Frm Act Ending in 7011                         250.00\n"
        "            Trnsfr To Act Ending in 9204\n"
        "  CHECKS AND OTHER DEBITS\n"
        "  DATE      DESCRIPTIION                                         AMOUNT\n"
        "   6/03     DBT CRD 1100 06/02/26 11111111                        100.00-\n"
        "            SAMPLE FUEL STOP\n"
        "            FAKETOWN      MN C#0001\n"
        "   6/08     Automatic Payment to Loan                              50.00-\n"
        "            Acct No. ACCT ENDING 7097\n"
        "\n"
        "Notice: See reverse side for important information                                    Member FDIC\n"
        + PAGE_HEADER.format(page=2, last4="9204") +
        "CHECKS AND OTHER DEBITS\n"
        "DATE      DESCRIPTIION                           AMOUNT\n"
        " 6/15     DBT CRD 0900 06/14/26 22222222           25.00-\n"
        "          SAMPLE HARDWARE\n"
        + PAGE_HEADER.format(page=3, last4="9204") +
        "CHECKS AND OTHER DEBITS\n"
        "DATE      DESCRIPTIION                           AMOUNT\n"
        "          WRAPVILLE      MN C#0001\n"
        " 6/30     Service Charge                           10.00-SC\n"
        "\n"
        "                       --- CHECKS IN NUMBER ORDER ---\n"
        "Date    Check No                Amount    Date    Check No                 Amount\n"
        " 6/27       1062                 75.00     6/13       1083*                40.00\n"
        "* Denotes missing check numbers\n"
        "\n"
        "DAILY BALANCE INFORMATION\n"
        "Date                Balance Date                Balance Date                 Balance\n"
        " 6/01             1,000.00   6/02             1,500.00   6/03              1,400.00\n"
        " 6/08             1,350.00   6/12             1,600.00   6/13              1,560.00\n"
        " 6/15             1,535.00   6/27             1,460.00   6/30              1,450.00\n"
        "                                                                                                 Page 4 of 4\n"
        "\n"
        "Check: 0 Amount: $500.00 Date: 6/2/2026   Check: 1062 Amount: $75.00 Date: 6/27/2026\n"
        "Check: 1083 Amount: $40.00 Date: 6/13/2026\n"
    )


def savings_text(*, period_start="4/01/26", period_end="6/30/26",
                 beginning="612.26", ending="112.74",
                 transfer_desc="Transfer from x9839 to x7040",
                 with_interest=True) -> str:
    """Savings form: blank summary counts, interest on the summary's extra
    columns, an Interest Deposit row inside DEPOSITS, the rate summary, and
    a period-start daily seed row (the audited no-transaction day)."""
    interest_amt = ".48" if with_interest else ".00"
    dep_section = (
        "  DEPOSITS AND OTHER CREDITS\n"
        "  DATE     DESCRIPTION                                             AMOUNT\n"
        "   6/30     Interest Deposit                                           .48\n"
    ) if with_interest else ""
    daily_tail = "   6/30               112.74\n" if with_interest else "\n"
    return (
        "  PO BOX 1 Faketown, MN 00000\n"
        "\n"
        "           SAMPLE OWNER                                              Date 6/30/26            Page     1\n"
        "           DBA SAMPLE FIXTURES                                       PRIMARY ACCOUNT   ACCT ENDING 9839\n"
        "\n"
        "  --------------------------------SUMMARY OF ACCOUNTS--------------------------------\n"
        "  ACCOUNT NUMBER ACCOUNT TITLE                             CURRENT BALANCE\n"
        f"  ACCT ENDING 9839   BUSINESS SAVINGS                             {ending}\n"
        "  SAVINGS ACCOUNT\n"
        "  BUSINESS SAVINGS                                                                           0\n"
        f"  ACCOUNT NUMBER                  ACCT ENDING 9839    Statement Dates   {period_start} thru   {period_end}\n"
        f"  BEGINNING BALANCE                         {beginning}    DAYS IN THE STATEMENT PERIOD          91\n"
        "        DEPOSITS/CREDITS                       .00\n"
        "      1 CHECKS/DEBITS                       500.00\n"
        f"  SERVICE CHARGE                               .00    Interest Earned                       {interest_amt}\n"
        f"  INTEREST PAID                                {interest_amt}    Annual Percentage Yield Earned       0.50%\n"
        f"  ENDING BALANCE                            {ending}    2026 Interest Paid                   1.23\n"
        "\n"
        "   Overdraft item fees year to date                                         $.00           $.00\n"
        "\n"
        "   Return item fees year to date                                            $.00           $.00\n"
        "\n"
        + dep_section +
        "  CHECKS AND OTHER DEBITS\n"
        "  DATE     DESCRIPTION                                             AMOUNT\n"
        f"   5/21     {transfer_desc}                            500.00-\n"
        "  DAILY BALANCE INFORMATION\n"
        "  DATE                BALANCE DATE                          BALANCE DATE                BALANCE\n"
        f"   4/01               {beginning}   5/21                         112.26{daily_tail}"
        "  INTEREST RATE SUMMARY\n"
        "                                   DATE              RATE\n"
        "                                    3/31                 0.500000%\n"
        "\n"
        "Notice: See reverse side for important information                                     Member FDIC\n"
    )


def savings_may_text() -> str:
    """The period the June statement RESTATES: 4/01–5/31, no interest."""
    return savings_text(period_start="4/01/26", period_end="5/31/26",
                        beginning="612.26", ending="112.26", with_interest=False)


# ── parser grammar ─────────────────────────────────────────────────────


def test_checking_statement_parses_completely():
    p = parse_statement_text(checking_text())
    assert (p.last4, p.kind) == ("9204", "checking")
    assert (p.period_start, p.period_end) == (date(2026, 6, 1), date(2026, 6, 30))
    assert (p.beginning_cents, p.ending_cents) == (100_000, 145_000)
    assert (p.deposits_count, p.deposits_total_cents) == (2, 75_000)
    assert (p.debits_count, p.debits_total_cents) == (5, 29_000)
    assert (p.service_charge_cents, p.interest_cents) == (1_000, 0)
    assert p.has_service_charge_detail and p.service_charge_detail_cents == 1_000

    by_section = {}
    for t in p.lines:
        by_section.setdefault(t.section, []).append(t)
    assert len(by_section[SECTION_DEPOSIT]) == 2
    assert len(by_section[SECTION_DEBIT]) == 3
    assert len(by_section[SECTION_CHECK]) == 2
    assert len(by_section[SECTION_SERVICE_CHARGE]) == 1

    # Transfer grammar #2 with its wrap glued
    transfer = next(t for t in p.lines if "Trnsfr Frm" in t.anchor)
    assert transfer.amount_cents == 25_000
    assert "Trnsfr To Act Ending in 9204" in transfer.description

    # The loan trap: ACCT ENDING inside a description, wrap glued, and the
    # account still resolved from the anchored summary line.
    loan = next(t for t in p.lines if "Automatic Payment to Loan" in t.anchor)
    assert "ACCT ENDING 7097" in loan.description
    assert p.last4 == "9204"

    # Cross-page-break wrap glue
    hardware = next(t for t in p.lines if "22222222" in t.anchor)
    assert "SAMPLE HARDWARE" in hardware.description
    assert "WRAPVILLE" in hardware.description

    checks = sorted(by_section[SECTION_CHECK], key=lambda t: t.txn_date)
    assert [(c.check_number, c.amount_cents) for c in checks] == [("1083", -4_000), ("1062", -7_500)]

    # Caption page must not have produced transactions
    assert len(p.lines) == 8
    assert len(p.daily_balances) == 9
    assert p.daily_balances[0] == (date(2026, 6, 1), 100_000)  # seed row


def test_savings_statement_interest_and_blank_count():
    p = parse_statement_text(savings_text())
    assert (p.last4, p.kind) == ("9839", "savings")
    assert p.deposits_count is None and p.deposits_total_cents == 0
    assert p.debits_count == 1 and p.debits_total_cents == 50_000
    assert p.interest_cents == 48
    sections = [t.section for t in p.lines]
    assert sections == [SECTION_INTEREST, SECTION_DEBIT]
    # Period-start seed row on a day with no transactions
    assert p.daily_balances[0] == (date(2026, 4, 1), 61_226)


def test_year_span_dec_to_jan():
    text = checking_text().replace(
        "Statement Dates   6/01/26 thru   6/30/26",
        "Statement Dates  12/01/26 thru   1/01/27",
    )
    p = parse_statement_text(text)
    assert p.period_start == date(2026, 12, 1)
    assert p.period_end == date(2027, 1, 1)
    # 6/xx rows resolve against period-end year (month 6 < start month 12)
    assert all(t.txn_date.year == 2027 for t in p.lines)


def test_not_a_statement_raises_parse_error():
    with pytest.raises(StatementParseError) as exc_info:
        parse_statement_text("INVOICE\nTotal due: 100.00\n")
    assert not isinstance(exc_info.value, StatementStructureError)


def test_debit_row_without_suffix_is_loud():
    text = checking_text().replace("           25.00-\n", "           25.00\n")
    with pytest.raises(StatementStructureError):
        parse_statement_text(text)


def test_wrap_with_no_open_transaction_is_loud():
    text = checking_text().replace(
        "   6/02     Deposit/Credit                                        500.00\n",
        "            Orphan wrap line\n"
        "   6/02     Deposit/Credit                                        500.00\n",
    )
    with pytest.raises(StatementStructureError):
        parse_statement_text(text)


# ── tie-out gate ───────────────────────────────────────────────────────


def _tie_out(text: str):
    return statement_service.compute_tie_out(parse_statement_text(text))


def _failed_names(report: dict) -> set[str]:
    return {c["name"] for c in report["checks"] if not c["ok"]}


def test_tie_out_passes_on_clean_fixtures():
    for text in (checking_text(), savings_text(), savings_may_text()):
        status, report = _tie_out(text)
        assert status == TIE_OUT_PASSED, _failed_names(report)


def test_tie_out_catches_missing_row():
    # Drop the 6/15 debit row + its wraps: summary still claims 5 debits.
    text = checking_text()
    for line in (" 6/15     DBT CRD 0900 06/14/26 22222222           25.00-\n",
                 "          SAMPLE HARDWARE\n",
                 "          WRAPVILLE      MN C#0001\n"):
        text = text.replace(line, "")
    status, report = _tie_out(text)
    assert status == TIE_OUT_FAILED
    assert {"debits_count", "debits_total", "daily_recompute", "ending_equation"} <= _failed_names(report)


def test_tie_out_catches_altered_daily_balance():
    text = checking_text().replace(" 6/12             1,600.00", " 6/12             1,600.01")
    status, report = _tie_out(text)
    assert status == TIE_OUT_FAILED
    assert _failed_names(report) == {"daily_recompute"}


def test_tie_out_catches_wrong_summary_count():
    text = checking_text().replace("      2 DEPOSITS/CREDITS", "      3 DEPOSITS/CREDITS")
    status, report = _tie_out(text)
    assert status == TIE_OUT_FAILED
    assert "deposits_count" in _failed_names(report)


def test_tie_out_catches_wrong_ending_balance():
    text = checking_text().replace(
        "  ENDING BALANCE                          1,450.00",
        "  ENDING BALANCE                          1,451.00",
    )
    status, report = _tie_out(text)
    assert status == TIE_OUT_FAILED
    assert {"summary_equation", "ending_equation"} <= _failed_names(report)


def test_occurrence_n_disambiguates_identical_twins():
    # Duplicate the 6/02 deposit (two identical rows), then shift every
    # amount that changes by +500: amount TOKENS are unique in the fixture,
    # so token replacement is spacing-proof (fragile column-aligned
    # replacements bit once already).
    text = checking_text().replace(
        "   6/02     Deposit/Credit                                        500.00\n",
        "   6/02     Deposit/Credit                                        500.00\n" * 2,
    ).replace("      2 DEPOSITS/CREDITS", "      3 DEPOSITS/CREDITS")
    for old, new in (
        ("750.00", "1,250.00"),      # summary deposits total
        ("1,500.00", "2,000.00"),    # daily 6/02 …
        ("1,400.00", "1,900.00"),
        ("1,350.00", "1,850.00"),
        ("1,600.00", "2,100.00"),
        ("1,560.00", "2,060.00"),
        ("1,535.00", "2,035.00"),
        ("1,460.00", "1,960.00"),
        ("1,450.00", "1,950.00"),    # … daily 6/30 + ENDING + title (all shift)
    ):
        text = text.replace(old, new)
    parsed = parse_statement_text(text)
    status, report = statement_service.compute_tie_out(parsed)
    assert status == TIE_OUT_PASSED, _failed_names(report)
    hashed = statement_service._hashed_lines(parsed)
    twins = [(o, h) for t, o, h in hashed if t.anchor == "Deposit/Credit"]
    assert [o for o, _h in twins] == [1, 2]
    assert len({h for _o, h in twins}) == 2


# ── import service (text-as-PDF via stubbed extractor) ─────────────────


@pytest.fixture
def svc(tenant_db, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(community_bank, "extract_pdf_text",
                        lambda pdf_bytes: pdf_bytes.decode("utf-8"))
    return tenant_db


def _import(db, text: str, filename="stmt.pdf"):
    return statement_service.import_statement(db, text.encode(), filename, imported_by="tester")


def test_import_creates_account_lines_attestations(svc):
    result = _import(svc, checking_text())
    assert result["status"] == "imported"
    assert result["tie_out_status"] == TIE_OUT_PASSED
    assert result["lines_added"] == 8 and result["lines_deduped"] == 0

    account = svc.query(BankAccount).one()
    assert account.last4 == "9204" and account.kind == "checking"
    assert svc.query(BankStatementLine).count() == 8
    assert svc.query(BankStatementLineSource).count() == 8
    imp = svc.query(BankStatementImport).one()
    assert imp.tie_out_status == TIE_OUT_PASSED
    assert imp.storage_path and imp.file_sha256


def test_duplicate_file_rejected(svc):
    first = _import(svc, checking_text())
    again = _import(svc, checking_text())
    assert again["status"] == "duplicate_file"
    assert again["import_id"] == first["import_id"]
    assert svc.query(BankStatementImport).count() == 1


def test_failed_tie_out_stores_report_but_no_lines(svc):
    bad = checking_text().replace(" 6/12             1,600.00", " 6/12             1,600.01")
    result = _import(svc, bad)
    assert result["status"] == "failed_tie_out"
    assert svc.query(BankStatementLine).count() == 0
    imp = svc.query(BankStatementImport).one()
    assert imp.tie_out_status == TIE_OUT_FAILED
    assert any(c["name"] == "daily_recompute" and not c["ok"]
               for c in imp.tie_out_report["checks"])


def test_parse_error_flavors(svc):
    not_ours = _import(svc, "INVOICE\nTotal due: 100.00\n")
    assert not_ours["status"] == "parse_error"
    assert not_ours["flavor"] == "not_recognized"
    assert "ACCOUNT NUMBER" in not_ours["error"]  # names the missing anchor
    broken = _import(svc, checking_text().replace("           25.00-\n", "           25.00\n"))
    assert broken["flavor"] == "structure"
    assert svc.query(BankStatementImport).count() == 0


def test_impossible_date_is_structure_error_not_500(svc):
    # Audit finding: a row dated 6/31 raised a bare ValueError that escaped
    # the service's except-arms as a 500 mid-batch. It must surface as a
    # structured parse_error result instead.
    text = checking_text().replace(
        "   6/02     Deposit/Credit",
        "   6/31     Deposit/Credit",
    )
    result = _import(svc, text)
    assert result["status"] == "parse_error"
    assert result["flavor"] == "structure"
    assert "6/31" in result["error"]
    assert svc.query(BankStatementImport).count() == 0


def test_restatement_dedupes_and_attests(svc):
    may = _import(svc, savings_may_text(), "may.pdf")
    assert may["status"] == "imported" and may["lines_added"] == 1

    june = _import(svc, savings_text(), "june.pdf")
    assert june["status"] == "imported"
    # The 5/21 transfer dedupes against May; the interest row is new.
    assert june["lines_added"] == 1 and june["lines_deduped"] == 1

    transfer = svc.query(BankStatementLine).filter_by(section=SECTION_DEBIT).one()
    assert svc.query(BankStatementLineSource).filter_by(line_id=transfer.id).count() == 2

    # Period-aware continuity: June's predecessor is a 3/31-ending statement
    # (absent here) — the *overlapping* May import must NOT be flagged.
    assert june["continuity_warnings"] == []


def test_overlap_drift_fails_loudly(svc):
    _import(svc, savings_may_text(), "may.pdf")
    drifted = savings_text(transfer_desc="Transfer from x9839 to x9999")
    result = _import(svc, drifted, "june.pdf")
    assert result["status"] == "failed_tie_out"
    imp = svc.get(BankStatementImport, __import__("uuid").UUID(result["import_id"]))
    overlap = next(c for c in imp.tie_out_report["checks"] if c["name"] == "overlap_integrity")
    assert not overlap["ok"]
    assert overlap["actual"][0]["description_drift"]
    # The drifted import contributed nothing.
    assert svc.query(BankStatementLine).count() == 1


def test_void_respects_co_attestation(svc):
    may = _import(svc, savings_may_text(), "may.pdf")
    june = _import(svc, savings_text(), "june.pdf")

    may_imp = svc.get(BankStatementImport, __import__("uuid").UUID(may["import_id"]))
    result = statement_service.void_import(svc, may_imp)
    # June still attests the shared transfer line — it must survive.
    assert result == {"status": "voided", "import_id": may["import_id"],
                      "lines_removed": 0, "lines_kept_co_attested": 1}
    assert svc.query(BankStatementLine).count() == 2

    june_imp = svc.get(BankStatementImport, __import__("uuid").UUID(june["import_id"]))
    result = statement_service.void_import(svc, june_imp)
    assert result["lines_removed"] == 2
    assert svc.query(BankStatementLine).count() == 0

    # Voided imports release their file hash: the same bytes re-import.
    again = _import(svc, savings_may_text(), "may.pdf")
    assert again["status"] == "imported"


def test_continuity_warning_on_adjacent_mismatch(svc):
    _import(svc, savings_may_text(), "may.pdf")
    # Adjacent successor (6/01 …) whose beginning ≠ May's ending.
    june_adj = savings_text(period_start="6/01/26", period_end="6/30/26",
                            beginning="112.74", ending="113.22",
                            with_interest=True)
    june_adj = june_adj.replace(
        "      1 CHECKS/DEBITS                       500.00",
        "        CHECKS/DEBITS                          .00",
    ).replace(
        "   5/21     Transfer from x9839 to x7040                            500.00-\n", ""
    ).replace(
        # NB: generated with beginning="112.74", so the daily line's first
        # cell is 112.74 (not the default 612.26) — match what's generated.
        "   4/01               112.74   5/21                         112.26   6/30               112.74\n",
        "   6/01               112.74   6/30                         113.22\n",
    )
    assert "5/21" not in june_adj  # both surgeries actually applied
    result = _import(svc, june_adj, "june.pdf")
    assert result["status"] == "imported"
    assert any(w["kind"] == "continuity_mismatch_predecessor"
               for w in result["continuity_warnings"])


# ── router surface (direct-call house style) ───────────────────────────


def _request():
    scope = {
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 80), "state": {},
    }
    req = StarletteRequest(scope)
    req.state.tenant = {"id": COMPANY}
    return req


def test_router_import_list_detail_void_lines(svc):
    import io

    from starlette.datastructures import UploadFile as StarletteUploadFile

    from gdx_dispatch.modules.bank_feeds import router as r

    upload = StarletteUploadFile(io.BytesIO(checking_text().encode()), filename="june.pdf")
    out = r.import_statements(_request(), [upload], USER, None, svc)
    assert out["results"][0]["status"] == "imported"
    import_id = out["results"][0]["import_id"]

    listing = r.list_statement_imports(None, False, None, svc)
    assert [i["id"] for i in listing["items"]] == [import_id]
    assert listing["items"][0]["tie_out_status"] == TIE_OUT_PASSED

    detail = r.get_statement_import(import_id, None, svc)
    assert detail["tie_out_report"]["checks"]

    accounts = r.list_statement_accounts(None, svc)
    assert accounts["items"][0]["last4"] == "9204"
    patched = r.patch_statement_account(
        accounts["items"][0]["id"], r.StatementAccountPatch(name="Main Checking"),
        _request(), USER, None, svc,
    )
    assert patched["name"] == "Main Checking"

    lines = r.list_statement_lines(
        account_id=accounts["items"][0]["id"], date_from=None, date_to=None,
        section=None, q="loan", limit=100, offset=0, _perm=None, db=svc,
    )
    assert lines["total"] == 1
    assert "ACCT ENDING 7097" in lines["items"][0]["description"]

    voided = r.void_statement_import(import_id, _request(), USER, None, svc)
    assert voided["status"] == "voided" and voided["lines_removed"] == 8

    listing = r.list_statement_imports(None, False, None, svc)
    assert listing["items"] == []
    listing = r.list_statement_imports(None, True, None, svc)
    assert len(listing["items"]) == 1
