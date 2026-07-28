"""Vendor account view — the open-item position derived from statements.

The whole point of this view is that a statement is a SNAPSHOT, not a ledger of
new charges: an unpaid invoice reappears until it clears, so summing statements
double-counts. On the first real dataset that error was 15x. These tests pin
the rule that avoids it and the diff that reads the supplier's own ledger back.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from gdx_dispatch.modules.vendor_statements.account import build_vendor_accounts
from gdx_dispatch.modules.vendor_statements.models import VendorStatement, VendorStatementLine

VENDOR = "Example Door Supply"


def _statement(db, statement_date, lines, vendor=VENDOR, code="ACME01"):
    """lines: (invoice_no, amount, balance, aging, line_date)"""
    stmt = VendorStatement(
        vendor_name=vendor,
        vendor_code=code,
        statement_date=statement_date,
        parser_name="test_v1",
        parser_version=1,
        raw_total=sum((Decimal(str(b)) for _, _, b, _, _ in lines), Decimal("0.00")),
        line_count=len(lines),
        status="parsed",
    )
    db.add(stmt)
    db.flush()
    for i, (inv, amount, balance, aging, line_date) in enumerate(lines):
        db.add(VendorStatementLine(
            statement_id=stmt.id,
            line_no=i,
            vendor_invoice_no=inv,
            vendor_job_no=f"9000{i}",
            po_ref=f"PO-{i}",
            line_date=line_date,
            amount=Decimal(str(amount)),
            balance=Decimal(str(balance)),
            aging_bucket=aging,
            description="8x7 door",
        ))
    db.flush()
    return stmt


# --------------------------------------------------------------------------- #
# the rule: latest statement is the position
# --------------------------------------------------------------------------- #
def test_open_balance_comes_from_the_latest_statement_not_the_sum(tenant_db):
    """The failure this view exists to prevent. Three statements each carrying
    the same unpaid invoice: the position is 100, not 300."""
    _statement(tenant_db, date(2026, 5, 1), [("INV1", 100, 100, "0-29", date(2026, 4, 1))])
    _statement(tenant_db, date(2026, 6, 1), [("INV1", 100, 100, "30-59", date(2026, 4, 1))])
    _statement(tenant_db, date(2026, 7, 1), [("INV1", 100, 100, "60-89", date(2026, 4, 1))])
    tenant_db.commit()

    account = build_vendor_accounts(tenant_db)[0]

    assert account.as_of == date(2026, 7, 1)
    assert account.open_balance == Decimal("100")
    assert account.open_line_count == 1
    assert account.statement_count == 3
    # And the repetition is surfaced as age, which is the useful reading of it.
    assert account.lines[0].statements_seen == 3
    assert account.lines[0].first_seen_on == date(2026, 5, 1)


def test_partial_payment_shows_as_paid_without_any_payment_record(tenant_db):
    """amount vs balance already encodes what's been applied."""
    _statement(tenant_db, date(2026, 7, 1), [("INV1", 1000, 400, "30-59", date(2026, 5, 1))])
    tenant_db.commit()

    line = build_vendor_accounts(tenant_db)[0].lines[0]
    assert line.amount == Decimal("1000")
    assert line.paid == Decimal("600")
    assert line.balance == Decimal("400")


def test_balance_above_original_never_reports_negative_paid(tenant_db):
    _statement(tenant_db, date(2026, 7, 1), [("INV1", 100, 150, "0-29", date(2026, 6, 1))])
    tenant_db.commit()
    assert build_vendor_accounts(tenant_db)[0].lines[0].paid == Decimal("0.00")


# --------------------------------------------------------------------------- #
# the diff: reading the supplier's ledger back
# --------------------------------------------------------------------------- #
def test_diff_reconstructs_a_chunk_payment(tenant_db):
    """Modelled on the real 06-12 → 06-26 move: six invoices cleared plus one
    paid down, summing to a round $32,000 chunk nobody recorded in GDX."""
    _statement(tenant_db, date(2026, 6, 12), [
        ("A", 10000, 10000, "30-59", date(2026, 5, 1)),
        ("B", 10000, 10000, "30-59", date(2026, 5, 1)),
        ("C", 9544.70, 9544.70, "30-59", date(2026, 5, 2)),
        ("D", 5000, 5000, "0-29", date(2026, 6, 1)),
    ])
    _statement(tenant_db, date(2026, 6, 26), [
        ("D", 5000, 2544.70, "30-59", date(2026, 6, 1)),
    ])
    tenant_db.commit()

    change = build_vendor_accounts(tenant_db)[0].change

    assert change.previous_statement_date == date(2026, 6, 12)
    assert change.cleared_count == 3
    assert change.cleared_total == Decimal("29544.70")
    assert change.paid_down_count == 1
    assert change.paid_down_total == Decimal("2455.30")
    assert change.implied_payment_total == Decimal("32000.00")


