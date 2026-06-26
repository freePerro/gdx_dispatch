"""#48 — regression guard for the silently-dropped model re-exports.

models/__init__.py re-imported the wholesale/distributor analytics models under
the WRONG names (`ChannelAnalytics`/`DistributorAnalytics` — the classes are
`ChannelAnalytic`/`DistributorAnalytic`). Because the imports are wrapped in
`try/except ImportError: pass`, the typo silently failed the whole line, so those
tables (and their line-mates CatalogItem/PricingTier/DealerOrder) never
registered with the ORM metadata. This locks the fix.
"""
from __future__ import annotations

import gdx_dispatch.models  # noqa: F401 — triggers the central registration hub
from gdx_dispatch.core.audit import TenantBase


def test_analytics_tables_are_registered():
    tables = set(TenantBase.metadata.tables)
    assert "channel_analytics" in tables
    assert "distributor_analytics" in tables


def test_line_mate_tables_register_too():
    # The typo'd line also carried these; they must register now that the line
    # no longer raises ImportError.
    tables = set(TenantBase.metadata.tables)
    assert "catalog_items" in tables or "wholesale_catalog_items" in tables
    assert "dealer_orders" in tables
