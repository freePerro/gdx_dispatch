"""Example plugin's own table — namespaced plug_example_* per ADR-013.

Inherits PluginBase so the plugin-host migration phase sees it on one metadata.
company_id scopes rows to the tenant (GDX is single-tenant-per-DB, but data still
carries company_id; the plugin filters by the forwarded tenant).
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, func

from gdx_dispatch.plugin_api.base import PluginBase


class ExampleItem(PluginBase):
    __tablename__ = "plug_example_items"

    id = Column(Integer, primary_key=True)
    company_id = Column(String(64), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