def test_diff_separates_new_charges_from_payments(tenant_db):
    _statement(tenant_db, date(2026, 6, 1), [("OLD", 500, 500, "30-59", date(2026, 5, 1))])
    _statement(tenant_db, date(2026, 7, 1), [
        ("OLD", 500, 500, "60-89", date(2026, 5, 1)),
        ("NEW", 900, 900, "0-29", date(2026, 6, 20)),
    ])
    tenant_db.commit()

    change = build_vendor_accounts(tenant_db)[0].change
    assert change.new_invoice_count == 1
    assert change.new_invoice_total == Decimal("900")
    assert change.implied_payment_total == Decimal("0.00")  # nothing moved


def test_a_new_invoice_that_arrives_part_paid_counts_what_it_added(tenant_db):
    """Real statements carry invoices already part-paid on first sight (one
    appeared at $6,543.52 with $4,000 applied). Nothing distinguishes money
    applied during this gap from money applied before the invoice showed up, so
    the new-charge figure is what it ADDED to the position — the balance — and
    the difference is deliberately not claimed as a payment in this period."""
    _statement(tenant_db, date(2026, 6, 1), [("OLD", 500, 500, "30-59", date(2026, 5, 1))])
    _statement(tenant_db, date(2026, 7, 1), [
        ("OLD", 500, 500, "60-89", date(2026, 5, 1)),
        ("NEW", 10000, 1000, "0-29", date(2026, 6, 20)),   # $9,000 already applied
    ])
    tenant_db.commit()

    account = build_vendor_accounts(tenant_db)[0]
    assert account.change.new_invoice_total == Decimal("1000")
    assert account.change.implied_payment_total == Decimal("0.00")
    # ...but the line itself still shows the full history.
    new_line = next(ln for ln in account.lines if ln.invoice_no == "NEW")
    assert new_line.amount == Decimal("10000")
    assert new_line.paid == Decimal("9000")
    assert account.open_balance == Decimal("1500")   # 500 + 1000, not 10500


def test_a_single_statement_has_no_change_block(tenant_db):
    _statement(tenant_db, date(2026, 7, 1), [("INV1", 100, 100, "0-29", date(2026, 6, 1))])
    tenant_db.commit()
    assert build_vendor_accounts(tenant_db)[0].change is None


# --------------------------------------------------------------------------- #
# aging + ordering
# --------------------------------------------------------------------------- #
def test_aging_totals_only_count_open_money(tenant_db):
    _statement(tenant_db, date(2026, 7, 1), [
        ("OPEN", 100, 100, "120+", date(2026, 1, 1)),
        ("SETTLED", 400, 0, "0-29", date(2026, 6, 1)),   # listed but nil balance
    ])
    tenant_db.commit()

    account = build_vendor_accounts(tenant_db)[0]
    assert account.open_balance == Decimal("100")
    assert account.open_line_count == 1          # the nil-balance line isn't "open"
    assert account.aging == {"120+": Decimal("100")}
    assert account.original_total == Decimal("500")   # but it IS still on the statement


def test_lines_are_ordered_oldest_money_first(tenant_db):
    _statement(tenant_db, date(2026, 7, 1), [
        ("NEWEST", 100, 100, "0-29", date(2026, 7, 1)),
        ("OLDEST", 100, 100, "120+", date(2026, 1, 1)),
        ("MIDDLE", 100, 100, "30-59", date(2026, 5, 1)),
    ])
    tenant_db.commit()

    assert [ln.invoice_no for ln in build_vendor_accounts(tenant_db)[0].lines] == [
        "OLDEST", "MIDDLE", "NEWEST",
    ]


def test_oldest_line_date_drives_days_open(tenant_db):
    _statement(tenant_db, date(2026, 7, 1), [
        ("A", 100, 100, "120+", date(2026, 1, 23)),
        ("B", 100, 100, "0-29", date(2026, 6, 30)),
    ])
    tenant_db.commit()

    account = build_vendor_accounts(tenant_db)[0]
    assert account.oldest_line_date == date(2026, 1, 23)
    assert account.days_oldest_open(date(2026, 7, 28)) == 186


