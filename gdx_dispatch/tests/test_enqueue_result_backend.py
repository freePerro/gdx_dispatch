"""A money path's enqueue never waits on the Celery result store (GDXA-819).

`apply_async_bounded` bounds the broker connection, but a task that stores its
result also makes `apply_async` subscribe to the result backend, and Celery
retries an unreachable Redis there for ~19 s. The receipt task did that inside
`record_payment`, the pay page's confirm and the Stripe webhook during a Redis
outage. These run the REAL enqueue, not conftest's stub, against a Redis port
that refuses, in a subprocess so the broker and backend URLs are the refused
one from import time and no other test's Celery config is touched.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap

import pytest

_PROBE = textwrap.dedent(
    """
    import json, time, types, uuid
    from gdx_dispatch.tasks import stale_intent_sweep
    from gdx_dispatch.tasks.billing_followup import (
        enqueue_payment_receipt, send_payment_receipt_task,
    )

    out = {}
    # Valid arguments: Celery checks them before it connects, and a TypeError
    # from that check would never reach the broker this probe measures.
    for name, task, kwargs in (
        ("receipt", send_payment_receipt_task, {"reference": "pi_probe"}),
        ("sweep", stale_intent_sweep.sweep_stale_intents, {"why": "payment_recorded"}),
    ):
        t0 = time.monotonic()
        try:
            stale_intent_sweep.apply_async_bounded(task, args=["x"], kwargs=kwargs)
            err = None
        except Exception as exc:  # the dead broker must raise, fast
            err = f"{type(exc).__name__}: {exc}"
        out[name] = {"seconds": time.monotonic() - t0, "error": err}

    t0 = time.monotonic()
    queued = enqueue_payment_receipt(str(uuid.uuid4()), reference="pi_probe")
    out["enqueue_payment_receipt"] = {"seconds": time.monotonic() - t0, "queued": queued}

    t0 = time.monotonic()
    queued = stale_intent_sweep.enqueue_stale_intent_sweep(
        types.SimpleNamespace(id=uuid.uuid4()), why="payment_recorded"
    )
    out["enqueue_stale_intent_sweep"] = {"seconds": time.monotonic() - t0, "queued": queued}
    print("PROBE " + json.dumps(out))
    """
)


def _refused_redis_url() -> str:
    # Bind, read the port, close: nothing listens there afterwards.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"redis://127.0.0.1:{port}/0"


def test_queueing_never_waits_on_the_result_backend():
    env = dict(os.environ, REDIS_URL=_refused_redis_url())
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env, capture_output=True, text=True, timeout=120,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line, f"probe printed nothing\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-4000:]}"
    out = json.loads(line[len("PROBE "):])

    for name in ("receipt", "sweep"):
        # Refused, and refused by the broker: not the result store's
        # RuntimeError, and not this guard's TypeError either.
        assert (out[name]["error"] or "").startswith(("OperationalError", "ConnectionError")), out
        assert out[name]["seconds"] < 3, out
    for name in ("enqueue_payment_receipt", "enqueue_stale_intent_sweep"):
        assert out[name]["queued"] is False, out
        assert out[name]["seconds"] < 3, out


@pytest.mark.real_broker_enqueue
def test_a_task_that_stores_its_result_is_refused_before_the_broker():
    from unittest.mock import patch

    from gdx_dispatch.tasks import stale_intent_sweep
    from gdx_dispatch.tasks.billing_followup import billing_followup_tick

    assert billing_followup_tick.ignore_result is False  # the probe's premise
    with patch.object(stale_intent_sweep.celery_app, "connection_for_write") as conn, \
            pytest.raises(TypeError, match="ignore_result"):
        stale_intent_sweep.apply_async_bounded(billing_followup_tick, args=[], kwargs={})
    conn.assert_not_called()


def test_every_task_a_money_path_queues_ignores_its_result():
    from gdx_dispatch.tasks.billing_followup import send_payment_receipt_task
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    assert send_payment_receipt_task.ignore_result is True
    assert sweep_stale_intents.ignore_result is True
