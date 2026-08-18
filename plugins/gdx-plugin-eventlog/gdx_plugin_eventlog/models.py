"""Event-log plugin table — namespaced plug_eventlog_* per ADR-013.
Inherit PluginBase so the plugin-host migration phase sees it on one metadata.
company_id scopes rows to the forwarded tenant; delivery_id makes the handler
idempotent under the platform's at-least-once delivery."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from gdx_dispatch.plugin_api.base import PluginBase


class EventLogEntry(PluginBase):
    __tablename__ = "plug_eventlog_events"

    id = Column(Integer, primary_key=True)
    company_id = Column(String(64), nullable=False, index=True)
    event_name = Column(String(120), nullable=False, index=True)
    # Unique so a re-delivered event (at-least-once) is a no-op, not a dupe row.
    delivery_id = Column(String(80), unique=True, nullable=True)
    payload_json = Column(Text, nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now())