# --------------------------------------------------------------------------- #
# multi-vendor + exclusions
# --------------------------------------------------------------------------- #
def test_vendors_are_separate_positions_biggest_first(tenant_db):
    _statement(tenant_db, date(2026, 7, 1), [("A", 100, 100, "0-29", date(2026, 6, 1))],
               vendor="Small Supply")
    _statement(tenant_db, date(2026, 7, 1), [("B", 900, 900, "0-29", date(2026, 6, 1))],
               vendor="Big Supply")
    tenant_db.commit()

    accounts = build_vendor_accounts(tenant_db)
    assert [a.vendor_name for a in accounts] == ["Big Supply", "Small Supply"]
    assert accounts[0].open_balance == Decimal("900")


def test_a_deleted_statement_never_becomes_the_position(tenant_db):
    _statement(tenant_db, date(2026, 6, 1), [("INV1", 100, 100, "0-29", date(2026, 5, 1))])
    bad = _statement(tenant_db, date(2026, 7, 1), [("INV1", 100, 99999, "0-29", date(2026, 5, 1))])
    bad.deleted_at = date(2026, 7, 2)
    tenant_db.commit()

    account = build_vendor_accounts(tenant_db)[0]
    assert account.as_of == date(2026, 6, 1)
    assert account.open_balance == Decimal("100")


def test_a_re_uploaded_duplicate_period_cannot_blank_the_diff(tenant_db):
    """The manual upload path dedupes on content hash only, so a byte-different
    re-print of a month already on file really does create a twin row. Left
    alone that twin becomes `previous`, the diff compares a statement to itself,
    and a genuine chunk payment reports as 'nothing moved'."""
    _statement(tenant_db, date(2026, 6, 1), [
        ("A", 5000, 5000, "30-59", date(2026, 5, 1)),
        ("B", 3000, 3000, "30-59", date(2026, 5, 2)),
    ])
    # same period, uploaded again (different bytes, so no hash collision)
    _statement(tenant_db, date(2026, 7, 1), [("B", 3000, 3000, "60-89", date(2026, 5, 2))])
    _statement(tenant_db, date(2026, 7, 1), [("B", 3000, 3000, "60-89", date(2026, 5, 2))])
    tenant_db.commit()

    account = build_vendor_accounts(tenant_db)[0]
    assert account.open_balance == Decimal("3000")
    # The June statement — not the July twin — must be the comparison point.
    assert account.change.previous_statement_date == date(2026, 6, 1)
    assert account.change.cleared_total == Decimal("5000")
    assert account.change.implied_payment_total == Decimal("5000")


def test_two_account_codes_are_two_accounts_not_one_merged_diff(tenant_db):
    """vendor_code is our customer number with the supplier. Grouping on name
    alone would compare one account's invoices against the other's — every line
    of one reading 'cleared', every line of the other 'new' — inventing a
    payment that never happened."""
    _statement(tenant_db, date(2026, 7, 1), [("A1", 5000, 5000, "0-29", date(2026, 6, 1))],
               code="ACCT-ONE")
    _statement(tenant_db, date(2026, 7, 1), [("B1", 8000, 8000, "0-29", date(2026, 6, 1))],
               code="ACCT-TWO")
    tenant_db.commit()

    accounts = build_vendor_accounts(tenant_db)
    assert len(accounts) == 2
    assert {a.vendor_code for a in accounts} == {"ACCT-ONE", "ACCT-TWO"}
    # Neither may report a fabricated payment from the other's lines.
    assert all(a.change is None for a in accounts)
    assert sorted(a.open_balance for a in accounts) == [Decimal("5000"), Decimal("8000")]


def test_undated_statements_are_not_collapsed_into_one_period(tenant_db):
    _statement(tenant_db, None, [("A", 100, 100, "0-29", date(2026, 6, 1))])
    _statement(tenant_db, None, [("B", 200, 200, "0-29", date(2026, 6, 2))])
    tenant_db.commit()
    assert build_vendor_accounts(tenant_db)[0].statement_count == 2


def test_no_statements_yields_no_accounts(tenant_db):
    assert build_vendor_accounts(tenant_db) == []


# --------------------------------------------------------------------------- #
# route ordering — /accounts must not be eaten by /{statement_id}
# --------------------------------------------------------------------------- #
def test_accounts_route_is_declared_before_the_uuid_route():
    """A literal path registered after a `/{uuid}` path 422s on the UUID parse.
    Assert on the router's real ordering rather than trusting file layout."""
    from gdx_dispatch.routers.vendor_statements import router

    paths = [r.path for r in router.routes]
    assert "/api/vendor-statements/accounts" in paths
    assert paths.index("/api/vendor-statements/accounts") < paths.index(
        "/api/vendor-statements/{statement_id}"
    )
