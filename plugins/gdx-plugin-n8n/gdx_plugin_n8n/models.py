"""n8n plugin activity table — namespaced plug_n8n_* per ADR-013.

Inherits PluginBase so the plugin-host migration phase sees it on one metadata.
This is an in-app MIRROR of the business events the automation layer saw — so the
owner can watch, inside GDX, exactly what their n8n workflows can trigger on. It
is NOT the forwarder: events reach n8n over the webhook path (core → celery →
n8n), because the plugin-host is network-isolated from n8n by design.

company_id scopes rows to the forwarded tenant; delivery_id makes the handler
idempotent under the platform's at-least-once delivery.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from gdx_dispatch.plugin_api.base import PluginBase


class N8nEvent(PluginBase):
    __tablename__ = "plug_n8n_events"

    id = Column(Integer, primary_key=True)
    company_id = Column(String(64), nullable=False, index=True)
    event_name = Column(String(120), nullable=False, index=True)
    # Unique so a re-delivered event (at-least-once) is a no-op, not a dupe row.
    delivery_id = Column(String(80), unique=True, nullable=True)
    payload_json = Column(Text, nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now())
