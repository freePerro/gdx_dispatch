"""The GDX business events an n8n workflow can trigger on.

A third-party plugin can't import core routers (ADR-013 — it binds to the public
plugin API only), so this mirrors core's WEBHOOK_EVENTS as plain data. Each entry
is (event_name, human "fires when"). Keep in step with
gdx_dispatch/routers/webhooks.py::WEBHOOK_EVENTS when core adds events.
"""
from __future__ import annotations

# (event, fires_when) — ordered by domain for a readable catalog screen.
EVENT_CATALOG: tuple[tuple[str, str], ...] = (
    ("job.created", "A job is created"),
    ("job.updated", "A job's details change"),
    ("job.completed", "A job is marked complete"),
    ("job.cancelled", "A job is cancelled"),
    ("estimate.sent", "An estimate is sent to a customer"),
    ("estimate.accepted", "A customer accepts an estimate"),
    ("estimate.declined", "A customer declines an estimate"),
    ("invoice.created", "An invoice is created"),
    ("invoice.sent", "An invoice is sent to a customer"),
    ("invoice.paid", "An invoice is fully paid"),
    ("invoice.overdue", "An invoice passes its due date unpaid"),
    ("customer.created", "A new customer is added"),
    ("customer.updated", "A customer's details change"),
    ("payment.succeeded", "A payment succeeds"),
    ("payment.failed", "A payment fails"),
    ("appointment.scheduled", "An appointment is scheduled"),
    ("appointment.confirmed", "An appointment is confirmed"),
    ("appointment.completed", "An appointment is completed"),
)
