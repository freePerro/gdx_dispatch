"""Payments sent (from processor confirmation emails) beside what each
statement says was applied. The bucketing rule, the dedupe, the payee match and
the bank lookup are each pinned here, because each one was a way the
hand-reconciliation of the first real dataset could have come out wrong.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from gdx_dispatch.modules.bank_feeds import oauth
from gdx_dispatch.modules.bank_feeds.models import (
    BankFeedAccount,
    BankFeedTransaction,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage, OutlookSettings
from gdx_dispatch.modules.vendor_statements.payments_sent import (
    _parse_confirmation,
    build_vendor_payments,
)
from gdx_dispatch.tests.test_vendor_account_view import VENDOR, _statement

SENDER = "noreply@portal.example.com"


def _preview(amount: str, mmddyyyy: str, txn: str, merchant: str = "EXAMPLE GROUP INC") -> str:
    return (
        f"${amount} USD\r\n{mmddyyyy} 11:42:49 AM\r\n{merchant}\r\n\r\n"
        f"Transaction Type CONVERSIONONLY\r\nPayment E-Check T0****204O\r\n"
        f"Transaction ID {txn}\r\nApproval Code 827585"
    )


def _mailbox(db, senders=(SENDER,)):
    """A mailbox, with the payment-confirmation senders configured."""
    if senders is not None:
        db.add(OutlookSettings(id=1, payment_confirmation_sender_allowlist=list(senders)))
    acct = OutlookAccount(id=uuid4(), user_id="u1", provider="outlook", upn="office@example.com")
    db.add(acct)
    db.flush()
    return acct.id


def _confirmation(db, account_id, amount, mmddyyyy, txn, *, payee=VENDOR,
                  sender=SENDER, folder="Inbox", merchant="EXAMPLE GROUP INC"):
    db.add(OutlookMessage(
        account_id=account_id,
        graph_message_id=f"g-{uuid4()}",
        subject=f"Payment Confirmation - {payee}",
        from_address=sender,
        direction="inbound",
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        body_preview=_preview(amount, mmddyyyy, txn, merchant),
        folder_display_name=folder,
    ))
    db.flush()


def _bank_account(db):
    inst = BannoInstitution(fi_host="bank.example.com", display_label="Bank")
    db.add(inst)
    db.flush()
    conn = BannoConnection(
        institution_id=inst.id, fi_host=inst.fi_host, banno_user_id="sub-1",
        access_token_enc=oauth._encrypt("at"), refresh_token_enc=oauth._encrypt("rt"),
    )
    db.add(conn)
    db.flush()
    acct = BankFeedAccount(connection_id=conn.id, external_account_id="a1")
    db.add(acct)
    db.flush()
    return acct.id


def _debit(db, account_id, cents, posted, pending=False):
    db.add(BankFeedTransaction(
        account_id=account_id, external_transaction_id=f"t-{uuid4()}",
        amount_cents=cents, posted_date=posted, payee="SUPPLIER", pending=pending,
    ))
    db.flush()


def _two_statements(db):
    # Jun 12: A 1000 + B 500 open. Jun 26: A cleared, B paid down to 200 → 1300 applied.
    _statement(db, date(2026, 6, 12), [
        ("A", 1000, 1000, "0-29", date(2026, 6, 1)),
        ("B", 500, 500, "0-29", date(2026, 6, 2)),
    ])
    _statement(db, date(2026, 6, 26), [
        ("B", 500, 200, "30-59", date(2026, 6, 2)),
    ])


def test_parse_reads_amount_date_and_transaction_id():
    assert _parse_confirmation(_preview("32,000.00", "06/19/2026", "TX-1")) == (
        date(2026, 6, 19), Decimal("32000.00"), "TX-1", "EXAMPLE GROUP INC",
    )
    assert _parse_confirmation("no amount here Transaction ID X") is None


def test_sent_sits_next_to_applied_for_the_statement_after_it(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "1,000.00", "06/15/2026", "TX-1")
    _confirmation(tenant_db, mb, "300.00", "06/20/2026", "TX-2")

    [vp] = build_vendor_payments(tenant_db)
    first, second = vp.statements
    assert first.applied_total is None and first.sent_count == 0
    assert second.applied_total == Decimal("1300.00")
    assert second.sent_total == Decimal("1300.00") and second.sent_count == 2
    assert vp.sent_total == Decimal("1300.00")


def test_a_payment_dated_on_a_statement_date_belongs_to_the_next_statement(tenant_db):
    """The statement is cut before that day's payments post — real data showed
    every same-day confirmation on the FOLLOWING statement."""
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "1,300.00", "06/12/2026", "TX-1")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.statements[0].sent_count == 0
    assert vp.statements[1].sent_total == Decimal("1300.00")


def test_a_payment_after_the_latest_statement_is_reported_as_not_yet_on_one(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "200.00", "06/26/2026", "TX-9")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.after_latest_total == Decimal("200.00") and vp.after_latest_count == 1
    assert all(r.sent_count == 0 for r in vp.statements)


def test_the_same_confirmation_in_two_folders_counts_once(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "1,300.00", "06/15/2026", "TX-1", folder="Inbox")
    _confirmation(tenant_db, mb, "1,300.00", "06/15/2026", "TX-1", folder="Supplier")

    [vp] = build_vendor_payments(tenant_db)
    assert len(vp.payments) == 1 and vp.sent_total == Decimal("1300.00")


def test_two_equal_payments_with_different_transaction_ids_are_two_payments(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "650.00", "06/15/2026", "TX-1")
    _confirmation(tenant_db, mb, "650.00", "06/15/2026", "TX-2")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.statements[1].sent_total == Decimal("1300.00")


def test_replies_forwards_and_other_payees_are_not_payments(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "1,300.00", "06/15/2026", "TX-1", sender="office@example.com")
    _confirmation(tenant_db, mb, "900.00", "06/15/2026", "TX-2", payee="Some Other Supplier")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.payments == [] and vp.sent_total == Decimal("0.00")


def test_a_payment_under_a_sister_trading_name_follows_the_merchant(tenant_db):
    """Real data: one credit desk, several trading names, one portal merchant.
    Five of thirteen payments were confirmed under the sister name and the
    statement applied all of them."""
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "300.00", "06/14/2026", "TX-1")
    _confirmation(tenant_db, mb, "1,000.00", "06/15/2026", "TX-2", payee="Sister Insulation, LLC")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.statements[1].sent_total == Decimal("1300.00")
    assert {p.paid_as for p in vp.payments} == {VENDOR, "Sister Insulation, LLC"}


def test_a_different_merchant_is_never_attributed_by_name_alone(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "300.00", "06/14/2026", "TX-1")
    _confirmation(tenant_db, mb, "1,000.00", "06/15/2026", "TX-2",
                  payee="Unrelated Co", merchant="UNRELATED CO")

    [vp] = build_vendor_payments(tenant_db)
    assert [p.reference for p in vp.payments] == ["TX-1"]


def test_a_merchant_shared_by_two_vendors_attributes_only_exact_names(tenant_db):
    _two_statements(tenant_db)
    _statement(tenant_db, date(2026, 6, 26), [("Q", 5, 5, "0-29", None)],
               vendor="Other Supplier", code="OTH01")
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "300.00", "06/14/2026", "TX-1")
    _confirmation(tenant_db, mb, "5.00", "06/14/2026", "TX-2", payee="Other Supplier")
    _confirmation(tenant_db, mb, "1,000.00", "06/15/2026", "TX-3", payee="Sister Insulation, LLC")

    by_name = {v.vendor_name: [p.reference for p in v.payments]
               for v in build_vendor_payments(tenant_db)}
    assert by_name == {VENDOR: ["TX-1"], "Other Supplier": ["TX-2"]}


def test_no_configured_senders_means_no_payments_and_says_so(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db, senders=None)
    _confirmation(tenant_db, mb, "1,300.00", "06/15/2026", "TX-1")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.payments == [] and vp.senders_configured is False
    assert vp.statements[1].applied_total == Decimal("1300.00")


def test_a_domain_entry_covers_the_portal_sender(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db, senders=("example.com",))
    _confirmation(tenant_db, mb, "1,300.00", "06/15/2026", "TX-1")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.senders_configured is True and vp.sent_total == Decimal("1300.00")


def test_payee_match_ignores_case_and_spacing(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "1,300.00", "06/15/2026", "TX-1",
                  payee="  " + VENDOR.upper().replace(" ", "  "))

    [vp] = build_vendor_payments(tenant_db)
    assert vp.sent_total == Decimal("1300.00")


def test_two_accounts_sharing_a_name_get_no_payments_attributed(tenant_db):
    _statement(tenant_db, date(2026, 6, 12), [("A", 10, 10, "0-29", None)], code="ACME01")
    _statement(tenant_db, date(2026, 6, 12), [("Z", 10, 10, "0-29", None)], code="ACME02")
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "10.00", "06/01/2026", "TX-1")

    vps = build_vendor_payments(tenant_db)
    assert len(vps) == 2 and all(v.payments == [] for v in vps)


def test_bank_debit_confirms_a_payment_once_within_the_window(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "650.00", "06/15/2026", "TX-1")
    _confirmation(tenant_db, mb, "650.00", "06/16/2026", "TX-2")
    _confirmation(tenant_db, mb, "999.00", "06/01/2026", "TX-3")
    bank = _bank_account(tenant_db)
    _debit(tenant_db, bank, -65000, date(2026, 6, 17))      # one debit for two payments
    _debit(tenant_db, bank, -99900, date(2026, 6, 20))      # outside the 7-day window
    _debit(tenant_db, bank, 65000, date(2026, 6, 17))       # a deposit, not a payment

    [vp] = build_vendor_payments(tenant_db)
    posted = {p.reference: p.bank_posted_on for p in vp.payments}
    assert posted == {"TX-3": None, "TX-1": date(2026, 6, 17), "TX-2": None}


def test_no_emails_still_reports_applied_per_statement(tenant_db):
    _two_statements(tenant_db)
    [vp] = build_vendor_payments(tenant_db)
    assert vp.payments == []
    assert vp.statements[1].applied_total == Decimal("1300.00")


def test_payments_route_is_declared_before_the_uuid_route():
    from gdx_dispatch.routers.vendor_statements import router

    paths = [r.path for r in router.routes]
    assert paths.index("/api/vendor-statements/payments") < paths.index(
        "/api/vendor-statements/{statement_id}"
    )


def _real_shaped_preview(pad: int) -> str:
    """A preview shaped like production's: Graph cuts bodyPreview at 255."""
    body = (
        "$11,000.00 USD\r\n09/24/2026 02:14:53 PM\r\nEXAMPLE GROUP INC\r\n\r\n"
        + "x" * pad
        + "Transaction Type CONVERSIONONLY\r\nPayment E-Check T0*************204O\r\n"
        "Transaction ID 240926O6D-6EF29533-2DDA-4BE2-A2E6-E32363F21C06\r\n"
        "Approval Code 290208\r\nApproval Message APPROVAL\r\nThank you for your payment"
    )
    return body[:255]


