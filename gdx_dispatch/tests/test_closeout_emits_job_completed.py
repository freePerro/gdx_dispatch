"""`/closeout` stages job.completed like `/complete` does (GDXA-1954).

`closeout_job` wrote the completion state by hand — stage, status,
completed_at, dispatch done, visits — and never called `_emit_job_event`, so
the main way a tech finishes a job sent no job.completed webhook, plugin event
or workflow trigger. It now goes through `_mark_job_completed`.

A restatement (re-closeout of a job that is still completed) must NOT emit a
second job.completed: the job did not complete again. Counting delivery rows
cannot prove that on its own — the delivery idempotency key is
(tenant, event, entity, subscription), so a second emit dedupes to the same
row while still reaching the plugin and workflow sinks. So the tests also
count calls through the real `emit_domain_event` (a pass-through spy, not a
stub: the delivery row is still asserted for the first closeout).

Harness mirrors test_job_closeout_supersede.py.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from gdx_dispatch.core.webhooks import emit as emit_mod
from gdx_dispatch.core.webhooks.models import WebhookDelivery
from gdx_dispatch.routers.webhooks import WebhookSubscription
from gdx_dispatch.tests.test_job_closeout_supersede import (
    TENANT,
    _closeout,
    _seed_job,
)
from gdx_dispatch.tests.test_job_closeout_supersede import db as _supersede_db  # noqa: F401


@pytest.fixture
def db(_supersede_db):  # noqa: F811 — the fixture param IS the import
    _supersede_db.add(
        WebhookSubscription(
            company_id=TENANT,
            name="n8n",
            url="https://hooks.example.test/job",
            events=json.dumps(["job.completed"]),
            active=True,
        )
    )
    _supersede_db.commit()
    return _supersede_db


@pytest.fixture
def completed_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = emit_mod.emit_domain_event

    def spy(db, event_type, entity_id, data, **kw):
        if event_type == "job.completed":
            calls.append({"entity_id": entity_id, "data": data})
        return real(db, event_type, entity_id, data, **kw)

    monkeypatch.setattr(emit_mod, "emit_domain_event", spy)
    return calls


def _deliveries(db) -> list[WebhookDelivery]:
    return list(
        db.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_type == "job.completed")
        ).scalars()
    )


def test_a_first_closeout_stages_one_job_completed(db, completed_calls) -> None:
    job = _seed_job(db)
    _closeout(db, job, hours=2.0)

    assert len(completed_calls) == 1
    call = completed_calls[0]
    assert call["entity_id"] == str(job.id)
    assert call["data"]["lifecycle_stage"] == "completed"
    assert call["data"]["status"] == "Completed"

    rows = _deliveries(db)
    assert len(rows) == 1, "the subscribed receiver gets one staged delivery"
    assert rows[0].company_id == TENANT

    db.refresh(job)
    assert job.lifecycle_stage == "completed"
    assert job.status == "Completed"
    assert job.dispatch_status == "done"
    assert job.completed_at is not None


def test_a_restatement_stages_no_second_job_completed(db, completed_calls) -> None:
    job = _seed_job(db)
    _closeout(db, job, hours=2.0)
    _closeout(db, job, hours=3.0)

    assert len(completed_calls) == 1, "a restatement is not a second completion"
    assert len(_deliveries(db)) == 1


def test_a_closeout_after_reopen_emits_again(db, completed_calls) -> None:
    """A re-opened job that is closed out again did complete again; the gate is
    the stage before the closeout, not whether a prior closeout exists. The
    event reaches the plugin and workflow sinks, but the webhook receiver does
    NOT see the re-completion: its delivery row dedupes on (tenant, event, job,
    subscription) — emit.py's documented at-least-once edge, which /complete
    shares. Pinned so the gap stays visible rather than implied away."""
    job = _seed_job(db)
    _closeout(db, job, hours=2.0)
    job.lifecycle_stage = "in_progress"
    job.status = "In Progress"
    job.dispatch_status = "on_site"
    db.commit()

    _closeout(db, job, hours=3.0)

    assert len(completed_calls) == 2
    assert len(_deliveries(db)) == 1
