"""Slice 1 — canonical job display-state derivation.

Pins the model Doug locked 2026-05-17 (4 typed terminals, money axis
overrides work axis once complete) against the REAL GDX prod
permutations queried from gdx-postgres:gdx_xdg:

  lifecycle_stage | billing_status | count
  completed       | invoiced       |   169   <- the 249-job lie: invoice
  service_call    | unbilled       |    31      is actually PAID
  completed       | unbilled       |    19
  scheduled       | unbilled       |     4
  estimate        | unbilled       |     2

The load-bearing assertion: a `completed` job whose invoice is `paid`
derives **Paid**, never "Complete" and never "Invoiced". That single
case is the deception Doug reported.
"""

from gdx_dispatch.core.job_display_state import (
    TYPE_LOST,
    TYPE_OPEN,
    TYPE_WON,
    derive_job_display_state,
)


def _s(**kw):
    return derive_job_display_state(**kw)


# --- The load-bearing case: the 249-job lie -------------------------------

def test_completed_with_paid_invoice_is_Paid_not_Complete():
    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "paid", "balance_due": 0, "amount_paid": 500}],
    )
    assert (st.stage, st.type, st.label) == ("paid", TYPE_WON, "Paid")
    assert st.is_finished is True


def test_completed_with_paid_invoice_by_balance_zero_even_if_status_lags():
    # billing_status/invoice.status can lag; balance_due==0 still = Paid.
    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "sent", "balance_due": 0, "amount_paid": 300}],
    )
    assert st.stage == "paid" and st.type == TYPE_WON


# --- Money axis overrides the work axis -----------------------------------

def test_completed_no_invoice_is_Ready_to_Bill():
    st = _s(lifecycle_stage="completed", invoices=[])
    assert (st.stage, st.type, st.label) == ("ready_to_bill", TYPE_OPEN, "Ready to Bill")


def test_invoice_sent_unpaid_is_Invoiced():
    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "sent", "balance_due": 250, "amount_paid": 0}],
    )
    assert (st.stage, st.label) == ("invoiced", "Invoiced")
    assert st.type == TYPE_OPEN


def test_invoice_overdue_is_Overdue():
    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "overdue", "balance_due": 250, "amount_paid": 0}],
    )
    assert (st.stage, st.label, st.type) == ("overdue", "Overdue", TYPE_OPEN)


def test_partial_payment_is_Partially_Paid():
    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "sent", "balance_due": 100, "amount_paid": 150}],
    )
    assert (st.stage, st.label, st.type) == ("partially_paid", "Partially Paid", TYPE_OPEN)


def test_multi_invoice_not_all_paid_is_not_Paid():
    st = _s(
        lifecycle_stage="completed",
        invoices=[
            {"status": "paid", "balance_due": 0, "amount_paid": 100},
            {"status": "sent", "balance_due": 80, "amount_paid": 0},
        ],
    )
    assert st.stage == "invoiced" and st.type == TYPE_OPEN


# --- Terminals ------------------------------------------------------------

def test_cancelled_beats_everything():
    st = _s(
        lifecycle_stage="cancelled",
        estimate_status="accepted",
        invoices=[{"status": "paid", "balance_due": 0, "amount_paid": 999}],
    )
    assert (st.stage, st.type, st.label) == ("cancelled", TYPE_LOST, "Cancelled")
    assert st.is_finished is True


def test_declined_estimate_is_Declined():
    st = _s(lifecycle_stage="estimate", estimate_status="declined", invoices=[])
    assert (st.stage, st.type, st.label) == ("declined", TYPE_LOST, "Declined")
    assert st.is_finished is True


def test_rejected_and_expired_estimate_also_Declined():
    for es in ("rejected", "expired"):
        st = _s(lifecycle_stage="estimate", estimate_status=es, invoices=[])
        assert st.stage == "declined" and st.type == TYPE_LOST


def test_declined_estimate_does_not_override_real_work_with_invoice():
    # An estimate may be declined but a later real job got invoiced —
    # the live money state wins, not the stale quote rejection.
    st = _s(
        lifecycle_stage="completed",
        estimate_status="declined",
        invoices=[{"status": "sent", "balance_due": 200, "amount_paid": 0}],
    )
    assert st.stage == "invoiced" and st.type == TYPE_OPEN


def test_accepted_estimate_in_progress_is_In_Progress_not_Declined():
    st = _s(lifecycle_stage="in_progress", estimate_status="accepted", invoices=[])
    assert (st.stage, st.label, st.type) == ("in_progress", "In Progress", TYPE_OPEN)


def test_written_off_stub_reachable_when_storage_arrives():
    # Slice 1: unreachable in prod (no such invoice.status yet). Slice 2
    # only adds storage — the branch already works.
    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "written_off", "balance_due": 400, "amount_paid": 0}],
    )
    assert (st.stage, st.type, st.label) == ("written_off", TYPE_LOST, "Written Off")
    assert st.is_finished is True


# --- Open work-axis states (real prod rows) -------------------------------

def test_service_call_unbilled():
    st = _s(lifecycle_stage="service_call", invoices=[])
    assert (st.stage, st.label, st.type) == ("service_call", "Service Call", TYPE_OPEN)
    assert st.is_finished is False


def test_scheduled_and_estimate_and_lead():
    assert _s(lifecycle_stage="scheduled").label == "Scheduled"
    assert _s(lifecycle_stage="estimate").label == "Estimate"
    assert _s(lifecycle_stage="lead").label == "Lead"
    for lc in ("scheduled", "estimate", "lead"):
        assert _s(lifecycle_stage=lc).type == TYPE_OPEN


# --- void invoices don't count as money -----------------------------------

def test_void_only_invoice_treated_as_no_invoice():
    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "void", "balance_due": 0, "amount_paid": 0}],
    )
    assert st.stage == "ready_to_bill"


def test_void_invoice_ignored_alongside_real_one():
    st = _s(
        lifecycle_stage="completed",
        invoices=[
            {"status": "void", "balance_due": 0, "amount_paid": 0},
            {"status": "paid", "balance_due": 0, "amount_paid": 700},
        ],
    )
    assert st.stage == "paid" and st.type == TYPE_WON


# --- never silently "Complete" / never silent Unknown ---------------------

def test_empty_lifecycle_no_invoice_is_not_Complete():
    # 173 prod rows have empty legacy `status`; derivation must not invent
    # "Complete" — it falls back honestly.
    st = _s(lifecycle_stage="", invoices=[])
    assert st.label != "Complete"
    assert (st.stage, st.label, st.type) == ("unknown", "Unknown", TYPE_OPEN)


def test_unmapped_stage_titlecased_not_dropped():
    st = _s(lifecycle_stage="weird_custom_stage")
    assert st.label == "Weird_Custom_Stage" and st.type == TYPE_OPEN


def test_decimal_and_string_amounts_coerce_safely():
    from decimal import Decimal

    st = _s(
        lifecycle_stage="completed",
        invoices=[{"status": "sent", "balance_due": Decimal("0.00"), "amount_paid": "450.00"}],
    )
    assert st.stage == "paid"  # balance 0 -> Paid, no crash on Decimal/str