def test_a_transaction_id_cut_off_by_the_preview_limit_is_not_read():
    whole = _parse_confirmation(_real_shaped_preview(0))
    assert whole is not None and whole[2] == "240926O6D-6EF29533-2DDA-4BE2-A2E6-E32363F21C06"
    assert _parse_confirmation(_real_shaped_preview(60))[2].endswith("C06")  # ID ends just before the cut
    for pad in (80, 95, 110):  # the cut falls inside the ID
        assert _parse_confirmation(_real_shaped_preview(pad)) is None, pad


def test_unreadable_confirmations_are_counted_once_whatever_name_they_carry(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    for gid, payee in (("g-cut", VENDOR), ("g-cut-copy", VENDOR), ("g-sister", "Sister Insulation, LLC")):
        preview = _real_shaped_preview(80 if payee == VENDOR else 95)
        tenant_db.add(OutlookMessage(
            account_id=mb, graph_message_id=gid, subject=f"Payment Confirmation - {payee}",
            from_address=SENDER, direction="inbound", body_preview=preview,
        ))
    tenant_db.flush()

    [vp] = build_vendor_payments(tenant_db)
    assert vp.payments == [] and vp.unreadable_count == 2


def test_a_confirmation_no_vendor_claims_is_counted_not_dropped(tenant_db):
    """The portal renaming the payee must not look like 'nothing was sent'."""
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "1,300.00", "06/15/2026", "TX-1", payee=VENDOR + ", Inc.")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.payments == [] and vp.unattributed_count == 1


