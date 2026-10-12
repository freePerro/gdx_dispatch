"""Guards conftest's ``broker_enqueue`` stub (GDXA-791).

Without it every test that records a payment spent ~19 s in the receipt
enqueue against a Redis no test environment runs (conftest says why). These
pin that the stub is in force by default, that it answers as a dead broker
(the "not queued" outcome tests always had), that a test's own
``monkeypatch.undo()`` cannot strip it, and that the opt-out marker really
hands back the real function.
"""
from __future__ import annotations

import pytest


def test_the_choke_point_is_stubbed_by_default(broker_enqueue):
    from gdx_dispatch.tasks import stale_intent_sweep

    assert stale_intent_sweep.apply_async_bounded is broker_enqueue


def test_a_receipt_enqueue_goes_through_the_stub_as_not_queued(broker_enqueue):
    """Pins the plumbing only: the stub is a mock, so this says nothing about
    how long the REAL enqueue takes against a dead broker."""
    from gdx_dispatch.tasks.billing_followup import (
        enqueue_payment_receipt,
        send_payment_receipt_task,
    )

    assert enqueue_payment_receipt("inv-1", reference="pi_1") is False
    task = broker_enqueue.call_args.args[0]
    assert task is send_payment_receipt_task
    assert broker_enqueue.call_args.kwargs == {
        "args": ["inv-1"], "kwargs": {"reference": "pi_1"},
    }


def test_a_test_can_take_the_queued_path(broker_enqueue):
    from gdx_dispatch.tasks.billing_followup import enqueue_payment_receipt

    broker_enqueue.side_effect = None
    assert enqueue_payment_receipt("inv-1") is True


def test_a_tests_monkeypatch_undo_does_not_strip_the_stub(broker_enqueue, monkeypatch):
    from gdx_dispatch.tasks import stale_intent_sweep

    monkeypatch.setenv("GDXA_791_PROBE", "1")
    monkeypatch.undo()
    assert stale_intent_sweep.apply_async_bounded is broker_enqueue


@pytest.mark.real_broker_enqueue
def test_the_marker_hands_back_the_real_function(broker_enqueue):
    from gdx_dispatch.tasks import stale_intent_sweep

    assert broker_enqueue is None
    assert stale_intent_sweep.apply_async_bounded.__module__ == stale_intent_sweep.__name__
    assert not hasattr(stale_intent_sweep.apply_async_bounded, "assert_called")