def test_the_merchant_line_reads_without_seconds_in_the_time():
    preview = "$5.00 USD\r\n06/15/2026 11:42 AM\r\nEXAMPLE GROUP INC\r\n\r\nTransaction ID TX-1\r\n"
    assert _parse_confirmation(preview)[3] == "EXAMPLE GROUP INC"


def test_one_debit_never_clears_payments_to_two_suppliers(tenant_db):
    _two_statements(tenant_db)
    _statement(tenant_db, date(2026, 6, 26), [("Q", 5, 5, "0-29", None)],
               vendor="Other Supplier", code="OTH01")
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "650.00", "06/15/2026", "TX-1")
    _confirmation(tenant_db, mb, "650.00", "06/15/2026", "TX-2",
                  payee="Other Supplier", merchant="OTHER MERCHANT")
    _debit(tenant_db, _bank_account(tenant_db), -65000, date(2026, 6, 17))

    cleared = [p.reference for v in build_vendor_payments(tenant_db)
               for p in v.payments if p.bank_posted_on]
    assert len(cleared) == 1


def test_a_pending_bank_row_never_confirms_a_payment(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "650.00", "06/15/2026", "TX-1")
    _debit(tenant_db, _bank_account(tenant_db), -65000, date(2026, 6, 17), pending=True)

    [vp] = build_vendor_payments(tenant_db)
    assert vp.payments[0].bank_posted_on is None


def test_a_payment_older_than_the_first_statement_lands_on_no_statement(tenant_db):
    _two_statements(tenant_db)
    mb = _mailbox(tenant_db)
    _confirmation(tenant_db, mb, "999.00", "06/01/2026", "TX-1")

    [vp] = build_vendor_payments(tenant_db)
    assert vp.sent_total == Decimal("999.00")
    assert all(r.sent_count == 0 for r in vp.statements)
    assert vp.after_latest_count == 0
